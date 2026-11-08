import Foundation

/// Metering only estimates end of speech; Stop & send remains available in noisy rooms.
struct SpeechEndDetector {
    var speechThreshold: Float = -42  // dBFS; tune for real watch microphones if needed
    var pauseDuration: TimeInterval = 1.1
    private var voicedSamples = 0
    private var lastVoice: TimeInterval?

    mutating func shouldStop(power: Float, at time: TimeInterval) -> Bool {
        if power > speechThreshold {
            voicedSamples += 1
            lastVoice = time
        }
        guard voicedSamples >= 2, let lastVoice else { return false } // two 100 ms samples; accepts short words
        return time - lastVoice >= pauseDuration
    }
}

/// Fish PCM: signed 16-bit little-endian mono. Preserve odd bytes between network chunks.
struct PCMStreamBuffer {
    static let sampleRate = 24000.0
    static let maxBytes = 2 * 1024 * 1024
    private var remainder: UInt8?
    private(set) var totalBytes = 0

    mutating func decode(_ data: Data) throws -> [Float] {
        totalBytes += data.count
        guard totalBytes <= Self.maxBytes else { throw VoiceAudioError.invalidStream }
        var bytes = Array(data)
        if let remainder { bytes.insert(remainder, at: 0) }
        remainder = bytes.count.isMultiple(of: 2) ? nil : bytes.removeLast()
        return stride(from: 0, to: bytes.count, by: 2).map { i in
            Float(Int16(bitPattern: UInt16(bytes[i]) | UInt16(bytes[i + 1]) << 8)) / 32768
        }
    }

    func finish() throws {
        guard totalBytes > 0, remainder == nil else { throw VoiceAudioError.invalidStream }
    }
}

enum VoiceAudioError: Error { case invalidStream }
