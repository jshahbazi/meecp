import AppKit
import Foundation
import XCTest
@testable import AppInstallation

// Opt in on a logged-in macOS session. These launch only signed synthetic apps
// under a temporary directory and pass their exact NSRunningApplication objects
// to shutdown; the user's installed meecp and its state are never touched.
final class NativeInstallLifecycleTests: XCTestCase {
    private var root: URL!
    private var launched: [NSRunningApplication] = []

    override func setUpWithError() throws {
        guard ProcessInfo.processInfo.environment["MCPME_RUN_INSTALL_LIFECYCLE_TESTS"] == "1" else {
            throw XCTSkip("Set MCPME_RUN_INSTALL_LIFECYCLE_TESTS=1 in a logged-in macOS session")
        }
        root = FileManager.default.temporaryDirectory.appendingPathComponent("meecp-native-install-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
    }

    override func tearDownWithError() throws {
        guard root != nil else { return }
        let matching = AppInstaller.runningCopies().filter { $0.bundleURL?.path.hasPrefix(root.path + "/") == true }
        for app in launched + matching where !app.isTerminated { _ = app.forceTerminate() }
        let deadline = Date().addingTimeInterval(5)
        while (launched + matching).contains(where: { !$0.isTerminated }) && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        try FileManager.default.removeItem(at: root)
    }

    private func tool(_ path: String, _ arguments: [String]) throws {
        let process = Process(); process.executableURL = URL(fileURLWithPath: path); process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try process.run(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw AppInstallError.copy }
    }

    private func fixture(_ app: URL, label: String, refusesQuit: Bool = false) throws {
        let contents = app.appendingPathComponent("Contents")
        let macos = contents.appendingPathComponent("MacOS")
        try FileManager.default.createDirectory(at: macos, withIntermediateDirectories: true)
        let swift = root.appendingPathComponent("fixture.swift")
        let code = """
        import AppKit
        import Foundation
        final class Fixture: NSObject, NSApplicationDelegate {
            func marker(_ name: String) {
                let path = Bundle.main.object(forInfoDictionaryKey: "TestMarker") as! String
                try! Data(name.utf8).write(to: URL(fileURLWithPath: path + "." + name))
            }
            func applicationDidFinishLaunching(_ notification: Notification) { marker("ready") }
            func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
                if Bundle.main.object(forInfoDictionaryKey: "RefusesQuit") as? Bool == true { return .terminateCancel }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.marker("stopped"); sender.reply(toApplicationShouldTerminate: true) }
                return .terminateLater
            }
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = Fixture(); app.delegate = delegate; app.run()
        """
        try code.write(to: swift, atomically: true, encoding: .utf8)
        try tool("/usr/bin/swiftc", [swift.path, "-o", macos.appendingPathComponent("meecp").path])
        let info: [String: Any] = ["CFBundleIdentifier": AppInstaller.bundleIdentifier, "CFBundleExecutable": "meecp",
                                   "CFBundlePackageType": "APPL", "CFBundleName": "meecp install test", "LSUIElement": true,
                                   "CFBundleVersion": "1", "CFBundleShortVersionString": "0.1.0",
                                   "LSMinimumSystemVersion": "26.0",
                                   "TestMarker": root.appendingPathComponent(label).path, "RefusesQuit": refusesQuit]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        try tool("/usr/bin/codesign", ["--force", "--sign", "-", app.path])
        try AppInstaller.verifyCandidate(app)
    }

    private func launch(_ app: URL) throws -> NSRunningApplication {
        let done = expectation(description: "Launch synthetic app")
        var result: NSRunningApplication?
        let config = NSWorkspace.OpenConfiguration(); config.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: app, configuration: config) { running, error in
            XCTAssertNil(error); result = running; done.fulfill()
        }
        wait(for: [done], timeout: 10)
        let running = try XCTUnwrap(result); launched.append(running)
        return running
    }

    private func waitForMarker(_ label: String, _ marker: String) {
        let path = root.appendingPathComponent(label + "." + marker).path
        let deadline = Date().addingTimeInterval(5)
        while !FileManager.default.fileExists(atPath: path) && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: path))
    }

    func testReadOnlyImageReplacementQuitsAndLaunchesNewCopy() throws {
        let destination = root.appendingPathComponent("test Applications/meecp.app")
        try fixture(destination, label: "old")
        let old = try launch(destination); waitForMarker("old", "ready")
        let contents = root.appendingPathComponent("image contents")
        try fixture(contents.appendingPathComponent("meecp.app"), label: "new")
        let image = root.appendingPathComponent("test.dmg")
        let mounted = root.appendingPathComponent("mounted")
        try FileManager.default.createDirectory(at: mounted, withIntermediateDirectories: false)
        try tool("/usr/bin/hdiutil", ["create", "-srcfolder", contents.path, "-format", "UDZO", image.path])
        try tool("/usr/bin/hdiutil", ["attach", image.path, "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mounted.path])
        defer { try? tool("/usr/bin/hdiutil", ["detach", mounted.path]) }
        let source = mounted.appendingPathComponent("meecp.app")
        XCTAssertTrue(AppInstaller.isOnReadOnlyVolume(source))
        XCTAssertFalse(AppInstaller.isOnReadOnlyVolume(destination))
        let done = expectation(description: "Install and restart")
        DispatchQueue.global().async {
            do {
                try AppInstaller.install(from: source, to: destination,
                    hooks: InstallHooks(verify: AppInstaller.verifyCandidate, stop: {
                        try AppInstaller.stopCopies([old], timeout: 5)
                        XCTAssertTrue(old.isTerminated)
                    }, launch: AppInstaller.launch))
            } catch { XCTFail("Synthetic installation failed: \(error)") }
            done.fulfill()
        }
        wait(for: [done], timeout: 30)
        waitForMarker("old", "stopped"); waitForMarker("new", "ready")
        try AppInstaller.verifySignature(destination)
    }

    func testRefusedQuitTimesOutWithoutForceTerminationOrReplacement() throws {
        let destination = root.appendingPathComponent("test Applications/meecp.app")
        let source = root.appendingPathComponent("source/meecp.app")
        try fixture(destination, label: "old", refusesQuit: true)
        try fixture(source, label: "new")
        let old = try launch(destination); waitForMarker("old", "ready")
        let done = expectation(description: "Refused quit")
        DispatchQueue.global().async {
            do {
                try AppInstaller.install(from: source, to: destination,
                    hooks: InstallHooks(verify: AppInstaller.verifyCandidate,
                        stop: { try AppInstaller.stopCopies([old], timeout: 0.3) }, launch: { _ in XCTFail("must not launch") }))
                XCTFail("Quit refusal must stop installation")
            } catch { XCTAssertEqual(error as? AppInstallError, .stop) }
            done.fulfill()
        }
        wait(for: [done], timeout: 10)
        XCTAssertFalse(old.isTerminated)
        let info = try PropertyListSerialization.propertyList(from: Data(contentsOf: destination.appendingPathComponent("Contents/Info.plist")), format: nil) as! [String: Any]
        XCTAssertEqual(info["RefusesQuit"] as? Bool, true)
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("new.ready").path))
    }

    func testTamperedNativeBundleFailsBeforeShutdown() throws {
        let source = root.appendingPathComponent("source/meecp.app")
        let destination = root.appendingPathComponent("test Applications/meecp.app")
        try fixture(source, label: "new")
        let info = source.appendingPathComponent("Contents/Info.plist")
        var data = try Data(contentsOf: info); data.append(Data("<!-- altered after signing -->".utf8)); try data.write(to: info)
        XCTAssertThrowsError(try AppInstaller.install(from: source, to: destination,
            hooks: InstallHooks(verify: AppInstaller.verifyCandidate,
                                stop: { XCTFail("must not stop") }, launch: { _ in XCTFail("must not launch") }))) {
            XCTAssertEqual($0 as? AppInstallError, .signature)
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: destination.path))
    }
}
