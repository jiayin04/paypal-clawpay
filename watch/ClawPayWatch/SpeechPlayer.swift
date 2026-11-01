import AVFoundation
import Observation

/// Speaks assistant replies: Fish Audio MP3 from the backend when available,
/// otherwise the watch's built-in voice, so a reply is always heard.
@MainActor
@Observable
final class SpeechPlayer: NSObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    enum Voice: String { case fishAudio = "Fish Audio", builtIn = "Built-in" }

    private(set) var isSpeaking = false
    private(set) var lastVoice: Voice?

    // Bumped by every speak()/stop(); work and callbacks from an older generation are ignored,
    // so a slow Fish Audio download or a late cancel callback never touches newer speech.
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var player: AVAudioPlayer?
    @ObservationIgnored private var utterance: AVSpeechUtterance?
    @ObservationIgnored private var playerText: String?  // spoken instead if the MP3 fails to decode
    @ObservationIgnored private let synthesizer = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String, client: AssistantClient?, useFishAudio: Bool) async {
        stop()
        let current = generation
        isSpeaking = true
        if useFishAudio, let client, let audio = try? await client.speech(text) {
            guard current == generation else { return }  // stopped or replaced while downloading
            if play(audio, text: text) {
                lastVoice = .fishAudio
                return
            }
        }
        guard current == generation else { return }
        speakBuiltIn(text)
    }

    func stop() {
        generation += 1
        player?.stop()
        player = nil
        utterance = nil
        synthesizer.stopSpeaking(at: .immediate)
        isSpeaking = false
    }

    private func speakBuiltIn(_ text: String) {
        lastVoice = .builtIn
        let next = AVSpeechUtterance(string: text)
        utterance = next
        synthesizer.speak(next)
    }

    private func play(_ audio: Data, text: String) -> Bool {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
        try? AVAudioSession.sharedInstance().setActive(true)
        guard let next = try? AVAudioPlayer(data: audio) else { return false }
        next.delegate = self
        player = next
        playerText = text
        if next.play() { return true }
        player = nil
        return false
    }

    private func finished(player finishedPlayer: AVAudioPlayer) {
        guard finishedPlayer === player else { return }
        player = nil
        isSpeaking = false
    }

    private func finished(utterance finishedUtterance: AVSpeechUtterance) {
        guard finishedUtterance === utterance else { return }
        utterance = nil
        isSpeaking = false
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.finished(player: player) }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor in
            guard player === self.player, let text = self.playerText else { return }
            self.player = nil
            self.speakBuiltIn(text)  // corrupt MP3: say it with the built-in voice instead
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished(utterance: utterance) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished(utterance: utterance) }
    }
}
