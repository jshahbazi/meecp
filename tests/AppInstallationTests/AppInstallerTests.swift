import XCTest
import Foundation
import Darwin
@testable import AppInstallation

final class AppInstallerTests: XCTestCase {
    private var root: URL!
    private var source: URL!
    private var destination: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("mcpme-install-tests-" + UUID().uuidString, isDirectory: true)
        source = root.appendingPathComponent("mounted/mcpme.app", isDirectory: true)
        destination = root.appendingPathComponent("test Applications/mcpme.app", isDirectory: true)
        try fixture(source, version: "new")
    }

    override func tearDownWithError() throws { try FileManager.default.removeItem(at: root) }

    private func fixture(_ app: URL, version: String) throws {
        let contents = app.appendingPathComponent("Contents", isDirectory: true)
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let info = ["CFBundleIdentifier": AppInstaller.bundleIdentifier, "CFBundleExecutable": "mcpme", "CFBundlePackageType": "APPL"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        try version.write(to: contents.appendingPathComponent("version"), atomically: true, encoding: .utf8)
    }

    private func version(_ app: URL) throws -> String {
        try String(contentsOf: app.appendingPathComponent("Contents/version"), encoding: .utf8)
    }

    private func workspaces() throws -> [URL] {
        try FileManager.default.contentsOfDirectory(at: destination.deletingLastPathComponent(), includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix(".mcpme-install-") }
    }

    func testReplacementVerifiesBeforeStoppingAndPreservesExternalData() throws {
        try fixture(destination, version: "old")
        let state = root.appendingPathComponent("Application Support/settings")
        try FileManager.default.createDirectory(at: state.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("synthetic-external-state".utf8).write(to: state)
        var events: [String] = []
        let hooks = InstallHooks(verify: { app in
            XCTAssertEqual(try self.version(app), "new"); events.append("verify")
        }, stop: {
            XCTAssertEqual(events, ["verify", "verify"])
            XCTAssertEqual(try self.version(self.destination), "old"); events.append("stop")
        }, launch: { app in
            XCTAssertEqual(app, self.destination); XCTAssertEqual(try self.version(app), "new")
            let recovery = try XCTUnwrap(self.workspaces().first)
            XCTAssertEqual(try self.version(recovery.appendingPathComponent("mcpme.app")), "old")
            events.append("launch")
        })
        try AppInstaller.install(from: source, to: destination, hooks: hooks)
        XCTAssertEqual(events, ["verify", "verify", "stop", "launch"])
        XCTAssertEqual(try version(destination), "new")
        XCTAssertEqual(try String(contentsOf: state, encoding: .utf8), "synthetic-external-state")
        XCTAssertTrue(try workspaces().isEmpty)
    }

    func testFirstInstallAndPathWithSpaces() throws {
        var stopped = false
        try AppInstaller.install(from: source, to: destination, hooks: InstallHooks(verify: { _ in }, stop: { stopped = true },
            launch: { app in XCTAssertTrue(stopped); XCTAssertEqual(try self.version(app), "new") }))
        XCTAssertEqual(try version(destination), "new")
        XCTAssertTrue(try workspaces().isEmpty)
    }

    func testStageSignatureFailureDoesNotStopOrChangeOldApp() throws {
        try fixture(destination, version: "old")
        var verified = 0
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in verified += 1; if verified == 2 { throw AppInstallError.signature } },
                                stop: { XCTFail("must not stop") }, launch: { _ in XCTFail("must not launch") })))
        XCTAssertEqual(try version(destination), "old")
        XCTAssertTrue(try workspaces().isEmpty)
    }

    func testStopFailureDoesNotReplace() throws {
        try fixture(destination, version: "old")
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in }, stop: { throw AppInstallError.stop }, launch: { _ in XCTFail("must not launch") }))) {
            XCTAssertEqual($0 as? AppInstallError, .stop)
        }
        XCTAssertEqual(try version(destination), "old")
        XCTAssertTrue(try workspaces().isEmpty)
    }

    func testConfirmedLaunchFailureRestoresAndRelaunchesOldApp() throws {
        try fixture(destination, version: "old")
        var launches: [String] = []
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in }, stop: {}, launch: { app in
                let value = try self.version(app); launches.append(value)
                if value == "new" { throw AppInstallError.launch }
            }))) { XCTAssertEqual($0 as? AppInstallError, .launch) }
        XCTAssertEqual(launches, ["new", "old"])
        XCTAssertEqual(try version(destination), "old")
        XCTAssertTrue(try workspaces().isEmpty)
    }

    func testFirstInstallLaunchFailureRemovesCandidate() throws {
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in }, stop: {}, launch: { _ in throw AppInstallError.launch }))) {
            XCTAssertEqual($0 as? AppInstallError, .launch)
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: destination.path))
        XCTAssertTrue(try workspaces().isEmpty)
    }

    func testUnknownLaunchOutcomeKeepsInstalledAndRecoveryCopies() throws {
        try fixture(destination, version: "old")
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in }, stop: {}, launch: { _ in throw AppInstallError.recovery }))) {
            XCTAssertEqual($0 as? AppInstallError, .recovery)
        }
        XCTAssertEqual(try version(destination), "new")
        let workspace = try XCTUnwrap(workspaces().first)
        XCTAssertEqual(try version(workspace.appendingPathComponent("mcpme.app")), "old")
        XCTAssertTrue(FileManager.default.fileExists(atPath: workspace.appendingPathComponent("RECOVERY.md").path))
    }

    func testChangedDestinationIsNotOverwritten() throws {
        try fixture(destination, version: "old")
        let moved = root.appendingPathComponent("previous.app")
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in }, stop: {
                try FileManager.default.moveItem(at: self.destination, to: moved)
                try self.fixture(self.destination, version: "foreign")
            }, launch: { _ in XCTFail("must not launch") }))) {
            XCTAssertEqual($0 as? AppInstallError, .destinationChanged)
        }
        XCTAssertEqual(try version(destination), "foreign")
        XCTAssertEqual(try version(moved), "old")
    }

    func testSymlinkDestinationIsRejectedBeforeStop() throws {
        try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: destination, withDestinationURL: source)
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in }, stop: { XCTFail("must not stop") }, launch: { _ in XCTFail("must not launch") })))
        XCTAssertEqual(try version(source), "new")
    }

    func testUnrelatedBundleIsRejectedBeforeStop() throws {
        try fixture(destination, version: "foreign")
        let info: [String: Any] = ["CFBundleIdentifier": "org.example.other", "CFBundleExecutable": "mcpme", "CFBundlePackageType": "APPL"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: destination.appendingPathComponent("Contents/Info.plist"))
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { _ in }, stop: { XCTFail("must not stop") }, launch: { _ in XCTFail("must not launch") })))
        XCTAssertEqual(try version(destination), "foreign")
    }

    func testConcurrentInstallIsRejectedBeforeStopping() throws {
        try fixture(destination, version: "old")
        var secondWasBlocked = false
        try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: { app in
                if app != self.source {
                    XCTAssertThrowsError(try AppInstaller.install(from: self.source, to: self.destination,
                        hooks: InstallHooks(verify: { _ in }, stop: { XCTFail("must not stop") }, launch: { _ in }))) {
                        XCTAssertEqual($0 as? AppInstallError, .busy); secondWasBlocked = true
                    }
                }
            }, stop: {}, launch: { _ in }))
        XCTAssertTrue(secondWasBlocked)
    }

    func testDestinationPrefersTheRunningPersonalInstallation() throws {
        let system = root.appendingPathComponent("system Applications")
        let personal = root.appendingPathComponent("personal Applications")
        let systemApp = system.appendingPathComponent("mcpme.app", isDirectory: true)
        let personalApp = personal.appendingPathComponent("mcpme.app", isDirectory: true)
        try fixture(systemApp, version: "system"); try fixture(personalApp, version: "personal")
        XCTAssertEqual(AppInstaller.selectDestination(system: system, personal: personal, running: [personalApp]), personalApp)
        XCTAssertEqual(AppInstaller.selectDestination(system: system, personal: personal, running: []), systemApp)
        XCTAssertEqual(AppInstaller.selectDestination(system: system, personal: personal, running: [systemApp]), systemApp)
    }

    func testCandidateMetadataRejectsUnsupportedSystemAndMalformedVersions() throws {
        let valid: [String: Any] = ["CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "1", "LSMinimumSystemVersion": "26.0"]
        try AppInstaller.validateCompatibility(valid)
        var unsupported = valid; unsupported["LSMinimumSystemVersion"] = "9999.0"
        XCTAssertThrowsError(try AppInstaller.validateCompatibility(unsupported))
        for key in valid.keys {
            var malformed = valid; malformed[key] = "not-a-version"
            XCTAssertThrowsError(try AppInstaller.validateCompatibility(malformed))
            malformed[key] = true
            XCTAssertThrowsError(try AppInstaller.validateCompatibility(malformed))
        }
    }
}
