import Foundation

public func appHostExitMessage(_ terminationReason: Process.TerminationReason, _ status: Int32) -> String {
    guard terminationReason == .exit else { return "Host stopped unexpectedly." }
    switch status {
    case 73: return "Another host owns this installation."
    case 78: return "The bundled preparation policy is invalid. Rebuild meecp and try again."
    case 70: return "Host failed to start."
    default: return "Host stopped unexpectedly."
    }
}
