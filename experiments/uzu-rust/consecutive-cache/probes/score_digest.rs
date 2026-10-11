//! Complete-vocabulary bit digest using the same cache and publication path.
#[path = "../src/cache.rs"]
mod cache;
#[path = "../src/protocol.rs"]
mod protocol;
#[path = "../src/scoring.rs"]
mod scoring;

fn main() {
    protocol::main(true);
}
