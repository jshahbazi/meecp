import AppKit
import Foundation

let directory = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let p = CGFloat(pixels)
        NSColor(calibratedRed: 0.17, green: 0.15, blue: 0.33, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: p*0.06, y: p*0.06, width: p*0.88, height: p*0.88), xRadius: p*0.20, yRadius: p*0.20).fill()
        let color = NSColor(calibratedRed: 0.68, green: 0.79, blue: 1, alpha: 1)
        color.setStroke()
        let branch = NSBezierPath()
        branch.lineWidth = p*0.055
        branch.lineCapStyle = .round
        branch.move(to: NSPoint(x: p*0.35, y: p*0.26))
        branch.line(to: NSPoint(x: p*0.35, y: p*0.74))
        branch.move(to: NSPoint(x: p*0.35, y: p*0.40))
        branch.curve(to: NSPoint(x: p*0.69, y: p*0.66), controlPoint1: NSPoint(x: p*0.69, y: p*0.40), controlPoint2: NSPoint(x: p*0.69, y: p*0.54))
        branch.stroke()
        color.setFill()
        for point in [(0.35,0.25),(0.35,0.75),(0.69,0.68)] {
            NSBezierPath(ovalIn: NSRect(x: p*point.0-p*0.075, y: p*point.1-p*0.075, width: p*0.15, height: p*0.15)).fill()
        }
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let filename = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(filename))
    }
}
