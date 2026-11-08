import AVFoundation
import Observation

/// Foreground, push-to-talk capture. A short pause ends recording; the completed transcript is sent once.
@MainActor
@Observable
final class SpeechCapture: NSObject, AVAudioRecorderDelegate {
    enum Phase: Equatable, Hashable {
        case idle, authorizing, recording, transcribing
        case ready(String), failed(String)
    }

    private(set) var phase: Phase = .idle
    @ObservationIgnored private var recorder: AVAudioRecorder?
    @ObservationIgnored private var file: URL?
    @ObservationIgnored private var client: AssistantClient?
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var meterTask: Task<Void, Never>?
    @ObservationIgnored private var endDetector = SpeechEndDetector()
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var sessionActive = false

    var elapsed: Int { Int(recorder?.currentTime ?? 0) }

    func start(client: AssistantClient) async {
        cancel()
        self.client = client
        let current = generation
        phase = .authorizing
        let allowed = await AVAudioApplication.requestRecordPermission()
        guard current == generation else { return }
        guard allowed else {
            phase = .failed("Allow microphone access in Settings, or type instead.")
            return
        }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .measurement)
            try session.setActive(true)
            sessionActive = true
            let url = FileManager.default.temporaryDirectory.appending(path: "voice-\(UUID()).wav")
            file = url
            let next = try AVAudioRecorder(url: url, settings: [
                AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000,
                AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false,
            ])
            next.isMeteringEnabled = true
            next.delegate = self
            recorder = next
            phase = .recording
            guard next.record(forDuration: 30) else { throw ApprovalError("Recording failed.") }
            endDetector = SpeechEndDetector()
            meterTask = Task {
                while !Task.isCancelled, phase == .recording {
                    try? await Task.sleep(for: .milliseconds(100))
                    guard !Task.isCancelled, let recorder, phase == .recording else { return }
                    recorder.updateMeters()
                    if endDetector.shouldStop(power: recorder.averagePower(forChannel: 0), at: recorder.currentTime) {
                        stop()
                        return
                    }
                }
            }
        } catch {
            cancel()
            phase = .failed("Couldn't start the microphone. Try again or type instead.")
        }
    }

    func stop() {
        guard phase == .recording else { return }
        meterTask?.cancel()
        meterTask = nil
        phase = .transcribing  // prevents a second stop while the delegate finishes
        recorder?.stop()  // delegate starts transcription, including the automatic 30-second stop
    }

    func cancel() {
        generation += 1
        meterTask?.cancel()
        meterTask = nil
        task?.cancel()
        task = nil
        recorder?.delegate = nil
        recorder?.stop()
        recorder = nil
        releaseSession()
        if let file { try? FileManager.default.removeItem(at: file) }
        file = nil
        client = nil
        phase = .idle
    }

    func accept(_ text: String) {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty, clean.unicodeScalars.count <= 500 else {
            phase = .failed("Say a shorter request.")
            return
        }
        phase = .ready(clean)
    }

    private func releaseSession() {
        guard sessionActive else { return }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        sessionActive = false
    }

    private func transcribe(_ url: URL) {
        guard let client else { return }
        phase = .transcribing
        let current = generation
        task = Task {
            defer { try? FileManager.default.removeItem(at: url) }
            do {
                let audio = try Data(contentsOf: url)
                let text = try await client.transcribe(audio)
                guard current == generation, !Task.isCancelled else { return }
                accept(text)
            } catch {
                guard current == generation, !Task.isCancelled else { return }
                phase = .failed(error.localizedDescription)
            }
            if current == generation { file = nil; task = nil }
        }
    }

    nonisolated func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) {
        Task { @MainActor in
            guard recorder === self.recorder, let url = self.file else { return }
            self.meterTask?.cancel()
            self.meterTask = nil
            self.recorder = nil
            self.releaseSession()
            if flag { self.transcribe(url) }
            else {
                self.cancel()
                self.phase = .failed("Recording stopped. Please try again.")
            }
        }
    }

    nonisolated func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder, error: Error?) {
        Task { @MainActor in
            guard recorder === self.recorder else { return }
            self.cancel()
            self.phase = .failed("Couldn't record audio. Try again.")
        }
    }

    #if DEBUG && targetEnvironment(simulator)
    /// Exercises the real WAV upload/transcription path using a synthetic fixture, without a live microphone.
    func transcribeFixture(_ url: URL, client: AssistantClient) {
        cancel()
        self.client = client
        let copy = FileManager.default.temporaryDirectory.appending(path: "voice-fixture-\(UUID()).wav")
        do {
            try FileManager.default.copyItem(at: url, to: copy)
            file = copy
            transcribe(copy)
        } catch { phase = .failed("Couldn't open the audio fixture.") }
    }
    #endif
}
