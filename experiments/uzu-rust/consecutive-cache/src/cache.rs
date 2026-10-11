//! One in-memory completed distribution. No native transformer/KV state retained.
pub const CACHE_POLICY: &str = "consecutive-full-vocabulary-logits-v1";

pub struct CacheEntry {
    pub input: Vec<u32>,
    pub context: u32,
    pub logits: Vec<f32>,
}

pub struct LastScoreCache {
    enabled: bool,
    entry: Option<CacheEntry>,
}

impl LastScoreCache {
    pub fn new(enabled: bool) -> Self {
        Self {
            enabled,
            entry: None,
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    pub fn policy(&self) -> &'static str {
        if self.enabled {
            CACHE_POLICY
        } else {
            "fresh-state"
        }
    }

    pub fn clear(&mut self) {
        self.entry = None;
    }

    // Taking before validation leaves no committed entry on failure or unwind.
    // A mismatch discards the prior distribution immediately: A/B/A cannot hit.
    pub fn take_matching(&mut self, input: &[u32], context: u32) -> Option<CacheEntry> {
        self.entry
            .take()
            .filter(|entry| self.enabled && entry.input == input && entry.context == context)
    }

    pub fn publish(&mut self, entry: CacheEntry) {
        self.entry = self.enabled.then_some(entry);
    }
}
