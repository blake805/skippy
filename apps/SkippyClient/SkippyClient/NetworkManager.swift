import Foundation

class NetworkManager {
    private var webSocketTask: URLSessionWebSocketTask?
    let serverURL = "ws://192.168.1.151:8000/ws/factory"
    
    var onMessageReceived: ((String) -> Void)?
    var onConnectionStatusChanged: ((Bool) -> Void)? // ⚡ Broadcasts connection state
    
    func connect() {
        guard let url = URL(string: serverURL) else { return }
        webSocketTask = URLSession.shared.webSocketTask(with: url)
        webSocketTask?.resume()
        
        // Verify the handshake actually completed before showing green:
        // sendPing only succeeds on an established connection.
        webSocketTask?.sendPing { [weak self] error in
            DispatchQueue.main.async {
                self?.onConnectionStatusChanged?(error == nil)
            }
        }
        receiveMessage()
    }
    
    private func receiveMessage() {
        webSocketTask?.receive { [weak self] result in
            guard let self = self else { return }
            
            if case .success(let message) = result, case .string(let text) = message {
                self.onMessageReceived?(text)
                self.receiveMessage()
            } else if case .failure(_) = result {
                DispatchQueue.main.async { self.onConnectionStatusChanged?(false) }
                
                // Exponential backoff or simple reconnect logic
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                    self.connect()
                }
            }
        }
    }
    
    func sendJSON(payload: [String: Any]) {
        guard let jsonData = try? JSONSerialization.data(withJSONObject: payload),
              let jsonString = String(data: jsonData, encoding: .utf8) else { return }
        webSocketTask?.send(.string(jsonString)) { _ in }
    }
}
