import AVFoundation
import Observation

/// Plays authenticated Fish PCM as it arrives, with a local voice fallback.
@MainActor
@Observable
final class SpeechPlayer: NSObject, AVSpeechSynthesizerDelegate {
    enum Voice: String { case fishAudio = "Fish Audio", builtIn = "Built-in" }
    private(set) var isSpeaking = false
    private(set) var lastVoice: Voice?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var speechTask: Task<Void, Never>?
    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var node: AVAudioPlayerNode?
    @ObservationIgnored private var queuedBuffers = 0
    @ObservationIgnored private var inputFinished = false
    @ObservationIgnored private var utterance: AVSpeechUtterance?
    @ObservationIgnored private let synthesizer = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String, client: AssistantClient?, useFishAudio: Bool) async {
        stop()
        let current = generation
        isSpeaking = true
        let next = Task { @MainActor in
            if useFishAudio, let client {
                do {
                    var decoder = PCMStreamBuffer()
                    var initialAudio = Data()
                    var started = false
                    try await client.streamSpeech(text) { chunk in
                        guard current == self.generation, !Task.isCancelled else { throw CancellationError() }
                        if !started {
                            initialAudio.append(chunk)
                            guard initialAudio.count >= 9600 else { return } // 200 ms prebuffer to reduce underruns
                            try self.enqueue(decoder.decode(initialAudio), generation: current)
                            initialAudio.removeAll()
                            started = true
                        } else {
                            try self.enqueue(decoder.decode(chunk), generation: current)
                        }
                    }
                    guard current == self.generation, !Task.isCancelled else { return }
                    if !initialAudio.isEmpty { try self.enqueue(decoder.decode(initialAudio), generation: current) }
                    try decoder.finish()
                    self.inputFinished = true
                    self.finishStreamIfDrained()
                    self.speechTask = nil
                    return
                } catch {
                    guard current == self.generation, !Task.isCancelled else { return }
                    self.stopAudio()
                }
            }
            guard current == self.generation, !Task.isCancelled else { return }
            self.speakBuiltIn(text)
            self.speechTask = nil
        }
        speechTask = next
        await next.value
    }

    func stop() {
        generation += 1
        speechTask?.cancel()
        speechTask = nil
        stopAudio()
        utterance = nil
        synthesizer.stopSpeaking(at: .immediate)
        isSpeaking = false
    }

    private func enqueue(_ samples: [Float], generation current: Int) throws {
        guard !samples.isEmpty else { return }
        let format = AVAudioFormat(standardFormatWithSampleRate: PCMStreamBuffer.sampleRate, channels: 1)!
        if engine == nil {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .spokenAudio)
            try session.setActive(true)
            let engine = AVAudioEngine()
            let node = AVAudioPlayerNode()
            engine.attach(node)
            engine.connect(node, to: engine.mainMixerNode, format: format)
            self.engine = engine
            self.node = node
            try engine.start()
            #if DEBUG && targetEnvironment(simulator)
            NSLog("ClawPay streaming playback started")
            #endif
        }
        guard let node, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)),
              let channel = buffer.floatChannelData?[0] else { throw VoiceAudioError.invalidStream }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        for (index, sample) in samples.enumerated() { channel[index] = sample }
        queuedBuffers += 1
        let nodeID = ObjectIdentifier(node)
        node.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in
            Task { @MainActor in
                guard current == self.generation, let activeNode = self.node,
                      nodeID == ObjectIdentifier(activeNode) else { return }
                self.queuedBuffers -= 1
                self.finishStreamIfDrained()
            }
        }
        if !node.isPlaying { node.play() }
        lastVoice = .fishAudio
    }

    private func finishStreamIfDrained() {
        guard inputFinished, queuedBuffers == 0 else { return }
        stopAudio()
        isSpeaking = false
        #if DEBUG && targetEnvironment(simulator)
        NSLog("ClawPay streaming playback finished")
        #endif
    }

    private func stopAudio() {
        node?.stop()
        node = nil
        engine?.stop()
        engine = nil
        queuedBuffers = 0
        inputFinished = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func speakBuiltIn(_ text: String) {
        #if DEBUG && targetEnvironment(simulator)
        NSLog("ClawPay built-in speech fallback")
        #endif
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
        try? AVAudioSession.sharedInstance().setActive(true)
        lastVoice = .builtIn
        let next = AVSpeechUtterance(string: text)
        utterance = next
        synthesizer.speak(next)
    }

    private func finished(_ finishedUtterance: AVSpeechUtterance) {
        guard finishedUtterance === utterance else { return }
        utterance = nil
        isSpeaking = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished(utterance) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished(utterance) }
    }
}
