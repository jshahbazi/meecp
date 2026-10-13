import XCTest
@testable import AppInstallation

final class HostExitStatusTests: XCTestCase {
    func testExitStatuses() {
        XCTAssertEqual(appHostExitMessage(.exit, 73), "Another host owns this installation.")
        XCTAssertEqual(appHostExitMessage(.exit, 78), "The bundled preparation policy is invalid. Rebuild meecp and try again.")
        XCTAssertEqual(appHostExitMessage(.exit, 70), "Host failed to start.")
        XCTAssertEqual(appHostExitMessage(.exit, 2), "Host stopped unexpectedly.")
    }

    func testSignalsNeverClaimOwnershipOrPolicyFailure() {
        XCTAssertEqual(appHostExitMessage(.uncaughtSignal, 73), "Host stopped unexpectedly.")
        XCTAssertEqual(appHostExitMessage(.uncaughtSignal, 78), "Host stopped unexpectedly.")
    }
}
