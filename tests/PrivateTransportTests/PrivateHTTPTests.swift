import XCTest
import CryptoKit
import Darwin
import Foundation
@testable import PrivateTransport

final class PrivateHTTPTests: XCTestCase {
    private enum FixtureFailure: Error { case bind }
    private func state(_ name: String = UUID().uuidString) throws -> URL {
        let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("meecp-\(name)-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    private func prepareSocket(_ state: URL, service: String = "manager") throws -> (URL, Int32) {
        let path = try PrivateHTTP.socketURL(stateDirectory: state, service: service)
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        chmod(path.deletingLastPathComponent().path, 0o700)
        addTeardownBlock { try? FileManager.default.removeItem(at: path.deletingLastPathComponent()) }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(fd, 0)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bytes = Array(path.path.utf8) + [0]
        XCTAssertLessThan(bytes.count, MemoryLayout.size(ofValue: address.sun_path))
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard result == 0, listen(fd, 1) == 0 else { close(fd); throw FixtureFailure.bind }
        chmod(path.path, 0o600)
        return (path, fd)
    }

    private func call(_ request: URLRequest, state: URL, service: String = "manager") throws -> (Data, HTTPURLResponse) {
        let exp = expectation(description: "private request")
        var output: (Data, HTTPURLResponse)?, failure: Error?
        PrivateHTTP.request(request, stateDirectory: state, service: service) { data, response, error in
            output = data.flatMap { body in response.map { (body, $0) } }
            failure = error
            exp.fulfill()
        }
        wait(for: [exp], timeout: 3)
        if let failure { throw failure }
        return try XCTUnwrap(output)
    }

    func testOwnerOnlyUnixHTTPServerReceivesBoundedRequest() throws {
        try assertOwnerOnlyRequest(method: "POST", path: "/api/status", body: Data("synthetic body".utf8))
    }

    func testPiSettingsPUTPreservesModelOnlyPayload() throws {
        let body = try JSONSerialization.data(withJSONObject: [
            "provider": "openai", "model": "gpt-6.1-sol", "base_url": NSNull()
        ])
        try assertOwnerOnlyRequest(method: "PUT", path: "/v1/pi/settings", body: body)
    }

    private func assertOwnerOnlyRequest(method: String, path: String, body: Data) throws {
        let state = try state()
        let (_, fd) = try prepareSocket(state)
        let queue = DispatchQueue(label: "private-http-fixture")
        defer { queue.sync {}; close(fd) }
        var received = Data()
        queue.async {
            var readable = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            guard poll(&readable, 1, 2_000) > 0 else { return }
            let client = accept(fd, nil, nil)
            guard client >= 0 else { return }
            defer { close(client) }
            var buffer = [UInt8](repeating: 0, count: 8192)
            var requestData = Data()
            repeat {
                let count = read(client, &buffer, buffer.count)
                if count <= 0 { break }
                requestData.append(contentsOf: buffer.prefix(count))
            } while !requestData.contains(Data("\r\n\r\n".utf8))
            if let end = requestData.range(of: Data("\r\n\r\n".utf8)),
               let headers = String(data: requestData[..<end.lowerBound], encoding: .utf8),
               let line = headers.split(separator: "\r\n").first(where: { $0.lowercased().hasPrefix("content-length:") }),
               let length = Int(line.split(separator: ":", maxSplits: 1)[1].trimmingCharacters(in: .whitespaces)) {
                while requestData.count < end.upperBound + length {
                    let count = read(client, &buffer, buffer.count)
                    if count <= 0 { break }
                    requestData.append(contentsOf: buffer.prefix(count))
                }
            }
            received = requestData
            let body = Data("synthetic response".utf8)
            let response = Data("HTTP/1.1 207 Multi-Status\r\nContent-Length: \(body.count)\r\n\r\n".utf8) + body
            response.withUnsafeBytes { _ = write(client, $0.baseAddress!, response.count) }
        }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:19385" + path)!)
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = 2
        request.setValue("Bearer synthetic-token", forHTTPHeaderField: "Authorization")
        let result = try call(request, state: state)
        XCTAssertEqual(result.0, Data("synthetic response".utf8))
        XCTAssertEqual(result.1.statusCode, 207)
        queue.sync {}
        let text = String(decoding: received, as: UTF8.self)
        XCTAssertTrue(text.hasPrefix(method + " " + path + " HTTP/1.1\r\n"))
        XCTAssertTrue(text.contains("Host: 127.0.0.1:19385"))
        XCTAssertTrue(text.contains("Authorization: Bearer synthetic-token"))
        let separator = try XCTUnwrap(received.range(of: Data("\r\n\r\n".utf8)))
        XCTAssertEqual(Data(received[separator.upperBound...]), body)
    }

    func testSocketPathMatchesPythonIdentityAlgorithm() throws {
        let state = try state()
        let resolved = try XCTUnwrap(state.path.withCString { Darwin.realpath($0, nil) })
        defer { free(resolved) }
        let digest = SHA256.hash(data: Data(String(cString: resolved).precomposedStringWithCanonicalMapping.utf8)).prefix(12).map { String(format: "%02x", $0) }.joined()
        let expected = "/private/tmp/mcpme-\(getuid())-\(digest)/manager.sock"
        XCTAssertEqual(try PrivateHTTP.socketURL(stateDirectory: state, service: "manager").path, expected)
    }

    func testRejectsMissingUnsafeOrSymlinkSocket() throws {
        let missing = try state()
        var request = URLRequest(url: URL(string: "http://127.0.0.1:19385/")!)
        request.timeoutInterval = 1
        XCTAssertThrowsError(try call(request, state: missing))

        let unsafe = try state("unsafe")
        let (_, fd) = try prepareSocket(unsafe)
        close(fd)
        let path = try PrivateHTTP.socketURL(stateDirectory: unsafe, service: "manager")
        chmod(path.path, 0o666)
        XCTAssertThrowsError(try call(request, state: unsafe))
        chmod(path.path, 0o600)
        chmod(path.deletingLastPathComponent().path, 0o755)
        XCTAssertThrowsError(try call(request, state: unsafe))
        chmod(path.deletingLastPathComponent().path, 0o700)
        let actual = path.appendingPathExtension("actual")
        try FileManager.default.moveItem(at: path, to: actual)
        try FileManager.default.createSymbolicLink(at: path, withDestinationURL: actual)
        XCTAssertThrowsError(try call(request, state: unsafe))
        try FileManager.default.removeItem(at: path)
        try FileManager.default.moveItem(at: actual, to: path)
        let directory = path.deletingLastPathComponent()
        let actualDirectory = directory.appendingPathExtension("actual")
        try FileManager.default.moveItem(at: directory, to: actualDirectory)
        addTeardownBlock { try? FileManager.default.removeItem(at: actualDirectory) }
        try FileManager.default.createSymbolicLink(at: directory, withDestinationURL: actualDirectory)
        XCTAssertThrowsError(try call(request, state: unsafe))
    }

    func testNoTCPFallbackOrRedirectFollowing() throws {
        let tcpState = try state("tcp")
        let listener = socket(AF_INET, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(listener, 0)
        defer { close(listener) }
        var address = sockaddr_in(); address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size); address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0; address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { XCTAssertEqual(Darwin.bind(listener, $0, socklen_t(MemoryLayout<sockaddr_in>.size)), 0) } }
        XCTAssertEqual(listen(listener, 1), 0)
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        XCTAssertEqual(getsockname(listener, withUnsafeMutablePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { $0 } }, &length), 0)
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(Int(UInt16(bigEndian: address.sin_port)))/")!)
        request.timeoutInterval = 1
        request.setValue("Bearer synthetic-token", forHTTPHeaderField: "Authorization")
        XCTAssertThrowsError(try call(request, state: tcpState))
        XCTAssertEqual(fcntl(listener, F_SETFL, O_NONBLOCK), 0)
        XCTAssertEqual(accept(listener, nil, nil), -1)
        XCTAssertEqual(errno, EWOULDBLOCK)

        let redirectState = try state("redirect")
        let (redirectPath, fd) = try prepareSocket(redirectState)
        addTeardownBlock { try? FileManager.default.removeItem(at: redirectPath.deletingLastPathComponent()) }
        defer { close(fd) }
        let location = request.url!.absoluteString
        DispatchQueue.global().async {
            let client = accept(fd, nil, nil)
            guard client >= 0 else { return }
            var ignored = [UInt8](repeating: 0, count: 4096); _ = read(client, &ignored, ignored.count)
            let value = Data("HTTP/1.1 302 Found\r\nLocation: \(location)\r\nContent-Length: 0\r\n\r\n".utf8)
            value.withUnsafeBytes { _ = write(client, $0.baseAddress!, value.count) }; close(client)
        }
        let redirect = try call(request, state: redirectState)
        XCTAssertEqual(redirect.1.statusCode, 302)
        XCTAssertEqual(accept(listener, nil, nil), -1)
        XCTAssertEqual(errno, EWOULDBLOCK)
    }

    func testRejectsMalformedAndOversizeResponses() throws {
        let responses = [
            Data("HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n".utf8),
            Data("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n".utf8),
            Data("HTTP/1.1 200 OK\r\n\r\nmissing length".utf8),
            Data("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort".utf8),
            Data("HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n".utf8) + Data(repeating: 120, count: 1_048_577),
        ]
        for response in responses {
            let state = try state()
            let (_, fd) = try prepareSocket(state)
            defer { close(fd) }
            DispatchQueue.global().async {
                let client = accept(fd, nil, nil)
                guard client >= 0 else { return }
                defer { close(client) }
                var flag: Int32 = 1
                setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &flag, socklen_t(MemoryLayout.size(ofValue: flag)))
                var ignored = [UInt8](repeating: 0, count: 4096)
                _ = read(client, &ignored, ignored.count)
                response.withUnsafeBytes { buffer in
                    var sent = 0
                    while sent < response.count {
                        let count = write(client, buffer.baseAddress!.advanced(by: sent), response.count - sent)
                        if count <= 0 { break }
                        sent += count
                    }
                }
            }
            var request = URLRequest(url: URL(string: "http://127.0.0.1:19385/api/status")!)
            request.timeoutInterval = 1
            XCTAssertThrowsError(try call(request, state: state))
        }
    }
}
