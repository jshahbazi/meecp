//! Experimental local worker. Only JSON protocol replies may reach stdout.
mod scoring;
use std::{
    fs::{self, File},
    io::{self, BufRead, Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::mpsc::{TrySendError, sync_channel},
    time::{Duration, Instant},
};

use hanashi::chat::hanashi::{config::HanashiConfig, renderer::Renderer};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use shoji::types::{
    basic::{ReasoningEffort, Token},
    session::chat::ChatMessage,
};
use uzu_engine::{
    backends::metal::Metal,
    engine::{
        Engine,
        language_model::{
            LanguageModel,
            stream::{LanguageModelStreamOptions, SamplingMethod},
        },
    },
};

const MAX_REQUEST_BYTES: usize = 1_048_576;
const MAX_OUTPUT_TOKENS: u32 = 4096;
const MAX_CONTEXT_TOKENS: u32 = 16384;
const PROFILE: &str = "qwen3.5-0.8b-mirai-l-uzu-metal-text-v1";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    text: String,
    max_output_tokens: u32,
    #[serde(default = "default_context")]
    context_tokens: u32,
}

#[derive(Clone, Deserialize)]
struct Endpoint {
    host: String,
    port: u16,
}

#[derive(Clone, Deserialize)]
struct ModelDeclaration {
    artifact: PathBuf,
    context_tokens: u32,
    engine_identity: Value,
}

#[derive(Clone, Deserialize)]
struct HostConfig {
    endpoint: Endpoint,
    api_key: String,
    generation: u64,
    nonce: String,
    model: ModelDeclaration,
}

fn default_context() -> u32 {
    4096
}

#[derive(Serialize)]
struct ResultMetadata {
    text: String,
    prompt_tokens: usize,
    completion_tokens: usize,
    stop_reason: &'static str,
    ttft_seconds: f64,
    prefill_seconds: f64,
    decode_seconds: f64,
    total_seconds: f64,
    peak_memory_bytes: Option<usize>,
    prompt_sha256: String,
    prompt_tokens_sha256: String,
}

struct Worker {
    engine: std::sync::Arc<Engine<Metal>>,
    model: LanguageModel<Metal>,
    renderer: Renderer,
    bos_token: Option<Token>,
    eos_token: Option<Token>,
}

impl Worker {
    fn load(path: &Path) -> Result<Self, &'static str> {
        // Engine::load_language_model automatically loads any speculator present.
        // Reject that input so all experiment requests use ordinary decoding.
        if path.join("speculator").exists() {
            return Err("speculator_not_supported");
        }
        let config: Value = serde_json::from_reader(
            File::open(path.join("config.json")).map_err(|_| "model_config_unavailable")?,
        )
        .map_err(|_| "invalid_model_config")?;
        let codec = config
            .get("token_codec_config")
            .ok_or("missing_chat_codec")?;
        if codec.get("type").and_then(Value::as_str) != Some("ChatCodecConfig") {
            return Err("unsupported_chat_codec");
        }
        // This worker deliberately supports the benchmark's Qwen 3.5 family only.
        // Use upstream's Hanashi field mappings and the model's exact template.
        let mut resolved = HanashiConfig::Qwen35
            .resolve()
            .map_err(|_| "renderer_config_error")?;
        resolved.rendering.jinja.template = codec
            .get("prompt_template")
            .and_then(Value::as_str)
            .ok_or("missing_chat_template")?
            .to_owned();
        let renderer = Renderer::new(resolved.rendering);
        let engine = Engine::<Metal>::new().map_err(|_| "metal_initialization_failed")?;
        let model = engine
            .load_language_model(path)
            .map_err(|_| "model_load_failed")?;
        let token = |name: &str| -> Result<Option<Token>, &'static str> {
            let Some(value) = codec.get(name).and_then(Value::as_str) else {
                return Ok(None);
            };
            let id = model
                .tokenizer()
                .token_to_id(value)
                .ok_or("special_token_unavailable")?;
            Ok(Some(Token {
                id,
                value: value.to_owned(),
                is_special: true,
            }))
        };
        let bos_token = token("bos_token")?;
        let eos_token = token("eos_token")?;
        Ok(Self {
            engine,
            model,
            renderer,
            bos_token,
            eos_token,
        })
    }

    fn generate(&self, request: Request) -> Result<ResultMetadata, &'static str> {
        if request.text.is_empty()
            || request.max_output_tokens == 0
            || request.max_output_tokens > MAX_OUTPUT_TOKENS
            || request.context_tokens == 0
            || request.context_tokens > MAX_CONTEXT_TOKENS
        {
            return Err("invalid_request_limits");
        }
        let started = Instant::now();
        let messages = vec![
            ChatMessage::system()
                .with_text("You are a helpful assistant.".to_owned())
                .with_reasoning_effort(ReasoningEffort::Disabled),
            ChatMessage::user().with_text(request.text),
        ];
        let rendered = self
            .renderer
            .render(
                &messages,
                true,
                self.bos_token.clone(),
                self.eos_token.clone(),
                None,
            )
            .map_err(|_| "prompt_render_failed")?;
        let prompt_sha256 = format!("{:x}", Sha256::digest(rendered.as_bytes()));
        let encoded = self
            .model
            .tokenizer()
            .encode(rendered, false)
            .map_err(|_| "tokenization_failed")?;
        let mut token_hasher = Sha256::new();
        for token in encoded.get_ids() {
            token_hasher.update(token.to_le_bytes());
        }
        let prompt_tokens_sha256 = format!("{:x}", token_hasher.finalize());
        let input: Vec<u64> = encoded.get_ids().iter().map(|id| *id as u64).collect();
        if input.len() + request.max_output_tokens as usize + 8 > request.context_tokens as usize {
            return Err("context_overflow");
        }
        let mut state = self
            .model
            .create_empty_state(Some(request.context_tokens), 0)
            .map_err(|_| "state_creation_failed")?;
        let stream_started = Instant::now();
        let mut stream = self
            .model
            .stream(
                &input,
                &mut state,
                LanguageModelStreamOptions {
                    sampling_method: SamplingMethod::Greedy,
                },
            )
            .map_err(|_| "prefill_failed")?;
        let mut first_token = None;
        let mut output = Vec::new();
        let mut completion_tokens = 0;
        let mut stop_reason = "length";
        for _ in 0..request.max_output_tokens {
            let Some(token) = stream.next() else {
                stop_reason = "stop";
                break;
            };
            let token = token.map_err(|_| "generation_failed")?;
            if first_token.is_none() {
                first_token = Some(Instant::now());
            }
            completion_tokens += 1;
            if self
                .model
                .generation_config()
                .stop_token_ids
                .contains(&token)
            {
                stop_reason = "stop";
                break;
            }
            output.push(token as u32);
        }
        // Dropping a stream waits for any queued GPU work before returning.
        drop(stream);
        let generation_finished = Instant::now();
        let text = self
            .model
            .tokenizer()
            .decode(&output, true)
            .map_err(|_| "decode_failed")?;
        let ttft_seconds = first_token.map_or(0.0, |at| at.duration_since(started).as_secs_f64());
        // Stream construction submits GPU prefill asynchronously. Waiting for
        // the first returned token accounts for that work, rather than reporting
        // only CPU command encoding as prefill duration.
        let prefill_seconds =
            first_token.map_or(0.0, |at| at.duration_since(stream_started).as_secs_f64());
        let decode_seconds = first_token.map_or(0.0, |at| {
            generation_finished.duration_since(at).as_secs_f64()
        });
        Ok(ResultMetadata {
            text,
            prompt_tokens: input.len(),
            completion_tokens,
            stop_reason,
            ttft_seconds,
            prefill_seconds,
            decode_seconds,
            total_seconds: started.elapsed().as_secs_f64(),
            peak_memory_bytes: self.engine.peak_memory_usage(),
            prompt_sha256,
            prompt_tokens_sha256,
        })
    }
}

fn write_reply(writer: &mut impl Write, reply: &impl Serialize) -> io::Result<()> {
    serde_json::to_writer(&mut *writer, reply)?;
    writer.write_all(b"\n")?;
    writer.flush()
}

fn run_lines(path: PathBuf) -> Result<(), &'static str> {
    let load_started = Instant::now();
    let worker = Worker::load(&path)?;
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    write_reply(&mut stdout, &json!({"ready": true, "engine": "uzu-rust", "version": "0.6.1",
        "load_seconds": load_started.elapsed().as_secs_f64(), "peak_memory_bytes": worker.engine.peak_memory_usage()}))
        .map_err(|_| "protocol_write_failed")?;
    let mut stdin = io::stdin().lock();
    loop {
        let mut bytes = Vec::new();
        let count = Read::by_ref(&mut stdin)
            .take(MAX_REQUEST_BYTES as u64 + 1)
            .read_until(b'\n', &mut bytes)
            .map_err(|_| "protocol_read_failed")?;
        if count == 0 {
            break;
        }
        if bytes.len() > MAX_REQUEST_BYTES {
            write_reply(&mut stdout, &json!({"error": "request_too_large"}))
                .map_err(|_| "protocol_write_failed")?;
            break;
        }
        let result = serde_json::from_slice::<Request>(&bytes)
            .map_err(|_| "invalid_request")
            .and_then(|request| worker.generate(request));
        match result {
            Ok(result) => write_reply(&mut stdout, &result),
            Err(code) => write_reply(&mut stdout, &json!({"error": code})),
        }
        .map_err(|_| "protocol_write_failed")?;
    }
    Ok(())
}

struct HttpRequest {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
    body_length: usize,
    read_deadline: Instant,
}

fn read_exact_deadline(
    stream: &mut TcpStream,
    mut bytes: &mut [u8],
    deadline: Instant,
) -> Result<(), &'static str> {
    while !bytes.is_empty() {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or("request_timeout")?;
        stream
            .set_read_timeout(Some(remaining))
            .map_err(|_| "invalid_connection")?;
        match stream.read(bytes) {
            Ok(0) => return Err("invalid_request"),
            Ok(count) => {
                if Instant::now() >= deadline {
                    return Err("request_timeout");
                }
                bytes = &mut bytes[count..];
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                ) =>
            {
                return Err("request_timeout");
            }
            Err(_) => return Err("invalid_request"),
        }
    }
    Ok(())
}

fn read_http(stream: &mut TcpStream) -> Result<HttpRequest, &'static str> {
    let read_deadline = Instant::now() + Duration::from_secs(5);
    stream
        .set_write_timeout(Some(Duration::from_secs(30)))
        .map_err(|_| "invalid_connection")?;
    let mut header = Vec::new();
    loop {
        let mut byte = [0];
        read_exact_deadline(stream, &mut byte, read_deadline)?;
        header.push(byte[0]);
        if header.ends_with(b"\r\n\r\n") {
            break;
        }
        if header.len() > 16384 {
            return Err("request_too_large");
        }
    }
    let header = std::str::from_utf8(&header).map_err(|_| "invalid_request")?;
    let mut lines = header.split("\r\n");
    let start: Vec<_> = lines.next().ok_or("invalid_request")?.split(' ').collect();
    if start.len() != 3 || start[2] != "HTTP/1.1" {
        return Err("invalid_request");
    }
    let mut headers = Vec::<(String, String)>::new();
    for line in lines.filter(|line| !line.is_empty()) {
        let (name, value) = line.split_once(':').ok_or("invalid_request")?;
        let name = name.to_ascii_lowercase();
        if name.is_empty()
            || !name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
            || headers.iter().any(|(existing, _)| existing == &name)
        {
            return Err("invalid_request");
        }
        headers.push((name, value.trim().to_owned()));
    }
    if headers.iter().any(|(name, _)| name == "transfer-encoding") {
        return Err("invalid_request");
    }
    let length = headers
        .iter()
        .find(|(name, _)| name == "content-length")
        .map(|(_, value)| value.parse::<usize>().map_err(|_| "invalid_request"))
        .transpose()?
        .unwrap_or(0);
    if length > 32768 {
        return Err("request_too_large");
    }
    Ok(HttpRequest {
        method: start[0].to_owned(),
        path: start[1].to_owned(),
        headers,
        body: Vec::new(),
        body_length: length,
        read_deadline,
    })
}

fn http_reply(mut stream: TcpStream, status: u16, body: Value) {
    let body = match serde_json::to_vec(&body) {
        Ok(body) => body,
        Err(_) => return,
    };
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        429 => "Too Many Requests",
        _ => "Internal Server Error",
    };
    let header = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n",
        body.len()
    );
    let _ = stream
        .write_all(header.as_bytes())
        .and_then(|_| stream.write_all(&body));
}

fn authorized(request: &HttpRequest, config: &HostConfig) -> Result<(), u16> {
    let header = |name: &str| {
        request
            .headers
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    };
    let allowed = [
        format!("127.0.0.1:{}", config.endpoint.port),
        format!("localhost:{}", config.endpoint.port),
    ];
    if !header("host").is_some_and(|host| allowed.iter().any(|allowed| allowed == host)) {
        return Err(403);
    }
    if header("origin").is_some_and(|origin| {
        !allowed
            .iter()
            .any(|host| origin == format!("http://{host}"))
    }) {
        return Err(403);
    }
    let expected = format!("Bearer {}", config.api_key);
    let actual = header("authorization").unwrap_or("");
    let mut difference = actual.len() ^ expected.len();
    for (index, expected) in expected.bytes().enumerate() {
        difference |= usize::from(expected ^ actual.as_bytes().get(index).copied().unwrap_or(0));
    }
    if difference != 0 {
        return Err(401);
    }
    Ok(())
}

fn run_server(descriptor: i32) -> Result<(), &'static str> {
    if descriptor < 3 {
        return Err("invalid_descriptor");
    }
    use std::os::fd::FromRawFd;
    let mut config_bytes = Vec::new();
    // Ownership of this descriptor is inherited from the host and ends here.
    unsafe { File::from_raw_fd(descriptor) }
        .take(65537)
        .read_to_end(&mut config_bytes)
        .map_err(|_| "invalid_descriptor")?;
    if config_bytes.len() > 65536 {
        return Err("invalid_descriptor");
    }
    let config: HostConfig =
        serde_json::from_slice(&config_bytes).map_err(|_| "invalid_descriptor")?;
    if config.endpoint.host != "127.0.0.1"
        || config.endpoint.port == 0
        || config.api_key.len() < 16
        || config.nonce.is_empty()
        || config.model.context_tokens != 4096
        || !config.model.engine_identity.is_object()
        || config.model.artifact.is_absolute()
        || config
            .model
            .artifact
            .components()
            .any(|part| !matches!(part, std::path::Component::Normal(_)))
        || config
            .model
            .artifact
            .file_name()
            .and_then(|part| part.to_str())
            != Some("model.safetensors")
    {
        return Err("invalid_descriptor");
    }
    let root = std::env::current_dir().map_err(|_| "model_path_unavailable")?;
    let path = root
        .join(&config.model.artifact)
        .parent()
        .ok_or("model_path_unavailable")?
        .to_path_buf();
    let worker = Worker::load(&path)?;
    let listener = TcpListener::bind((config.endpoint.host.as_str(), config.endpoint.port))
        .map_err(|_| "endpoint_unavailable")?;
    let (sender, receiver) = sync_channel::<(TcpStream, Request)>(4);
    let accept_config = config.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else {
                break;
            };
            let mut request = match read_http(&mut stream) {
                Ok(request) => request,
                Err(code) => {
                    http_reply(stream, 400, json!({"error": code}));
                    continue;
                }
            };
            if let Err(status) = authorized(&request, &accept_config) {
                http_reply(stream, status, json!({"error": "request_rejected"}));
                continue;
            }
            // Read the request body only after Host, Origin, and credentials pass.
            request.body.resize(request.body_length, 0);
            if read_exact_deadline(&mut stream, &mut request.body, request.read_deadline).is_err() {
                http_reply(stream, 400, json!({"error": "invalid_request"}));
                continue;
            }
            if request.method == "GET" && matches!(request.path.as_str(), "/health" | "/props") {
                http_reply(
                    stream,
                    200,
                    json!({"ready": true, "generation": accept_config.generation,
                    "nonce": accept_config.nonce, "context_tokens": 4096, "profile": PROFILE,
                    "engine_identity": accept_config.model.engine_identity}),
                );
                continue;
            }
            if request.method != "POST" || request.path != "/generate" {
                http_reply(stream, 404, json!({"error": "route_not_found"}));
                continue;
            }
            let request = match serde_json::from_slice::<Request>(&request.body) {
                Ok(request)
                    if !request.text.is_empty()
                        && request.text.chars().count() <= 8192
                        && request.text.len() <= 16384
                        && request.max_output_tokens > 0
                        && request.max_output_tokens <= 256
                        && request.context_tokens == 4096 =>
                {
                    request
                }
                _ => {
                    http_reply(stream, 400, json!({"error": "invalid_request"}));
                    continue;
                }
            };
            match sender.try_send((stream, request)) {
                Ok(()) => {}
                Err(TrySendError::Full((stream, _))) => {
                    http_reply(stream, 429, json!({"error": "queue_full"}))
                }
                Err(TrySendError::Disconnected((stream, _))) => {
                    http_reply(stream, 500, json!({"error": "engine_unavailable"}));
                    break;
                }
            }
        }
    });
    for (stream, request) in receiver {
        // The native stream finishes GPU work even if the client disconnects.
        // State is fresh and never made available to another request mid-flight.
        match worker.generate(request) {
            Ok(result) => {
                let prefill_tps =
                    result.prompt_tokens as f64 / result.prefill_seconds.max(f64::MIN_POSITIVE);
                let decode_tps = result.completion_tokens.saturating_sub(1) as f64
                    / result.decode_seconds.max(f64::MIN_POSITIVE);
                http_reply(
                    stream,
                    200,
                    json!({"text": result.text, "input_tokens": result.prompt_tokens,
                    "output_tokens": result.completion_tokens, "stop_reason": result.stop_reason,
                    "metrics": {"ttft_seconds": result.ttft_seconds, "prefill_seconds": result.prefill_seconds,
                        "decode_seconds": result.decode_seconds, "total_seconds": result.total_seconds,
                        "prefill_tps": prefill_tps, "decode_tps": decode_tps,
                        "prompt_sha256": result.prompt_sha256, "prompt_tokens_sha256": result.prompt_tokens_sha256},
                    "peak_memory_bytes": result.peak_memory_bytes}),
                );
            }
            Err("context_overflow") => http_reply(stream, 400, json!({"error": "context_limit"})),
            Err(code) => {
                http_reply(stream, 500, json!({"error": code}));
                // Any native failure after dispatch fences the whole process.
                // The host must reap it before another instance can own the slot.
                return Err("native_operation_failed");
            }
        }
    }
    Ok(())
}

fn run() -> Result<(), &'static str> {
    let mut args = std::env::args_os().skip(1);
    let mode = args.next().ok_or("argument_required")?;
    let value = args.next().ok_or("argument_required")?;
    if args.next().is_some() {
        return Err("unexpected_argument");
    }
    if mode == "--model-path" {
        run_lines(PathBuf::from(value))
    } else if mode == "--score-model-path" {
        scoring::run_lines(&PathBuf::from(value))
    } else if mode == "--meecp-config-fd" || mode == "--mcpme-config-fd" {
        run_server(
            value
                .to_str()
                .ok_or("invalid_descriptor")?
                .parse()
                .map_err(|_| "invalid_descriptor")?,
        )
    } else {
        Err("unknown_argument")
    }
}

fn main() {
    // The engine's panics and Metal diagnostics must never expose request data.
    std::panic::set_hook(Box::new(|_| {}));
    let stderr = fs::OpenOptions::new().write(true).open("/dev/null");
    if let Ok(stderr) = stderr {
        use std::os::fd::AsRawFd;
        unsafe {
            libc::dup2(stderr.as_raw_fd(), libc::STDERR_FILENO);
        }
    }
    // Capture is opt-in upstream; a request worker must not write prompt state.
    unsafe {
        std::env::remove_var("UZU_CAPTURE_FIRST_PREFILL");
        std::env::remove_var("UZU_CAPTURE_FIRST_DECODE");
    }
    if let Err(code) = run() {
        if std::env::args_os()
            .nth(1)
            .is_some_and(|mode| mode == "--model-path" || mode == "--score-model-path")
        {
            let _ = write_reply(
                &mut io::stdout().lock(),
                &json!({"ready": false, "error": code}),
            );
        }
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> HostConfig {
        HostConfig {
            endpoint: Endpoint {
                host: "127.0.0.1".to_owned(),
                port: 8123,
            },
            api_key: "test-only-credential".to_owned(),
            generation: 1,
            nonce: "test".to_owned(),
            model: ModelDeclaration {
                artifact: PathBuf::from("models/uzu/model.safetensors"),
                context_tokens: 4096,
                engine_identity: json!({"runtime": "uzu"}),
            },
        }
    }

    fn request(headers: &[(&str, &str)]) -> HttpRequest {
        HttpRequest {
            method: "GET".to_owned(),
            path: "/health".to_owned(),
            body: Vec::new(),
            body_length: 0,
            read_deadline: Instant::now() + Duration::from_secs(5),
            headers: headers
                .iter()
                .map(|(name, value)| (name.to_string(), value.to_string()))
                .collect(),
        }
    }

    #[test]
    fn every_route_requires_authentication_and_loopback_authority() {
        let config = config();
        assert_eq!(
            authorized(&request(&[("host", "127.0.0.1:8123")]), &config),
            Err(401)
        );
        assert_eq!(
            authorized(
                &request(&[
                    ("host", "127.0.0.1:8123"),
                    ("authorization", "Bearer test-only-credential")
                ]),
                &config
            ),
            Ok(())
        );
        assert_eq!(
            authorized(
                &request(&[
                    ("host", "127.0.0.1:8124"),
                    ("authorization", "Bearer test-only-credential")
                ]),
                &config
            ),
            Err(403)
        );
        assert_eq!(
            authorized(
                &request(&[
                    ("host", "127.0.0.1:8123"),
                    ("authorization", "Bearer test-only-credential"),
                    ("origin", "https://example.com")
                ]),
                &config
            ),
            Err(403)
        );
        assert_eq!(
            authorized(
                &request(&[
                    ("host", "localhost:8123"),
                    ("authorization", "Bearer test-only-credential"),
                    ("origin", "http://localhost:8123")
                ]),
                &config
            ),
            Ok(())
        );
    }

    fn parse_http(bytes: &'static [u8]) -> Result<HttpRequest, &'static str> {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let writer = std::thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            stream.write_all(bytes).unwrap();
        });
        let (mut stream, _) = listener.accept().unwrap();
        let result = read_http(&mut stream);
        writer.join().unwrap();
        result
    }

    #[test]
    fn framing_rejects_duplicate_security_headers_and_oversized_body() {
        assert!(matches!(
            parse_http(
                b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:8123\r\nHost: example.com\r\n\r\n"
            ),
            Err("invalid_request")
        ));
        assert!(matches!(
            parse_http(
                b"POST /generate HTTP/1.1\r\nHost: 127.0.0.1:8123\r\nContent-Length: 32769\r\n\r\n"
            ),
            Err("request_too_large")
        ));
        assert!(matches!(parse_http(b"POST /generate HTTP/1.1\r\nHost: 127.0.0.1:8123\r\nTransfer-Encoding: chunked\r\n\r\n"),
            Err("invalid_request")));
    }

    #[test]
    fn fragmented_reads_cannot_extend_the_absolute_deadline() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let writer = std::thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            for _ in 0..8 {
                if stream.write_all(b"x").is_err() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        });
        let (mut stream, _) = listener.accept().unwrap();
        let deadline = Instant::now() + Duration::from_millis(50);
        let mut bytes = [0; 8];
        assert_eq!(
            read_exact_deadline(&mut stream, &mut bytes, deadline),
            Err("request_timeout")
        );
        drop(stream);
        writer.join().unwrap();
    }
}
