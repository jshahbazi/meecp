import AppKit
import Darwin
import Foundation

public enum AppInstallError: String, Error {
    case invalidBundle = "invalid_install_bundle"
    case invalidDestination = "invalid_install_destination"
    case busy = "installation_already_running"
    case signature = "install_signature_invalid"
    case incompatible = "install_bundle_incompatible"
    case copy = "install_copy_failed"
    case stop = "existing_app_did_not_quit"
    case destinationChanged = "install_destination_changed"
    case replace = "install_replace_failed"
    case launch = "installed_app_launch_failed"
    case recovery = "install_recovery_required"
}

// File operations and lifecycle callbacks are separate so replacement failures
// can be exercised without quitting the user's installation or opening its data.
public struct InstallHooks {
    public let verify: (URL) throws -> Void
    public let stop: () throws -> Void
    public let launch: (URL) throws -> Void

    public init(verify: @escaping (URL) throws -> Void, stop: @escaping () throws -> Void,
                launch: @escaping (URL) throws -> Void) {
        self.verify = verify; self.stop = stop; self.launch = launch
    }
}

public enum AppInstaller {
    public static let bundleIdentifier = "com.jshahbazi.mcpme"

    public static func isOnReadOnlyVolume(_ app: URL) -> Bool {
        (try? app.resourceValues(forKeys: [.volumeIsReadOnlyKey]).volumeIsReadOnly) == true
    }

    public static func destination(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        let system = URL(fileURLWithPath: "/Applications", isDirectory: true)
        let personal = home.appendingPathComponent("Applications", isDirectory: true)
        return selectDestination(system: system, personal: personal, running: runningCopies().compactMap { $0.bundleURL })
    }

    static func selectDestination(system: URL, personal: URL, running: [URL]) -> URL {
        let known = [system.appendingPathComponent("meecp.app", isDirectory: true),
                     personal.appendingPathComponent("meecp.app", isDirectory: true)]
        let current = Set(running.map { $0.standardizedFileURL }).intersection(Set(known.map { $0.standardizedFileURL }))
        if current.count == 1, let destination = current.first { return destination }
        if FileManager.default.fileExists(atPath: system.appendingPathComponent("meecp.app").path) {
            return system.appendingPathComponent("meecp.app", isDirectory: true)
        }
        if FileManager.default.fileExists(atPath: personal.appendingPathComponent("meecp.app").path) ||
            !FileManager.default.isWritableFile(atPath: system.path) {
            return personal.appendingPathComponent("meecp.app", isDirectory: true)
        }
        return system.appendingPathComponent("meecp.app", isDirectory: true)
    }

    private struct Identity: Equatable {
        let device: dev_t
        let inode: ino_t
    }

    private static func identity(_ url: URL) throws -> Identity? {
        var value = stat()
        if lstat(url.path, &value) != 0 {
            if errno == ENOENT { return nil }
            throw AppInstallError.invalidDestination
        }
        guard value.st_mode & S_IFMT == S_IFDIR else { throw AppInstallError.invalidDestination }
        return Identity(device: value.st_dev, inode: value.st_ino)
    }

    public static func validateIdentity(_ app: URL) throws {
        guard try identity(app) != nil,
              let data = try? Data(contentsOf: app.appendingPathComponent("Contents/Info.plist")),
              let info = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              info["CFBundleIdentifier"] as? String == bundleIdentifier,
              info["CFBundleExecutable"] as? String == "meecp",
              info["CFBundlePackageType"] as? String == "APPL" else { throw AppInstallError.invalidBundle }
    }

    private static func tool(_ executable: String, _ arguments: [String], failure: AppInstallError) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { throw failure }
        process.waitUntilExit()
        guard process.terminationReason == .exit, process.terminationStatus == 0 else { throw failure }
    }

    public static func verifySignature(_ app: URL) throws {
        try validateIdentity(app)
        try tool("/usr/bin/codesign", ["--verify", "--strict", "--deep", app.path], failure: .signature)
    }

    public static func verifyCandidate(_ app: URL) throws {
        try verifySignature(app)
        guard let data = try? Data(contentsOf: app.appendingPathComponent("Contents/Info.plist")),
              let info = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else {
            throw AppInstallError.incompatible
        }
        try validateCompatibility(info)
        try tool("/usr/bin/lipo", [app.appendingPathComponent("Contents/MacOS/meecp").path, "-verify_arch", "arm64"],
                 failure: .incompatible)
    }

    static func validateCompatibility(_ info: [String: Any]) throws {
        let pattern = "^[0-9]+(?:\\.[0-9]+){0,2}$"
        for key in ["CFBundleShortVersionString", "CFBundleVersion", "LSMinimumSystemVersion"] {
            guard let value = info[key] as? String, value.range(of: pattern, options: .regularExpression) != nil else {
                throw AppInstallError.incompatible
            }
        }
        let components = (info["LSMinimumSystemVersion"] as! String).split(separator: ".")
        let values = components.compactMap { Int($0) }
        guard values.count == components.count else { throw AppInstallError.incompatible }
        let minimum = OperatingSystemVersion(majorVersion: values[0], minorVersion: values.count > 1 ? values[1] : 0,
                                             patchVersion: values.count > 2 ? values[2] : 0)
        guard ProcessInfo.processInfo.isOperatingSystemAtLeast(minimum) else { throw AppInstallError.incompatible }
    }

    // Run off the main thread. Stage and verify before asking the existing app
    // to quit. RENAME_SWAP keeps an installed bundle present even if we crash.
    // No operation here reads or writes Application Support.
    public static func install(from source: URL, to destination: URL, hooks: InstallHooks) throws {
        let files = FileManager.default
        try validateIdentity(source); try hooks.verify(source)
        guard destination.lastPathComponent == "meecp.app",
              source.resolvingSymlinksInPath() != destination.resolvingSymlinksInPath() else {
            throw AppInstallError.invalidDestination
        }
        let parent = destination.deletingLastPathComponent()
        do { try files.createDirectory(at: parent, withIntermediateDirectories: true) }
        catch { throw AppInstallError.invalidDestination }
        guard try identity(parent) != nil, files.isWritableFile(atPath: parent.path) else {
            throw AppInstallError.invalidDestination
        }
        // Keep the lock inode: unlinking it would let another installer lock a
        // different inode while this transaction is still running.
        let lock = open(parent.appendingPathComponent(".meecp-install.lock").path,
                        O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard lock >= 0 else { throw AppInstallError.busy }
        defer { close(lock) }
        var lockInfo = stat()
        guard fstat(lock, &lockInfo) == 0, lockInfo.st_mode & S_IFMT == S_IFREG,
              lockInfo.st_uid == getuid(), lockInfo.st_nlink == 1,
              flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw AppInstallError.busy }
        defer { flock(lock, LOCK_UN) }
        let previous = try identity(destination)
        if previous != nil { try validateIdentity(destination) }
        let workspace = parent.appendingPathComponent(".meecp-install-" + UUID().uuidString.lowercased(), isDirectory: true)
        do { try files.createDirectory(at: workspace, withIntermediateDirectories: false,
                                       attributes: [.posixPermissions: 0o700]) }
        catch { throw AppInstallError.copy }
        let staged = workspace.appendingPathComponent("meecp.app", isDirectory: true)
        var retainRecovery = false
        defer { if !retainRecovery { try? files.removeItem(at: workspace) } }
        let recovery = "# meecp installation recovery\n\nIf installation was interrupted, the meecp.app beside this folder is the newly installed copy. After replacement, meecp.app in this folder is the previous copy. Quit meecp before restoring that previous copy. Application Support was not changed by the installer.\n"
        do { try recovery.write(to: workspace.appendingPathComponent("RECOVERY.md"), atomically: true, encoding: .utf8) }
        catch { throw AppInstallError.copy }
        try tool("/usr/bin/ditto", [source.path, staged.path], failure: .copy)
        try validateIdentity(staged); try hooks.verify(staged)
        let candidate = try identity(staged)
        try hooks.stop()
        guard try identity(destination) == previous else { throw AppInstallError.destinationChanged }
        let flag = previous == nil ? UInt32(RENAME_EXCL) : UInt32(RENAME_SWAP)
        guard renamex_np(staged.path, destination.path, flag) == 0 else { throw AppInstallError.replace }
        // A crash after this point leaves the old bundle and recovery note in
        // the private sibling folder. A confirmed launch failure rolls back.
        retainRecovery = previous != nil
        do { try hooks.launch(destination) }
        catch {
            if error as? AppInstallError == .recovery { throw AppInstallError.recovery }
            guard (try? identity(destination)) == candidate else { throw AppInstallError.recovery }
            if previous != nil {
                guard renamex_np(staged.path, destination.path, UInt32(RENAME_SWAP)) == 0 else {
                    throw AppInstallError.recovery
                }
                retainRecovery = false
                // Relaunch the restored app; preserve it even if opening fails.
                try? hooks.launch(destination)
            } else {
                guard renamex_np(destination.path, staged.path, UInt32(RENAME_EXCL)) == 0 else {
                    retainRecovery = true; throw AppInstallError.recovery
                }
            }
            throw AppInstallError.launch
        }
        retainRecovery = false
    }

    public static func runningCopies(excluding pid: pid_t = getpid()) -> [NSRunningApplication] {
        NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
            .filter { !$0.isTerminated && $0.processIdentifier != pid }
    }

    public static func stopRunningCopies(timeout: TimeInterval = 75) throws {
        try stopCopies(runningCopies(), timeout: timeout)
        guard runningCopies().isEmpty else { throw AppInstallError.stop }
    }

    static func stopCopies(_ running: [NSRunningApplication], timeout: TimeInterval) throws {
        // Validate every target before requesting termination of any of them.
        for app in running {
            guard app.bundleIdentifier == bundleIdentifier, app.processIdentifier != getpid(),
                  let bundle = app.bundleURL, let executable = app.executableURL,
                  let name = Bundle(url: bundle)?.object(forInfoDictionaryKey: "CFBundleExecutable") as? String,
                  ["meecp", "mcpme"].contains(name),
                  executable.resolvingSymlinksInPath() == bundle.appendingPathComponent("Contents/MacOS/\(name)").resolvingSymlinksInPath()
            else { throw AppInstallError.stop }
            try verifySignature(bundle)
        }
        for app in running where !app.isTerminated {
            guard app.terminate() else { throw AppInstallError.stop }
        }
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        while running.contains(where: { !$0.isTerminated }) && ProcessInfo.processInfo.systemUptime < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        // Never force-kill a discovered PID. Normal meecp quit owns host cleanup.
        guard running.allSatisfy({ $0.isTerminated }) else { throw AppInstallError.stop }
    }

    public static func launch(_ app: URL) throws {
        let completed = DispatchSemaphore(value: 0)
        let result = LaunchResult()
        DispatchQueue.main.async {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.createsNewApplicationInstance = true
            NSWorkspace.shared.openApplication(at: app, configuration: configuration) { running, error in
                if let running = running {
                    result.error = error == nil && !running.isTerminated &&
                        running.bundleURL?.resolvingSymlinksInPath() == app.resolvingSymlinksInPath() ? nil : .recovery
                } else { result.error = error != nil ? .launch : .recovery }
                completed.signal()
            }
        }
        // A timeout has an unknown outcome; do not swap underneath a launch
        // that macOS may still complete. Retain the recovery copy instead.
        guard completed.wait(timeout: .now() + 30) == .success else { throw AppInstallError.recovery }
        if let error = result.error { throw error }
    }

    private final class LaunchResult: @unchecked Sendable { var error: AppInstallError? = .recovery }
}
