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

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            RepoSectionHeader(title: "Repositories", systemImage: "arrow.triangle.branch") {
                factory.requestGitRepos()
                if !factory.gitSelectedRepo.isEmpty {
                    factory.requestGitDetail(factory.gitSelectedRepo)
                }
            }
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

    private enum DiffLane: String, CaseIterable, Identifiable {
        case working = "Working tree"
        case staged = "Staged"
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
                changesAndDiff(detail)
                composer(detail)
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
            if detail.ahead > 0 || detail.behind > 0 {
                Text("↑\(detail.ahead) ↓\(detail.behind)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if !detail.lastCommit.isEmpty {
                Text("\(detail.lastCommit.hash) \(detail.lastCommit.subject) · \(detail.lastCommit.when)")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            Spacer()
            branchControls(detail)
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
