import Foundation

let toolDescription = "Validate JSON syntax and report its top-level type. Documents are limited to 256 KiB of UTF-8 and 512 nested containers; exceeding either limit returns json_limit_exceeded."

enum JSONSyntaxResult {
    case valid(String)
    case invalid
    case limitExceeded
}

// JSONSerialization may discard a leading escaped U+FEFF from a String.
// Decode the tool document separately so syntax validation receives its exact
// Unicode text; envelope routing and identifiers keep their existing contract.
struct ValidationDocument: Decodable {
    struct Parameters: Decodable {
        struct Arguments: Decodable { let document: String }
        let arguments: Arguments
    }
    let params: Parameters
}

// Validate grammar without materializing numbers or decoded JSON values.
// Swift String already supplies valid UTF-8; a \u escape needs exactly four
// hex digits, including surrogate code units allowed by the JSON grammar.
struct JSONSyntaxScanner {
    static let maximumBytes = 262_144
    static let maximumDepth = 512
    private enum Frame {
        case arrayFirst, arrayValue, arrayDelimiter
        case objectFirst, objectKey, objectColon, objectValue, objectDelimiter
    }
    private var bytes: [UInt8] = []
    private var index = 0
    private var stack: [Frame] = []
    private var limitExceeded = false

    init(_ document: String) {
        if document.utf8.count > Self.maximumBytes {
            limitExceeded = true
        } else {
            bytes = Array(document.utf8)
        }
    }

    private var current: UInt8? { index < bytes.count ? bytes[index] : nil }

    private mutating func whitespace() {
        while let byte = current, byte == 32 || byte == 9 || byte == 10 || byte == 13 { index += 1 }
    }

    private mutating func consume(_ byte: UInt8) -> Bool {
        guard current == byte else { return false }
        index += 1
        return true
    }

    private mutating func literal(_ value: [UInt8]) -> Bool {
        guard bytes.count - index >= value.count,
              bytes[index..<(index + value.count)].elementsEqual(value) else { return false }
        index += value.count
        return true
    }

    private mutating func string() -> Bool {
        guard consume(34) else { return false }
        while let byte = current {
            index += 1
            if byte == 34 { return true }
            if byte < 32 { return false }
            if byte == 92 {
                guard let escaped = current else { return false }
                index += 1
                switch escaped {
                case 34, 47, 92, 98, 102, 110, 114, 116: break
                case 117:
                    for _ in 0..<4 {
                        guard let hex = current,
                              (48...57).contains(hex) || (65...70).contains(hex) || (97...102).contains(hex)
                        else { return false }
                        index += 1
                    }
                default: return false
                }
            }
        }
        return false
    }

    private mutating func digits() -> Bool {
        let start = index
        while let byte = current, (48...57).contains(byte) { index += 1 }
        return index > start
    }

    private mutating func number() -> Bool {
        _ = consume(45)
        if !consume(48) {
            guard let byte = current, (49...57).contains(byte), digits() else { return false }
        }
        if consume(46), !digits() { return false }
        if current == 101 || current == 69 {
            index += 1
            if current == 43 || current == 45 { index += 1 }
            if !digits() { return false }
        }
        return true
    }

    private mutating func container(_ frame: Frame, kind: String) -> String? {
        guard stack.count < Self.maximumDepth else { limitExceeded = true; return nil }
        index += 1
        stack.append(frame)
        return kind
    }

    private mutating func value() -> String? {
        guard let byte = current else { return nil }
        switch byte {
        case 123: return container(.objectFirst, kind: "object")
        case 91: return container(.arrayFirst, kind: "array")
        case 34: return string() ? "string" : nil
        case 116: return literal([116, 114, 117, 101]) ? "boolean" : nil
        case 102: return literal([102, 97, 108, 115, 101]) ? "boolean" : nil
        case 110: return literal([110, 117, 108, 108]) ? "null" : nil
        case 45, 48...57: return number() ? "number" : nil
        default: return nil
        }
    }

    mutating func validate() -> JSONSyntaxResult {
        if limitExceeded { return .limitExceeded }
        whitespace()
        guard let kind = value() else { return .invalid }
        while let frame = stack.last {
            whitespace()
            switch frame {
            case .arrayFirst:
                if consume(93) { stack.removeLast(); continue }
                stack[stack.count - 1] = .arrayDelimiter
                if value() == nil { return limitExceeded ? .limitExceeded : .invalid }
            case .arrayValue:
                stack[stack.count - 1] = .arrayDelimiter
                if value() == nil { return limitExceeded ? .limitExceeded : .invalid }
            case .arrayDelimiter:
                if consume(93) { stack.removeLast() }
                else if consume(44) { stack[stack.count - 1] = .arrayValue }
                else { return .invalid }
            case .objectFirst:
                if consume(125) { stack.removeLast(); continue }
                if !string() { return .invalid }
                stack[stack.count - 1] = .objectColon
            case .objectKey:
                if !string() { return .invalid }
                stack[stack.count - 1] = .objectColon
            case .objectColon:
                if !consume(58) { return .invalid }
                stack[stack.count - 1] = .objectValue
            case .objectValue:
                stack[stack.count - 1] = .objectDelimiter
                if value() == nil { return limitExceeded ? .limitExceeded : .invalid }
            case .objectDelimiter:
                if consume(125) { stack.removeLast() }
                else if consume(44) { stack[stack.count - 1] = .objectKey }
                else { return .invalid }
            }
        }
        whitespace()
        return index == bytes.count ? .valid(kind) : .invalid
    }
}

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
    "name": "validate_json", "description": toolDescription,
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
              arguments["document"] is String,
              let document = (try? JSONDecoder().decode(ValidationDocument.self, from: data))?.params.arguments.document else {
            failure(id, code: -32602, message: "invalid_tool_arguments"); continue
        }
        var scanner = JSONSyntaxScanner(document)
        let valid: Bool
        let kind: String
        switch scanner.validate() {
        case .valid(let topLevelKind): valid = true; kind = topLevelKind
        case .invalid: valid = false; kind = "invalid"
        case .limitExceeded:
            send(id, result: ["content": [["type": "text", "text": "json_limit_exceeded"]], "isError": true])
            continue
        }
        let result: [String: Any] = ["valid": valid, "kind": kind]
        let textData = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        send(id, result: ["content": [["type": "text", "text": String(decoding: textData, as: UTF8.self)]],
                          "structuredContent": result, "isError": false])
    default: failure(id, code: -32601, message: "method_not_found")
    }
}
