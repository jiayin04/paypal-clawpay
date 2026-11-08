import Foundation

/// Response of `POST /api/v1/assistant/messages`.
struct AssistantReply: Decodable {
    struct Item: Decodable, Equatable {
        let name: String
        let qty: Int
        let lineTotal: String
    }
    struct Product: Decodable, Identifiable {
        var id: String { merchantId + ":" + sku }
        let sku: String
        let name: String
        let merchantId: String
        let price: String
    }
    struct Order: Decodable {
        let orderId: String
        let status: String?
        let total: String?
        let merchant: String?
        let items: [Item]?
        let paidAt: String?
    }

    let reply: String
    let order: Order?
    let approvalRequired: Bool
    let products: [Product]?
}

struct AgentProgress: Decodable, Equatable {
    let stage: String
    let message: String
    let status: String
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

#if DEBUG && targetEnvironment(simulator)
extension PreparedApproval {
    /// Offline appearance fixture; the review screen disables interaction.
    static var visualReview: PreparedApproval {
        let payload = CanonicalApproval(amountMinor: 2600, currency: "USD", deviceId: "visual-review-only",
                                        expiresAt: Int(Date().timeIntervalSince1970) + 120,
                                        merchantName: "Channel3 Marketplace", orderId: "visual-review-only",
                                        purpose: "clawpay.order_approval.v1")
        return PreparedApproval(payload: payload, bytes: Data(), challengeId: "visual-review-only",
                                nonce: "visual-review-only", keyVersion: 1)
    }
}
#endif

struct ApprovalResult: Decodable {
    let status: String  // paid | payment_pending | payment_failed | denied
    let message: String
}

/// Talks to the ClawPay backend with a `cpd_` device token: the claw's (approve with its
/// button) or the watch's own (registered with its approver key; approve on the watch).
struct AssistantClient: Equatable {
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

    /// Action summaries come from executed backend stages, not private model reasoning.
    func sendStreaming(_ text: String, reset: Bool, approver: String,
                       onProgress: @MainActor (AgentProgress) -> Void) async throws -> AssistantReply {
        var request = URLRequest(url: baseURL.appending(path: "api/v1/assistant/messages/stream"))
        request.httpMethod = "POST"
        request.timeoutInterval = 60
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text, "reset": reset, "approver": approver])
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              http.value(forHTTPHeaderField: "Content-Type")?.contains("application/x-ndjson") == true else {
            throw AssistantError(errorDescription: "Couldn't connect to the agent. Check your connection.")
        }
        struct Event: Decodable {
            let type: String
            let stage: String?
            let message: String?
            let status: String?
            let data: AssistantReply?
            struct Failure: Decodable { let code: String; let message: String }
            let error: Failure?
        }
        var line = Data()
        var total = 0
        for try await byte in bytes {
            try Task.checkCancellation()
            total += 1
            guard total <= 256 * 1024, line.count <= 64 * 1024 else {
                throw AssistantError(errorDescription: "Agent response was too large. Check your current order.")
            }
            if byte == 10 {
                if line.isEmpty { continue }
                let event = try Self.decoder.decode(Event.self, from: line)
                line.removeAll(keepingCapacity: true)
                if event.type == "progress", let stage = event.stage, let message = event.message, let status = event.status {
                    await onProgress(AgentProgress(stage: stage, message: message, status: status))
                } else if event.type == "result", let reply = event.data {
                    return reply
                } else if event.type == "error", let error = event.error {
                    throw AssistantError(errorDescription: error.message, code: error.code)
                }
            } else { line.append(byte) }
        }
        // Never retry an interrupted mutation automatically; it may have created a cart.
        throw AssistantError(errorDescription: "Connection interrupted. Check your current order before retrying.")
    }

    func receipt(orderID: String) async throws -> AssistantReply.Order {
        let data = try await request("GET", "api/v1/orders/\(orderID)/receipt", nil, timeout: 25)
        return try Self.decoder.decode(AssistantReply.Order.self, from: data)
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

    /// Authenticated PCM chunks, delivered before the full utterance has been generated.
    func streamSpeech(_ text: String, onChunk: @MainActor (Data) throws -> Void) async throws {
        var request = URLRequest(url: baseURL.appending(path: "api/v1/assistant/speech/stream"))
        request.httpMethod = "POST"
        request.timeoutInterval = 8
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              http.value(forHTTPHeaderField: "X-Audio-Format") == "pcm-s16le",
              http.value(forHTTPHeaderField: "X-Audio-Sample-Rate") == "24000",
              http.value(forHTTPHeaderField: "X-Audio-Channels") == "1" else {
            throw VoiceAudioError.invalidStream
        }
        var chunk = Data()
        var count = 0
        for try await byte in bytes {
            try Task.checkCancellation()
            count += 1
            guard count <= PCMStreamBuffer.maxBytes else { throw VoiceAudioError.invalidStream }
            chunk.append(byte)
            if chunk.count >= 4800 {  // 100 ms of mono audio
                try await onChunk(chunk)
                chunk.removeAll(keepingCapacity: true)
            }
        }
        guard count > 0, count.isMultiple(of: 2) else { throw VoiceAudioError.invalidStream }
        if !chunk.isEmpty { try await onChunk(chunk) }
    }

    /// Raw microphone audio goes only to the transcription endpoint, never directly to the agent.
    func transcribe(_ wav: Data) async throws -> String {
        let data = try await request("POST", "api/v1/assistant/transcriptions", nil, timeout: 45, wav: wav)
        struct Transcript: Decodable { let text: String }
        return try JSONDecoder().decode(Transcript.self, from: data).text
    }

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    private func request(_ method: String, _ path: String, _ body: [String: Any]?,
                         timeout: TimeInterval, wav: Data? = nil) async throws -> Data {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let wav {
            request.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
            request.httpBody = wav
        } else if let body {
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
