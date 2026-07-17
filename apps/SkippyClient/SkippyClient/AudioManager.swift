import AVFoundation

class AudioManager {
    private let audioEngine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private var isSetup = false // ⚡ Tracks if hardware is initialized
    
    var isPlaying: Bool { return playerNode.isPlaying }
    
    // Notice we removed the init() block entirely!
    
    private func ensureSetup() {
        if isSetup { return }
        audioEngine.attach(playerNode)
        let mainMixer = audioEngine.mainMixerNode
        audioEngine.connect(playerNode, to: mainMixer, format: mainMixer.outputFormat(forBus: 0))
        isSetup = true
    }
    
    func stop() {
        if playerNode.isPlaying { playerNode.stop() }
    }
    
    func scheduleAudioChunk(base64String: String, completion: @escaping () -> Void) {
        ensureSetup() // ⚡ Only initialize the speakers when we actually have audio to play
        
        guard let data = Data(base64Encoded: base64String) else { return }
        let tempURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".wav")
        
        do {
            try data.write(to: tempURL)
            let audioFile = try AVAudioFile(forReading: tempURL)
            
            if !audioEngine.isRunning { try? audioEngine.start() }
            
            playerNode.scheduleFile(audioFile, at: nil) { [weak self] in
                try? FileManager.default.removeItem(at: tempURL)
                DispatchQueue.main.async {
                    if let self = self, !self.playerNode.isPlaying {
                        completion()
                    }
                }
            }
            
            if !playerNode.isPlaying { playerNode.play() }
            
        } catch {
            print("Audio Error: \(error)")
        }
    }
}
