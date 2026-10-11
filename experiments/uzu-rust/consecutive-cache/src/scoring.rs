//! Isolated exact-token scoring protocol. Request content is never diagnostic data.
use std::{
    collections::HashSet,
    fs::File,
    io::{self, BufRead, Read, Write},
    path::Path,
    sync::Arc,
    time::Instant,
};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uzu_engine::{
    backends::metal::Metal,
    engine::{Engine, language_model::LanguageModel},
};

use crate::cache::{CacheEntry, LastScoreCache};

const MAX_CONTEXT_TOKENS: u32 = 16384;
const MAX_REQUEST_BYTES: usize = 1_048_576;

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
enum ScoreRequest {
    ResetCache {},
    Tokenize {
        text: String,
        #[serde(default)]
        add_special_tokens: bool,
    },
    Score {
        input_token_ids: Vec<u32>,
        candidate_token_ids: Vec<u32>,
        context_tokens: u32,
        temperature: f64,
    },
}

#[derive(Serialize)]
struct ScoreMetrics {
    state_creation_seconds: f64,
    prefill_seconds: f64,
    readout_seconds: f64,
    score_seconds: f64,
    total_seconds: f64,
}

#[derive(Serialize)]
struct ScoreResult {
    input_tokens: usize,
    prompt_tokens_sha256: String,
    candidate_token_ids: Vec<u32>,
    logits: Vec<f64>,
    log_probabilities: Vec<f64>,
    candidate_probabilities: Vec<f64>,
    temperature: f64,
    cache_hit: bool,
    truncated: bool,
    metrics: ScoreMetrics,
    peak_memory_bytes: Option<usize>,
    cache: CacheTelemetry,
}

struct NativeBackend {
    engine: Arc<Engine<Metal>>,
    model: LanguageModel<Metal>,
    vocab_size: usize,
}

struct NativeScores {
    logits: Vec<f32>,
    state_creation_seconds: f64,
    prefill_seconds: f64,
    readout_seconds: f64,
}

trait ScoringBackend {
    fn vocab_size(&self) -> usize;
    fn max_context_length(&self) -> Option<u32>;
    fn peak_memory_usage(&self) -> Option<usize>;
    fn tokenize(&mut self, text: String, special: bool) -> Result<Vec<u32>, &'static str>;
    fn score(&mut self, input: &[u32], context: u32) -> Result<NativeScores, &'static str>;
}

struct ScoreWorker<B> {
    backend: B,
    cache: LastScoreCache,
    digest_mode: bool,
    hits: u64,
    native_score_calls: u64,
}

#[derive(Serialize)]
struct CacheTelemetry {
    enabled: bool,
    hits: u64,
    native_score_calls: u64,
}

struct PendingReply {
    value: Value,
    // None means a successful tokenize helper retains the committed entry.
    next_entry: Option<CacheEntry>,
    hit: bool,
}

fn write_reply(writer: &mut impl Write, reply: &impl Serialize) -> io::Result<()> {
    serde_json::to_writer(&mut *writer, reply)?;
    writer.write_all(b"\n")?;
    writer.flush()
}

fn token_hash(ids: &[u32]) -> String {
    let mut hasher = Sha256::new();
    for id in ids {
        hasher.update(id.to_le_bytes());
    }
    format!("{:x}", hasher.finalize())
}

fn validate_score(
    input: &[u32],
    candidates: &[u32],
    context: u32,
    temperature: f64,
    vocab_size: usize,
) -> Result<(), &'static str> {
    if input.is_empty()
        || !(2..=16).contains(&candidates.len())
        || context == 0
        || context > MAX_CONTEXT_TOKENS
        || !temperature.is_finite()
        || temperature <= 0.0
        || input
            .iter()
            .chain(candidates)
            .any(|id| *id as usize >= vocab_size)
        || candidates.iter().collect::<HashSet<_>>().len() != candidates.len()
    {
        return Err("invalid_scoring_request");
    }
    if input.len() > context as usize {
        return Err("context_overflow");
    }
    Ok(())
}

/// The common vocabulary normalization constant cancels from Jev's candidate
/// normalization. Apply its temperature directly to raw candidate logits in f64.
fn calibrated_probabilities(logits: &[f64], temperature: f64) -> Result<Vec<f64>, &'static str> {
    if logits.is_empty()
        || logits.iter().any(|value| !value.is_finite())
        || !temperature.is_finite()
        || temperature <= 0.0
    {
        return Err("invalid_score_values");
    }
    let maximum = logits.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let exponentials: Vec<f64> = logits
        .iter()
        .map(|value| ((value - maximum) / temperature).exp())
        .collect();
    let total: f64 = exponentials.iter().sum();
    Ok(exponentials.iter().map(|value| value / total).collect())
}

fn candidate_readout(
    vocabulary_logits: &[f32],
    candidates: &[u32],
    temperature: f64,
) -> Result<(Vec<f64>, Vec<f64>, Vec<f64>), &'static str> {
    if vocabulary_logits.is_empty()
        || vocabulary_logits.iter().any(|value| !value.is_finite())
        || candidates
            .iter()
            .any(|id| *id as usize >= vocabulary_logits.len())
    {
        return Err("invalid_score_values");
    }
    let maximum = vocabulary_logits
        .iter()
        .copied()
        .fold(f32::NEG_INFINITY, f32::max) as f64;
    let log_partition = maximum
        + vocabulary_logits
            .iter()
            .map(|value| (f64::from(*value) - maximum).exp())
            .sum::<f64>()
            .ln();
    let logits: Vec<f64> = candidates
        .iter()
        .map(|id| f64::from(vocabulary_logits[*id as usize]))
        .collect();
    let log_probabilities = logits.iter().map(|value| value - log_partition).collect();
    let candidate_probabilities = calibrated_probabilities(&logits, temperature)?;
    Ok((logits, log_probabilities, candidate_probabilities))
}

impl NativeBackend {
    fn load(path: &Path) -> Result<Self, &'static str> {
        if path.join("speculator").exists() {
            return Err("speculator_not_supported");
        }
        let config: Value = serde_json::from_reader(
            File::open(path.join("config.json")).map_err(|_| "model_config_unavailable")?,
        )
        .map_err(|_| "invalid_model_config")?;
        let vocab_size = config
            .get("decoder_config")
            .and_then(|decoder| decoder.get("vocab_size"))
            .and_then(Value::as_u64)
            .filter(|size| *size > 0 && *size <= u32::MAX as u64)
            .ok_or("invalid_model_config")? as usize;
        let engine = Engine::<Metal>::new().map_err(|_| "metal_initialization_failed")?;
        let model = engine
            .load_language_model(path)
            .map_err(|_| "model_load_failed")?;
        Ok(Self {
            engine,
            model,
            vocab_size,
        })
    }
}

impl ScoringBackend for NativeBackend {
    fn vocab_size(&self) -> usize {
        self.vocab_size
    }
    fn max_context_length(&self) -> Option<u32> {
        self.model.max_context_length()
    }
    fn peak_memory_usage(&self) -> Option<usize> {
        self.engine.peak_memory_usage()
    }
    fn tokenize(&mut self, text: String, special: bool) -> Result<Vec<u32>, &'static str> {
        self.model
            .tokenizer()
            .encode(text, special)
            .map(|encoding| encoding.get_ids().to_vec())
            .map_err(|_| "tokenization_failed")
    }
    fn score(&mut self, input: &[u32], context: u32) -> Result<NativeScores, &'static str> {
        self.model
            .score_next_token(input, context)
            .map(|scores| NativeScores {
                logits: scores.logits,
                state_creation_seconds: scores.state_creation_seconds,
                prefill_seconds: scores.prefill_seconds,
                readout_seconds: scores.readout_seconds,
            })
            .map_err(|_| "native_scoring_failed")
    }
}

impl<B: ScoringBackend> ScoreWorker<B> {
    fn new(backend: B, enabled: bool, digest_mode: bool) -> Self {
        Self {
            backend,
            cache: LastScoreCache::new(enabled),
            digest_mode,
            hits: 0,
            native_score_calls: 0,
        }
    }

    fn telemetry(&self, pending_hit: bool) -> CacheTelemetry {
        CacheTelemetry {
            enabled: self.cache.enabled(),
            hits: self.hits + u64::from(pending_hit),
            native_score_calls: self.native_score_calls,
        }
    }

    fn handle(&mut self, request: ScoreRequest) -> Result<PendingReply, &'static str> {
        let result = self.handle_inner(request);
        if result.is_err() {
            self.cache.clear();
        }
        result
    }

    fn handle_inner(&mut self, request: ScoreRequest) -> Result<PendingReply, &'static str> {
        match request {
            ScoreRequest::ResetCache {} => {
                self.cache.clear();
                Ok(PendingReply {
                    value: json!({"cache_reset":true,"cache":self.telemetry(false)}),
                    next_entry: None,
                    hit: false,
                })
            }
            ScoreRequest::Tokenize {
                text,
                add_special_tokens,
            } => {
                if text.is_empty() {
                    return Err("invalid_tokenization_request");
                }
                let started = Instant::now();
                let ids = self.backend.tokenize(text, add_special_tokens)?;
                Ok(PendingReply {
                    value: json!({"token_ids":ids,"token_count":ids.len(),"token_ids_sha256":token_hash(&ids),
                    "vocab_size":self.backend.vocab_size(),"tokenization_seconds":started.elapsed().as_secs_f64(),
                    "cache":self.telemetry(false)}),
                    next_entry: None,
                    hit: false,
                })
            }
            ScoreRequest::Score {
                input_token_ids,
                candidate_token_ids,
                context_tokens,
                temperature,
            } => {
                let started = Instant::now();
                let previous = self.cache.take_matching(&input_token_ids, context_tokens);
                validate_score(
                    &input_token_ids,
                    &candidate_token_ids,
                    context_tokens,
                    temperature,
                    self.backend.vocab_size(),
                )?;
                if self
                    .backend
                    .max_context_length()
                    .is_some_and(|maximum| context_tokens > maximum)
                {
                    return Err("context_overflow");
                }
                let prompt_tokens_sha256 = token_hash(&input_token_ids);
                let scoring_started = Instant::now();
                let cache_hit = previous.is_some();
                let scores = match previous {
                    Some(entry) => NativeScores {
                        logits: entry.logits,
                        state_creation_seconds: 0.0,
                        prefill_seconds: 0.0,
                        readout_seconds: 0.0,
                    },
                    None => {
                        // NativeBackend::score invokes score_next_token directly.
                        // This counts API dispatches, including failed attempts.
                        self.native_score_calls += 1;
                        self.backend.score(&input_token_ids, context_tokens)?
                    }
                };
                if scores.logits.len() != self.backend.vocab_size()
                    || scores.logits.iter().any(|value| !value.is_finite())
                {
                    return Err("invalid_score_values");
                }
                let readout_started = Instant::now();
                let (logits, log_probabilities, candidate_probabilities) =
                    candidate_readout(&scores.logits, &candidate_token_ids, temperature)?;
                let readout_seconds =
                    scores.readout_seconds + readout_started.elapsed().as_secs_f64();
                let result = ScoreResult {
                    input_tokens: input_token_ids.len(),
                    prompt_tokens_sha256,
                    candidate_token_ids,
                    logits,
                    log_probabilities,
                    candidate_probabilities,
                    temperature,
                    cache_hit,
                    truncated: false,
                    metrics: ScoreMetrics {
                        state_creation_seconds: scores.state_creation_seconds,
                        prefill_seconds: scores.prefill_seconds,
                        readout_seconds,
                        score_seconds: scoring_started.elapsed().as_secs_f64(),
                        total_seconds: started.elapsed().as_secs_f64(),
                    },
                    peak_memory_bytes: self.backend.peak_memory_usage(),
                    cache: self.telemetry(cache_hit),
                };
                let value = if self.digest_mode {
                    let mut digest = Sha256::new();
                    for logit in &scores.logits {
                        digest.update(logit.to_bits().to_le_bytes());
                    }
                    json!({"vocab_logits_sha256":format!("{:x}", digest.finalize()),
                        "vocab_size":self.backend.vocab_size(),"prompt_tokens_sha256":result.prompt_tokens_sha256,
                        "input_tokens":result.input_tokens,"cache_hit":cache_hit,"truncated":false,
                        "cache":self.telemetry(cache_hit)})
                } else {
                    serde_json::to_value(result).map_err(|_| "protocol_serialization_failed")?
                };
                Ok(PendingReply {
                    value,
                    next_entry: self.cache.enabled().then_some(CacheEntry {
                        input: input_token_ids,
                        context: context_tokens,
                        logits: scores.logits,
                    }),
                    hit: cache_hit,
                })
            }
        }
    }

    fn process_line(&mut self, bytes: &[u8], output: &mut impl Write) -> Result<(), &'static str> {
        let result = serde_json::from_slice::<ScoreRequest>(bytes)
            .map_err(|_| "invalid_request")
            .and_then(|request| self.handle(request));
        match result {
            Ok(pending) => {
                if write_reply(output, &pending.value).is_err() {
                    self.cache.clear();
                    return Err("protocol_write_failed");
                }
                // The only publication site follows serialization, write, newline and flush.
                if let Some(entry) = pending.next_entry {
                    self.cache.publish(entry);
                }
                self.hits += u64::from(pending.hit);
                Ok(())
            }
            Err(code) => {
                self.cache.clear();
                write_reply(output, &json!({"error":code,"cache":self.telemetry(false)}))
                    .map_err(|_| "protocol_write_failed")?;
                if matches!(code, "native_scoring_failed" | "invalid_score_values") {
                    Err("native_operation_failed")
                } else {
                    Ok(())
                }
            }
        }
    }
}

pub(super) fn run_lines(
    path: &Path,
    enabled: bool,
    digest_mode: bool,
    output: &mut impl Write,
) -> Result<(), &'static str> {
    let started = Instant::now();
    let mut worker = ScoreWorker::new(NativeBackend::load(path)?, enabled, digest_mode);
    write_reply(output, &json!({"ready":true,"engine":"uzu-rust","version":"0.6.1",
        "mode":if digest_mode { "exact-token-digest-cache-v1" } else { "exact-token-scoring-cache-v1" },
        "upstream_revision":"8d01581474760bd1fc7f4a547ff1d02f7dada437",
        "scoring_patch_sha256":option_env!("MEECP_UZU_SCORING_PATCH_SHA256").unwrap_or("unverified"),
        "cache_policy":worker.cache.policy(),"cache_enabled":worker.cache.enabled(),
        "cache_key":"exact-token-ids+context-tokens+model-instance",
        "cache":worker.telemetry(false),
        "load_seconds":started.elapsed().as_secs_f64(),"vocab_size":worker.backend.vocab_size(),
        "peak_memory_bytes":worker.backend.peak_memory_usage()}))
        .map_err(|_| "protocol_write_failed")?;
    let mut stdin = io::stdin().lock();
    loop {
        let mut bytes = Vec::new();
        let count = Read::by_ref(&mut stdin)
            .take(MAX_REQUEST_BYTES as u64 + 1)
            .read_until(b'\n', &mut bytes)
            .map_err(|_| "protocol_read_failed")?;
        if count == 0 {
            return Ok(());
        }
        if bytes.len() > MAX_REQUEST_BYTES {
            worker.cache.clear();
            write_reply(
                output,
                &json!({"error":"request_too_large","cache":worker.telemetry(false)}),
            )
            .map_err(|_| "protocol_write_failed")?;
            return Ok(());
        }
        worker.process_line(&bytes, output)?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeBackend {
        calls: usize,
        fail_score: bool,
        invalid_output: bool,
        fail_tokenize: bool,
    }
    impl ScoringBackend for FakeBackend {
        fn vocab_size(&self) -> usize {
            6
        }
        fn max_context_length(&self) -> Option<u32> {
            Some(MAX_CONTEXT_TOKENS)
        }
        fn peak_memory_usage(&self) -> Option<usize> {
            None
        }
        fn tokenize(&mut self, _: String, _: bool) -> Result<Vec<u32>, &'static str> {
            if self.fail_tokenize {
                Err("tokenization_failed")
            } else {
                Ok(vec![1, 2])
            }
        }
        fn score(&mut self, _: &[u32], _: u32) -> Result<NativeScores, &'static str> {
            self.calls += 1;
            if self.fail_score {
                return Err("native_scoring_failed");
            }
            Ok(NativeScores {
                logits: if self.invalid_output {
                    vec![f32::NAN; 6]
                } else {
                    vec![-0.0, -1.0, 0.0, 1.0, 2.0, 3.0]
                },
                state_creation_seconds: 0.1,
                prefill_seconds: 0.2,
                readout_seconds: 0.3,
            })
        }
    }
    fn worker(enabled: bool, digest: bool) -> ScoreWorker<FakeBackend> {
        ScoreWorker::new(
            FakeBackend {
                calls: 0,
                fail_score: false,
                invalid_output: false,
                fail_tokenize: false,
            },
            enabled,
            digest,
        )
    }
    fn request(input: &[u32], candidates: &[u32], context: u32, temperature: f64) -> Value {
        json!({"operation":"score", "input_token_ids":input, "candidate_token_ids":candidates,
            "context_tokens":context,"temperature":temperature})
    }
    fn post(worker: &mut ScoreWorker<FakeBackend>, request: Value) -> Value {
        let mut output = Vec::new();
        worker
            .process_line(&serde_json::to_vec(&request).unwrap(), &mut output)
            .unwrap();
        serde_json::from_slice(&output).unwrap()
    }
    fn standard() -> Value {
        request(&[1, 2], &[2, 3], 8192, 1.22)
    }

    #[test]
    fn disabled_policy_always_scores_and_holds_no_distribution() {
        let mut worker = worker(false, false);
        assert_eq!(worker.cache.policy(), "fresh-state");
        for _ in 0..3 {
            assert_eq!(post(&mut worker, standard())["cache_hit"], false);
        }
        assert_eq!(worker.backend.calls, 3);
        assert!(worker.cache.take_matching(&[1, 2], 8192).is_none());
    }

    #[test]
    fn identical_consecutive_requests_skip_native_execution_and_preserve_every_scalar_bit() {
        let mut worker = worker(true, false);
        let fresh = post(&mut worker, standard());
        let hit = post(&mut worker, standard());
        assert_eq!(worker.backend.calls, 1);
        assert_eq!(fresh["cache_hit"], false);
        assert_eq!(hit["cache_hit"], true);
        for field in ["logits", "log_probabilities", "candidate_probabilities"] {
            for (actual, expected) in hit[field]
                .as_array()
                .unwrap()
                .iter()
                .zip(fresh[field].as_array().unwrap())
            {
                assert_eq!(
                    actual.as_f64().unwrap().to_bits(),
                    expected.as_f64().unwrap().to_bits()
                );
            }
        }
        assert_eq!(hit["metrics"]["state_creation_seconds"], 0.0);
        assert_eq!(hit["metrics"]["prefill_seconds"], 0.0);
        assert!(hit["metrics"]["readout_seconds"].as_f64().unwrap() < 0.3);
    }

    #[test]
    fn exact_identity_context_and_single_entry_fence_a_b_a() {
        let mut worker = worker(true, false);
        for (input, context) in [
            (&[1, 2][..], 8192),
            (&[2, 1][..], 8192),
            (&[1, 2][..], 8192),
            (&[1, 2][..], 8193),
        ] {
            assert_eq!(
                post(&mut worker, request(input, &[2, 3], context, 1.22))["cache_hit"],
                false
            );
        }
        assert_eq!(worker.backend.calls, 4);
    }

    #[test]
    fn changed_candidates_order_and_temperature_recompute_readout_from_full_vocabulary() {
        let mut worker = worker(true, false);
        post(&mut worker, standard());
        let hit = post(&mut worker, request(&[1, 2], &[5, 2, 0], 8192, 0.93));
        assert_eq!(worker.backend.calls, 1);
        assert_eq!(hit["cache_hit"], true);
        // Compare complete protocol round trips. serde_json's default parser
        // can round a decimal differently from its in-memory f64 input.
        let control = post(
            &mut self::worker(false, false),
            request(&[1, 2], &[5, 2, 0], 8192, 0.93),
        );
        for field in ["logits", "log_probabilities", "candidate_probabilities"] {
            for (actual, expected) in hit[field]
                .as_array()
                .unwrap()
                .iter()
                .zip(control[field].as_array().unwrap())
            {
                assert_eq!(
                    actual.as_f64().unwrap().to_bits(),
                    expected.as_f64().unwrap().to_bits()
                );
            }
        }
    }

    #[test]
    fn digest_probe_uses_same_hit_path_and_preserves_all_f32_bits() {
        let mut worker = worker(true, true);
        let fresh = post(&mut worker, standard());
        let hit = post(&mut worker, standard());
        let mut hash = Sha256::new();
        for value in [-0.0_f32, -1.0, 0.0, 1.0, 2.0, 3.0] {
            hash.update(value.to_bits().to_le_bytes());
        }
        assert_eq!(hit["vocab_logits_sha256"], format!("{:x}", hash.finalize()));
        assert_eq!(fresh["vocab_logits_sha256"], hit["vocab_logits_sha256"]);
        assert_eq!(hit["cache_hit"], true);
        assert_eq!(worker.backend.calls, 1);
    }

    #[test]
    fn successful_tokenize_helpers_preserve_scoring_entry_failed_helpers_clear_it() {
        let mut worker = worker(true, false);
        post(&mut worker, standard());
        post(
            &mut worker,
            json!({"operation":"tokenize","text":"synthetic helper"}),
        );
        assert_eq!(post(&mut worker, standard())["cache_hit"], true);
        worker.backend.fail_tokenize = true;
        assert_eq!(
            post(
                &mut worker,
                json!({"operation":"tokenize","text":"synthetic helper"})
            )["error"],
            "tokenization_failed"
        );
        assert_eq!(post(&mut worker, standard())["cache_hit"], false);
    }

    #[test]
    fn every_parse_and_validation_failure_clears_previous_success() {
        let mut worker = worker(true, false);
        for invalid in [
            b"{".to_vec(),
            serde_json::to_vec(&request(&[], &[2, 3], 8192, 1.22)).unwrap(),
            serde_json::to_vec(&request(&[1, 2], &[2, 2], 8192, 1.22)).unwrap(),
            serde_json::to_vec(&request(&[1, 2], &[2, 3], 1, 1.22)).unwrap(),
            serde_json::to_vec(&json!({"operation":"tokenize","text":""})).unwrap(),
        ] {
            post(&mut worker, standard());
            let mut output = Vec::new();
            worker.process_line(&invalid, &mut output).unwrap();
            assert!(
                serde_json::from_slice::<Value>(&output)
                    .unwrap()
                    .get("error")
                    .is_some()
            );
            assert_eq!(post(&mut worker, standard())["cache_hit"], false);
        }
    }

    #[test]
    fn native_and_readout_faults_fence_worker_and_leave_no_reusable_entry() {
        for invalid_output in [false, true] {
            let mut worker = worker(true, false);
            post(&mut worker, standard());
            worker.backend.fail_score = !invalid_output;
            worker.backend.invalid_output = invalid_output;
            let mut output = Vec::new();
            let bytes = serde_json::to_vec(&request(&[2, 1], &[2, 3], 8192, 1.22)).unwrap();
            assert_eq!(
                worker.process_line(&bytes, &mut output),
                Err("native_operation_failed")
            );
            assert!(worker.cache.take_matching(&[1, 2], 8192).is_none());
            worker.backend.fail_score = false;
            worker.backend.invalid_output = false;
            assert_eq!(post(&mut worker, standard())["cache_hit"], false);
        }
    }

    struct FailingWriter {
        fail_flush: bool,
    }
    impl Write for FailingWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.fail_flush {
                Ok(bytes.len())
            } else {
                Err(io::Error::other("synthetic write failure"))
            }
        }
        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("synthetic flush failure"))
        }
    }

    #[test]
    fn publication_waits_for_serialization_write_and_flush_on_miss_and_hit() {
        for fail_flush in [false, true] {
            for hit in [false, true] {
                let mut worker = worker(true, false);
                if hit {
                    post(&mut worker, standard());
                }
                assert_eq!(
                    worker.process_line(
                        &serde_json::to_vec(&standard()).unwrap(),
                        &mut FailingWriter { fail_flush }
                    ),
                    Err("protocol_write_failed")
                );
                assert_eq!(post(&mut worker, standard())["cache_hit"], false);
            }
        }
    }

    #[test]
    fn a_pending_reply_never_establishes_a_cache_hit_before_publication() {
        let mut worker = worker(true, false);
        let request: ScoreRequest = serde_json::from_value(standard()).unwrap();
        let pending = worker.handle(request).unwrap();
        assert!(pending.next_entry.is_some());
        assert!(worker.cache.take_matching(&[1, 2], 8192).is_none());
        drop(pending);
        assert_eq!(post(&mut worker, standard())["cache_hit"], false);
        assert_eq!(worker.backend.calls, 2);
    }

    #[test]
    fn reset_clears_only_distribution_and_keeps_cumulative_accurate_counters() {
        let mut worker = worker(true, false);
        assert_eq!(
            post(&mut worker, standard())["cache"]["native_score_calls"],
            1
        );
        let hit = post(&mut worker, standard());
        assert_eq!(hit["cache"]["native_score_calls"], 1);
        assert_eq!(hit["cache"]["hits"], 1);
        let reset = post(&mut worker, json!({"operation":"reset_cache"}));
        assert_eq!(reset["cache_reset"], true);
        assert_eq!(reset["cache"]["hits"], 1);
        assert_eq!(reset["cache"]["native_score_calls"], 1);
        let fresh = post(&mut worker, standard());
        assert_eq!(fresh["cache_hit"], false);
        assert_eq!(fresh["cache"]["native_score_calls"], 2);
    }

    #[test]
    fn failed_reset_flush_leaves_cache_cleared_and_failed_hit_never_counts_success() {
        let mut worker = worker(true, false);
        post(&mut worker, standard());
        let bytes = serde_json::to_vec(&json!({"operation":"reset_cache"})).unwrap();
        assert_eq!(
            worker.process_line(&bytes, &mut FailingWriter { fail_flush: true }),
            Err("protocol_write_failed")
        );
        let fresh = post(&mut worker, standard());
        assert_eq!(fresh["cache_hit"], false);
        assert_eq!(fresh["cache"]["hits"], 0);
        assert_eq!(
            worker.process_line(
                &serde_json::to_vec(&standard()).unwrap(),
                &mut FailingWriter { fail_flush: false }
            ),
            Err("protocol_write_failed")
        );
        assert_eq!(worker.hits, 0);
    }

    #[test]
    fn calibration_matches_official_probability_power_formula() {
        let logits = [-3.0, 1.25, -0.5, 2.0];
        let temperature = 1.22;
        let uncalibrated = calibrated_probabilities(&logits, 1.0).unwrap();
        let weights: Vec<f64> = uncalibrated
            .iter()
            .map(|p| p.powf(1.0 / temperature))
            .collect();
        let sum: f64 = weights.iter().sum();
        let calibrated = calibrated_probabilities(&logits, temperature).unwrap();
        for (actual, weight) in calibrated.iter().zip(weights) {
            assert!((actual - weight / sum).abs() < 1e-14);
        }
    }

    #[test]
    fn calibration_is_shift_invariant_and_stable_at_large_logits() {
        let expected = calibrated_probabilities(&[-2.0, 0.0, 1.0], 0.93).unwrap();
        let actual = calibrated_probabilities(&[9998.0, 10000.0, 10001.0], 0.93).unwrap();
        assert_eq!(expected, actual);
        assert!((actual.iter().sum::<f64>() - 1.0).abs() < 1e-14);
        assert_eq!(
            calibrated_probabilities(&[0.0, 0.0], 1.22).unwrap(),
            vec![0.5, 0.5]
        );
        assert_eq!(
            calibrated_probabilities(&[f64::MAX, -f64::MAX], 1.22).unwrap(),
            vec![1.0, 0.0]
        );
    }

    #[test]
    fn readout_uses_full_vocabulary_log_partition_and_ordered_candidates() {
        let vocabulary = [-1.0, 0.0, 1.0, 2.0, -3.0];
        let (logits, log_probs, probs) = candidate_readout(&vocabulary, &[3, 1], 1.22).unwrap();
        assert_eq!(logits, [2.0, 0.0]);
        let full_mass: f64 = vocabulary.iter().map(|value| f64::from(*value).exp()).sum();
        assert!((log_probs[0] - (2.0_f64.exp() / full_mass).ln()).abs() < 1e-14);
        assert!((log_probs[1] - (1.0 / full_mass).ln()).abs() < 1e-14);
        let from_log_probs = calibrated_probabilities(&log_probs, 1.22).unwrap();
        for (direct, normalized) in probs.iter().zip(from_log_probs) {
            assert!((direct - normalized).abs() < 1e-14);
        }
    }

    #[test]
    fn invalid_math_and_tokens_are_rejected() {
        assert!(calibrated_probabilities(&[], 1.22).is_err());
        for temperature in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert!(calibrated_probabilities(&[0.0, 1.0], temperature).is_err());
            assert!(validate_score(&[1], &[2, 3], 8192, temperature, 100).is_err());
        }
        assert!(calibrated_probabilities(&[f64::NAN, 0.0], 1.22).is_err());
        assert!(candidate_readout(&[f32::INFINITY, 0.0], &[0, 1], 1.22).is_err());
        assert!(candidate_readout(&[0.0, 0.0], &[0, 2], 1.22).is_err());
        assert!(validate_score(&[], &[2, 3], 8192, 1.22, 100).is_err());
        assert!(validate_score(&[1], &[2, 2], 8192, 1.22, 100).is_err());
        assert!(validate_score(&[100], &[2, 3], 8192, 1.22, 100).is_err());
        assert!(validate_score(&[1], &[2], 8192, 1.22, 100).is_err());
        assert!(validate_score(&[1], &(0..17).collect::<Vec<_>>(), 8192, 1.22, 100).is_err());
        assert_eq!(
            validate_score(&[1, 2], &[2, 3], 1, 1.22, 100),
            Err("context_overflow")
        );
        assert_eq!(validate_score(&[1, 2], &[2, 3], 2, 1.22, 100), Ok(()));
        assert!(validate_score(&[1], &[2, 3], 16385, 1.22, 100).is_err());
    }

    #[test]
    fn protocol_rejects_unknown_fields_and_wrong_token_types() {
        assert!(serde_json::from_value::<ScoreRequest>(json!({"operation":"score","input_token_ids":[1],
            "candidate_token_ids":[2,3],"context_tokens":8192,"temperature":1.22,"cache_prompt":true})).is_err());
        assert!(
            serde_json::from_value::<ScoreRequest>(
                json!({"operation":"score","input_token_ids":[-1],
            "candidate_token_ids":[2,3],"context_tokens":8192,"temperature":1.22})
            )
            .is_err()
        );
        assert!(
            serde_json::from_value::<ScoreRequest>(
                json!({"operation":"tokenize","text":"test","private":true})
            )
            .is_err()
        );
        assert!(matches!(
            serde_json::from_value::<ScoreRequest>(json!({"operation":"tokenize","text":"test"}))
                .unwrap(),
            ScoreRequest::Tokenize {
                add_special_tokens: false,
                ..
            }
        ));
    }

    #[test]
    fn token_identity_matches_little_endian_u32_protocol() {
        assert_eq!(
            token_hash(&[1, 2]),
            format!("{:x}", Sha256::digest([1, 0, 0, 0, 2, 0, 0, 0]))
        );
        assert_ne!(token_hash(&[1, 2]), token_hash(&[2, 1]));
    }
}
