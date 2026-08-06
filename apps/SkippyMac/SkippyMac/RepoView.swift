import SwiftUI

/// The repo panel: what state each workspace repository is in, what this
/// session has changed, and the two write actions — commit and branch — that
/// the hub performs on the human's explicit click. Agent-initiated commits go
/// through the approval card instead; this panel is the human's own hands.
struct RepoView: View {
    @ObservedObject var factory: FactoryClient

    var body: some View {
        HSplitView {
            RepoList(factory: factory)
                .frame(minWidth: 220, idealWidth: 260, maxWidth: 320)
            RepoDetailPane(factory: factory)
                .frame(minWidth: 480)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

// MARK: - Repo list

private struct RepoList: View {
    @ObservedObject var factory: FactoryClient
    @State private var showNewRepo = false
    @State private var showClone = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            RepoSectionHeader(
                title: "Repositories",
                systemImage: "arrow.triangle.branch",
                onRefresh: {
                    factory.requestGitRepos()
                    if !factory.gitSelectedRepo.isEmpty {
                        factory.requestGitDetail(factory.gitSelectedRepo)
                    }
                },
                trailing: {
                    HStack(spacing: 4) {
                        Button {
                            showNewRepo = true
                        } label: {
                            Image(systemName: "plus")
                        }
                        .buttonStyle(.borderless)
                        .help("New repo — local, and on GitHub when connected")
                        Button {
                            showClone = true
                        } label: {
                            Image(systemName: "square.and.arrow.down")
                        }
                        .buttonStyle(.borderless)
                        .help("Clone one of your GitHub repos")
                    }
                }
            )
            Divider()
            if factory.gitRepos.isEmpty {
                EmptyState(
                    icon: "arrow.triangle.branch",
                    title: "No repositories",
                    message: factory.gitNotice.isEmpty
                        ? "No workspace root is a git repository."
                        : factory.gitNotice
                )
            } else {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(factory.gitRepos) { repo in
                            RepoRow(repo: repo, selected: repo.name == factory.gitSelectedRepo) {
                                factory.requestGitDetail(repo.name)
                            }
                        }
                    }
                    .padding(12)
                }
            }
        }
        .background(Theme.railBackground)
        .sheet(isPresented: $showNewRepo) {
            NewRepoSheet(factory: factory)
        }
        .sheet(isPresented: $showClone) {
            CloneSheet(factory: factory)
        }
    }
}

// MARK: - New repo / clone sheets

private struct NewRepoSheet: View {
    @ObservedObject var factory: FactoryClient
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var isPrivate = true

    private var trimmed: String { name.trimmingCharacters(in: .whitespaces) }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New repository")
                .font(.headline)
            TextField("name", text: $name)
                .textFieldStyle(.roundedBorder)
                .onSubmit { create() }
            Toggle("Private on GitHub", isOn: $isPrivate)
            Text(factory.githubStatus?.connected == true
                 ? "Created under the workspace root, with a GitHub twin wired as origin."
                 : "No GitHub token is set, so this creates a local repo only — connect GitHub in Settings to get a remote.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Create") { create() }
                    .buttonStyle(.borderedProminent)
                    .disabled(trimmed.isEmpty || factory.gitSyncing)
            }
        }
        .padding(20)
        .frame(width: 380)
        .onAppear { factory.requestGitHubStatus() }
    }

    private func create() {
        guard !trimmed.isEmpty else { return }
        factory.gitNew(name: trimmed, isPrivate: isPrivate)
        dismiss()
    }
}

private struct CloneSheet: View {
    @ObservedObject var factory: FactoryClient
    @Environment(\.dismiss) private var dismiss
    @State private var filter = ""

    private var repos: [GitHubRepo] {
        let query = filter.trimmingCharacters(in: .whitespaces).lowercased()
        guard !query.isEmpty else { return factory.githubRepos }
        return factory.githubRepos.filter { $0.fullName.lowercased().contains(query) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Clone from GitHub")
                .font(.headline)
            if factory.githubStatus?.connected == false {
                Text(factory.githubStatus?.headline ?? "Not connected")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("Paste a GitHub token in Settings first.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if factory.githubRepos.isEmpty {
                ProgressView("Asking GitHub for your repositories…")
                    .frame(maxWidth: .infinity, minHeight: 120)
            } else {
                TextField("filter", text: $filter)
                    .textFieldStyle(.roundedBorder)
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(repos) { repo in
                            Button {
                                factory.gitClone(fullName: repo.fullName)
                                dismiss()
                            } label: {
                                HStack {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(repo.fullName)
                                            .font(.callout.weight(.medium))
                                        if !repo.detail.isEmpty {
                                            Text(repo.detail)
                                                .font(.caption)
                                                .foregroundStyle(.secondary)
                                                .lineLimit(1)
                                        }
                                    }
                                    Spacer()
                                    if repo.isPrivate {
                                        Image(systemName: "lock.fill")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .padding(.vertical, 4)
                            .padding(.horizontal, 8)
                        }
                    }
                }
                .frame(minHeight: 200, maxHeight: 320)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
            }
        }
        .padding(20)
        .frame(width: 440)
        .onAppear {
            factory.requestGitHubStatus()
            factory.requestGitHubRepos()
        }
    }
}

private struct RepoRow: View {
    let repo: GitRepoSummary
    let selected: Bool
    let onSelect: () -> Void

    var body: some View {
        Button(action: onSelect) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(repo.name)
                        .font(.callout.weight(.semibold))
                    Spacer()
                    if repo.changes > 0 {
                        StatusPill(text: "\(repo.changes)", color: Theme.accent(for: .coding))
                    }
                }
                HStack(spacing: 6) {
                    Image(systemName: "arrow.triangle.branch")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text(repo.branch.isEmpty ? "(unborn)" : repo.branch)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .card(accent: selected ? Theme.accent(for: .coding) : .clear)
    }
}

// MARK: - Detail

private struct RepoDetailPane: View {
    @ObservedObject var factory: FactoryClient
    /// Which diff to show: the working tree, or what a commit would include.
    @State private var lane: DiffLane = .working
    @State private var commitMessage = ""
    @State private var newBranchName = ""
    @State private var tab: DetailTab = .changes

    private enum DiffLane: String, CaseIterable, Identifiable {
        case working = "Working tree"
        case staged = "Staged"
        var id: String { rawValue }
    }

    private enum DetailTab: String, CaseIterable, Identifiable {
        case changes = "Changes"
        case files = "Files"
        var id: String { rawValue }
    }

    var body: some View {
        if let detail = factory.gitDetail {
            VStack(alignment: .leading, spacing: 14) {
                header(detail)
                if let error = detail.error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
                Picker("", selection: $tab) {
                    ForEach(DetailTab.allCases) { tab in
                        Text(tab.rawValue).tag(tab)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 200)
                if tab == .files {
                    FileBrowser(factory: factory, repo: detail.repo)
                } else {
                    changesAndDiff(detail)
                    composer(detail)
                }
                if !factory.gitNotice.isEmpty {
                    Text(factory.gitNotice)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            .padding(16)
        } else {
            EmptyState(
                icon: "arrow.triangle.branch",
                title: "No repository open",
                message: "Select a repository to see its branch, changes, and diff."
            )
        }
    }

    private func header(_ detail: GitRepoDetail) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(detail.repo)
                .font(.title3.weight(.semibold))
            StatusPill(
                text: detail.branch.isEmpty ? "(unborn)" : detail.branch,
                color: Theme.accent(for: .coding)
            )
            if !detail.lastCommit.isEmpty {
                Text("\(detail.lastCommit.hash) \(detail.lastCommit.subject) · \(detail.lastCommit.when)")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            Spacer()
            syncControls(detail)
            branchControls(detail)
        }
    }

    /// Pull and Push, wearing the ahead/behind counts. Pull is ff-only and
    /// push is current-branch-to-origin — the hub enforces both; these
    /// buttons just say what they will do.
    private func syncControls(_ detail: GitRepoDetail) -> some View {
        HStack(spacing: 8) {
            Button {
                factory.gitPull(repo: detail.repo)
            } label: {
                Label(detail.behind > 0 ? "Pull ↓\(detail.behind)" : "Pull",
                      systemImage: "arrow.down.circle")
            }
            .disabled(factory.gitSyncing)
            .help("git pull --ff-only from origin")

            Button {
                factory.gitPush(repo: detail.repo)
            } label: {
                Label(detail.ahead > 0 ? "Push ↑\(detail.ahead)" : "Push",
                      systemImage: "arrow.up.circle")
            }
            .disabled(factory.gitSyncing)
            .help("git push the current branch to origin")
        }
    }

    private func branchControls(_ detail: GitRepoDetail) -> some View {
        HStack(spacing: 8) {
            Menu {
                ForEach(detail.branches, id: \.self) { branch in
                    Button(branch) {
                        factory.gitBranch(repo: detail.repo, name: branch, create: false)
                    }
                    .disabled(branch == detail.branch)
                }
            } label: {
                Label("Switch", systemImage: "arrow.triangle.swap")
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            // The hub refuses a switch over uncommitted changes; disabling
            // here just says so before the round trip.
            .disabled(!detail.isClean || detail.branches.count < 2)

            TextField("new branch", text: $newBranchName)
                .textFieldStyle(.roundedBorder)
                .frame(width: 140)
            Button("Create") {
                let name = newBranchName.trimmingCharacters(in: .whitespaces)
                guard !name.isEmpty else { return }
                factory.gitBranch(repo: detail.repo, name: name, create: true)
                newBranchName = ""
            }
            .disabled(newBranchName.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    @ViewBuilder
    private func changesAndDiff(_ detail: GitRepoDetail) -> some View {
        if detail.isClean {
            EmptyState(
                icon: "checkmark.seal",
                title: "Working tree clean",
                message: "Nothing has changed since the last commit."
            )
        } else {
            VStack(alignment: .leading, spacing: 10) {
                RepoSectionHeader(title: "Changes", systemImage: "doc.badge.ellipsis", trailing: {
                    Picker("", selection: $lane) {
                        ForEach(DiffLane.allCases) { lane in
                            Text(lane.rawValue).tag(lane)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 200)
                })
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(detail.changes) { change in
                            HStack(spacing: 8) {
                                Text(change.badge)
                                    .font(.system(.caption2, design: .monospaced).weight(.semibold))
                                    .foregroundStyle(change.status == "??" ? Color.green : Color.orange)
                                    .frame(width: 30, alignment: .leading)
                                Text(change.path)
                                    .font(.system(.caption, design: .monospaced))
                                    .lineLimit(1)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 120)

                let diff = lane == .staged ? detail.stagedDiff : detail.diff
                if diff.isEmpty {
                    Text(lane == .staged
                         ? "Nothing is staged yet. Commit stages everything shown above."
                         : "No tracked-file changes. Untracked files do not appear in a diff.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .frame(maxWidth: .infinity, minHeight: 80)
                } else {
                    DiffView(diff: diff)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }

    private func composer(_ detail: GitRepoDetail) -> some View {
        HStack(spacing: 10) {
            TextField("Commit message — say why, not just what", text: $commitMessage)
                .textFieldStyle(.roundedBorder)
                .onSubmit { commit(detail) }
            Button {
                commit(detail)
            } label: {
                Label("Commit all", systemImage: "checkmark.circle")
            }
            .buttonStyle(.borderedProminent)
            .disabled(detail.isClean || commitMessage.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private func commit(_ detail: GitRepoDetail) {
        let message = commitMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty, !detail.isClean else { return }
        factory.gitCommit(repo: detail.repo, message: message)
        commitMessage = ""
    }
}

// MARK: - File explorer (read-only)

/// A lazy folder tree beside a read-only viewer. Each directory is fetched
/// when first expanded; tapping a file asks the hub for its text. No editing
/// here — Skippy edits, you review.
private struct FileBrowser: View {
    @ObservedObject var factory: FactoryClient
    let repo: String

    var body: some View {
        HSplitView {
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    FileTreeLevel(factory: factory, repo: repo, path: "", depth: 0)
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(minWidth: 200, idealWidth: 260, maxWidth: 360)
            .background(Theme.railBackground)
            viewer
                .frame(minWidth: 300, maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            if factory.fileListings[""] == nil {
                factory.requestFiles(repo: repo, path: "")
            }
        }
    }

    @ViewBuilder
    private var viewer: some View {
        if let file = factory.openFile {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(file.path)
                        .font(.system(.caption, design: .monospaced).weight(.semibold))
                        .lineLimit(1)
                        .truncationMode(.head)
                    Spacer()
                    if file.truncated {
                        StatusPill(text: "truncated", color: .orange)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.top, 8)
                Divider()
                if let error = file.error {
                    EmptyState(icon: "doc.questionmark", title: "Cannot show this file", message: error)
                } else {
                    ScrollView([.vertical, .horizontal]) {
                        Text(file.text)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .topLeading)
                            .padding(10)
                    }
                }
            }
        } else {
            EmptyState(
                icon: "doc.text.magnifyingglass",
                title: "No file open",
                message: "Pick a file on the left to read it. Viewing only — edits go through Skippy."
            )
        }
    }
}

/// One directory's entries. Rendered only once the hub has answered for this
/// path, so the tree grows exactly as fast as the user opens it.
private struct FileTreeLevel: View {
    @ObservedObject var factory: FactoryClient
    let repo: String
    let path: String
    let depth: Int

    var body: some View {
        if let entries = factory.fileListings[path] {
            if entries.isEmpty {
                Text("empty")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .padding(.leading, CGFloat(depth) * 14 + 22)
            } else {
                ForEach(entries) { entry in
                    FileTreeRow(
                        factory: factory, repo: repo, entry: entry,
                        path: path.isEmpty ? entry.name : "\(path)/\(entry.name)",
                        depth: depth
                    )
                }
            }
        } else {
            ProgressView()
                .controlSize(.small)
                .padding(.leading, CGFloat(depth) * 14 + 22)
        }
    }
}

private struct FileTreeRow: View {
    @ObservedObject var factory: FactoryClient
    let repo: String
    let entry: FileEntry
    /// The repo-relative path of this entry itself.
    let path: String
    let depth: Int
    @State private var expanded = false

    var body: some View {
        Button {
            if entry.isDirectory {
                expanded.toggle()
                if expanded && factory.fileListings[path] == nil {
                    factory.requestFiles(repo: repo, path: path)
                }
            } else {
                factory.requestFile(repo: repo, path: path)
            }
        } label: {
            HStack(spacing: 6) {
                if entry.isDirectory {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: 10)
                } else {
                    Spacer().frame(width: 10)
                }
                Image(systemName: entry.isDirectory ? "folder" : "doc.text")
                    .font(.caption)
                    .foregroundStyle(entry.isDirectory ? Color.accentColor : .secondary)
                Text(entry.name)
                    .font(.system(.caption, design: .monospaced))
                    .lineLimit(1)
                Spacer()
                if !entry.sizeText.isEmpty {
                    Text(entry.sizeText)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.leading, CGFloat(depth) * 14)
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(
            factory.openFile?.path == path && !entry.isDirectory
                ? Color.accentColor.opacity(0.15) : Color.clear
        )

        if entry.isDirectory && expanded {
            FileTreeLevel(factory: factory, repo: repo, path: path, depth: depth + 1)
        }
    }
}

// MARK: - Shared header

private struct RepoSectionHeader<Trailing: View>: View {
    let title: String
    let systemImage: String
    var onRefresh: (() -> Void)?
    @ViewBuilder var trailing: () -> Trailing

    init(
        title: String,
        systemImage: String,
        onRefresh: (() -> Void)? = nil,
        @ViewBuilder trailing: @escaping () -> Trailing = { EmptyView() }
    ) {
        self.title = title
        self.systemImage = systemImage
        self.onRefresh = onRefresh
        self.trailing = trailing
    }

    var body: some View {
        HStack(spacing: 8) {
            Label(title, systemImage: systemImage)
                .font(.headline)
            Spacer()
            trailing()
            if let onRefresh {
                Button(action: onRefresh) {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Refresh")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}
