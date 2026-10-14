import Darwin
import Foundation
import XCTest
@testable import AppInstallation

final class XcodeApplicationTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("xcode-validation-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try FileManager.default.removeItem(at: root) }

    private func fixture(_ name: String, identifier: String = "com.apple.dt.Xcode") throws -> URL {
        let app = root.appendingPathComponent(name), contents = app.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let info = ["CFBundleIdentifier": identifier, "CFBundleExecutable": "Xcode", "CFBundlePackageType": "APPL"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        return app
    }

    func testUnsignedSpoofedXcodeIsRejectedBeforeLaunch() throws {
        let app = try fixture("Fake.app")
        // This candidate satisfied the original absolute/.app launch predicate.
        XCTAssertTrue(app.path.hasPrefix("/") && app.lastPathComponent.hasSuffix(".app"))
        XCTAssertNil(XcodeApplication.verifiedURL(for: app.path, excluding: []))
    }

    func testAuthenticInnerAppleExecutableDoesNotAuthenticateOuterBundle() throws {
        let app = try fixture("Xcode.app")
        let inner = app.appendingPathComponent("Contents/Developer/usr/bin")
        try FileManager.default.createDirectory(at: inner, withIntermediateDirectories: true)
        let original = URL(fileURLWithPath: "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild")
        guard FileManager.default.fileExists(atPath: original.path) else { throw XCTSkip("full Xcode unavailable") }
        try FileManager.default.copyItem(at: original, to: inner.appendingPathComponent("xcodebuild"))
        XCTAssertNil(XcodeApplication.verifiedURL(for: app.path, excluding: []))
    }

    func testWrongAppleApplicationAndWrongBundleIdentityAreRejected() throws {
        XCTAssertNil(XcodeApplication.verifiedURL(for: "/System/Applications/TextEdit.app", excluding: []))
        let app = try fixture("Other.app", identifier: "com.example.other")
        var inspected = false
        XCTAssertNil(XcodeApplication.validatedURL(for: app.path, excluding: [], signatureIsValid: { _ in
            inspected = true; return true
        }))
        XCTAssertFalse(inspected)
    }

    func testRenamedCustomLocationsAndCanonicalAliasKeepSignedIdentity() throws {
        let app = try fixture("Personal Applications/Xcode Beta.app")
        let alias = root.appendingPathComponent("alias.app")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: app)
        var inspected: URL?
        let result = XcodeApplication.validatedURL(for: alias.path, excluding: [], signatureIsValid: {
            inspected = $0; return true
        })
        XCTAssertNotNil(result)
        XCTAssertEqual(result, XcodeApplication.canonicalURL(app.path))
        XCTAssertEqual(inspected, result)
    }

    func testWritablePreparationStateAndItsAliasesAreRejectedBeforeSignatureInspection() throws {
        let app = try fixture("state/pi-setup/work/attempt/Xcode.app")
        let alias = root.appendingPathComponent("Xcode.app")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: app)
        for path in [app.path, alias.path] {
            XCTAssertNil(XcodeApplication.validatedURL(for: path, excluding: [root.appendingPathComponent("state")],
                signatureIsValid: { _ in XCTFail("excluded state reached signature inspection"); return true }))
        }
        let sibling = try fixture("state-other/Xcode.app")
        XCTAssertNotNil(XcodeApplication.validatedURL(for: sibling.path, excluding: [root.appendingPathComponent("state")],
            signatureIsValid: { _ in true }))
    }

    func testMalformedMissingAndWritableCandidatesAreRejected() throws {
        let app = try fixture("Xcode.app")
        for path in ["relative.app", app.path + "\n", app.path + "/../Xcode.app", root.path + "/missing.app"] {
            XCTAssertNil(XcodeApplication.verifiedURL(for: path, excluding: []))
        }
        XCTAssertEqual(chmod(app.path, 0o777), 0)
        XCTAssertNil(XcodeApplication.validatedURL(for: app.path, excluding: [], signatureIsValid: { _ in
            XCTFail("writable app reached signature inspection"); return true
        }))
    }

    func testCaseAliasCannotBypassPreparationStateExclusion() throws {
        _ = try fixture("state/attempt/Xcode.app")
        let alias = root.appendingPathComponent("STATE/attempt/Xcode.app")
        guard FileManager.default.fileExists(atPath: alias.path) else { throw XCTSkip("case-sensitive filesystem") }
        XCTAssertNil(XcodeApplication.validatedURL(for: alias.path, excluding: [root.appendingPathComponent("state")],
            signatureIsValid: { _ in XCTFail("case alias reached signature inspection"); return true }))
    }

    func testCandidateReplacementDuringValidationIsRejected() throws {
        let app = try fixture("Xcode.app"), moved = root.appendingPathComponent("old.app")
        XCTAssertNil(XcodeApplication.validatedURL(for: app.path, excluding: [], signatureIsValid: { _ in
            do {
                try FileManager.default.moveItem(at: app, to: moved)
                _ = try self.fixture("Xcode.app")
            } catch { XCTFail("fixture replacement failed") }
            return true
        }))
    }

    func testInstalledAppleXcodePassesCompleteSignedIdentityValidation() throws {
        let app = URL(fileURLWithPath: "/Applications/Xcode.app")
        guard FileManager.default.fileExists(atPath: app.path) else { throw XCTSkip("full Xcode unavailable") }
        XCTAssertEqual(XcodeApplication.verifiedURL(for: app.path, excluding: [root]),
                       XcodeApplication.canonicalURL(app.path))
    }
}
