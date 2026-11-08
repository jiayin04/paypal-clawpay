import CryptoKit
import Foundation
import LocalAuthentication
import Security

/// The watch's payment-approval key: P-256, the same algorithm the claw signs with.
///
/// On a real watch the private key lives in the Secure Enclave, never leaves it, and
/// every signature needs user presence (unlocked watch on the wrist, or the passcode),
/// enforced by the hardware. The simulator has no Secure Enclave, so it uses a software
/// key in the Keychain and skips the presence check; that build is for demos only.
struct ApproverKey {
    enum Kind: String { case secureEnclave = "Secure Enclave", software = "Software (simulator)" }

    private enum Stored {
        case enclave(Data)  // SecureEnclave dataRepresentation: an opaque handle, useless off this watch
        case software(P256.Signing.PrivateKey)
    }

    let kind: Kind
    let publicKeyPEM: String
    private let stored: Stored

    /// Short id to compare with what the server registered.
    var fingerprint: String {
        SHA256.hash(data: Data(publicKeyPEM.utf8)).prefix(6).map { String(format: "%02x", $0) }.joined()
    }

    static func loadOrCreate() throws -> ApproverKey {
        #if targetEnvironment(simulator)
        let key: P256.Signing.PrivateKey
        if let raw = Keychain.read(account: softwareAccount) {
            key = try P256.Signing.PrivateKey(rawRepresentation: raw)
        } else {
            key = P256.Signing.PrivateKey()
            try Keychain.write(key.rawRepresentation, account: softwareAccount)
        }
        return ApproverKey(kind: .software, publicKeyPEM: key.publicKey.pemRepresentation, stored: .software(key))
        #else
        guard SecureEnclave.isAvailable else { throw ApprovalError("This watch has no Secure Enclave.") }
        if let blob = Keychain.read(account: enclaveAccount) {
            // Loading the handle needs no authentication; signing with it does.
            let key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob)
            return ApproverKey(kind: .secureEnclave, publicKeyPEM: key.publicKey.pemRepresentation, stored: .enclave(blob))
        }
        var error: Unmanaged<CFError>?
        // Destroyed if the passcode is removed; every signature needs user presence.
        guard let access = SecAccessControlCreateWithFlags(
            nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly, [.privateKeyUsage, .userPresence], &error
        ) else { throw ApprovalError("Could not create the key's access policy.") }
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
        try Keychain.write(key.dataRepresentation, account: enclaveAccount)
        return ApproverKey(kind: .secureEnclave, publicKeyPEM: key.publicKey.pemRepresentation,
                           stored: .enclave(key.dataRepresentation))
        #endif
    }

    /// Asks for user presence (wrist + unlocked, or passcode) with `reason` on screen, then
    /// returns the ECDSA-SHA256 signature over `payload` as DER, the format the server verifies.
    func sign(_ payload: Data, reason: String) async throws -> Data {
        switch stored {
        case .software(let key):
            return try key.signature(for: payload).derRepresentation
        case .enclave(let blob):
            let context = LAContext()
            var policy = LAPolicy.deviceOwnerAuthenticationWithWristDetection
            if !context.canEvaluatePolicy(policy, error: nil) { policy = .deviceOwnerAuthentication }
            do {
                guard try await context.evaluatePolicy(policy, localizedReason: reason) else {
                    throw ApprovalError("Approval cancelled.")
                }
            } catch let error as LAError {
                throw ApprovalError(error.code == .userCancel || error.code == .appCancel
                                    ? "Approval cancelled." : "Unlock your watch to approve.")
            }
            // The authenticated context satisfies the key's .userPresence requirement.
            defer { context.invalidate() }  // one signature per presence check
            let key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob, authenticationContext: context)
            return try key.signature(for: payload).derRepresentation
        }
    }

    private static let enclaveAccount = "approver.secure-enclave.v1"
    private static let softwareAccount = "approver.software.v1"
}

struct ApprovalError: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

/// The `cpd_` device token, in the Keychain rather than UserDefaults.
enum DeviceToken {
    private static let account = "device-token.v1"

    static func load() -> String {
        if let data = Keychain.read(account: account), let token = String(data: data, encoding: .utf8) { return token }
        // One-time move from the earlier @AppStorage("deviceToken") location.
        let legacy = UserDefaults.standard.string(forKey: "deviceToken") ?? ""
        if !legacy.isEmpty {
            do { try save(legacy) }
            catch { /* Keep the original token until migration succeeds. */ }
        }
        return legacy
    }

    static func save(_ token: String) throws {
        try Keychain.write(Data(token.utf8), account: account)
        UserDefaults.standard.removeObject(forKey: "deviceToken")
    }
}

/// Minimal generic-password Keychain wrapper, this-device-only.
enum Keychain {
    static let service = "com.clawpay.watch"

    static func read(account: String) -> Data? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: account, kSecReturnData as String: true]
        var item: CFTypeRef?
        return SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess ? item as? Data : nil
    }

    static func write(_ data: Data, account: String) throws {
        let match: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: account]
        // Update in place so a failed write cannot delete the last saved value.
        let attributes: [String: Any] = [kSecValueData as String: data]
        let updated = SecItemUpdate(match as CFDictionary, attributes as CFDictionary)
        if updated == errSecSuccess { return }
        guard updated == errSecItemNotFound else { throw ApprovalError("Keychain error \(updated).") }
        var item = match
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw ApprovalError("Keychain error \(status).") }
    }

    static func remove(account: String) {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword,
                       kSecAttrService as String: service, kSecAttrAccount as String: account] as CFDictionary)
    }
}
