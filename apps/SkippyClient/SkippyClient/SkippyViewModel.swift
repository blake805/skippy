import SwiftUI
import Foundation

class SkippyViewModel: ObservableObject {
    @Published var sessions: [ChatSession] = []
    @Published var selectedSessionId: UUID? {
        didSet { loadSelectedSession() }
    }
    
    @Published var currentAgentTask: String? = nil
    
    @Published var messages: [String] = []
    @Published var inputText: String = ""
    @Published var isProcessing: Bool = false
    @Published var logText: String = ""
    @Published var showLogs: Bool = false
    
    @Published var selectedMode: String = "Shop"
    @Published var isConnected: Bool = false
    
    @Published var terminalLog: String = ""
    @Published var isTerminalOpen: Bool = false
    @Published var terminalAuthRequest: (command: String, explanation: String)? = nil
    @Published var deploymentAuthRequest: (targetFile: String, summary: String, content: String)? = nil
    
    @Published var isConversationMode = false {
        didSet {
            if isConversationMode {
                speechManager.requestAuthorization()
                speechManager.unmute()
                speechManager.startListening()
            } else {
                speechManager.stopListening()
            }
        }
    }
    @Published var isListening = false
    @Published var showFileImporter = false
    
    private func updateAgentTask(from log: String) {
        if log.contains("[Architect]") { currentAgentTask = "Architect is analyzing..." }
        else if log.contains("[Triage Cop]") { currentAgentTask = "Triage Cop is routing task..." }
        else if log.contains("[Engineer]") { currentAgentTask = "Engineer is writing code..." }
        else if log.contains("[Execution Engine]") { currentAgentTask = "Running code in secure sandbox..." }
        else if log.contains("[QA Lead]") { currentAgentTask = "QA is reviewing execution logs..." }
        else if log.contains("[Executive Summarizer]") { currentAgentTask = "Formatting final response..." }
        else if log.contains("Success") { currentAgentTask = "Transmitting payload..." }
    }
    
    // --- MANAGERS ---
    private let networkManager = NetworkManager()
    private let audioManager = AudioManager()
    private let speechManager = SpeechManager()
    private var isSkippyTalking = false
    
    init() {
        loadSessionsFromDisk()
        if sessions.isEmpty { createNewSession() }
        else { selectedSessionId = sessions.first?.id }
        
        setupBindings()
        networkManager.connect()
    }
    
    private func setupBindings() {
        // Network Bindings
        networkManager.onMessageReceived = { [weak self] jsonString in
            self?.handleIncomingJSON(jsonString)
        }
        
        networkManager.onConnectionStatusChanged = { [weak self] status in
            self?.isConnected = status
        }
        
        // Speech Bindings
        speechManager.onStateChanged = { [weak self] listening in
            DispatchQueue.main.async { self?.isListening = listening }
        }
        
        speechManager.onCommandRecognized = { [weak self] command in
            DispatchQueue.main.async { self?.sendMessage(textOverride: command) }
        }
    }
    
    // --- CHAT HISTORY MANAGEMENT ---
    func createNewSession() {
        let newSession = ChatSession(title: "New Project", messages: [])
        sessions.insert(newSession, at: 0)
        selectedSessionId = newSession.id
        saveSessionsToDisk()
    }
    
    func deleteSession(at offsets: IndexSet) {
        sessions.remove(atOffsets: offsets)
        saveSessionsToDisk()
        if sessions.isEmpty {
            createNewSession()
        } else if let id = selectedSessionId, !sessions.contains(where: { $0.id == id }) {
            selectedSessionId = sessions.first?.id
        }
    }
    
    private func loadSelectedSession() {
        guard let id = selectedSessionId, let session = sessions.first(where: { $0.id == id }) else { return }
        self.messages = session.messages
        self.logText = ""
        self.terminalLog = ""
    }
    
    private func updateCurrentSession() {
        guard let id = selectedSessionId, let index = sessions.firstIndex(where: { $0.id == id }) else { return }
        sessions[index].messages = self.messages
        if sessions[index].title == "New Project", let firstMsg = self.messages.first {
            let cleanTitle = firstMsg.replacingOccurrences(of: "You: ", with: "")
            sessions[index].title = String(cleanTitle.prefix(25)) + "..."
        }
        saveSessionsToDisk()
    }
    
    private func saveSessionsToDisk() {
        if let data = try? JSONEncoder().encode(sessions) {
            UserDefaults.standard.set(data, forKey: "skippy_sessions")
        }
    }
    
    private func loadSessionsFromDisk() {
        if let data = UserDefaults.standard.data(forKey: "skippy_sessions"),
           let savedSessions = try? JSONDecoder().decode([ChatSession].self, from: data) {
            self.sessions = savedSessions
        }
    }
    
    // --- FILE PARSING & MESSAGING ---
    func extractFileContents(from prompt: String) -> String {
        let enrichedPrompt = prompt
        guard let regex = try? NSRegularExpression(pattern: "(~?/[^\\s]+(?:/[^\\s]+)*\\.\\w+)") else { return prompt }
        let matches = regex.matches(in: prompt, range: NSRange(prompt.startIndex..., in: prompt))
        var injections = ""
        for match in matches {
            if let range = Range(match.range, in: prompt) {
                let path = String(prompt[range])
                let expandedPath = NSString(string: path).expandingTildeInPath
                if let content = try? String(contentsOfFile: expandedPath, encoding: .utf8) {
                    injections += "\n\n--- INJECTED FILE CONTENTS: \(path) ---\n\(String(content.prefix(10000)))\n---------------------------\n"
                } else {
                    injections += "\n\n--- FILE ERROR: Could not read \(path) on MacBook ---\n"
                }
            }
        }
        return enrichedPrompt + injections
    }
    func attachFile(from url: URL) {
        // Picker URLs are security-scoped; read the contents NOW while we
        // still have access, instead of pasting the path into the text box
        // (which broke on paths with spaces and lost access rights).
        let didAccess = url.startAccessingSecurityScopedResource()
        defer { if didAccess { url.stopAccessingSecurityScopedResource() } }
        
        let fileName = url.lastPathComponent
        
        // Text files are injected inline into the prompt.
        if let content = try? String(contentsOf: url, encoding: .utf8) {
            sendInjectedFile(fileName: fileName, fileContent: content, messageText: inputText)
            inputText = ""
            return
        }
        
        // Binary files are base64-uploaded to the Mac Studio so Skippy's tools
        // can reach them there by path.
        do {
            let data = try Data(contentsOf: url)
            guard data.count <= 60_000_000 else {
                messages.append("❌ \(fileName) is too large to attach (60MB limit).")
                updateCurrentSession()
                return
            }
            sendBinaryFile(fileName: fileName, base64: data.base64EncodedString(), messageText: inputText)
            inputText = ""
        } catch {
            messages.append("❌ Could not read \(fileName): \(error.localizedDescription)")
            updateCurrentSession()
        }
    }
    
    func sendBinaryFile(fileName: String, base64: String, messageText: String) {
        let userMessage = messageText.isEmpty ? "I have attached a file: \(fileName)" : messageText
        
        messages.append("You: (Attached \(fileName)) \(messageText)")
        updateCurrentSession()
        
        logText = ""
        terminalLog = ""
        isProcessing = true
        currentAgentTask = "Uploading \(fileName) to Mac Studio..."
        
        isSkippyTalking = true
        speechManager.mute()
        audioManager.stop()
        
        let payload: [String: Any] = [
            "mode": selectedMode,
            "text": userMessage,
            "history": messages.filter { $0.starts(with: "You:") || $0.starts(with: "Skippy:") },
            "use_tts": isConversationMode,
            "attachment": ["name": fileName, "data_base64": base64]
        ]
        
        networkManager.sendJSON(payload: payload)
    }
    
    func sendInjectedFile(fileName: String, fileContent: String, messageText: String = "") {
            // Build the composite message
            let userMessage = messageText.isEmpty ? "I have attached a file: \(fileName)" : messageText
            let finalMessage = "--- INJECTED FILE: \(fileName) ---\n\(fileContent)\n\n\(userMessage)"
            
            messages.append("You: (Attached \(fileName)) \(messageText)")
            updateCurrentSession()
            
            logText = ""
            terminalLog = ""
            isProcessing = true
            currentAgentTask = "Skippy is processing attached file..."
            
            // Mute audio while processing
            isSkippyTalking = true
            speechManager.mute()
            audioManager.stop()
            
            let payload: [String: Any] = [
                "mode": selectedMode,
                "text": finalMessage,
                "history": messages.filter { $0.starts(with: "You:") || $0.starts(with: "Skippy:") },
                "use_tts": isConversationMode
            ]
            
            networkManager.sendJSON(payload: payload)
        }
    func sendMessage(textOverride: String? = nil) {
        let msgToSend = textOverride ?? inputText
        guard !msgToSend.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        
        messages.append("You: \(msgToSend)")
        updateCurrentSession()
        
        if textOverride == nil { inputText = "" }
        logText = ""
        terminalLog = ""
        
        // ⚡ UI Updates for the Indicator
        isProcessing = true
        currentAgentTask = "Skippy is thinking..."
        
        // 🚨 TRAFFIC COP
        isSkippyTalking = true
        speechManager.mute()
        audioManager.stop()
        
        let enrichedMsg = extractFileContents(from: msgToSend)
        let payload: [String: Any] = [
            "mode": selectedMode,
            "text": enrichedMsg,
            "history": messages.filter { $0.starts(with: "You:") || $0.starts(with: "Skippy:") },
            "use_tts": isConversationMode
        ]
        
        networkManager.sendJSON(payload: payload)
    }
    
    func sendAuthResponse(approve: Bool) {
        let status = approve ? "APPROVE" : "DENY"
        networkManager.sendJSON(payload: ["type": "auth_response", "status": status])
        self.terminalAuthRequest = nil
        self.deploymentAuthRequest = nil
    }
    
    // --- DATA HANDLING ---
    private func handleIncomingJSON(_ jsonString: String) {
        guard let data = jsonString.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data, options: []) as? [String: Any],
              let msgType = json["type"] as? String else { return }
        
        DispatchQueue.main.async {
            if msgType == "log", let content = json["content"] as? String {
                self.logText += content + "\n"
                // ⚡ Pass the log to our parser to update the UI
                self.updateAgentTask(from: content)
                
            } else if msgType == "chat", let content = json["content"] as? String {
                self.messages.append("Skippy: \(content)")
                self.updateCurrentSession()
                // ⚡ Clear the indicator when chatting
                self.isProcessing = false
                self.currentAgentTask = nil
                
            } else if msgType == "write_file", let path = json["path"] as? String, let fileContent = json["content"] as? String {
                self.writeLocalFile(path: path, content: fileContent)
                
            } else if msgType == "terminal_auth", let cmd = json["command"] as? String {
                self.terminalAuthRequest = (command: cmd, explanation: json["explanation"] as? String ?? "")
                
            } else if msgType == "deployment_auth", let targetFile = json["target_file"] as? String {
                self.deploymentAuthRequest = (
                    targetFile: targetFile,
                    summary: json["summary"] as? String ?? "No summary provided.",
                    content: json["content"] as? String ?? ""
                )
                
            } else if msgType == "terminal_stream_start" {
                self.terminalLog = "--- TERMINAL ACTIVE ---\n"
                self.isTerminalOpen = true
                
            } else if msgType == "terminal_stream", let content = json["content"] as? String {
                self.terminalLog += content
                
            } else if msgType == "audio", let base64String = json["data"] as? String {
                
                // 🚨 TRAFFIC COP
                self.isSkippyTalking = true
                self.speechManager.mute()
                
                self.audioManager.scheduleAudioChunk(base64String: base64String) { [weak self] in
                    DispatchQueue.main.async {
                        if self?.audioManager.isPlaying == false {
                            self?.isSkippyTalking = false
                            if self?.isConversationMode == true { self?.speechManager.unmute() }
                        }
                    }
                }
                
            } else if msgType == "done" {
                // ⚡ Clear the indicator when the pipeline shuts down
                self.isProcessing = false
                self.currentAgentTask = nil
                
                // 🚨 TRAFFIC COP
                if self.audioManager.isPlaying == false {
                    self.isSkippyTalking = false
                    if self.isConversationMode { self.speechManager.unmute() }
                }
            }
        }
    }
    
    private func writeLocalFile(path: String, content: String) {
        let expandedPath = NSString(string: path).expandingTildeInPath
        let url = URL(fileURLWithPath: expandedPath)
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: nil)
            try content.write(to: url, atomically: true, encoding: .utf8)
            self.messages.append("✅ File successfully written to your MacBook: \(path)")
            self.updateCurrentSession()
        } catch {
            self.messages.append("❌ File Write Error on MacBook: \(error.localizedDescription)")
            self.updateCurrentSession()
        }
    }
}
