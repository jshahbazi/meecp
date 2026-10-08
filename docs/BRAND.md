# meecp visual identity

The identity centers on the approved matte lowercase **m on an ivory cylindrical pedestal**, viewed at a slight three-quarter angle. Rounded arches connect the app name to local MCP services. Repository artwork pairs the sculptural scene with the stylized `meecp` wordmark on the left.

![meecp repository banner](../assets/brand/meecp-banner.png)

## Assets

| File | Use |
| --- | --- |
| `assets/brand/meecp-banner.png` | Repository/README banner, including the wordmark. |
| `assets/brand/local-relay-hero.png` | Approved illustration without text; composition reference. |
| `assets/brand/pedestal-icon-master.png` | Transparent full-color standalone m and pedestal. |
| `assets/brand/menu-bar-pedestal-master.png` | Transparent black template, preserving the angle and pedestal. |
| `assets/brand/menu-bar-pedestal.png` | Native 18 × 18 pixel template export. |
| `assets/brand/menu-bar-pedestal@2x.png` | Native 36 × 36 pixel template export for an 18-point icon. |
| `assets/brand/app-icon.png` | 1024 × 1024 app icon with mint tile. |
| `assets/brand/icon-preview.html` | Local preview at 16, 18, and 20 points in light/dark appearances. |
| `assets/brand/manifest.json` | SHA-256 provenance for checked-in raster assets. |

The menu bar template contains only the angled m and pedestal, with transparent openings and no service connections. macOS applies the foreground tint through `NSImage.isTemplate`. Do not add color or shadows to this template. The full-color illustration is brand artwork rather than an architecture diagram.

## Palette and typography

| Color | Value | Role |
| --- | --- | --- |
| Ink | `#17332D` | Wordmark, foreground, native fallback text. |
| Mint | `#BAE8D8` | App icon tile and quiet accent surfaces. |
| Lilac | `#C9BFEB` | Secondary illustration accent. |
| Ivory | `#F7F5EE` | Repository artwork background. |

The wordmark uses rounded geometric lettering. Native controls keep the system font and semantic macOS colors so text stays legible across appearances.

## Rendering and packaging

`Sources/MeecpMenuBar/Brand.swift` renders the checked-in image masters into exact-size native bitmap representations. `scripts/meecp-icon.swift` shares that rendering source to generate the standard macOS iconset. No additional dependencies are required.

```sh
mkdir -p .build
swiftc Sources/MeecpMenuBar/Brand.swift scripts/meecp-icon.swift -o .build/meecp-icon
.build/meecp-icon assets/brand runtime/MeecpAppIcon.iconset
iconutil -c icns runtime/MeecpAppIcon.iconset -o runtime/MeecpAppIcon.icns
uv run --locked scripts/build_meecp_app.py --output dist/meecp-brand.app
```

Use a fresh output app path. The native builder declares `CFBundleIconFile`, bundles the brand assets, and records the icon/source hashes before signing. The menu bar and Apps header use the same checked-in masters. The host package includes these assets so bundled documentation retains its images.

To refresh the checked-in menu preview PNGs, pass `assets/brand` as an optional third argument to `.build/meecp-icon`. Normal app builds write generated icons into build scratch directories.

## Provenance

Illustration, wordmark banner, transparent sculpture, and monochrome template were generated with the built-in ImageGen tool on 2026-10-01. Exact generation and edit briefs are in [BRAND_PROMPTS.md](BRAND_PROMPTS.md). The banner wordmark was edited to the current meecp name on 2026-10-08; the resulting opaque landscape asset is 1902 × 827 pixels. Native exports are rendered by AppKit; image masters remain in Git and build outputs remain ignored.
