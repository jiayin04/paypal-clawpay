import SwiftUI
import WatchKit

struct ChatLine: Identifiable, Equatable {
    let id = UUID()
    let fromUser: Bool
    let text: String
}

struct PendingOrder: Equatable {
    enum State: Equatable {
        case awaiting, working, paid, processing
        case confirming(PreparedApproval)  // verified challenge, waiting for the user's Confirm
        case failed(String)
    }

    let orderId: String
    let total: String  // from the chat reply; replaced by the signed amount once verified
    let merchant: String?
    var signedTotal: String?
    var state: State = .awaiting

    var displayTotal: String { signedTotal ?? total }
}

struct ContentView: View {
    // Editable in Settings; simulator builds can also seed them from SIMCTL_CHILD_CLAWPAY_* variables.
    @AppStorage("baseURL") private var baseURL = "http://127.0.0.1:8000"
    @State private var deviceToken = ""  // kept in the Keychain, not UserDefaults
    @AppStorage("useFishAudio") private var useFishAudio = true
    // On only when deviceToken is this watch's own registered device (scripts/register_watch.py).
    @AppStorage("approveOnWatch") private var approveOnWatch = false

    @State private var lines: [ChatLine] = []
    @State private var pendingOrder: PendingOrder?
    @State private var busy = false
    @State private var resetNext = false
    @State private var seeded = false
    @State private var speaker = SpeechPlayer()
    @State private var approverKey: ApproverKey?
    @State private var keyError: String?
    @State private var autoApprove = false

    private let suggestions = ["Buy two milk", "Find bread", "What did I spend today?"]

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 8) {
                        if lines.isEmpty && !busy {
                            HeroView(suggestions: suggestions, onPick: send)
                        }
                        ForEach(lines) { line in
                            ChatBubble(line: line,
                                       speaking: speaker.isSpeaking && line.id == lines.last?.id && !line.fromUser,
                                       voice: speaker.lastVoice)
                        }
                        if busy {
                            TypingBubble()
                        }
                        if let order = pendingOrder {
                            ApprovalCard(order: order, approveOnWatch: approveOnWatch, onApprove: approve,
                                         onConfirm: confirm, onCancel: { pendingOrder?.state = .awaiting })
                                .transition(.scale(scale: 0.9).combined(with: .opacity))
                        }
                        // Clearance so the last item can scroll above the floating mic button.
                        Color.clear.frame(height: 44).id("bottom")
                    }
                    .padding(.horizontal, 2)
                    .animation(.spring(duration: 0.35), value: lines)
                    .animation(.spring(duration: 0.35), value: pendingOrder)
                }
                .onChange(of: lines) { scrollToBottom(proxy) }
                .onChange(of: busy) { scrollToBottom(proxy) }
                .onChange(of: pendingOrder) { scrollToBottom(proxy) }
                .onChange(of: deviceToken) { DeviceToken.save(deviceToken) }
            }
            .containerBackground(Theme.background, for: .navigation)
            .navigationTitle("ClawPay")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    NavigationLink {
                        SettingsView(baseURL: $baseURL, deviceToken: $deviceToken, useFishAudio: $useFishAudio,
                                     approveOnWatch: $approveOnWatch, lastVoice: speaker.lastVoice,
                                     approverKey: approverKey, keyError: keyError)
                    } label: {
                        Image(systemName: "gearshape.fill")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button(action: newConversation) {
                        Image(systemName: "square.and.pencil")
                    }
                    .disabled(lines.isEmpty || busy)
                }
                ToolbarItemGroup(placement: .bottomBar) {
                    Spacer()
                    MicButton(busy: busy, speaking: speaker.isSpeaking, onSubmit: send)
                    Spacer()
                }
            }
        }
        .onAppear(perform: seedFromEnvironment)
    }

    private var client: AssistantClient? {
        guard let url = URL(string: baseURL) else { return nil }
        return AssistantClient(baseURL: url, token: deviceToken)  // nil for plain HTTP off this machine
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("bottom", anchor: .bottom) }
    }

    private func seedFromEnvironment() {
        guard !seeded else { return }
        seeded = true
        deviceToken = DeviceToken.load()
        loadApproverKey()
        #if targetEnvironment(simulator)
        // Demo and test hooks, compiled into simulator builds only: a real watch is configured in Settings.
        let env = ProcessInfo.processInfo.environment
        if let url = env["CLAWPAY_BASE_URL"], !url.isEmpty { baseURL = url }
        if let token = env["CLAWPAY_TOKEN"], !token.isEmpty { deviceToken = token }
        if let flag = env["CLAWPAY_APPROVE_ON_WATCH"], !flag.isEmpty { approveOnWatch = flag == "1" }
        autoApprove = env["CLAWPAY_DEMO_APPROVE"] == "1"  // taps Approve and Confirm automatically
        if let prompt = env["CLAWPAY_DEMO_PROMPT"], !prompt.isEmpty { send(prompt) }
        #endif
    }

    private func newConversation() {
        speaker.stop()
        lines = []
        pendingOrder = nil
        resetNext = true
    }

    private func send(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !busy else { return }
        guard let client else {
            lines.append(ChatLine(fromUser: false, text: "Add an https server URL and device token in Settings."))
            return
        }
        speaker.stop()
        lines.append(ChatLine(fromUser: true, text: text))
        busy = true
        let reset = resetNext
        resetNext = false
        Task {
            do {
                let answer = try await client.send(text, reset: reset, approver: approveOnWatch ? "watch" : "device")
                busy = false
                lines.append(ChatLine(fromUser: false, text: answer.reply))
                // Keep the card across later replies; a new order replaces it, a new chat clears it.
                if let order = answer.order {
                    pendingOrder = answer.approvalRequired
                        ? PendingOrder(orderId: order.orderId, total: order.total ?? "your order",
                                       merchant: order.merchant) : nil
                }
                WKInterfaceDevice.current().play(answer.approvalRequired ? .notification : .click)
                if autoApprove, approveOnWatch, answer.approvalRequired {
                    autoApprove = false
                    approve()
                }
                await speaker.speak(answer.reply, client: client, useFishAudio: useFishAudio)
            } catch {
                busy = false
                lines.append(ChatLine(fromUser: false, text: error.localizedDescription))
                WKInterfaceDevice.current().play(.failure)
            }
        }
    }
}

extension ContentView {
    private func loadApproverKey() {
        do {
            let key = try ApproverKey.loadOrCreate()
            approverKey = key
            // Public key only, for scripts/register_watch.py --simulator.
            let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            try? key.publicKeyPEM.write(to: docs.appending(path: "approver_public_key.pem"), atomically: true, encoding: .utf8)
        } catch {
            keyError = error.localizedDescription
        }
    }

    /// Step 1: fetch and verify the challenge, then show its signed amount for confirmation.
    private func approve() {
        guard let order = pendingOrder, order.state == .awaiting || order.isRetryable,
              let client, approverKey != nil else {
            if approverKey == nil { pendingOrder?.state = .failed(keyError ?? "No approval key on this watch.") }
            return
        }
        pendingOrder?.state = .working
        Task {
            do {
                let prepared = try await client.prepareApproval(orderId: order.orderId)
                guard pendingOrder?.orderId == order.orderId else { return }
                pendingOrder?.signedTotal = prepared.payload.display
                pendingOrder?.state = .confirming(prepared)
                WKInterfaceDevice.current().play(.click)
                if autoApprove {
                    autoApprove = false
                    confirm()
                }
            } catch {
                fail(order, error)
            }
        }
    }

    /// Step 2: the user confirmed the signed amount; presence check, sign in the Secure Enclave, submit.
    private func confirm() {
        guard let order = pendingOrder, case .confirming(let prepared) = order.state,
              let client, let key = approverKey else { return }
        pendingOrder?.state = .working
        Task {
            do {
                let result = try await client.submit(prepared, with: key)
                guard pendingOrder?.orderId == order.orderId else { return }
                switch result.status {
                case "paid":
                    pendingOrder?.state = .paid
                    WKInterfaceDevice.current().play(.success)
                case "payment_pending":
                    pendingOrder?.state = .processing
                    WKInterfaceDevice.current().play(.success)
                default:
                    pendingOrder?.state = .failed(result.message)
                    WKInterfaceDevice.current().play(.failure)
                }
                await speaker.speak(result.status == "paid" ? "Paid \(prepared.payload.display)." : result.message,
                                    client: client, useFishAudio: useFishAudio)
            } catch {
                fail(order, error)
            }
        }
    }

    private func fail(_ order: PendingOrder, _ error: Error) {
        guard pendingOrder?.orderId == order.orderId else { return }
        // The approval already went through (e.g. its response was lost and this is a retry):
        // never claim "not paid" here; the order status says what happened.
        if let code = (error as? AssistantError)?.code, ["challenge_consumed", "invalid_order_status"].contains(code) {
            pendingOrder?.state = .processing
            return
        }
        pendingOrder?.state = .failed(error.localizedDescription)
        WKInterfaceDevice.current().play(.failure)
    }
}

extension PendingOrder {
    var isConfirming: Bool {
        if case .confirming = state { return true }
        return false
    }

    /// A cancelled prompt or network error can be retried; the server keeps the challenge open.
    var isRetryable: Bool {
        if case .failed = state { return true }
        return false
    }
}

// MARK: - Pieces

private struct HeroView: View {
    let suggestions: [String]
    let onPick: (String) -> Void

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Circle().fill(Theme.accent).frame(width: 44, height: 44)
                    .shadow(color: Theme.accentSolid.opacity(0.6), radius: 8)
                Image(systemName: "bag.fill").font(.title3).foregroundStyle(.white)
            }
            Text("Tap the mic and say what you need.")
                .font(.footnote)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            ForEach(suggestions, id: \.self) { suggestion in
                Button { onPick(suggestion) } label: {
                    Text(suggestion).font(.footnote).frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(Theme.accentSolid)
            }
        }
        .padding(.top, 2)
    }
}

private struct ChatBubble: View {
    let line: ChatLine
    let speaking: Bool
    let voice: SpeechPlayer.Voice?

    var body: some View {
        HStack(alignment: .bottom, spacing: 4) {
            if line.fromUser { Spacer(minLength: 18) }
            VStack(alignment: .leading, spacing: 3) {
                Text(line.text).font(.footnote)
                if speaking {
                    Label(voice?.rawValue ?? "Speaking", systemImage: "waveform")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .symbolEffect(.variableColor.iterative)
                }
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .foregroundStyle(.white)
            .background {
                if line.fromUser {
                    RoundedRectangle(cornerRadius: 14).fill(Theme.accent)
                } else {
                    RoundedRectangle(cornerRadius: 14).fill(Theme.assistantBubble)
                }
            }
            if !line.fromUser { Spacer(minLength: 18) }
        }
        .transition(.move(edge: line.fromUser ? .trailing : .leading).combined(with: .opacity))
    }
}

private struct TypingBubble: View {
    var body: some View {
        HStack {
            Image(systemName: "ellipsis")
                .font(.title3.bold())
                .symbolEffect(.variableColor.iterative.dimInactiveLayers)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 14).fill(Theme.assistantBubble))
            Spacer()
        }
        .accessibilityLabel("ClawPay is thinking")
    }
}

private struct ApprovalCard: View {
    let order: PendingOrder
    let approveOnWatch: Bool
    let onApprove: () -> Void
    let onConfirm: () -> Void
    let onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            header
            Text(order.displayTotal)
                .font(.system(.title2, design: .rounded).weight(.bold))
                .foregroundStyle(.white)
            if let merchant = order.merchant, !order.isConfirming {
                Text(merchant).font(.caption2).foregroundStyle(.secondary)
            }
            footer
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 16).fill(tint.opacity(0.16)))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(tint.opacity(0.7), lineWidth: 1))
        .animation(.spring(duration: 0.3), value: order.state)
    }

    private var tint: Color {
        switch order.state {
        case .paid, .processing: Theme.paid
        case .failed: .red
        default: Theme.approval
        }
    }

    @ViewBuilder private var header: some View {
        switch order.state {
        case .paid:
            Label("Paid", systemImage: "checkmark.seal.fill").font(.caption.bold()).foregroundStyle(Theme.paid)
                .symbolEffect(.bounce, value: order.state)
        case .processing:
            Label("Payment processing", systemImage: "clock.fill").font(.caption.bold()).foregroundStyle(Theme.paid)
        case .failed:
            Label("Not paid", systemImage: "exclamationmark.triangle.fill").font(.caption.bold()).foregroundStyle(.red)
        case .confirming:
            Label("Confirm payment", systemImage: "lock.shield.fill").font(.caption.bold())
                .foregroundStyle(Theme.approval)
        default:
            Label(approveOnWatch ? "Approve payment" : "Approve on ClawPay",
                  systemImage: approveOnWatch ? "lock.shield.fill" : "hand.tap.fill")
                .font(.caption.bold())
                .foregroundStyle(Theme.approval)
                .symbolEffect(.pulse)
        }
    }

    @ViewBuilder private var footer: some View {
        switch order.state {
        case .paid:
            Text("Charged to your linked PayPal.").font(.caption2).foregroundStyle(.white.opacity(0.85))
        case .processing:
            Text("Approved. PayPal is confirming; ask \"order status\" later.").font(.caption2)
                .foregroundStyle(.white.opacity(0.85))
        case .failed(let message):
            Text(message).font(.caption2).foregroundStyle(.white.opacity(0.85))
            if approveOnWatch { approveButton(title: "Try again", action: onApprove) }
        case .working:
            HStack(spacing: 6) {
                ProgressView().frame(width: 18, height: 18)
                Text("Checking…").font(.caption2)
            }
        case .confirming(let prepared):
            // Amount and merchant above come from the signed payload, not the chat.
            Text("Paying \(prepared.payload.merchantName). Your watch will ask you to confirm it's you.")
                .font(.caption2).foregroundStyle(.white.opacity(0.85))
            approveButton(title: "Confirm & pay", action: onConfirm)
            Button("Cancel", role: .cancel, action: onCancel).font(.footnote)
        case .awaiting:
            if approveOnWatch {
                approveButton(title: "Approve", action: onApprove)
            } else {
                Text("Hold the button on your device to pay.").font(.caption2).foregroundStyle(.white.opacity(0.85))
            }
        }
    }

    private func approveButton(title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: "lock.open.fill").font(.footnote.bold()).frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .tint(Theme.approval)
        .padding(.top, 2)
        .accessibilityHint("Asks you to confirm on the watch, then pays")
    }
}

private struct MicButton: View {
    let busy: Bool
    let speaking: Bool
    let onSubmit: (String) -> Void

    var body: some View {
        // TextFieldLink opens the system input sheet: dictation first, then scribble/keyboard.
        TextFieldLink(prompt: Text("Ask ClawPay")) {
            ZStack {
                Circle().fill(Theme.accent)
                    .shadow(color: Theme.accentSolid.opacity(speaking ? 0.9 : 0.5), radius: speaking ? 10 : 5)
                Image(systemName: speaking ? "waveform" : "mic.fill")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.white)
                    .symbolEffect(.variableColor.iterative, isActive: speaking)
            }
            .frame(width: 50, height: 50)
            .opacity(busy ? 0.4 : 1)
        } onSubmit: { text in
            onSubmit(text)
        }
        .buttonStyle(.plain)
        .disabled(busy)
        .accessibilityLabel("Talk to ClawPay")
    }
}

struct SettingsView: View {
    @Binding var baseURL: String
    @Binding var deviceToken: String
    @Binding var useFishAudio: Bool
    @Binding var approveOnWatch: Bool
    let lastVoice: SpeechPlayer.Voice?
    let approverKey: ApproverKey?
    let keyError: String?

    var body: some View {
        Form {
            Section("Voice") {
                Toggle("Fish Audio", isOn: $useFishAudio)
                if let lastVoice {
                    Text("Last reply: \(lastVoice.rawValue)").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Section {
                Toggle("Approve on this watch", isOn: $approveOnWatch)
                if let key = approverKey {
                    LabeledContent("Key", value: key.kind.rawValue).font(.caption2)
                    LabeledContent("ID", value: key.fingerprint).font(.caption2.monospaced())
                    ShareLink(item: key.publicKeyPEM) { Label("Share public key", systemImage: "square.and.arrow.up") }
                } else if let keyError {
                    Text(keyError).font(.caption2).foregroundStyle(.red)
                }
            } header: {
                Text("Payments")
            } footer: {
                Text("Register this watch's key as its own device first (scripts/register_watch.py) and use the token it returns.")
            }
            Section("Server") {
                TextField("http://host:8000", text: $baseURL)
                    .textContentType(.URL)
            }
            Section("Device token (cpd_)") {
                SecureField("cpd_…", text: $deviceToken)
            }
        }
        .navigationTitle("Settings")
    }
}

#Preview {
    ContentView()
}
