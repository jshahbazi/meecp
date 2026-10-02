import AppKit
import Foundation

@main
struct McpmeIconGenerator {
    static func main() {
        guard CommandLine.arguments.count == 3 || CommandLine.arguments.count == 4 else {
            report("Usage: mcpme-icon <source-asset-directory> <output-iconset-directory> [menu-preview-directory]")
            exit(2)
        }
        do {
            try generate()
        } catch let error as IconError {
            report(error.message)
            exit(1)
        } catch {
            report("Unable to write icon assets.")
            exit(1)
        }
    }

    private static func generate() throws {
        let sourceDirectory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let outputDirectory = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        for filename in [McpmeBrand.appMasterFilename, McpmeBrand.menuMasterFilename] {
            let url = sourceDirectory.appendingPathComponent(filename)
            guard FileManager.default.fileExists(atPath: url.path) else {
                throw IconError.missingAsset(filename)
            }
            guard McpmeBrand.sourceImage(named: filename, assetDirectory: sourceDirectory) != nil else {
                throw IconError.invalidAsset(filename)
            }
        }
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        for size in [16, 32, 128, 256, 512] {
            for scale in [1, 2] {
                let pixels = size * scale
                let image = McpmeBrand.appIcon(size: CGFloat(pixels), assetDirectory: sourceDirectory)
                guard let representation = image.representations.first as? NSBitmapImageRep,
                      let png = representation.representation(using: .png, properties: [:]) else {
                    throw IconError.encodingFailed
                }
                let suffix = scale == 2 ? "@2x" : ""
                let filename = "icon_\(size)x\(size)\(suffix).png"
                try png.write(to: outputDirectory.appendingPathComponent(filename), options: .atomic)
            }
        }
        guard CommandLine.arguments.count == 4 else { return }
        let previewDirectory = URL(fileURLWithPath: CommandLine.arguments[3], isDirectory: true)
        try FileManager.default.createDirectory(at: previewDirectory, withIntermediateDirectories: true)
        guard let menuImage = McpmeBrand.menuBarImage(assetDirectory: sourceDirectory) else {
            throw IconError.invalidAsset(McpmeBrand.menuMasterFilename)
        }
        for (index, representation) in menuImage.representations.enumerated() {
            guard let bitmap = representation as? NSBitmapImageRep,
                  let png = bitmap.representation(using: .png, properties: [:]) else {
                throw IconError.encodingFailed
            }
            let suffix = index == 0 ? "" : "@2x"
            try png.write(to: previewDirectory.appendingPathComponent("menu-bar-pedestal\(suffix).png"), options: .atomic)
        }
    }

    private static func report(_ message: String) {
        FileHandle.standardError.write(Data("\(message)\n".utf8))
    }

    private enum IconError: Error {
        case missingAsset(String)
        case invalidAsset(String)
        case encodingFailed

        var message: String {
            switch self {
            case .missingAsset(let filename):
                return "Required source asset is missing: \(filename)"
            case .invalidAsset(let filename):
                return "Required source asset cannot be decoded: \(filename)"
            case .encodingFailed:
                return "Unable to encode icon PNG."
            }
        }
    }
}
