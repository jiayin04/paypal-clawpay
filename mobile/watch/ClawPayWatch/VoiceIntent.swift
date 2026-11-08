import AppIntents
import Foundation

/// Siri supplies speech-to-text; the foreground app runs its existing agent loop.
struct AskClawPayIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask ClawPay"
    static let description = IntentDescription("Shop, check orders, or check spending with ClawPay.")
    static let openAppWhenRun = true
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication

    @Parameter(title: "Request", requestValueDialog: "What would you like to do?")
    var request: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask ClawPay \(\.$request)")
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        try VoiceRequestInbox.enqueue(request)
        return .result()
    }
}

struct ClawPayShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskClawPayIntent(), phrases: [
            "Ask \(.applicationName)",
            "Talk to \(.applicationName)",
        ], shortTitle: "Ask ClawPay", systemImageName: "mic")
    }
}

/// Encrypted, single-use handoff across cold launch / Siri dismissal. No payment is signed here.
@MainActor
enum VoiceRequestInbox {
    static let changed = Notification.Name("com.clawpay.voice-request")
    private static let account = "voice-request.v1"
    private struct Pending: Codable {
        let text: String
        let createdAt: Date
    }

    static func enqueue(_ raw: String, now: Date = Date()) throws {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.unicodeScalars.count <= 500 else {
            throw AssistantError(errorDescription: "Say a short request, up to 500 characters.")
        }
        let data = try JSONEncoder().encode(Pending(text: text, createdAt: now))
        try Keychain.write(data, account: account)
        NotificationCenter.default.post(name: changed, object: nil)
    }

    static func take(now: Date = Date()) -> String? {
        guard let data = Keychain.read(account: account) else { return nil }
        Keychain.remove(account: account)
        guard let pending = try? JSONDecoder().decode(Pending.self, from: data),
              (0...120).contains(now.timeIntervalSince(pending.createdAt)) else { return nil }
        return pending.text
    }

    #if DEBUG && targetEnvironment(simulator)
    static func selfCheck() throws {
        let original = Keychain.read(account: account)
        defer {
            if let original { try? Keychain.write(original, account: account) }
            else { Keychain.remove(account: account) }
        }
        let now = Date()
        try Keychain.write(JSONEncoder().encode(Pending(text: "Find socks", createdAt: now)), account: account)
        assert(take(now: now) == "Find socks")
        assert(take(now: now) == nil)  // no duplicate execution
        try Keychain.write(JSONEncoder().encode(Pending(text: "Expired request", createdAt: now)), account: account)
        assert(take(now: now.addingTimeInterval(121)) == nil)
        try Keychain.write(JSONEncoder().encode(Pending(text: "Future request", createdAt: now)), account: account)
        assert(take(now: now.addingTimeInterval(-1)) == nil)
        try Keychain.write(Data("invalid JSON".utf8), account: account)
        assert(take(now: now) == nil)
        for invalid in ["   ", String(repeating: "a", count: 501)] {
            do {
                try enqueue(invalid)
                assertionFailure("Invalid voice request accepted")
            } catch { }
        }
    }
    #endif
}
