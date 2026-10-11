//! Experimental Sol scoring worker with opt-in completed-distribution reuse.
mod cache;
mod protocol;
mod scoring;

fn main() {
    protocol::main(false);
}
