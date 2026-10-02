import CryptoKit
import Darwin
import Foundation

/// Bounded JSON HTTP over an owner-only Unix socket. No TCP or proxy fallback.
public enum PrivateHTTP {
    private enum Failure: Error { case unavailable }
    private static let limit = 1024 * 1024

    public static func socketURL(stateDirectory: URL, service: String) throws -> URL {
        guard service == "manager" || service == "jevk5" ||
                UUID(uuidString: service)?.uuidString.lowercased() == service else { throw Failure.unavailable }
        // Foundation can rewrite /private/var as /var and decompose Unicode.
        // Match Python's POSIX resolution, then NFC, for the shared identity.
        guard let resolved = stateDirectory.path.withCString({ Darwin.realpath($0, nil) }) else { throw Failure.unavailable }
        defer { free(resolved) }
        let state = String(cString: resolved).precomposedStringWithCanonicalMapping
        let identity = SHA256.hash(data: Data(state.utf8)).prefix(12).map { String(format: "%02x", $0) }.joined()
        return URL(fileURLWithPath: "/private/tmp/mcpme-\(getuid())-\(identity)/\(service).sock")
    }

    public static func request(_ request: URLRequest, stateDirectory: URL, service: String,
                               completion: @escaping (Data?, HTTPURLResponse?, Error?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let result = try perform(request, stateDirectory: stateDirectory, service: service)
                completion(result.0, result.1, nil)
            } catch {
                completion(nil, nil, Failure.unavailable)
            }
        }
    }

    private static func perform(_ request: URLRequest, stateDirectory: URL, service: String) throws -> (Data, HTTPURLResponse) {
        let path = try socketURL(stateDirectory: stateDirectory, service: service)
        var directoryInfo = stat(), socketInfo = stat()
        guard lstat(path.deletingLastPathComponent().path, &directoryInfo) == 0,
              directoryInfo.st_mode & S_IFMT == S_IFDIR, directoryInfo.st_uid == getuid(),
              directoryInfo.st_mode & 0o7777 == 0o700,
              lstat(path.path, &socketInfo) == 0, socketInfo.st_mode & S_IFMT == S_IFSOCK,
              socketInfo.st_uid == getuid(), socketInfo.st_mode & 0o7777 == 0o600,
              let url = request.url, url.scheme == "http", url.host == "127.0.0.1",
              let port = url.port, (1...65535).contains(port), url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw Failure.unavailable }
        let body = request.httpBody ?? Data()
        let method = request.httpMethod ?? "GET"
        guard body.count <= limit, ["GET", "POST", "PATCH"].contains(method) else { throw Failure.unavailable }
        var headers = "\(method) \(components.percentEncodedPath.isEmpty ? "/" : components.percentEncodedPath) HTTP/1.1\r\n"
        headers += "Host: 127.0.0.1:\(port)\r\nConnection: close\r\nContent-Length: \(body.count)\r\n"
        for (name, value) in request.allHTTPHeaderFields ?? [:] {
            guard !name.contains("\r"), !name.contains("\n"), !value.contains("\r"), !value.contains("\n"),
                  !["host", "connection", "content-length", "transfer-encoding"].contains(name.lowercased()) else { throw Failure.unavailable }
            headers += name + ": " + value + "\r\n"
        }
        var output = Data((headers + "\r\n").utf8)
        guard output.count <= 16_384 else { throw Failure.unavailable }
        output.append(body)

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure.unavailable }
        defer { Darwin.close(fd) }
        var noSignal: Int32 = 1
        let timeout = request.timeoutInterval
        guard timeout.isFinite, timeout > 0, timeout <= 330 else { throw Failure.unavailable }
        var deadline = timeval(tv_sec: Int(timeout), tv_usec: 0)
        guard setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout.size(ofValue: noSignal))) == 0,
              setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &deadline, socklen_t(MemoryLayout.size(ofValue: deadline))) == 0,
              setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &deadline, socklen_t(MemoryLayout.size(ofValue: deadline))) == 0 else { throw Failure.unavailable }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bytes = Array(path.path.utf8) + [UInt8(0)]
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw Failure.unavailable }
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in buffer.copyBytes(from: bytes) }
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard connected == 0 else { throw Failure.unavailable }
        // The pathname is protected; also verify the UID on the connected peer.
        var peerUID = uid_t(), peerGID = gid_t()
        guard getpeereid(fd, &peerUID, &peerGID) == 0, peerUID == getuid() else { throw Failure.unavailable }
        try output.withUnsafeBytes { buffer in
            var sent = 0
            while sent < output.count {
                let count = Darwin.write(fd, buffer.baseAddress!.advanced(by: sent), output.count - sent)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw Failure.unavailable }
                sent += count
            }
        }
        var data = Data(), buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count < 0 && errno == EINTR { continue }
            guard count >= 0 else { throw Failure.unavailable }
            if count == 0 { break }
            guard data.count + count <= limit + 16_384 else { throw Failure.unavailable }
            data.append(contentsOf: buffer.prefix(count))
        }
        guard let split = data.range(of: Data("\r\n\r\n".utf8)), split.lowerBound <= 16_384,
              let header = String(data: data[..<split.lowerBound], encoding: .utf8) else { throw Failure.unavailable }
        let lines = header.components(separatedBy: "\r\n")
        let status = lines[0].split(separator: " ")
        guard status.count >= 2, status[0] == "HTTP/1.1", let code = Int(status[1]), (200...599).contains(code) else { throw Failure.unavailable }
        var length: Int?
        for line in lines.dropFirst() {
            let field = line.split(separator: ":", maxSplits: 1)
            guard field.count == 2 else { throw Failure.unavailable }
            let name = field[0].lowercased()
            if name == "transfer-encoding" { throw Failure.unavailable }
            if name == "content-length" {
                guard length == nil, let size = Int(field[1].trimmingCharacters(in: .whitespaces)), (0...limit).contains(size) else { throw Failure.unavailable }
                length = size
            }
        }
        let payload = Data(data[split.upperBound...])
        guard length == payload.count, let response = HTTPURLResponse(url: url, statusCode: code, httpVersion: "HTTP/1.1", headerFields: nil) else { throw Failure.unavailable }
        return (payload, response)
    }
}
