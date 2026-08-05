import SwiftUI
import Foundation

// MARK: - Managed services

/// One process this app boots and watches. The stack is (see skippy_llm.py):
///   fast   8080  Qwen3-Coder-30B  — cheap turns, chat lane, compressor
///   heavy  8081  Qwen3-Coder-480B — the coding brain behind the agent loop
///   voice  8083  Qwen3-30B-A3B    — the conversational brain behind /ws/voice
///   hub    8000  skippy_factory   — FastAPI: /ws/factory, /ws/voice, /health
enum Service: String, CaseIterable, Identifiable {
    case fast, heavy, voiceBrain, hub

    var id: String { rawValue }

    var title: String {
        switch self {
        case .fast: return "Fast (30B Coder)"
        case .heavy: return "Heavy (480B)"
        case .voiceBrain: return "Voice Brain (30B)"
        case .hub: return "Skippy Hub"
        }
    }

    var port: Int {
        switch self {
        case .fast: return 8080
        case .heavy: return 8081
        case .voiceBrain: return 8083
        case .hub: return 8000
        }
    }

    /// Anything that answers HTTP counts as alive; model servers 404 on paths
    /// they do not implement and that is still a running server.
    var healthPath: String {
        self == .hub ? "/health" : "/v1/models"
    }
}

enum ServiceState: Equatable {
    case offline, starting, online

    var label: String {
        switch self {
        case .offline: return "OFFLINE"
        case .starting: return "STARTING"
        case .online: return "ONLINE"
        }
    }

    var color: Color {
        switch self {
        case .offline: return .red
        case .starting: return .yellow
        case .online: return .green
        }
    }
}

// MARK: - Process Manager

class ServerManager: ObservableObject {
    @Published var logs: String = "System Ready...\n"
    @Published var states: [Service: ServiceState] = Dictionary(
        uniqueKeysWithValues: Service.allCases.map { ($0, .offline) }
    )
    @Published var isBooting = false
    @Published var isDebugMode = false

    // Live system stats
    @Published var cpuPercent: Double = 0.0
    @Published var ramPercent: Double = 0.0
    @Published var cpuText: String = "Calculating..."
    @Published var ramText: String = "Calculating..."

    // -- Persisted configuration -------------------------------------------
    //
    // The workspace roots are the agent's blast radius, so they are a setting a
    // person edits deliberately, not a constant. The old build hardcoded the
    // *test fixtures* here, which is why the first live chat greeted its user
    // with "you have two workspace roots: fixture-app, fixture-lib".
    @Published var workspaceRoots: String {
        didSet { UserDefaults.standard.set(workspaceRoots, forKey: "skippy.workspaceRoots") }
    }
    /// Shared secret for /ws/voice. Generated once and persisted, so clients do
    /// not have to be re-pasted a new token after every reboot (the old flow
    /// kept it in /tmp, which macOS wipes on restart).
    @Published var voiceToken: String {
        didSet { UserDefaults.standard.set(voiceToken, forKey: "skippy.voiceToken") }
    }
    @Published var autoBoot: Bool {
        didSet { UserDefaults.standard.set(autoBoot, forKey: "skippy.autoBoot") }
    }

    let totalRAM: Double = Double(ProcessInfo.processInfo.physicalMemory) / (1024 * 1024 * 1024)

    private var processes: [Service: Process] = [:]
    private var statsTimer: Timer?
    private var healthTimer: Timer?
    private var isFetchingStats = false

    static let repoDirName = "skippy"

    private var home: String { FileManager.default.homeDirectoryForCurrentUser.path }
    private var repoPath: String { "\(home)/\(ServerManager.repoDirName)" }

    private let debugLogURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("\(ServerManager.repoDirName)/skippy_server_debug.log")

    init() {
        let defaults = UserDefaults.standard
        workspaceRoots = defaults.string(forKey: "skippy.workspaceRoots")
            ?? "\(FileManager.default.homeDirectoryForCurrentUser.path)/\(ServerManager.repoDirName)"
        voiceToken = defaults.string(forKey: "skippy.voiceToken") ?? Self.generateToken()
        autoBoot = defaults.object(forKey: "skippy.autoBoot") as? Bool ?? true
        // First run: persist the generated token so it survives restarts.
        if defaults.string(forKey: "skippy.voiceToken") == nil {
            defaults.set(voiceToken, forKey: "skippy.voiceToken")
        }
        startMonitoring()
        startHealthPolling()
    }

    static func generateToken() -> String {
        let alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return String((0..<40).map { _ in alphabet.randomElement()! })
    }

    func regenerateToken() {
        voiceToken = Self.generateToken()
        log("🔑 New voice token generated. Update it in each client's Settings, then reboot the hub.")
    }

    // -- Logging ------------------------------------------------------------

    private func writeToDebugFile(_ message: String) {
        guard isDebugMode else { return }
        let timestamp = DateFormatter.localizedString(from: Date(), dateStyle: .short, timeStyle: .medium)
        guard let data = "[\(timestamp)] \(message)\n".data(using: .utf8) else { return }
        if FileManager.default.fileExists(atPath: debugLogURL.path) {
            if let handle = try? FileHandle(forWritingTo: debugLogURL) {
                handle.seekToEndOfFile()
                handle.write(data)
                handle.closeFile()
            }
        } else {
            try? data.write(to: debugLogURL)
        }
    }

    func log(_ message: String) {
        writeToDebugFile(message)
        DispatchQueue.main.async {
            self.logs += "\(message)\n"
            if self.logs.count > 50000 {
                self.logs = String(self.logs.suffix(50000))
            }
        }
    }

    // MARK: - Boot sequence
    //
    // Staged rather than fire-and-forget. The old build launched four commands
    // and flipped every status light green immediately; a model server that
    // died on load stayed "ONLINE" until someone read the log. Here each stage
    // is confirmed by an actual HTTP answer before the light changes.

    func bootSequence() {
        guard !isBooting else { return }
        isBooting = true

        Task {
            await self.boot()
            await MainActor.run { self.isBooting = false }
        }
    }

    private func boot() async {
        log("🚀 Skippy boot sequence starting…")
        log("   Workspace roots: \(workspaceRoots)")

        // Stage 0: make sure nothing stale is squatting on our ports.
        await clearPorts()

        // Offline mode is not optional: without it mlx_lm.server phones the
        // Hugging Face API per model revision check, 401s, and dies.
        let offline = "HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1"
        let enter = "cd \(repoPath) && source venv/bin/activate"

        // Stage 1: the three model servers, in parallel. The heavy model takes
        // minutes to map 260GB of weights; starting it first means its load
        // overlaps the smaller two rather than following them.
        launch(.heavy, command: "\(enter) && \(offline) python -m mlx_lm.server --model mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit --host 127.0.0.1 --port 8081")
        launch(.fast, command: "\(enter) && \(offline) python -m mlx_lm.server --model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --host 127.0.0.1 --port 8080")
        launch(.voiceBrain, command: "\(enter) && \(offline) python -m mlx_lm.server --model mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit --host 127.0.0.1 --port 8083")

        // Stage 2: wait for the small models; the hub needs them for its chat
        // and voice lanes. The heavy model keeps loading in the background —
        // the hub retries per request, so it does not need to gate the boot.
        for service in [Service.fast, Service.voiceBrain] {
            let ok = await waitUntilOnline(service, timeout: 300)
            log(ok ? "✅ \(service.title) is answering on \(service.port)."
                   : "⚠️ \(service.title) did not answer within 5 minutes — check the log above.")
        }

        // Stage 3: the hub, configured for the real stack:
        //  - real workspace roots (the whole point of this rebuild)
        //  - LAN bind + voice token, so the MacBook, iPhone and Core2 can reach it
        //  - the cloned studio voice on Chatterbox, Parakeet STT, 24kHz out
        let roots = workspaceRoots
            .split(separator: ":")
            .map { ($0 as NSString).expandingTildeInPath }
            .joined(separator: ":")
        let env = [
            "SKIPPY_WORKSPACE_ROOTS=\"\(roots)\"",
            "SKIPPY_BIND_HOST=0.0.0.0",
            "SKIPPY_VOICE_TOKEN=\"\(voiceToken)\"",
            "SKIPPY_VOICE_STT=mlx:mlx-community/parakeet-tdt-0.6b-v3",
            "SKIPPY_VOICE_TTS=mlx:mlx-community/chatterbox-turbo-fp16",
            "SKIPPY_VOICE_TTS_REF=\(repoPath)/voice_ref_studio.wav",
            "SKIPPY_VOICE_OUT_RATE=24000",
            offline,
        ].joined(separator: " ")
        launch(.hub, command: "\(enter) && \(env) python skippy_factory.py")

        let hubUp = await waitUntilOnline(.hub, timeout: 180)
        log(hubUp ? "✅ Hub is answering on 8000 (LAN bind, token required for voice)."
                  : "⚠️ Hub did not answer within 3 minutes — check the log above.")

        // Stage 4: the heavy brain, which was loading this whole time.
        let heavyUp = await waitUntilOnline(.heavy, timeout: 900)
        log(heavyUp ? "✅ Heavy model is answering on 8081."
                    : "⚠️ Heavy model still not answering after 15 minutes.")

        log(hubUp ? "🎉 Boot complete. Clients connect to port 8000." : "🛑 Boot finished with errors.")
    }

    private func launch(_ service: Service, command: String) {
        DispatchQueue.main.async { self.states[service] = .starting }
        log("▶️ Starting \(service.title) on port \(service.port)…")
        processes[service] = runCommand(command)
    }

    func killAll() {
        log("🛑 Terminating all server instances…")
        for (_, process) in processes {
            process.terminate()
        }
        processes.removeAll()
        Task { await self.clearPorts() }
        DispatchQueue.main.async {
            for service in Service.allCases { self.states[service] = .offline }
        }
    }

    private func clearPorts() async {
        let ports = Service.allCases.map { String($0.port) }.joined(separator: ",")
        _ = await runAndWait("lsof -ti:\(ports) | xargs kill -9 2>/dev/null; true")
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        log("Ports \(ports) cleared.")
    }

    // MARK: - Health

    private func healthURL(_ service: Service) -> URL {
        URL(string: "http://127.0.0.1:\(service.port)\(service.healthPath)")!
    }

    /// Alive means "answered HTTP at all". mlx_lm's route table varies between
    /// versions; a 404 from a listening server is still a listening server.
    private func isAnswering(_ service: Service) async -> Bool {
        var request = URLRequest(url: healthURL(service))
        request.timeoutInterval = 3
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            return response is HTTPURLResponse
        } catch {
            return false
        }
    }

    private func waitUntilOnline(_ service: Service, timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if await isAnswering(service) {
                await MainActor.run { self.states[service] = .online }
                return true
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
        await MainActor.run { self.states[service] = .offline }
        return false
    }

    /// Background poll so the lights reflect reality, not the last button press.
    private func startHealthPolling() {
        healthTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { _ in
            Task {
                for service in Service.allCases {
                    let alive = await self.isAnswering(service)
                    await MainActor.run {
                        // Do not stomp "starting": a model mid-load is not offline.
                        if alive {
                            self.states[service] = .online
                        } else if self.states[service] == .online {
                            self.states[service] = .offline
                        }
                    }
                }
            }
        }
    }

    // MARK: - Shell plumbing

    private func runCommand(_ command: String) -> Process {
        let task = Process()
        let pipe = Pipe()
        task.executableURL = URL(fileURLWithPath: "/bin/zsh")
        task.arguments = ["-c", command]
        task.standardOutput = pipe
        task.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if let output = String(data: data, encoding: .utf8), !output.isEmpty {
                self.log(output.trimmingCharacters(in: .newlines))
            }
        }
        try? task.run()
        return task
    }

    private func runAndWait(_ command: String) async -> String {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let task = Process()
                let pipe = Pipe()
                task.executableURL = URL(fileURLWithPath: "/bin/zsh")
                task.arguments = ["-c", command]
                task.standardOutput = pipe
                task.standardError = pipe
                try? task.run()
                task.waitUntilExit()
                let data = (try? pipe.fileHandleForReading.readToEnd()) ?? Data()
                continuation.resume(returning: String(data: data, encoding: .utf8) ?? "")
            }
        }
    }

    // MARK: - Performance Monitor

    private func startMonitoring() {
        statsTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { _ in
            self.fetchSystemStats()
        }
    }

    private func fetchSystemStats() {
        guard !isFetchingStats else { return }
        isFetchingStats = true

        DispatchQueue.global(qos: .background).async {
            let cpuTask = Process()
            let cpuPipe = Pipe()
            cpuTask.executableURL = URL(fileURLWithPath: "/bin/zsh")
            cpuTask.arguments = ["-c", "ps -A -o %cpu | awk '{s+=$1} END {print s}'"]
            cpuTask.standardOutput = cpuPipe
            try? cpuTask.run()
            cpuTask.waitUntilExit()

            var newCpuPercent = 0.0
            var newCpuText = ""
            if let cpuData = try? cpuPipe.fileHandleForReading.readToEnd(),
               let cpuStr = String(data: cpuData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
               let cpuTotal = Double(cpuStr) {
                let activeCores = Double(ProcessInfo.processInfo.activeProcessorCount)
                newCpuPercent = min(cpuTotal / (activeCores * 100.0), 1.0)
                newCpuText = String(format: "%.1f%% usage", (newCpuPercent * 100.0))
            }

            let ramTask = Process()
            let ramPipe = Pipe()
            ramTask.executableURL = URL(fileURLWithPath: "/bin/zsh")
            ramTask.arguments = ["-c", "vm_stat | grep -E 'Pages active|Pages wired down|Pages occupied by compressor' | awk '{sum+=$NF} END {print sum * 16384 / 1073741824}'"]
            ramTask.standardOutput = ramPipe
            try? ramTask.run()
            ramTask.waitUntilExit()

            var newRamPercent = 0.0
            var newRamText = ""
            if let ramData = try? ramPipe.fileHandleForReading.readToEnd(),
               let ramStr = String(data: ramData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
               let usedGB = Double(ramStr) {
                newRamPercent = min(usedGB / self.totalRAM, 1.0)
                newRamText = String(format: "%.1f GB used", usedGB)
            }

            DispatchQueue.main.async {
                if !newCpuText.isEmpty {
                    self.cpuText = newCpuText
                    self.cpuPercent = newCpuPercent
                }
                if !newRamText.isEmpty {
                    self.ramText = newRamText
                    self.ramPercent = newRamPercent
                }
                self.isFetchingStats = false
            }
        }
    }

    var anythingRunning: Bool {
        states.values.contains { $0 != .offline }
    }
}

// MARK: - Dashboard UI

struct ContentView: View {
    @StateObject private var manager = ServerManager()
    @State private var bootedOnLaunch = false

    var body: some View {
        VStack(spacing: 16) {
            header
            configuration
            performance
            statusCards
            console
            controls
        }
        .padding()
        .frame(minWidth: 900, minHeight: 720)
        .onAppear {
            guard !bootedOnLaunch else { return }
            bootedOnLaunch = true
            if manager.autoBoot && !manager.anythingRunning {
                manager.log("Auto-boot is on; starting the stack.")
                manager.bootSequence()
            }
        }
    }

    private var header: some View {
        HStack {
            Text("Skippy Server")
                .font(.largeTitle)
                .fontWeight(.heavy)
            Spacer()
            Toggle("Boot on launch", isOn: $manager.autoBoot)
                .toggleStyle(SwitchToggleStyle(tint: .blue))
            Toggle("Debug Mode", isOn: $manager.isDebugMode)
                .toggleStyle(SwitchToggleStyle(tint: .orange))
                .help("Streams all terminal output to ~/\(ServerManager.repoDirName)/skippy_server_debug.log")
        }
    }

    private var configuration: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Workspace roots").font(.headline).frame(width: 130, alignment: .leading)
                TextField("colon-separated repository paths", text: $manager.workspaceRoots)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.body, design: .monospaced))
                    .help("The repositories Skippy may read and edit, separated by colons. Takes effect at the next boot.")
            }
            HStack {
                Text("Voice token").font(.headline).frame(width: 130, alignment: .leading)
                Text(manager.voiceToken)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(manager.voiceToken, forType: .string)
                }
                Button("Regenerate") { manager.regenerateToken() }
                Spacer()
            }
            Text("Paste the voice token into each client's Settings once — it persists across reboots.")
                .font(.caption)
                .foregroundColor(.gray)
        }
        .padding()
        .background(Color.gray.opacity(0.1))
        .cornerRadius(10)
    }

    private var performance: some View {
        HStack(spacing: 40) {
            ProgressBar(title: "CPU", percentage: manager.cpuPercent, textValue: manager.cpuText, color: .blue)
            ProgressBar(title: "RAM (\(Int(manager.totalRAM))GB)", percentage: manager.ramPercent, textValue: manager.ramText, color: .purple)
        }
        .padding()
        .background(Color.gray.opacity(0.1))
        .cornerRadius(10)
    }

    private var statusCards: some View {
        HStack(spacing: 15) {
            ForEach(Service.allCases) { service in
                StatusCard(
                    title: service.title,
                    port: "\(service.port)",
                    state: manager.states[service] ?? .offline
                )
            }
        }
    }

    private var console: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Text(manager.logs)
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                    .textSelection(.enabled)
                    .id("log-end")
            }
            .background(Color.black.opacity(0.8))
            .foregroundColor(.green)
            .cornerRadius(10)
            .onChange(of: manager.logs) { _ in
                proxy.scrollTo("log-end", anchor: .bottom)
            }
        }
    }

    private var controls: some View {
        HStack {
            Button(action: { manager.bootSequence() }) {
                Text("INITIATE BOOT")
                    .fontWeight(.bold)
                    .padding()
                    .frame(maxWidth: .infinity)
                    .background(Color.blue)
                    .foregroundColor(.white)
                    .cornerRadius(8)
            }
            .buttonStyle(.plain)
            .disabled(manager.isBooting || manager.anythingRunning)

            Button(action: {
                manager.killAll()
                manager.log("⏳ Waiting for ports to clear before rebooting…")
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                    manager.bootSequence()
                }
            }) {
                Text("REBOOT SYSTEM")
                    .fontWeight(.bold)
                    .padding()
                    .frame(maxWidth: .infinity)
                    .background(Color.orange)
                    .foregroundColor(.white)
                    .cornerRadius(8)
            }
            .buttonStyle(.plain)
            .disabled(manager.isBooting || !manager.anythingRunning)

            Button(action: { manager.killAll() }) {
                Text("KILL SERVERS")
                    .fontWeight(.bold)
                    .padding()
                    .frame(maxWidth: .infinity)
                    .background(Color.red)
                    .foregroundColor(.white)
                    .cornerRadius(8)
            }
            .buttonStyle(.plain)
            .disabled(!manager.anythingRunning)
        }
    }
}

// Custom Component: Sleek Progress Bar
struct ProgressBar: View {
    var title: String
    var percentage: Double
    var textValue: String
    var color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                Text("\(Int(percentage * 100))%")
                    .font(.headline)
                    .foregroundColor(color)
            }

            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Rectangle()
                        .frame(width: geometry.size.width, height: 12)
                        .opacity(0.2)
                        .foregroundColor(color)

                    Rectangle()
                        .frame(width: min(CGFloat(max(0, percentage)) * geometry.size.width, geometry.size.width), height: 12)
                        .foregroundColor(color)
                        .animation(.linear(duration: 0.5), value: percentage)
                }
                .cornerRadius(6)
            }
            .frame(height: 12)

            Text(textValue)
                .font(.system(size: 10, design: .monospaced))
                .foregroundColor(.gray)
                .lineLimit(1)
        }
    }
}

struct StatusCard: View {
    let title: String
    let port: String
    let state: ServiceState

    var body: some View {
        VStack(alignment: .leading) {
            Text(title).font(.headline)
            Text("Port: \(port)").font(.subheadline).foregroundColor(.gray)
            HStack {
                Circle()
                    .fill(state.color)
                    .frame(width: 10, height: 10)
                Text(state.label)
                    .font(.caption)
                    .fontWeight(.bold)
            }
            .padding(.top, 5)
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.gray.opacity(0.1))
        .cornerRadius(10)
    }
}
