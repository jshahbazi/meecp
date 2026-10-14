import Darwin
import Foundation
import Security

/// Validate the application at the native launch boundary, independently of
/// backend metadata. Custom and renamed Xcode installations remain supported.
public enum XcodeApplication {
    public static func verifiedURL(for path: String, excluding roots: [URL]) -> URL? {
        validatedURL(for: path, excluding: roots, signatureIsValid: appleSignature)
    }

    static func validatedURL(for path: String, excluding roots: [URL],
                             signatureIsValid: (URL) -> Bool) -> URL? {
        guard path.hasPrefix("/"), path.utf8.count <= 4096,
              !path.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              !path.split(separator: "/").contains("..") else { return nil }
        guard let app = canonicalURL(path), app.pathExtension.lowercased() == "app",
              !insideExcludedRoot(app, roots: roots), let before = identity(app), trustedAncestors(app),
              let bundle = Bundle(url: app), bundle.bundleIdentifier == "com.apple.dt.Xcode",
              bundle.object(forInfoDictionaryKey: "CFBundlePackageType") as? String == "APPL",
              signatureIsValid(app), identity(app) == before,
              canonicalURL(app.path) == app, !insideExcludedRoot(app, roots: roots), trustedAncestors(app) else { return nil }
        return app
    }

    static func canonicalURL(_ path: String) -> URL? {
        guard let resolved = realpath(path, nil) else { return nil }
        defer { free(resolved) }
        guard let value = String(validatingCString: resolved) else { return nil }
        return URL(fileURLWithPath: value, isDirectory: true)
    }

    private static func insideExcludedRoot(_ app: URL, roots: [URL]) -> Bool {
        let excluded = roots.map { canonicalURL($0.path) ?? $0.standardizedFileURL }
        let identities = excluded.compactMap { identity($0) }
        var current = app
        while true {
            if excluded.contains(where: { $0.path == current.path }) { return true }
            if let value = identity(current), identities.contains(where: {
                $0.device == value.device && $0.inode == value.inode
            }) { return true }
            if current.path == "/" { return false }
            current.deleteLastPathComponent()
        }
    }

    private struct Identity: Equatable {
        let device: dev_t
        let inode: ino_t
        let changeSeconds: time_t
        let changeNanoseconds: Int
    }

    private static func identity(_ url: URL) -> Identity? {
        var details = stat()
        guard lstat(url.path, &details) == 0, details.st_mode & S_IFMT == S_IFDIR else { return nil }
        return Identity(device: details.st_dev, inode: details.st_ino,
                        changeSeconds: details.st_ctimespec.tv_sec,
                        changeNanoseconds: details.st_ctimespec.tv_nsec)
    }

    private static func trustedAncestors(_ app: URL) -> Bool {
        let applications = identity(URL(fileURLWithPath: "/Applications", isDirectory: true))
        var current = app
        while true {
            var details = stat()
            guard lstat(current.path, &details) == 0, details.st_mode & S_IFMT == S_IFDIR,
                  details.st_uid == 0 || details.st_uid == getuid() else { return false }
            let systemApplications = applications?.device == details.st_dev && applications?.inode == details.st_ino && details.st_uid == 0 &&
                details.st_gid == 80 && details.st_mode & 0o7777 == 0o775
            guard details.st_mode & 0o022 == 0 || systemApplications else { return false }
            if current.path == "/" { return true }
            current.deleteLastPathComponent()
        }
    }

    private static func appleSignature(_ app: URL) -> Bool {
        var code: SecStaticCode?
        var requirement: SecRequirement?
        let defaults = SecCSFlags(rawValue: 0)
        // Match Xcode's Apple/App Store and Developer ID certificate forms.
        // Developer ID certificates must themselves belong to Apple's team.
        let xcodeRequirement = "identifier \"com.apple.dt.Xcode\" and (anchor apple or " +
            "(anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.9] exists) or " +
            "(anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and " +
            "certificate leaf[field.1.2.840.113635.100.6.1.13] exists and " +
            "certificate leaf[subject.OU] = \"59GAB85EFG\"))"
        guard SecStaticCodeCreateWithPath(app as CFURL, defaults, &code) == errSecSuccess,
              let code,
              SecRequirementCreateWithString(xcodeRequirement as CFString,
                                             defaults, &requirement) == errSecSuccess,
              let requirement else { return false }
        let flags = SecCSFlags(rawValue: kSecCSStrictValidate | kSecCSCheckAllArchitectures | kSecCSCheckNestedCode)
        guard SecStaticCodeCheckValidity(code, flags, requirement) == errSecSuccess else { return false }
        // Xcode's App Store signature has a shared Apple signing certificate.
        // Require Apple's authenticated CodeDirectory team as well, since the
        // App Store certificate also signs other developers' applications.
        var information: CFDictionary?
        guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation),
                                            &information) == errSecSuccess,
              let information = information as? [String: Any] else { return false }
        return information[kSecCodeInfoTeamIdentifier as String] as? String == "59GAB85EFG"
    }
}
