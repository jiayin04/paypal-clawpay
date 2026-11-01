import Foundation

/// Response of `POST /api/v1/assistant/messages`.
struct AssistantReply: Decodable {
    struct Order: Decodable {
        let orderId: String
        let status: String?
        let total: String?
        let merchant: String?
    }

    let reply: String
    let order: Order?
    let approvalRequired: Bool
}

struct AssistantError: LocalizedError {
    let errorDescription: String?
    var code: String? = nil  // the server's error.code, e.g. "challenge_consumed"
}

/// The server-built approval payload (contracts.md §5). The watch signs these exact bytes.
struct CanonicalApproval: Decodable, Equatable {
    let amountMinor: Int
    let currency: String
    let deviceId: String
    let expiresAt: Int
    let merchantName: String
    let orderId: String
    let purpose: String

    /// Currencies the watch will sign, with their minor-unit exponent (contracts.md §5:
    /// the device refuses any currency not in its table).
    static let exponents = ["MYR": 2, "USD": 2]

    /// "MYR 13.80": rendered from the signed bytes, not from the chat.
    var display: String {
        let exponent = Self.exponents[currency] ?? 2
        let scale = Int(pow(10, Double(exponent)))
        return String(format: "%@ %d.%0*d", currency, amountMinor / scale, exponent, amountMinor % scale)
    }
}

/// A verified challenge, ready to sign once the user confirms what it shows.
struct PreparedApproval: Equatable {
    let payload: CanonicalApproval
    fileprivate let bytes: Data
    fileprivate let challengeId: String
    fileprivate let nonce: String
    fileprivate let keyVersion: Int
}

struct ApprovalResult: Decodable {
    let status: String  // paid | payment_pending | payment_failed | denied
    let message: String
}

/// Talks to the ClawPay backend with a `cpd_` device token: the claw's (approve with its
/// button) or the watch's own (registered with its approver key; approve on the watch).
struct AssistantClient {
    let baseURL: URL
    let token: String

    /// Refuses plain HTTP except to this machine, so the bearer token never crosses a network in clear.
    init?(baseURL: URL, token: String) {
        let loopback = ["127.0.0.1", "localhost", "::1"].contains(baseURL.host() ?? "")
        guard baseURL.scheme == "https" || (baseURL.scheme == "http" && loopback), !token.isEmpty else { return nil }
        self.baseURL = baseURL
        self.token = token
    }

    func send(_ text: String, reset: Bool = false, approver: String = "device") async throws -> AssistantReply {
        let data = try await request("POST", "api/v1/assistant/messages",
                                     ["text": text, "reset": reset, "approver": approver], timeout: 60)
        return try Self.decoder.decode(AssistantReply.self, from: data)
    }

    /// Step 1: fetch the order's challenge and check it is the order on screen, still valid
    /// and in a currency the watch understands. Nothing is signed here.
    func prepareApproval(orderId: String) async throws -> PreparedApproval {
        struct Challenge: Decodable {
            let challengeId: String
            let canonicalB64url: String
            let nonce: String
            let keyVersion: Int

            // Literal keys, decoded without convertFromSnakeCase: that strategy does not map
            // "canonical_b64url" to canonicalB64url (the digits change the capitalisation).
            enum CodingKeys: String, CodingKey {
                case challengeId = "challenge_id", canonicalB64url = "canonical_b64url"
                case nonce, keyVersion = "key_version"
            }
        }
        let raw = try await request("GET", "api/v1/orders/\(orderId)/approval-challenge", nil, timeout: 20)
        let challenge = try JSONDecoder().decode(Challenge.self, from: raw)
        guard let bytes = Data(base64URL: challenge.canonicalB64url),
              let payload = try? Self.decoder.decode(CanonicalApproval.self, from: bytes),
              payload.purpose == "clawpay.order_approval.v1", payload.orderId == orderId.lowercased()
        else { throw ApprovalError("The approval request did not match this order.") }
        guard CanonicalApproval.exponents[payload.currency] != nil else {
            throw ApprovalError("This watch cannot approve \(payload.currency) payments.")
        }
        guard payload.amountMinor > 0 else { throw ApprovalError("Invalid amount.") }
        guard Double(payload.expiresAt) > Date().timeIntervalSince1970 else {
            throw ApprovalError("This approval expired. Ask for the order again.")
        }
        return PreparedApproval(payload: payload, bytes: bytes, challengeId: challenge.challengeId,
                                nonce: challenge.nonce, keyVersion: challenge.keyVersion)
    }

    /// Step 2, after the user confirmed the amount shown from `prepared.payload`: presence
    /// check (wrist/passcode) and signature in the Secure Enclave, then submit. Money moves
    /// only after the server verifies the signature against this watch's registered key.
    func submit(_ prepared: PreparedApproval, with key: ApproverKey) async throws -> ApprovalResult {
        let payload = prepared.payload
        let signature = try await key.sign(prepared.bytes, reason: "Pay \(payload.display) to \(payload.merchantName)")
        let body: [String: Any] = [
            "challenge_id": prepared.challengeId, "device_id": payload.deviceId, "key_version": prepared.keyVersion,
            "nonce": prepared.nonce, "decision": "approved", "signature": signature.base64URLEncodedString(),
        ]
        // The server waits up to ~10 s for PayPal before answering payment_pending.
        let result = try await request("POST", "api/v1/orders/\(payload.orderId)/gadget-approval", body, timeout: 30)
        return try Self.decoder.decode(ApprovalResult.self, from: result)
    }

    /// Fish Audio MP3 for a reply. Throws when the backend has no TTS configured (503).
    func speech(_ text: String) async throws -> Data {
        try await request("POST", "api/v1/assistant/speech", ["text": text], timeout: 25)
    }

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    private func request(_ method: String, _ path: String, _ body: [String: Any]?,
                         timeout: TimeInterval) async throws -> Data {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            struct Envelope: Decodable { struct Body: Decodable { let code: String?; let message: String }; let error: Body }
            let error = (try? JSONDecoder().decode(Envelope.self, from: data))?.error
            throw AssistantError(errorDescription: error?.message ?? "Server error \(status).", code: error?.code)
        }
        return data
    }
}

extension Data {
    init?(base64URL value: String) {
        var base64 = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        self.init(base64Encoded: base64)
    }

    func base64URLEncodedString() -> String {
        base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
