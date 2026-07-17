import Speech
import AVFoundation

class SpeechManager: NSObject {
    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let audioEngine = AVAudioEngine() // Owns its own engine again
    private var silenceTimer: Timer?
    
    var onCommandRecognized: ((String) -> Void)?
    var onStateChanged: ((Bool) -> Void)?
    
    func requestAuthorization() {
        SFSpeechRecognizer.requestAuthorization { _ in }
    }
    
    func startListening() {
        // STRICT LOCK: If we are already running a task, do not start another one!
        if recognitionTask != nil { return }
        
        // Aggressive cleanup before starting
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest = nil
        
        recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        guard let request = recognitionRequest else { return }
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = false
        
        let inputNode = audioEngine.inputNode
        let format = inputNode.outputFormat(forBus: 0)
        
        inputNode.installTap(onBus: 0, bufferSize: 512, format: format) { buffer, _ in
            request.append(buffer)
        }
        
        try? audioEngine.start()
        onStateChanged?(true)
        
        recognitionTask = speechRecognizer?.recognitionTask(with: request) { [weak self] result, error in
            guard let self = self else { return }
            
            if let res = result {
                self.resetSilenceTimer(with: res.bestTranscription.formattedString)
            }
            
            if let err = error as NSError? {
                self.stopListening()
                // Auto-restart only if it wasn't a deliberate cancel
                if err.domain != "kAFAssistantErrorDomain" {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                        self.startListening()
                    }
                }
            }
        }
    }
    
    private func resetSilenceTimer(with currentText: String) {
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: false) { [weak self] _ in
            let cmd = currentText.trimmingCharacters(in: .whitespacesAndNewlines)
            if cmd.count > 2 {
                self?.stopListening()
                self?.onCommandRecognized?(cmd)
            }
        }
    }
    
    func stopListening() {
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionTask = nil
        silenceTimer?.invalidate()
        onStateChanged?(false)
    }
    
    // We don't need the mute functions anymore, we are doing a hard stop.
    func mute() { stopListening() }
    func unmute() { startListening() }
}
