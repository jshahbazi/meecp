// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JevK5MenuBar",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "JevK5MenuBar", targets: ["JevK5MenuBar"]),
               .executable(name: "McpmeMenuBar", targets: ["McpmeMenuBar"])],
    targets: [.target(name: "PrivateTransport"),
              .executableTarget(name: "JevK5MenuBar", dependencies: ["PrivateTransport"]),
              .executableTarget(name: "McpmeMenuBar", dependencies: ["PrivateTransport"]),
              .testTarget(name: "PrivateTransportTests", dependencies: ["PrivateTransport"], path: "tests/PrivateTransportTests")]
)
