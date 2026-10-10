# Rust dependency provenance and notices

The worker pins official Uzu 0.6.1 to commit `8d01581474760bd1fc7f4a547ff1d02f7dada437`. `Cargo.lock` pins the complete Rust dependency graph. `THIRD_PARTY.json` records package versions, declared SPDX license expressions, registry checksums or Git revisions, upstream repositories, and hashes of retained license/notice files. Paths in this inventory are relative to this experiment directory.

The inventory conservatively includes every package resolved by the locked Cargo graph, including build-only dependencies and dependencies for other platforms. The `licenses/` directory retains available upstream license and notice files from those package sources. Uzu workspace crates use the repository's MIT license.

Available notices were retained from the crates.io source distribution, or from the matching published source revision when the package omitted license files. Source-revision URLs and hashes are recorded in the inventory.

Packages without separate license/notice files in either their source distribution or the matching upstream revision: `r-efi-5.3.0`, `r-efi-6.0.0`, `simd_helpers-0.1.0`. Their declared license expressions remain in the inventory.
