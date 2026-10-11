//! Private verification protocol: full-vocabulary bit digests, never raw scores.
use std::{
    collections::HashSet,
    fs::{File, OpenOptions},
    io::{self, BufRead, Read, Write},
    os::fd::{AsRawFd, FromRawFd},
    path::Path,
};

use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uzu_engine::{backends::metal::Metal, engine::Engine};

const MAX_REQUEST_BYTES: usize = 1_048_576;
const MAX_CONTEXT_TOKENS: u32 = 16384;

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Score {
        input_token_ids: Vec<u32>,
        candidate_token_ids: Vec<u32>,
        context_tokens: u32,
        temperature: f64,
    },
}

fn reply(writer: &mut impl Write, value: &Value) -> Result<(), &'static str> {
    serde_json::to_writer(&mut *writer, value).map_err(|_| "protocol_write_failed")?;
    writer
        .write_all(b"\n")
        .map_err(|_| "protocol_write_failed")?;
    writer.flush().map_err(|_| "protocol_write_failed")
}

// Retain a private protocol descriptor while silencing both process output
// descriptors before model loading. Upstream diagnostics cannot enter replies.
fn protocol_output() -> Result<File, &'static str> {
    let null = OpenOptions::new()
        .write(true)
        .open("/dev/null")
        .map_err(|_| "output_setup_failed")?;
    let fd = unsafe { libc::fcntl(libc::STDOUT_FILENO, libc::F_DUPFD_CLOEXEC, 3) };
    if fd < 0 {
        return Err("output_setup_failed");
    }
    let protocol = unsafe { File::from_raw_fd(fd) };
    if unsafe { libc::dup2(null.as_raw_fd(), libc::STDOUT_FILENO) } < 0
        || unsafe { libc::dup2(null.as_raw_fd(), libc::STDERR_FILENO) } < 0
    {
        return Err("output_setup_failed");
    }
    Ok(protocol)
}

fn run(output: &mut impl Write) -> Result<(), &'static str> {
    let mut args = std::env::args_os().skip(1);
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--score-model-path")) {
        return Err("invalid_arguments");
    }
    let path = args.next().ok_or("invalid_arguments")?;
    if args.next().is_some() {
        return Err("invalid_arguments");
    }
    let path = Path::new(&path);
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
    reply(
        output,
        &json!({
            "ready":true,"mode":"exact-token-digest-v1","engine":"uzu-rust","version":"0.6.1",
            "upstream_revision":"8d01581474760bd1fc7f4a547ff1d02f7dada437",
            "scoring_patch_sha256":option_env!("MEECP_UZU_SCORING_PATCH_SHA256").unwrap_or("unverified"),
            "cache_policy":"fresh-state","vocab_size":vocab_size
        }),
    )?;
    let mut input = io::stdin().lock();
    loop {
        let mut bytes = Vec::new();
        let count = Read::by_ref(&mut input)
            .take(MAX_REQUEST_BYTES as u64 + 1)
            .read_until(b'\n', &mut bytes)
            .map_err(|_| "protocol_read_failed")?;
        if count == 0 {
            return Ok(());
        }
        if bytes.len() > MAX_REQUEST_BYTES {
            return Err("request_too_large");
        }
        let request = match serde_json::from_slice::<Request>(&bytes) {
            Ok(request) => request,
            Err(_) => {
                reply(output, &json!({"error":"invalid_request"}))?;
                continue;
            }
        };
        let Request::Score {
            input_token_ids,
            candidate_token_ids,
            context_tokens,
            temperature,
        } = request;
        if input_token_ids.is_empty()
            || !(2..=16).contains(&candidate_token_ids.len())
            || context_tokens == 0
            || context_tokens > MAX_CONTEXT_TOKENS
            || !temperature.is_finite()
            || temperature <= 0.0
            || input_token_ids
                .iter()
                .chain(&candidate_token_ids)
                .any(|id| *id as usize >= vocab_size)
            || candidate_token_ids.iter().collect::<HashSet<_>>().len() != candidate_token_ids.len()
        {
            reply(output, &json!({"error":"invalid_scoring_request"}))?;
            continue;
        }
        if input_token_ids.len() > context_tokens as usize
            || model
                .max_context_length()
                .is_some_and(|maximum| context_tokens > maximum)
        {
            reply(output, &json!({"error":"context_overflow"}))?;
            continue;
        }
        // Each call owns new native state. Any native failure exits the process.
        let scores = model
            .score_next_token(&input_token_ids, context_tokens)
            .map_err(|_| "native_scoring_failed")?;
        if scores.logits.len() != vocab_size || scores.logits.iter().any(|value| !value.is_finite())
        {
            return Err("invalid_score_values");
        }
        let mut logits_hash = Sha256::new();
        for value in &scores.logits {
            logits_hash.update(value.to_bits().to_le_bytes());
        }
        let mut prompt_hash = Sha256::new();
        for id in &input_token_ids {
            prompt_hash.update(id.to_le_bytes());
        }
        reply(
            output,
            &json!({
                "vocab_logits_sha256":format!("{:x}", logits_hash.finalize()),
                "vocab_size":vocab_size,"prompt_tokens_sha256":format!("{:x}", prompt_hash.finalize()),
                "input_tokens":input_token_ids.len(),"cache_hit":false,"truncated":false
            }),
        )?;
    }
}

fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let Ok(output) = protocol_output() else {
        std::process::exit(1)
    };
    // Disable upstream captures before creating any worker threads.
    unsafe {
        std::env::remove_var("UZU_CAPTURE_FIRST_PREFILL");
        std::env::remove_var("UZU_CAPTURE_FIRST_DECODE");
    }
    let mut output = io::BufWriter::new(output);
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run(&mut output)));
    let code = match result {
        Ok(Ok(())) => return,
        Ok(Err(code)) => code,
        Err(_) => "native_operation_failed",
    };
    let _ = reply(&mut output, &json!({"error":code}));
    std::process::exit(1);
}
