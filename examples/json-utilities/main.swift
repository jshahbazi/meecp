import Foundation

// A foreground, dependency-free MCP tool. stdout contains protocol messages only.
let inputSchema: [String: Any] = [
    "type": "object", "properties": ["document": ["type": "string"]],
    "required": ["document"], "additionalProperties": false
]
let outputSchema: [String: Any] = [
    "type": "object", "properties": ["valid": ["type": "boolean"], "kind": ["type": "string"]],
    "required": ["valid", "kind"], "additionalProperties": false
]
let tool: [String: Any] = [
    "name": "validate_json", "description": "Validate a JSON document and report its top-level type.",
    "inputSchema": inputSchema, "outputSchema": outputSchema
]
func send(_ id: Any, result: [String: Any]) {
    let envelope: [String: Any] = ["jsonrpc": "2.0", "id": id, "result": result]
    guard let data = try? JSONSerialization.data(withJSONObject: envelope, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
}
func failure(_ id: Any, code: Int, message: String) {
    let envelope: [String: Any] = ["jsonrpc": "2.0", "id": id, "error": ["code": code, "message": message]]
    guard let data = try? JSONSerialization.data(withJSONObject: envelope) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
}
while let line = readLine() {
    guard line.utf8.count <= 1_048_576,
          let data = line.data(using: .utf8),
          let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let method = request["method"] as? String else { continue }
    guard let id = request["id"] else { continue }
    switch method {
    case "initialize":
        send(id, result: ["protocolVersion": "2025-11-25", "capabilities": ["tools": [:]],
                          "serverInfo": ["name": "JSON Utilities", "version": "1.0.0"]])
    case "ping": send(id, result: [:])
    case "tools/list": send(id, result: ["tools": [tool]])
    case "tools/call":
        guard let params = request["params"] as? [String: Any], params["name"] as? String == "validate_json",
              let arguments = params["arguments"] as? [String: Any], arguments.count == 1,
              let document = arguments["document"] as? String else {
            failure(id, code: -32602, message: "invalid_tool_arguments"); continue
        }
        var valid = false
        var kind = "invalid"
        if let bytes = document.data(using: .utf8),
           let value = try? JSONSerialization.jsonObject(with: bytes, options: [.fragmentsAllowed]) {
            valid = true
            switch value {
            case is NSNull: kind = "null"
            case is [String: Any]: kind = "object"
            case is [Any]: kind = "array"
            case is String: kind = "string"
            case let number as NSNumber:
                kind = CFGetTypeID(number) == CFBooleanGetTypeID() ? "boolean" : "number"
            default: kind = "unknown"
            }
        }
        let result: [String: Any] = ["valid": valid, "kind": kind]
        let textData = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        send(id, result: ["content": [["type": "text", "text": String(decoding: textData, as: UTF8.self)]],
                          "structuredContent": result, "isError": false])
    default: failure(id, code: -32601, message: "method_not_found")
    }
}
