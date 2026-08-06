import XCTest
@testable import SkippyMac

final class SkippyMacTests: XCTestCase {
    func testModeWireValues() {
        XCTAssertEqual(AgentMode.re.wireMode, "RE")
        XCTAssertEqual(AgentMode.coding.wireMode, "Agent")
        XCTAssertEqual(AgentMode.chat.wireMode, "Chat")
    }

    func testCodeApprovalCarriesDiffAndFiles() {
        let approval = PendingApproval(
            taskId: "t1",
            kind: .code,
            explanation: "Skippy wants to change your files.",
            detail: "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
            diff: "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
            files: ["calc/ops.py"]
        )
        XCTAssertEqual(approval.kind, .code)
        XCTAssertTrue(approval.diff.contains("+new"))
        XCTAssertEqual(approval.files, ["calc/ops.py"])
    }

    func testDeviceApprovalDefaultsToDeviceKindWithNoDiff() {
        let approval = PendingApproval(
            taskId: nil,
            explanation: "Write 4 bytes to serial",
            detail: "{...}"
        )
        XCTAssertEqual(approval.kind, .device)
        XCTAssertTrue(approval.diff.isEmpty)
        XCTAssertTrue(approval.files.isEmpty)
        XCTAssertEqual(approval.sequence, 0)
    }

    func testDeviceApprovalCarriesItsSequenceNumber() {
        let approval = PendingApproval(
            taskId: nil,
            explanation: "Write 4 bytes to serial",
            detail: "{...}",
            sequence: 3
        )
        XCTAssertEqual(approval.sequence, 3)
    }

    func testRunCardStartsRunningAndMeasuresElapsed() {
        let card = RunCard(task: "rename a function", mode: .coding, startedAt: Date(timeIntervalSinceNow: -90))
        XCTAssertEqual(card.state, .running)
        XCTAssertFalse(card.state.isTerminal)
        XCTAssertEqual(card.elapsedText, "1m 30s")
        XCTAssertTrue(RunState.finished(status: "completed").isTerminal)
    }

    func testMemorySnapshotParsesTheHubPayload() {
        let snapshot = MemorySnapshot(payload: [
            "type": "memory",
            "project_id": "skippy",
            "conventions": ["test_command": "pytest -q"],
            "decisions": [[
                "id": "0001",
                "title": "Use wsproto",
                "recorded": "2026-08-04T10:00:00",
                "superseded": false,
                "stale_paths": ["gone.py"],
            ]],
            "sessions": [[
                "session_id": "20260804-100000-01",
                "recorded": "2026-08-04T10:00:00",
                "status": "done",
                "task": "wire the voice lane",
                "summary": "Voice lane is live.",
                "files_changed": ["skippy_voice.py"],
                "mode": "coding",
            ]],
        ])
        XCTAssertEqual(snapshot.projectId, "skippy")
        XCTAssertNil(snapshot.error)
        XCTAssertFalse(snapshot.isEmpty)
        XCTAssertEqual(snapshot.conventions["test_command"], "pytest -q")
        XCTAssertEqual(snapshot.decisions.first?.title, "Use wsproto")
        XCTAssertEqual(snapshot.decisions.first?.stalePaths, ["gone.py"])
        XCTAssertEqual(snapshot.sessions.first?.filesChanged, ["skippy_voice.py"])
    }

    func testMemorySnapshotSurvivesAnErrorPayload() {
        let snapshot = MemorySnapshot(payload: ["type": "memory", "error": "NAS is not mounted"])
        XCTAssertEqual(snapshot.error, "NAS is not mounted")
        XCTAssertTrue(snapshot.isEmpty)
        XCTAssertTrue(snapshot.decisions.isEmpty)
    }

    // MARK: - Repo panel

    func testGitRepoSummaryParsesTheHubPayload() {
        let summary = GitRepoSummary(from: [
            "name": "skippy",
            "branch": "main",
            "changes": 3,
            "ahead": 1,
            "behind": 0,
            "last_commit": ["hash": "abc1234", "subject": "wire the voice lane",
                            "when": "2 hours ago", "author": "Blake"],
        ])
        XCTAssertEqual(summary.name, "skippy")
        XCTAssertEqual(summary.branch, "main")
        XCTAssertEqual(summary.changes, 3)
        XCTAssertEqual(summary.ahead, 1)
        XCTAssertEqual(summary.lastCommit.hash, "abc1234")
        XCTAssertFalse(summary.lastCommit.isEmpty)
    }

    func testGitRepoDetailParsesChangesBranchesAndDiffs() {
        let detail = GitRepoDetail(payload: [
            "type": "git",
            "repo": "skippy",
            "branch": "feature/panel",
            "ahead": 0, "behind": 2,
            "changes": [
                ["status": "M", "path": "skippy_git.py"],
                ["status": "??", "path": "new_file.py"],
            ],
            "branches": ["main", "feature/panel"],
            "last_commit": ["hash": "abc1234", "subject": "initial",
                            "when": "now", "author": "Blake"],
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
            "staged_diff": "",
            "untracked": ["new_file.py"],
        ])
        XCTAssertEqual(detail.repo, "skippy")
        XCTAssertEqual(detail.branch, "feature/panel")
        XCTAssertEqual(detail.behind, 2)
        XCTAssertEqual(detail.changes.count, 2)
        XCTAssertEqual(detail.changes[0].badge, "M")
        XCTAssertEqual(detail.changes[1].badge, "new")
        XCTAssertEqual(detail.branches, ["main", "feature/panel"])
        XCTAssertTrue(detail.diff.contains("+new"))
        XCTAssertTrue(detail.stagedDiff.isEmpty)
        XCTAssertFalse(detail.isClean)
        XCTAssertNil(detail.error)
    }

    func testGitRepoDetailSurvivesAnErrorPayload() {
        let detail = GitRepoDetail(payload: ["type": "git", "error": "no roots configured"])
        XCTAssertEqual(detail.error, "no roots configured")
        XCTAssertTrue(detail.isClean)
        XCTAssertTrue(detail.branches.isEmpty)
    }

    // MARK: - RE dashboard

    func testHexDumpParsesLooseInput() {
        XCTAssertEqual(HexDump.parse("de ad be ef"), [0xde, 0xad, 0xbe, 0xef])
        XCTAssertEqual(HexDump.parse("0xDEADBEEF"), [0xde, 0xad, 0xbe, 0xef])
        XCTAssertNil(HexDump.parse("abc"))  // odd digit count
    }

    func testHexDumpAsciiAndLayout() {
        let bytes: [UInt8] = Array("AB\u{01}".utf8)
        XCTAssertEqual(HexDump.ascii(bytes), "AB.")
        let dump = HexDump.dump([0x41, 0x42])
        XCTAssertTrue(dump.hasPrefix("0000  41 42"))
        XCTAssertTrue(dump.contains("|AB|"))
    }

    func testHexDumpDiffOffsetsIncludeLengthChanges() {
        let changed = HexDump.diffOffsets([0x01, 0x02, 0x03], [0x01, 0x09, 0x03, 0x04])
        XCTAssertEqual(changed, [1, 3])
    }

    func testREDeviceParsesSerialAndSynthesizesNet() {
        let serial = REDevice(from: [
            "kind": "serial", "host": "macbook",
            "port": "/dev/cu.usbserial", "description": "FTDI",
        ])
        XCTAssertEqual(serial.kind, .serial)
        XCTAssertTrue(serial.isLocal)
        XCTAssertEqual(serial.label, "/dev/cu.usbserial")

        let net = REDevice(netAddress: "10.0.0.5", port: 8080)
        XCTAssertEqual(net.kind, .net)
        XCTAssertEqual(net.label, "10.0.0.5:8080")
    }

    func testBenchNodeParsesAFullStatus() {
        let node = BenchNode(from: [
            "client_id": "devices:bench", "host": "bench", "node": "bench",
            "firmware": "io-node 1.0", "battery": 100, "charging": false,
            "rssi": -74, "ip": "192.168.1.145", "uptime_s": 317, "actions": 1,
            "busy": false, "uart_open": false,
            "ports": ["uart", "i2c", "gpio", "adc"],
            "online": true, "seen_seconds_ago": 2.9,
        ])
        XCTAssertEqual(node.id, "devices:bench")
        XCTAssertEqual(node.host, "bench")
        XCTAssertEqual(node.label, "bench")
        XCTAssertEqual(node.detail, "io-node 1.0 · 192.168.1.145")
        XCTAssertTrue(node.online)
        XCTAssertFalse(node.busy)
        XCTAssertEqual(node.battery, 100)
        XCTAssertEqual(node.rssi, -74)
        XCTAssertEqual(node.ports, ["uart", "i2c", "gpio", "adc"])
        XCTAssertEqual(node.healthSummary, "100%  -74 dBm  up 5m")
    }

    func testBenchNodeSurvivesMissingFields() {
        // A hello with no node_status yet: the hub only knows the client id.
        let node = BenchNode(from: ["client_id": "devices:garage"])
        XCTAssertEqual(node.id, "devices:garage")
        XCTAssertEqual(node.host, "garage")  // derived from the client id
        XCTAssertEqual(node.label, "garage")
        XCTAssertNil(node.battery)
        XCTAssertNil(node.rssi)
        XCTAssertTrue(node.ports.isEmpty)
        XCTAssertFalse(node.online)
        XCTAssertEqual(node.healthSummary, "")
        XCTAssertEqual(node.lastSeenText, "last seen: unknown")
    }

    func testBenchNodeOfflineKeepsLastKnownHealth() {
        let node = BenchNode(from: [
            "client_id": "devices:bench", "host": "bench", "node": "bench",
            "battery": 4, "rssi": -89, "online": false,
            "seen_seconds_ago": 1225.4,
        ])
        XCTAssertFalse(node.online)
        XCTAssertEqual(node.battery, 4)
        XCTAssertEqual(node.lastSeenText, "last seen 20m ago")
        XCTAssertEqual(node.healthSummary, "4%  -89 dBm")
    }

    func testGitHubStatusParsesConnectedAndNotConnected() {
        let connected = GitHubStatus(payload: [
            "type": "github", "op": "status", "connected": true,
            "login": "blake", "name": "Blake W",
        ])
        XCTAssertTrue(connected.connected)
        XCTAssertEqual(connected.login, "blake")
        XCTAssertEqual(connected.headline, "Connected as blake")

        let bare = GitHubStatus(payload: ["type": "github", "op": "status", "connected": false])
        XCTAssertFalse(bare.connected)
        XCTAssertEqual(bare.headline, "Not connected")

        let rejected = GitHubStatus(payload: [
            "type": "github", "op": "set_token",
            "error": "GitHub rejected the token (401).",
        ])
        XCTAssertFalse(rejected.connected)
        XCTAssertEqual(rejected.headline, "GitHub rejected the token (401).")
    }

    func testGitHubRepoParsesTheClonePickerFields() {
        let repo = GitHubRepo(from: [
            "full_name": "blake/skippy", "name": "skippy", "private": true,
            "description": "shop jarvis", "updated": "2026-08-05T12:00:00Z",
        ])
        XCTAssertEqual(repo.id, "blake/skippy")
        XCTAssertEqual(repo.name, "skippy")
        XCTAssertTrue(repo.isPrivate)
        XCTAssertEqual(repo.detail, "shop jarvis")
    }

    func testFilesListingParsesEntriesAndSurvivesAnError() {
        let listing = FilesListing(payload: [
            "type": "files", "repo": "skippy", "path": "src",
            "entries": [
                ["name": "lib", "dir": true, "size": 0],
                ["name": "main.py", "dir": false, "size": 2048],
            ],
        ])
        XCTAssertEqual(listing.repo, "skippy")
        XCTAssertEqual(listing.path, "src")
        XCTAssertEqual(listing.entries.count, 2)
        XCTAssertTrue(listing.entries[0].isDirectory)
        XCTAssertEqual(listing.entries[1].sizeText, "2.0 KB")
        XCTAssertNil(listing.error)

        let refused = FilesListing(payload: ["type": "files", "error": "Sandbox violation: escape"])
        XCTAssertEqual(refused.error, "Sandbox violation: escape")
        XCTAssertTrue(refused.entries.isEmpty)
    }

    func testFileContentParsesTextTruncationAndBinaryRefusal() {
        let file = FileContent(payload: [
            "type": "file", "repo": "skippy", "path": "src/main.py",
            "text": "print('hi')\n", "truncated": false,
        ])
        XCTAssertEqual(file.text, "print('hi')\n")
        XCTAssertFalse(file.truncated)
        XCTAssertNil(file.error)

        let binary = FileContent(payload: [
            "type": "file", "error": "This looks like a binary file; the viewer only shows text.",
        ])
        XCTAssertNotNil(binary.error)
        XCTAssertTrue(binary.text.isEmpty)
    }

    func testREFindingParsesSupersededFlag() {
        let finding = REFinding(from: [
            "id": "0002", "kind": "constant", "title": "Seed",
            "confidence": "likely", "superseded": true, "text": "body",
        ])
        XCTAssertEqual(finding.id, "0002")
        XCTAssertTrue(finding.superseded)
        XCTAssertEqual(finding.kind, "constant")
    }
}
