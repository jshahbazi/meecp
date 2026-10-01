import AppKit
import ImageIO

/// Native rendering for mcpme's approved sculpture assets.
internal enum McpmeBrand {
    private static let mint = CGColor(srgbRed: 186.0 / 255.0, green: 232.0 / 255.0, blue: 216.0 / 255.0, alpha: 1)
    static let appMasterFilename = "pedestal-icon-master.png"
    static let menuMasterFilename = "menu-bar-pedestal-master.png"

    /// A monochrome template with both standard- and Retina-resolution representations.
    static func menuBarImage(assetDirectory: URL? = nil) -> NSImage? {
        guard let source = sourceImage(named: menuMasterFilename, assetDirectory: assetDirectory) else {
            return nil
        }
        let image = NSImage(size: NSSize(width: 18, height: 18))
        for pixels in [18, 36] {
            let representation = bitmap(pixels: pixels) { context, dimension in
                draw(source, in: CGRect(x: 0, y: 0, width: dimension, height: dimension), context: context)
            }
            representation.size = image.size
            image.addRepresentation(representation)
        }
        image.isTemplate = true
        image.accessibilityDescription = "mcpme projects"
        return image
    }

    /// Creates an exact-pixel-size app icon, independent of the display's backing scale.
    /// A missing sculpture source leaves the mint tile as the runtime fallback.
    static func appIcon(size: CGFloat, assetDirectory: URL? = nil) -> NSImage {
        let pixels = max(1, Int(size.rounded()))
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        let source = sourceImage(named: appMasterFilename, assetDirectory: assetDirectory)
        let representation = bitmap(pixels: pixels) { context, dimension in
            let inset = dimension * 0.06
            let background = CGRect(x: inset, y: inset, width: dimension - 2 * inset, height: dimension - 2 * inset)
            let cornerRadius = dimension * 0.22
            context.setFillColor(mint)
            context.addPath(CGPath(roundedRect: background, cornerWidth: cornerRadius, cornerHeight: cornerRadius, transform: nil))
            context.fillPath()

            if let source {
                let side = dimension * 0.72
                let inset = (dimension - side) / 2
                draw(source, in: CGRect(x: inset, y: inset, width: side, height: side), context: context)
            }
        }
        representation.size = image.size
        image.addRepresentation(representation)
        return image
    }

    static func sourceImage(named filename: String, assetDirectory: URL? = nil) -> CGImage? {
        let directory = assetDirectory ?? Bundle.main.resourceURL?.appendingPathComponent("assets/brand", isDirectory: true)
        guard let url = directory?.appendingPathComponent(filename),
              let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
            return nil
        }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }

    private static func draw(_ source: CGImage, in bounds: CGRect, context: CGContext) {
        let scale = min(bounds.width / CGFloat(source.width), bounds.height / CGFloat(source.height))
        let size = CGSize(width: CGFloat(source.width) * scale, height: CGFloat(source.height) * scale)
        let destination = CGRect(x: bounds.midX - size.width / 2, y: bounds.midY - size.height / 2, width: size.width, height: size.height)
        context.interpolationQuality = .high
        context.draw(source, in: destination)
    }

    private static func bitmap(pixels: Int, draw: (CGContext, CGFloat) -> Void) -> NSBitmapImageRep {
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(
                data: nil,
                width: pixels,
                height: pixels,
                bitsPerComponent: 8,
                bytesPerRow: 0,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              ) else {
            preconditionFailure("Unable to allocate icon bitmap")
        }
        context.setAllowsAntialiasing(true)
        context.setShouldAntialias(true)
        draw(context, CGFloat(pixels))
        guard let cgImage = context.makeImage() else {
            preconditionFailure("Unable to create icon image")
        }
        return NSBitmapImageRep(cgImage: cgImage)
    }
}
