import SwiftUI
import Foundation

// MARK: - Process Manager
class ServerManager: ObservableObject {
    @Published var logs: String = "System Ready...\n"
    @Published var is70BRunning = false
    @Published var isCompressorRunning = false
    @Published var is405BRunning = false
    @Published var isBackendRunning = false
    
    // Debug Mode Toggle
    @Published var isDebugMode = false
    
    // Live System Stats
    @Published var cpuPercent: Double = 0.0
    @Published var ramPercent: Double = 0.0
    @Published var cpuText: String = "Calculating..."
    @Published var ramText: String = "Calculating..."
    
    // Auto-detect the Mac Studio's Total RAM in Gigabytes
    let totalRAM: Double = Double(ProcessInfo.processInfo.physicalMemory) / (1024 * 1024 * 1024)
    
    // Store processes
    private var process70B: Process?
    private var processCompressor: Process?
    private var process405B: Process?
    private var processBackend: Process?
    
    private var statsTimer: Timer?
    private var isFetchingStats = false // Safety lock to prevent thread pileups
    
    // The path for our permanent debug log
    private let debugLogURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("shop-jarvis/skippy_server_debug.log")
    
    init() {
        startMonitoring()
    }
    
    // Helper function to write to the log file in real-time
    private func writeToDebugFile(_ message: String) {
        guard isDebugMode else { return }
        
        let timestamp = DateFormatter.localizedString(from: Date(), dateStyle: .short, timeStyle: .medium)
        let logLine = "[\(timestamp)] \(message)\n"
        
        guard let data = logLine.data(using: .utf8) else { return }
        
        if FileManager.default.fileExists(atPath: debugLogURL.path) {
            if let fileHandle = try? FileHandle(forWritingTo: debugLogURL) {
                fileHandle.seekToEndOfFile()
                fileHandle.write(data)
                fileHandle.closeFile()
            }
        } else {
            try? data.write(to: debugLogURL)
        }
    }
    
    func log(_ message: String) {
        // 1. Immediately save to disk if Debug Mode is ON
        writeToDebugFile(message)
        
        // 2. Update the UI safely
        DispatchQueue.main.async {
            self.logs += "\(message)\n"
            // Keep roughly the last 50,000 characters to prevent UI freezing
            if self.logs.count > 50000 {
                self.logs = String(self.logs.suffix(50000))
            }
        }
    }
    
    func bootSequence() {
        log("🚀 Initiating Tri-Brain Boot Sequence...")
        
        // Offline mode is not optional. Without it mlx_lm.server calls the Hugging
        // Face API to check each model's revision, which fails with a 401 and takes
        // the server down with it — and reaches the network at runtime besides.
        let offline = "HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1"
        
        // 1. Boot 30B Architect
        process70B = runCommand("cd ~/shop-jarvis && source venv/bin/activate && \(offline) python -m mlx_lm.server --model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --host 127.0.0.1 --port 8080")
        is70BRunning = true
        
        // 2. Boot 32B Compressor
        processCompressor = runCommand("cd ~/shop-jarvis && source venv/bin/activate && \(offline) python -m mlx_lm.server --model mlx-community/Qwen2.5-Coder-32B-Instruct-4bit --host 127.0.0.1 --port 8082")
        isCompressorRunning = true
        
        // 3. Boot 480B Engineer
        process405B = runCommand("cd ~/shop-jarvis && source venv/bin/activate && \(offline) python -m mlx_lm.server --model mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit --host 127.0.0.1 --port 8081")
        is405BRunning = true
        
        // 4. Boot FastAPI Backend
        processBackend = runCommand("cd ~/shop-jarvis && source venv/bin/activate && python skippy_factory.py")
        isBackendRunning = true
    }
    
    func killAll() {
        log("🛑 Terminating all server instances...")
        process70B?.terminate()
        processCompressor?.terminate()
        process405B?.terminate()
        processBackend?.terminate()
        
        is70BRunning = false
        isCompressorRunning = false
        is405BRunning = false
        isBackendRunning = false
        
        // Ensure all service ports are cleared
        _ = runCommand("lsof -ti:8080,8081,8082,8000 | xargs kill -9")
        log("All ports cleared.")
    }
    
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
    
    // MARK: - Performance Monitor
    private func startMonitoring() {
        statsTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { _ in
            self.fetchSystemStats()
        }
    }
    
    private func fetchSystemStats() {
        // Safety check to prevent threads from piling up
        guard !isFetchingStats else { return }
        isFetchingStats = true
        
        DispatchQueue.global(qos: .background).async {
            // 1. CPU Usage (Executes in ~0.02 seconds)
            let cpuTask = Process()
            let cpuPipe = Pipe()
            cpuTask.executableURL = URL(fileURLWithPath: "/bin/zsh")
            // ps sums up total CPU % across all cores
            cpuTask.arguments = ["-c", "ps -A -o %cpu | awk '{s+=$1} END {print s}'"]
            cpuTask.standardOutput = cpuPipe
            try? cpuTask.run()
            cpuTask.waitUntilExit()
            
            var newCpuPercent = 0.0
            var newCpuText = ""
            if let cpuData = try? cpuPipe.fileHandleForReading.readToEnd(),
               let cpuStr = String(data: cpuData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
               let cpuTotal = Double(cpuStr) {
                // Normalize against your Mac Studio's actual core count
                let activeCores = Double(ProcessInfo.processInfo.activeProcessorCount)
                newCpuPercent = min(cpuTotal / (activeCores * 100.0), 1.0)
                newCpuText = String(format: "%.1f%% usage", (newCpuPercent * 100.0))
            }
            
            // 2. RAM Usage (Matches Activity Monitor exactly)
            let ramTask = Process()
            let ramPipe = Pipe()
            ramTask.executableURL = URL(fileURLWithPath: "/bin/zsh")
            // Extracts App Memory (Active) + Wired + Compressed, multiplied by Apple Silicon's 16k page size
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
                // Release the lock
                self.isFetchingStats = false
            }
        }
    }
}

// MARK: - Dashboard UI
struct ContentView: View {
    @StateObject private var manager = ServerManager()
    
    var body: some View {
        VStack(spacing: 20) {
            // Header
            HStack {
                Text("Skippy Server Architecture")
                    .font(.largeTitle)
                    .fontWeight(.heavy)
                
                Spacer()
                
                // Debug Mode Toggle in Header
                Toggle("Debug Mode", isOn: $manager.isDebugMode)
                    .toggleStyle(SwitchToggleStyle(tint: .orange))
                    .help("Streams all terminal output to ~/shop-jarvis/skippy_server_debug.log")
            }
            
            // Performance Progress Bars
            HStack(spacing: 40) {
                ProgressBar(title: "CPU", percentage: manager.cpuPercent, textValue: manager.cpuText, color: .blue)
                ProgressBar(title: "RAM (\(Int(manager.totalRAM))GB)", percentage: manager.ramPercent, textValue: manager.ramText, color: .purple)
            }
            .padding()
            .background(Color.gray.opacity(0.1))
            .cornerRadius(10)
            
            // Status Cards
            HStack(spacing: 15) {
                StatusCard(title: "30B Architect", port: "8080", isRunning: manager.is70BRunning)
                StatusCard(title: "32B Compressor", port: "8082", isRunning: manager.isCompressorRunning)
                StatusCard(title: "480B Engineer", port: "8081", isRunning: manager.is405BRunning)
                StatusCard(title: "Factory Backend", port: "8000", isRunning: manager.isBackendRunning)
            }
            
            // Log Console
            ScrollView {
                Text(manager.logs)
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                    .textSelection(.enabled)
            }
            .background(Color.black.opacity(0.8))
            .foregroundColor(.green)
            .cornerRadius(10)
            
            // Controls
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
                .disabled(manager.isBackendRunning)
                
                Button(action: {
                    manager.killAll()
                    manager.log("⏳ Waiting for ports to clear before rebooting...")
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
                .disabled(!manager.isBackendRunning)
                
                Button(action: { manager.killAll() }) {
                    Text("KILL SERVERS")
                        .fontWeight(.bold)
                        .padding()
                        .frame(maxWidth: .infinity)
                        .background(Color.red)
                        .foregroundColor(.white)
                        .cornerRadius(8)
                }
                .disabled(!manager.isBackendRunning)
            }
        }
        .padding()
        .frame(minWidth: 850, minHeight: 650)
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
    let isRunning: Bool
    
    var body: some View {
        VStack(alignment: .leading) {
            Text(title).font(.headline)
            Text("Port: \(port)").font(.subheadline).foregroundColor(.gray)
            HStack {
                Circle()
                    .fill(isRunning ? Color.green : Color.red)
                    .frame(width: 10, height: 10)
                Text(isRunning ? "ONLINE" : "OFFLINE")
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
