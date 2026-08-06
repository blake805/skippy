import Foundation

/// Thin URLSessionWebSocketTask wrapper with auto-reconnect.
final class WebSocketSession: NSObject, URLSessionWebSocketDelegate {
    var onMessage: (([String: Any]) -> Void)?
    var onBinary: ((Data) -> Void)?
    var onStateChange: ((Bool) -> Void)?

    private var task: URLSessionWebSocketTask?
    private var session: URLSession!
    private var url: URL?
    private var shouldRun = false
    private var reconnectPending = false
    private let queue = DispatchQueue(label: "skippy.ws")

    override init() {
        super.init()
        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    func connect(to url: URL) {
        queue.async {
            self.shouldRun = true
            self.url = url
            self.open()
        }
    }

    func disconnect() {
        queue.async {
            self.shouldRun = false
            self.task?.cancel(with: .goingAway, reason: nil)
            self.task = nil
            DispatchQueue.main.async { self.onStateChange?(false) }
        }
    }

    func sendJSON(_ payload: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else { return }
        queue.async {
            self.task?.send(.string(text)) { _ in }
        }
    }

    /// Verbatim text, for relays that must not re-serialize what they carry.
    func sendText(_ text: String) {
        queue.async {
            self.task?.send(.string(text)) { _ in }
        }
    }

    func sendBinary(_ data: Data) {
        queue.async {
            self.task?.send(.data(data)) { _ in }
        }
    }

    private func open() {
        guard shouldRun, let url else { return }
        task?.cancel(with: .goingAway, reason: nil)
        let next = session.webSocketTask(with: url)
        task = next
        next.resume()
        listen(on: next)
        // Ping forces the handshake to complete so connected flips true.
        next.sendPing { [weak self] error in
            DispatchQueue.main.async {
                self?.onStateChange?(error == nil)
            }
            if error != nil {
                self?.scheduleReconnect(for: next)
            }
        }
    }

    private func listen(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                DispatchQueue.main.async { self.onStateChange?(false) }
                self.scheduleReconnect(for: task)
            case .success(let message):
                switch message {
                case .string(let text):
                    if let data = text.data(using: .utf8),
                       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        DispatchQueue.main.async { self.onMessage?(obj) }
                    }
                case .data(let data):
                    DispatchQueue.main.async { self.onBinary?(data) }
                @unknown default:
                    break
                }
                self.listen(on: task)
            }
        }
    }

    /// Reconnect only in response to the *current* task failing. Every open()
    /// cancels the previous task, and that cancellation fires the old task's
    /// failure callbacks; without this guard each reconnect scheduled another
    /// one, and the session tore down a healthy connection every two seconds
    /// forever. (That loop is why voice "couldn't hear" anyone: the socket
    /// never lived long enough to carry a full utterance.)
    private func scheduleReconnect(for failed: URLSessionWebSocketTask?) {
        queue.async {
            guard self.shouldRun, !self.reconnectPending else { return }
            guard failed == nil || failed === self.task else { return }
            self.reconnectPending = true
            self.queue.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                guard let self else { return }
                self.reconnectPending = false
                guard self.shouldRun else { return }
                self.open()
            }
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        DispatchQueue.main.async { self.onStateChange?(true) }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        DispatchQueue.main.async { self.onStateChange?(false) }
        scheduleReconnect(for: webSocketTask)
    }
}
