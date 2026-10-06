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

async function mockProvider(response) {
  const requests = [];
  const server = createServer(async (request, reply) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    requests.push({ body: JSON.parse(body), authorization: request.headers.authorization });
    response(reply, requests.length);
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

async function workerRun(start, toolHandler, { cancelOnTool = false } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "mcpme-pi-test-"));
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
    assert.ok(!result.stdout.includes("Private assistant response"));
    for (const request of provider.requests) {
      assert.equal(request.authorization, `Bearer ${SECRET}`);
      assert.deepEqual(request.body.tools.map((tool) => tool.function.name).sort(),
        ["check_package", "fetch_url", "install_dependencies", "read_file", "run_command", "write_file"]);
      assert.ok(!JSON.stringify(request.body).includes("discovered-context-sentinel"));
      assert.ok(!JSON.stringify(request.body).includes("discovered-extension-sentinel"));
      assert.ok(!JSON.stringify(request.body).includes("inherited-key-sentinel"));
    }
    assert.deepEqual(result.workspaceFiles.sort(), [".pi", "AGENTS.md"]);
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
    assert.equal(provider.requests.length, 4);
    assert.deepEqual(result.frames.at(-1), { type: "failed", code: "package_invalid" });
    assert.equal(result.stderr, "");
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
    assert.equal(provider.requests[0].authorization, "Bearer mcpme-no-api-key");
    assert.equal(result.stderr, "");
  } finally { await provider.close(); }
});
