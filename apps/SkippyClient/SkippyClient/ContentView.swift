import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject var viewModel = SkippyViewModel()
    
    @State private var pickerMode: String = "Shop"
    @State private var showDevWarning: Bool = false
    
    var body: some View {
        NavigationSplitView {
            // SIDEBAR
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("Projects")
                        .font(.title2)
                        .fontWeight(.bold)
                    Spacer()
                    Button(action: { viewModel.createNewSession() }) {
                        Image(systemName: "square.and.pencil")
                            .font(.system(size: 18, weight: .semibold))
                    }
                    .buttonStyle(PlainButtonStyle())
                    .foregroundColor(.blue)
                }
                .padding()
                .background(.ultraThinMaterial)
                
                Divider()
                
                List(selection: $viewModel.selectedSessionId) {
                    ForEach(viewModel.sessions) { session in
                        Text(session.title)
                            .padding(.vertical, 4)
                            .tag(session.id)
                    }
                    .onDelete(perform: viewModel.deleteSession)
                }
                .listStyle(SidebarListStyle())
            }
        } detail: {
            ZStack {
                // MAIN CHAT BACKGROUND
                Color(NSColor.windowBackgroundColor).edgesIgnoringSafeArea(.all)
                
                VStack(spacing: 0) {
                    // SLEEK HEADER
                    HStack {
                        Image(systemName: "cpu.fill")
                            .font(.title2)
                            .foregroundColor(.blue)
                        
                        Text("Skippy")
                            .font(.title3)
                            .fontWeight(.bold)
                        
                        // ⚡ Connection Status Dot
                        Circle()
                            .fill(viewModel.isConnected ? Color.green : Color.red)
                            .frame(width: 8, height: 8)
                            .shadow(color: viewModel.isConnected ? Color.green.opacity(0.8) : Color.red.opacity(0.8), radius: 3, x: 0, y: 0)
                            .padding(.leading, 2)
                            .help(viewModel.isConnected ? "Connected to Mac Studio" : "Disconnected from Backend")
                        
                        Spacer()
                        
                        Picker("", selection: $pickerMode) {
                            Text("Shop Engineer").tag("Shop")
                            Text("Software Dev").tag("Software")
                            Text("Electronics").tag("Electronics")
                            Text("CAM/G-Code").tag("CNC")
                            Text("Whiteboard").tag("Whiteboard")
                            Text("Developer").tag("Developer")
                        }
                        .pickerStyle(SegmentedPickerStyle())
                        .frame(width: 450)
                        .onChange(of: pickerMode) { oldValue, newValue in
                            if newValue == "Developer" && viewModel.selectedMode != "Developer" {
                                showDevWarning = true
                            } else {
                                viewModel.selectedMode = newValue
                            }
                        }
                        
                        Spacer()
                        
                        // MIC STATUS
                        HStack(spacing: 8) {
                            if viewModel.isConversationMode {
                                Circle()
                                    .fill(viewModel.isListening ? Color.green : Color.red.opacity(0.8))
                                    .frame(width: 10, height: 10)
                                    .animation(.easeInOut, value: viewModel.isListening)
                            }
                            Toggle("Voice", isOn: $viewModel.isConversationMode)
                                .toggleStyle(SwitchToggleStyle(tint: .blue))
                                .labelsHidden()
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.black.opacity(0.1))
                        .cornerRadius(20)
                    }
                    .padding(.horizontal, 20)
                    .padding(.vertical, 12)
                    .background(.regularMaterial)
                    .shadow(color: Color.black.opacity(0.05), radius: 5, x: 0, y: 5)
                    .zIndex(1)
                    
                    // CHAT AREA
                                        ScrollViewReader { proxy in
                                            ScrollView {
                                                // Empty State Welcome Screen
                                                if viewModel.messages.isEmpty {
                                                    VStack(spacing: 15) {
                                                        Image(systemName: "macstudio.fill")
                                                            .font(.system(size: 50))
                                                            .foregroundColor(Color.gray.opacity(0.4))
                                                        Text("How can I assist you in the shop today?")
                                                            .font(.title2)
                                                            .fontWeight(.medium)
                                                            .foregroundColor(Color.gray.opacity(0.6))
                                                    }
                                                    .frame(maxWidth: .infinity)
                                                    .padding(.top, 100)
                                                } else {
                                                    VStack(alignment: .leading, spacing: 20) {
                                                        ForEach(viewModel.messages.indices, id: \.self) { index in
                                                            let msg = viewModel.messages[index]
                                                            let isUser = msg.starts(with: "You:")
                                                            
                                                            HStack {
                                                                if isUser { Spacer() }
                                                                
                                                                Text(msg.replacingOccurrences(of: "You: ", with: "").replacingOccurrences(of: "Skippy: ", with: ""))
                                                                    .padding(16)
                                                                    .background(isUser ?
                                                                                AnyView(LinearGradient(gradient: Gradient(colors: [Color.blue, Color.indigo]), startPoint: .topLeading, endPoint: .bottomTrailing)) :
                                                                                AnyView(Color(NSColor.controlBackgroundColor)))
                                                                    .foregroundColor(isUser ? .white : .primary)
                                                                    .clipShape(RoundedRectangle(cornerRadius: 18))
                                                                    .textSelection(.enabled)
                                                                    .shadow(color: Color.black.opacity(0.1), radius: 4, x: 0, y: 2)
                                                                
                                                                if !isUser { Spacer() }
                                                            }
                                                            .id(index)
                                                        }
                                                    }
                                                    .padding(.horizontal, 20)
                                                    .padding(.vertical, 25)
                                                }
                                            }
                                            .onChange(of: viewModel.messages.count) { oldValue, newValue in
                                                withAnimation(.easeOut(duration: 0.3)) {
                                                    proxy.scrollTo(newValue - 1, anchor: .bottom)
                                                }
                                            }
                                            // ⚡ THE NEW DRAG AND DROP MODIFIER
                                            .onDrop(of: [.fileURL], isTargeted: nil) { providers in
                                                guard let provider = providers.first else { return false }
                                                
                                                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { (item, error) in
                                                    guard let data = item as? Data,
                                                          let url = URL(dataRepresentation: data, relativeTo: nil) else { return }
                                                    
                                                    DispatchQueue.main.async {
                                                        do {
                                                            if url.startAccessingSecurityScopedResource() {
                                                                defer { url.stopAccessingSecurityScopedResource() }
                                                                let fileContent = try String(contentsOf: url, encoding: .utf8)
                                                                let fileName = url.lastPathComponent
                                                                
                                                                // Send the file and whatever text is currently typed in the input box
                                                                viewModel.sendInjectedFile(fileName: fileName, fileContent: fileContent, messageText: viewModel.inputText)
                                                                viewModel.inputText = "" // Clear the input box after sending
                                                            }
                                                        } catch {
                                                            viewModel.messages.append("System Error: Failed to read dropped file.")
                                                        }
                                                    }
                                                }
                                                return true
                                            }
                                        }
                    
                    // SLEEK ACCORDIONS (Logs)
                    VStack(spacing: 8) {
                        DisclosureGroup(isExpanded: $viewModel.isTerminalOpen) {
                            ScrollView {
                                Text(viewModel.terminalLog)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundColor(.green)
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(12)
                            }
                            .frame(maxHeight: 180)
                            .background(Color.black.opacity(0.85))
                            .cornerRadius(10)
                            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.1), lineWidth: 1))
                        } label: {
                            Label("Live Terminal Execution", systemImage: "terminal")
                                .font(.subheadline.weight(.semibold))
                        }
                        .padding(.horizontal, 20)
                        
                        DisclosureGroup(isExpanded: $viewModel.showLogs) {
                            ScrollView {
                                Text(viewModel.logText)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundColor(.gray)
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(12)
                            }
                            .frame(maxHeight: 180)
                            .background(Color(NSColor.controlBackgroundColor))
                            .cornerRadius(10)
                            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.gray.opacity(0.2), lineWidth: 1))
                        } label: {
                            Label("Agent Internal Logs", systemImage: "cpu")
                                .font(.subheadline.weight(.semibold))
                        }
                        .padding(.horizontal, 20)
                    }
                    .padding(.bottom, 10)
                    
                    // MODERN PILL INPUT BAR
                    HStack(alignment: .bottom, spacing: 12) {
                        Button(action: { viewModel.showFileImporter = true }) {
                            Image(systemName: "paperclip.circle.fill")
                                .font(.system(size: 28))
                                .foregroundColor(.gray.opacity(0.8))
                        }
                        .buttonStyle(PlainButtonStyle())
                        .padding(.bottom, 2)
                        .fileImporter(isPresented: $viewModel.showFileImporter, allowedContentTypes: [.item]) { result in
                            if let url = try? result.get() {
                                viewModel.attachFile(from: url)
                            }
                        }
                        
                        TextField(viewModel.isProcessing ? "Skippy is thinking..." : "Message Skippy...", text: $viewModel.inputText, axis: .vertical)
                            .textFieldStyle(PlainTextFieldStyle())
                            .font(.system(size: 16))
                            .lineLimit(1...5)
                            .padding(.vertical, 6)
                            .disabled(viewModel.isProcessing || viewModel.terminalAuthRequest != nil || viewModel.deploymentAuthRequest != nil)
                            .onSubmit { viewModel.sendMessage() }
                        
                        Button(action: { viewModel.sendMessage() }) {
                            Image(systemName: "arrow.up.circle.fill")
                                .font(.system(size: 28))
                                .foregroundColor(viewModel.inputText.trimmingCharacters(in: .whitespaces).isEmpty ? .gray.opacity(0.5) : .blue)
                        }
                        .buttonStyle(PlainButtonStyle())
                        .padding(.bottom, 2)
                        .disabled(viewModel.inputText.trimmingCharacters(in: .whitespaces).isEmpty || viewModel.isProcessing || viewModel.terminalAuthRequest != nil || viewModel.deploymentAuthRequest != nil)
                        .keyboardShortcut(.defaultAction)
                    }
                    .padding(.horizontal, 15)
                    .padding(.vertical, 10)
                    .background(Color(NSColor.controlBackgroundColor))
                    .clipShape(Capsule())
                    .overlay(Capsule().stroke(Color.gray.opacity(0.2), lineWidth: 1))
                    .shadow(color: Color.black.opacity(0.05), radius: 5, x: 0, y: 2)
                    .padding(.horizontal, 20)
                    .padding(.bottom, 20)
                    .padding(.top, 10)
                }
                
                // --- CINEMATIC MODALS ---
                if let auth = viewModel.terminalAuthRequest {
                    ZStack {
                        Color.black.opacity(0.4).edgesIgnoringSafeArea(.all).blur(radius: 5)
                        
                        VStack(alignment: .leading, spacing: 16) {
                            HStack {
                                Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.red).font(.title2)
                                Text("God Mode Authorization").font(.title3).fontWeight(.bold)
                            }
                            
                            Text("Skippy is attempting to execute a root terminal command on the Mac Studio:")
                                .font(.subheadline)
                                .foregroundColor(.secondary)
                            
                            Text(auth.command)
                                .font(.system(.body, design: .monospaced))
                                .padding()
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(Color.black.opacity(0.8))
                                .foregroundColor(.green)
                                .cornerRadius(8)
                                .textSelection(.enabled)
                            
                            Text("Reason: \(auth.explanation)")
                                .font(.subheadline).italic().foregroundColor(.gray)
                            
                            HStack(spacing: 15) {
                                Button(action: { viewModel.sendAuthResponse(approve: false) }) {
                                    Text("Deny").frame(maxWidth: .infinity)
                                }.buttonStyle(.bordered).tint(.red).controlSize(.large)
                                
                                Button(action: { viewModel.sendAuthResponse(approve: true) }) {
                                    Text("Approve").frame(maxWidth: .infinity)
                                }.buttonStyle(.borderedProminent).tint(.green).controlSize(.large)
                            }.padding(.top, 5)
                        }
                        .padding(25)
                        .frame(width: 500)
                        .background(.regularMaterial)
                        .cornerRadius(20)
                        .shadow(color: Color.black.opacity(0.2), radius: 30, x: 0, y: 15)
                    }
                    .zIndex(2)
                }
                
                if let deploy = viewModel.deploymentAuthRequest {
                    ZStack {
                        Color.black.opacity(0.4).edgesIgnoringSafeArea(.all).blur(radius: 5)
                        
                        VStack(alignment: .leading, spacing: 16) {
                            HStack {
                                Image(systemName: "shippingbox.fill").foregroundColor(.blue).font(.title2)
                                Text("System Upgrade Ready").font(.title3).fontWeight(.bold)
                            }
                            
                            Text("Skippy successfully compiled and tested an upgrade for `\(deploy.targetFile)`.")
                                .font(.subheadline)
                                .foregroundColor(.secondary)
                            
                            Text(deploy.summary)
                                .font(.system(.body, design: .monospaced))
                                .padding()
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(Color.black.opacity(0.8))
                                .foregroundColor(.cyan)
                                .cornerRadius(8)
                                .textSelection(.enabled)
                            
                            Text("Proceeding will overwrite the production file and reboot the backend.")
                                .font(.subheadline).italic().foregroundColor(.gray)
                            
                            HStack(spacing: 15) {
                                Button(action: { viewModel.sendAuthResponse(approve: false) }) {
                                    Text("Abort").frame(maxWidth: .infinity)
                                }.buttonStyle(.bordered).tint(.red).controlSize(.large)
                                
                                Button(action: { viewModel.sendAuthResponse(approve: true) }) {
                                    Text("Deploy & Reboot").frame(maxWidth: .infinity)
                                }.buttonStyle(.borderedProminent).tint(.blue).controlSize(.large)
                            }.padding(.top, 5)
                        }
                        .padding(25)
                        .frame(width: 550)
                        .background(.regularMaterial)
                        .cornerRadius(20)
                        .shadow(color: Color.black.opacity(0.2), radius: 30, x: 0, y: 15)
                    }
                    .zIndex(2)
                }
            }
        }
        .alert("Restricted Access", isPresented: $showDevWarning) {
            Button("Cancel", role: .cancel) { pickerMode = viewModel.selectedMode }
            Button("Proceed", role: .destructive) { viewModel.selectedMode = "Developer" }
        } message: {
            Text("You are entering Developer Mode to modify Skippy's internal source code and CI/CD pipeline. Are you sure you want to do this?")
        }
        .frame(minWidth: 900, minHeight: 700)
    }
}
