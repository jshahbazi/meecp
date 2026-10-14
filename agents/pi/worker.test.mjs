import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm, writeFile, mkdir, readdir } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

const WORKER = resolve(process.env.MCPME_PI_TEST_WORKER ?? new URL("worker.mjs", import.meta.url).pathname);
const SECRET = "synthetic-private-value-never-emitted";
const TOOLS = ["ask_user", "capabilities", "check_package", "ensure_metal_toolchain", "extract_archive", "fetch_url",
  "install_dependencies", "list_files", "prepare_environment", "read_file", "read_process",
  "run_command", "start_process", "stop_process", "write_file", "native_command", "preflight", "retain_resource"].sort();

async function mockProvider(response) {
  const requests = [];
  const server = createServer(async (request, reply) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    requests.push({ body: JSON.parse(body), authorization: request.headers.authorization });
    response(reply, requests.length, requests.at(-1).body);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { requests, url: `http://127.0.0.1:${server.address().port}/v1`,
    close: () => new Promise((done) => server.close(done)) };
}

function completion(reply, delta, finishReason) {
  reply.writeHead(200, { "Content-Type": "text/event-stream" });
  reply.write(`data: ${JSON.stringify({ id: "mock-response", object: "chat.completion.chunk", created: 1,
    model: "test-model", choices: [{ index: 0, delta: { role: "assistant", ...delta }, finish_reason: null }] })}\n\n`);
  reply.write(`data: ${JSON.stringify({ id: "mock-response", object: "chat.completion.chunk", created: 1,
    model: "test-model", choices: [{ index: 0, delta: {}, finish_reason: finishReason }] })}\n\n`);
  reply.end("data: [DONE]\n\n");
}

async function workerRun(start, toolHandler, { cancelOnTool = false, onFrame } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "meecp-pi-test-"));
  await writeFile(join(directory, "AGENTS.md"), "Untrusted sentinel: DO NOT include discovered-context-sentinel");
  await mkdir(join(directory, ".pi/extensions"), { recursive: true });
  await writeFile(join(directory, ".pi/extensions/trap.js"),
    "throw new Error('discovered-extension-sentinel');");
  const child = spawn(process.execPath, [WORKER], {
    cwd: directory, stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, OPENAI_API_KEY: "inherited-key-sentinel", PI_DEBUG: "1" },
  });
  let stdout = "";
  let stderr = "";
  let buffered = "";
  const frames = [];
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    buffered += chunk;
    while (buffered.includes("\n")) {
      const position = buffered.indexOf("\n");
      const frame = JSON.parse(buffered.slice(0, position));
      buffered = buffered.slice(position + 1);
      frames.push(frame);
      onFrame?.(frame, child);
      if (frame.type === "tool_call") {
        if (cancelOnTool) { child.stdin.end(); continue; }
        const result = toolHandler?.(frame) ?? { success: true, output: "accepted" };
        child.stdin.write(JSON.stringify({ type: "tool_result", id: frame.id, ...result }) + "\n");
      }
    }
  });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdin.write(JSON.stringify(start) + "\n");
  const timeout = setTimeout(() => child.kill("SIGKILL"), 15000);
  const [code] = await once(child, "exit");
  clearTimeout(timeout);
  assert.equal(code, 0, "worker must exit after a terminal frame");
  const workspaceFiles = await readdir(directory);
  await rm(directory, { recursive: true, force: true });
  return { frames, stdout, stderr, workspaceFiles };
}

const start = (url) => ({ type: "start", provider: "openai-compatible", model: "test-model",
  api_key: SECRET, base_url: url, prompt: `Prepare an app. Private input: ${SECRET}` });

test("real Pi SDK completes a brokered write/build loop without discovery or transcript logging", async () => {
  const provider = await mockProvider((reply, round) => {
    if (round === 1) completion(reply, { tool_calls: [{ index: 0, id: "provider-tool-1", type: "function",
      function: { name: "write_file", arguments: JSON.stringify({ path: "app.js", content: "console.log('ready');" }) } }] }, "tool_calls");
    else if (round === 2) completion(reply, { tool_calls: [{ index: 0, id: "provider-tool-2", type: "function",
      function: { name: "run_command", arguments: JSON.stringify({ argv: ["node", "app.js"], timeout_seconds: 10 }) } }] }, "tool_calls");
    else completion(reply, { content: `Finished. Private assistant response: ${SECRET}` }, "stop");
  });
  try {
    const result = await workerRun(start(provider.url), (frame) => ({ success: true,
      output: frame.name === "run_command" ? "ready" : "written" }));
    assert.equal(provider.requests.length, 3);
    assert.deepEqual(result.frames.filter((frame) => frame.type === "tool_call").map((frame) => frame.name),
      ["write_file", "run_command", "check_package"]);
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
    assert.equal(result.stderr, "");
    assert.ok(!result.stdout.includes(SECRET));
    assert.equal(result.frames.filter((frame) => frame.type === "assistant_text").length, 1);
    for (const request of provider.requests) {
      assert.equal(request.authorization, `Bearer ${SECRET}`);
      assert.deepEqual(request.body.tools.map((tool) => tool.function.name).sort(),
        TOOLS);
      assert.ok(!JSON.stringify(request.body).includes("discovered-context-sentinel"));
      assert.ok(!JSON.stringify(request.body).includes("discovered-extension-sentinel"));
      assert.ok(!JSON.stringify(request.body).includes("inherited-key-sentinel"));
    }
    assert.deepEqual(result.workspaceFiles.sort(), [".pi", "AGENTS.md"]);
  } finally { await provider.close(); }
});

test("real SDK accepts live steering and emits only bounded assistant text", async () => {
  const instruction = "Keep the requested native inference engine.";
  const provider = await mockProvider((reply, round) => {
    if (round === 1) completion(reply, { tool_calls: [{ index: 0, id: "steering-tool", type: "function",
      function: { name: "capabilities", arguments: "{}" } }] }, "tool_calls");
    else completion(reply, { content: "I will keep that engine and verify it." }, "stop");
  });
  let sent = false;
  try {
    const result = await workerRun(start(provider.url), undefined, { onFrame(frame, child) {
      if (!sent && frame.type === "tool_call" && frame.name === "capabilities") {
        sent = true;
        child.stdin.write(JSON.stringify({ type: "user_message",
          id: "12345678-1234-1234-1234-123456789abc", text: instruction }) + "\n");
      }
    } });
    assert.ok(sent);
    assert.ok(provider.requests.slice(1).some((request) => request.body.messages.some((message) =>
      message.role === "user" && JSON.stringify(message.content).includes(instruction))));
    assert.equal(result.frames.filter((frame) => frame.type === "message_accepted").length, 1);
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
    const assistant = result.frames.filter((frame) => frame.type === "assistant_text");
    assert.ok(assistant.length > 0 && assistant.every((frame) => Object.keys(frame).sort().join(",") === "text,type"));
    assert.equal(result.stderr, "");
    assert.ok(!result.stdout.includes(SECRET));
    assert.deepEqual(result.workspaceFiles.sort(), [".pi", "AGENTS.md"]);
  } finally { await provider.close(); }
});

test("steering arriving during final host verification is handled before done", async () => {
  const instruction = "Include an additional output field.";
  const provider = await mockProvider((reply) => completion(reply, { content: "The package is prepared." }, "stop"));
  let sent = false;
  try {
    const result = await workerRun(start(provider.url), undefined, { onFrame(frame, child) {
      if (!sent && frame.type === "tool_call" && frame.name === "check_package") {
        sent = true;
        child.stdin.write(JSON.stringify({ type: "user_message",
          id: "12345678-1234-1234-1234-123456789abc", text: instruction }) + "\n");
      }
    } });
    assert.ok(provider.requests.slice(1).some((request) => request.body.messages.some((message) =>
      message.role === "user" && JSON.stringify(message.content).includes(instruction))));
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
    assert.equal(result.stderr, "");
  } finally { await provider.close(); }
});

test("startup messages enter the SDK and thinking is excluded from live text", async () => {
  const instruction = "Preserve the selected repository.";
  const hiddenThought = "hidden-reasoning-sentinel";
  const provider = await mockProvider((reply) => completion(reply,
    { content: "é".repeat(5000), reasoning_content: hiddenThought }, "stop"));
  let sent = false;
  try {
    const result = await workerRun(start(provider.url), undefined, { onFrame(frame, child) {
      if (!sent && frame.type === "progress" && frame.stage === "starting") {
        sent = true;
        child.stdin.write(JSON.stringify({ type: "user_message",
          id: "12345678-1234-1234-1234-123456789abc", text: instruction }) + "\n");
      }
    } });
    assert.ok(provider.requests.some((request) => request.body.messages.some((message) =>
      message.role === "user" && JSON.stringify(message.content).includes(instruction))));
    assert.ok(result.frames.filter((frame) => frame.type === "assistant_text")
      .every((frame) => Buffer.byteLength(frame.text, "utf8") <= 8192));
    assert.ok(!result.stdout.includes(hiddenThought));
    assert.equal(result.stderr, "");
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
  } finally { await provider.close(); }
});

test("host rejection resumes the same SDK session for repair before completion", async () => {
  const provider = await mockProvider((reply) => completion(reply, { content: "Prepared" }, "stop"));
  let checks = 0;
  try {
    const result = await workerRun(start(provider.url), (frame) => {
      assert.equal(frame.name, "check_package");
      checks++;
      return { success: checks > 1, output: checks > 1 ? "package_verified" : "manifest_hash_mismatch" };
    });
    assert.equal(provider.requests.length, 2);
    assert.ok(JSON.stringify(provider.requests[1].body.messages).includes("manifest_hash_mismatch"));
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
    assert.equal(result.stderr, "");
    assert.ok(!result.stdout.includes(SECRET));
  } finally { await provider.close(); }
});

test("repeated package rejection stops after bounded repair attempts", async () => {
  const provider = await mockProvider((reply) => completion(reply, { content: "Prepared" }, "stop"));
  try {
    const result = await workerRun(start(provider.url), () => ({ success: false, output: "invalid_package_schema" }));
    assert.equal(provider.requests.length, 12);
    assert.deepEqual(result.frames.at(-1), { type: "failed", code: "package_invalid" });
    assert.equal(result.stderr, "");
  } finally { await provider.close(); }
});

test("real SDK routes general build, runtime, background-service and question tools", async () => {
  const calls = [
    ["capabilities", {}],
    ["list_files", { path: "source", recursive: true, offset: 500, limit: 500 }],
    ["read_file", { path: "source/large.txt", offset: 131072, limit: 131072 }],
    ["fetch_url", { url: "https://example.org/compiler.tar.gz", path: "compiler.tar.gz" }],
    ["extract_archive", { archive_path: "compiler.tar.gz", target_path: "compiler", strip_components: 1 }],
    ["prepare_environment", { runtime: "node", target_path: "package/runtime" }],
    ["install_dependencies", { ecosystem: "rust", project_path: "source" }],
    ["run_command", { argv: ["compiler/bin/build", "--locked", ""], cwd: "source", network: true, timeout_seconds: 1800 }],
    ["start_process", { argv: ["package/service"], cwd: "package", network: true }],
    ["read_process", { process_id: "temporary-service" }],
    ["ask_user", { kind: "text", question: "Which account should this integrate with?", options: ["Personal", "Work"] }],
    ["ask_user", { kind: "secret", question: "Supply the service credential.", credential_name: "SERVICE_API_KEY" }],
    ["run_command", { argv: ["package/check-service"], network: true, env: { TEST_MODE: "1" },
      credentials: { SERVICE_API_KEY: "opaque-secret-id" } }],
    ["start_process", { argv: ["package/private-service"], network: true,
      credentials: { SERVICE_API_KEY: "opaque-secret-id" } }],
    ["stop_process", { process_id: "temporary-service" }],
  ];
  const seen = [];
  const provider = await mockProvider((reply, round) => {
    if (round > calls.length) return completion(reply, { content: "Prepared" }, "stop");
    const [name, args] = calls[round - 1];
    completion(reply, { tool_calls: [{ index: 0, id: `general-tool-${round}`, type: "function",
      function: { name, arguments: JSON.stringify(args) } }] }, "tool_calls");
  });
  try {
    const result = await workerRun(start(provider.url), (frame) => {
      seen.push([frame.name, frame.arguments]);
      return { success: true, output: frame.name === "ask_user" && frame.arguments.kind === "secret"
        ? JSON.stringify({ kind: "secret", credential_name: "SERVICE_API_KEY", secret_id: "opaque-secret-id" }) : "accepted" };
    });
    assert.deepEqual(seen, [...calls, ["check_package", {}]]);
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
    assert.equal(result.frames.filter((frame) => frame.stage === "waiting_for_user").length, 2);
    assert.equal(result.stderr, "");
    assert.ok(!result.stdout.includes(SECRET));
    const secretReply = provider.requests.at(-1).body.messages.find((message) => message.role === "tool"
      && message.content.includes("opaque-secret-id"));
    assert.ok(secretReply, "model sees only the host's opaque credential binding");
  } finally { await provider.close(); }
});

test("long setup context compacts through the real SDK without writing a transcript", async () => {
  let summaries = 0;
  const provider = await mockProvider((reply, round, body) => {
    if (body.messages.some((message) => message.role === "system"
        && message.content.includes("You are a context summarization assistant"))) {
      summaries++;
      return completion(reply, { content: "## Goal\nBuild the requested integration.\n## Progress\nRead the large source file.\n## Next Steps\nVerify package." }, "stop");
    }
    if (round === 1) completion(reply, { tool_calls: [{ index: 0, id: "large-read", type: "function",
      function: { name: "read_file", arguments: JSON.stringify({ path: "large.txt", offset: 0, limit: 131072 }) } }] }, "tool_calls");
    else completion(reply, { content: "Prepared" }, "stop");
  });
  try {
    const result = await workerRun(start(provider.url), (frame) => ({ success: true,
      output: frame.name === "read_file" ? "source material ".repeat(8500) : "accepted" }));
    assert.ok(summaries > 0, "large context triggers a summarization request");
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
    assert.deepEqual(result.workspaceFiles.sort(), [".pi", "AGENTS.md"]);
    assert.equal(result.stderr, "");
    assert.ok(!result.stdout.includes(SECRET));
  } finally { await provider.close(); }
});

test("EOF cancels an outstanding host tool and emits only a fixed terminal code", async () => {
  const provider = await mockProvider((reply) => completion(reply, { tool_calls: [{ index: 0, id: "provider-tool-1", type: "function",
    function: { name: "read_file", arguments: JSON.stringify({ path: "package.json" }) } }] }, "tool_calls"));
  try {
    const result = await workerRun(start(provider.url), undefined, { cancelOnTool: true });
    assert.deepEqual(result.frames.at(-1), { type: "failed", code: "cancelled" });
    assert.equal(result.stderr, "");
    assert.ok(!result.stdout.includes(SECRET));
    assert.equal(provider.requests.length, 1);
  } finally { await provider.close(); }
});

test("provider error bodies and credentials are never emitted", async () => {
  const provider = await mockProvider((reply) => {
    reply.writeHead(500, { "Content-Type": "application/json" });
    reply.end(JSON.stringify({ error: { message: SECRET } }));
  });
  try {
    const result = await workerRun(start(provider.url));
    assert.deepEqual(result.frames.at(-1), { type: "failed", code: "provider_failed" });
    assert.equal(result.stderr, "");
    assert.ok(!result.stdout.includes(SECRET));
  } finally { await provider.close(); }
});

test("unknown catalog models produce a fixed unsupported-model failure", async () => {
  const result = await workerRun({ type: "start", provider: "openai", model: "unknown-test-model",
    api_key: SECRET, prompt: SECRET, base_url: null });
  assert.deepEqual(result.frames.at(-1), { type: "failed", code: "model_unsupported" });
  assert.equal(result.stderr, "");
  assert.ok(!result.stdout.includes(SECRET));
});

test("compatible local providers accept an empty key without using inherited credentials", async () => {
  const provider = await mockProvider((reply) => completion(reply, { content: "Prepared" }, "stop"));
  try {
    const result = await workerRun({ ...start(provider.url), api_key: "" });
    assert.deepEqual(result.frames.at(-1), { type: "done", success: true });
    assert.equal(provider.requests[0].authorization, "Bearer meecp-no-api-key");
    assert.equal(result.stderr, "");
  } finally { await provider.close(); }
});
