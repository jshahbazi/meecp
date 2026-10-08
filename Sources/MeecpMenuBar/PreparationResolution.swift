import Foundation

struct PreparationResolutionPolicy: Equatable {
    let operations: [String]
    let network: [String]
    let wallSeconds: Int
    let diskBytes: Int
    let memoryMB: Int

    init?(_ object: [String: Any]) {
        guard Set(object.keys) == ["schema_version", "operations", "network", "wall_seconds", "disk_bytes", "memory_mb"],
              preparationInteger(object["schema_version"]) == 1,
              let operations = object["operations"] as? [String], (1...2).contains(operations.count),
              operations.contains("resolve_dependencies"), Set(operations).count == operations.count,
              Set(operations).isSubset(of: ["resolve_dependencies", "discover_interface"]),
              let network = object["network"] as? [String], network.count <= 2,
              Set(network).count == network.count, Set(network).isSubset(of: ["pypi.org", "files.pythonhosted.org"]),
              let seconds = preparationInteger(object["wall_seconds"]), (1...600).contains(seconds),
              let disk = preparationInteger(object["disk_bytes"]), (1_048_576...8_589_934_592).contains(disk),
              let memory = preparationInteger(object["memory_mb"]), (128...4096).contains(memory) else { return nil }
        self.operations = operations; self.network = network; wallSeconds = seconds; diskBytes = disk; memoryMB = memory
    }

    var object: [String: Any] {
        ["schema_version": 1, "operations": operations, "network": network, "wall_seconds": wallSeconds, "disk_bytes": diskBytes, "memory_mb": memoryMB]
    }
    var display: String {
        let work = operations.map { $0 == "resolve_dependencies" ? "Resolve Python dependencies" : "Discover the App's MCP tools" }.joined(separator: ", ")
        return "Work: " + work + "\nDownload hosts: " + (network.isEmpty ? "None" : network.joined(separator: ", ")) +
            "\nDeadline: \(wallSeconds) seconds\nDisk budget: " + ByteCountFormatter.string(fromByteCount: Int64(diskBytes), countStyle: .file) +
            "\nMemory budget: \(memoryMB) MB\nDependency resolution may download wheels from the reviewed hosts. Tool discovery starts the selected App in the host's restricted workspace and lists its tools."
    }
}

struct PreparationResolutionReview {
    let context: PreparationContext
    let candidateID: String
    let planID: String
    let planDigest: String
    let expiresAt: Double
    let policy: PreparationResolutionPolicy
    let executionAvailable: Bool

    init?(_ object: [String: Any], context: PreparationContext, candidateID: String, policy: PreparationResolutionPolicy) {
        guard preparationInteger(object["schema_version"]) == 1,
              let request = object["request"] as? [String: Any],
              request["import_id"] as? String == context.importID,
              preparationInteger(request["revision"]) == context.revision,
              request["source_revision"] as? String == context.sourceRevision,
              request["candidate_id"] as? String == candidateID,
              let kind = request["candidate_kind"] as? String, ["python-callable", "python-mcp"].contains(kind),
              policy.operations.contains("discover_interface") == (kind == "python-mcp"),
              let returnedPolicy = request["policy"] as? [String: Any], PreparationResolutionPolicy(returnedPolicy) == policy,
              let planID = preparationUUID(object["plan_id"]), let planDigest = preparationDigest(object["plan_digest"]),
              let expiresAt = preparationNumber(object["expires_at"]), expiresAt > 0,
              let available = preparationBoolean(object["execution_available"]), preparationJSON(object, maximum: 1_048_576) != nil else { return nil }
        self.context = context; self.candidateID = candidateID; self.planID = planID; self.planDigest = planDigest
        self.expiresAt = expiresAt; self.policy = policy; executionAvailable = available
    }
    func canStart(at time: Double = Date().timeIntervalSince1970) -> Bool { executionAvailable && expiresAt > time }
}

struct PreparationResolutionJob {
    let id: String
    let importID: String
    let revision: Int
    let sourceRevision: String
    let candidateID: String
    let state: String
    let stage: String
    let progress: [String: Int]
    let updatedAt: Double
    let ownershipSettled: Bool
    let reservationRetained: Bool
    let resultDigest: String?
    let error: String?
    static let terminal: Set<String> = ["succeeded", "canceled", "failed", "interrupted"]

    init?(_ object: [String: Any]) {
        let binding = object["request"] as? [String: Any] ?? object
        guard let id = preparationUUID(object["job_id"]), let importID = preparationUUID(binding["import_id"]),
              let revision = preparationInteger(binding["revision"]), revision > 0,
              let source = preparationText(binding["source_revision"], maximum: 256),
              let candidate = preparationDigest(binding["candidate_id"]),
              let state = preparationCode(object["state"]), ["queued", "running", "cancel_requested", "succeeded", "canceled", "failed", "interrupted"].contains(state),
              let stage = preparationCode(object["stage"]), ["queued", "snapshotting", "resolving", "installing", "discovering", "publishing", "settling", "succeeded"].contains(stage),
              let updated = preparationNumber(object["updated_at"]), updated >= 0,
              let settled = preparationBoolean(object["ownership_settled"]), let retained = preparationBoolean(object["reservation_retained"]),
              let progress = object["progress"] as? [String: Any] else { return nil }
        let limits = ["commands_finished": 4096, "wheels_acquired": 4096, "bytes_acquired": 8_589_934_592, "files_checked": 200000, "tools_discovered": 128]
        guard Set(progress.keys) == Set(limits.keys) else { return nil }
        var counters: [String: Int] = [:]
        for (key, limit) in limits {
            guard let value = preparationInteger(progress[key]), (0...limit).contains(value) else { return nil }; counters[key] = value
        }
        let digest = preparationDigest(object["result_digest"]), error = preparationCode(object["error_code"])
        guard object["result_digest"] is NSNull || digest != nil,
              object["error_code"] is NSNull || error != nil,
              state != "succeeded" || (digest != nil && settled && !retained) else { return nil }
        self.id = id; self.importID = importID; self.revision = revision; sourceRevision = source; candidateID = candidate
        self.state = state; self.stage = stage; self.progress = counters; updatedAt = updated
        ownershipSettled = settled; reservationRetained = retained; resultDigest = digest; self.error = error
    }
    var active: Bool { !Self.terminal.contains(state) || !ownershipSettled || reservationRetained }
    var needsRecovery: Bool { Self.terminal.contains(state) && (!ownershipSettled || reservationRetained) }
    var summary: String {
        let stages = ["queued": "Waiting to begin", "snapshotting": "Saving selected inputs", "resolving": "Resolving reviewed dependencies",
            "installing": "Preparing dependencies", "discovering": "Discovering the App's MCP tools", "publishing": "Verifying resolved inputs", "settling": "Finishing owned work", "succeeded": "Complete"]
        return state.replacingOccurrences(of: "_", with: " ").capitalized + " · " + stages[stage]! +
            "\nCommands: \(progress["commands_finished"] ?? 0) · Wheels: \(progress["wheels_acquired"] ?? 0) · Bytes acquired: \(progress["bytes_acquired"] ?? 0)" +
            "\nFiles checked: \(progress["files_checked"] ?? 0) · Tools discovered: \(progress["tools_discovered"] ?? 0)" +
            (needsRecovery ? "\nThe host retains ownership and resources until recovery settles." : "") + (error.map { "\n" + $0 } ?? "")
    }
}
