//! Isolated exact-token scoring protocol. Request content is never diagnostic data.
use std::{
    collections::HashSet,
    fs::File,
    io::{self, BufRead, Read},
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

use crate::{MAX_CONTEXT_TOKENS, MAX_REQUEST_BYTES, write_reply};

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
enum ScoreRequest {
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
}

struct ScoreWorker {
    engine: Arc<Engine<Metal>>,
    model: LanguageModel<Metal>,
    vocab_size: usize,
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

impl ScoreWorker {
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

    fn handle(&self, request: ScoreRequest) -> Result<Value, &'static str> {
        match request {
            ScoreRequest::Tokenize {
                text,
                add_special_tokens,
            } => {
                if text.is_empty() {
                    return Err("invalid_tokenization_request");
                }
                let started = Instant::now();
                let encoding = self
                    .model
                    .tokenizer()
                    .encode(text, add_special_tokens)
                    .map_err(|_| "tokenization_failed")?;
                let ids = encoding.get_ids();
                Ok(
                    json!({"token_ids":ids,"token_count":ids.len(),"token_ids_sha256":token_hash(ids),
                    "vocab_size":self.vocab_size,"tokenization_seconds":started.elapsed().as_secs_f64()}),
                )
            }
            ScoreRequest::Score {
                input_token_ids,
                candidate_token_ids,
                context_tokens,
                temperature,
            } => {
                let started = Instant::now();
                validate_score(
                    &input_token_ids,
                    &candidate_token_ids,
                    context_tokens,
                    temperature,
                    self.vocab_size,
                )?;
                if self
                    .model
                    .max_context_length()
                    .is_some_and(|maximum| context_tokens > maximum)
                {
                    return Err("context_overflow");
                }
                let prompt_tokens_sha256 = token_hash(&input_token_ids);
                let scoring_started = Instant::now();
                let scores = self
                    .model
                    .score_next_token(&input_token_ids, context_tokens)
                    .map_err(|_| "native_scoring_failed")?;
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
                    cache_hit: false,
                    truncated: false,
                    metrics: ScoreMetrics {
                        state_creation_seconds: scores.state_creation_seconds,
                        prefill_seconds: scores.prefill_seconds,
                        readout_seconds,
                        score_seconds: scoring_started.elapsed().as_secs_f64(),
                        total_seconds: started.elapsed().as_secs_f64(),
                    },
                    peak_memory_bytes: self.engine.peak_memory_usage(),
                };
                serde_json::to_value(result).map_err(|_| "protocol_serialization_failed")
            }
        }
    }
}

pub(super) fn run_lines(path: &Path) -> Result<(), &'static str> {
    let started = Instant::now();
    let worker = ScoreWorker::load(path)?;
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    write_reply(&mut stdout, &json!({"ready":true,"engine":"uzu-rust","version":"0.6.1",
        "mode":"exact-token-scoring-v1","upstream_revision":"8d01581474760bd1fc7f4a547ff1d02f7dada437",
        "scoring_patch_sha256":option_env!("MEECP_UZU_SCORING_PATCH_SHA256").unwrap_or("unverified"),
        "cache_policy":"fresh-state","vocab_size":worker.vocab_size,
        "load_seconds":started.elapsed().as_secs_f64(),"peak_memory_bytes":worker.engine.peak_memory_usage()}))
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
            write_reply(&mut stdout, &json!({"error":"request_too_large"}))
                .map_err(|_| "protocol_write_failed")?;
            return Ok(());
        }
        let result = serde_json::from_slice::<ScoreRequest>(&bytes)
            .map_err(|_| "invalid_request")
            .and_then(|request| worker.handle(request));
        let fatal = matches!(
            result,
            Err("native_scoring_failed") | Err("invalid_score_values")
        );
        match result {
            Ok(result) => write_reply(&mut stdout, &result),
            Err(code) => write_reply(&mut stdout, &json!({"error":code})),
        }
        .map_err(|_| "protocol_write_failed")?;
        // A failed native operation fences this process; the owner must reap it.
        if fatal {
            return Err("native_operation_failed");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
