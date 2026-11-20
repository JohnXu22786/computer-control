import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';

async function waitForFile(path) {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    try {
      readFileSync(path);
      return;
    } catch {
      await delay(10);
    }
  }
  throw new Error(`timed out waiting for ${path}`);
}

async function callWithTimeout(tool, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      tool.execute({}),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`tool call timed out after ${timeoutMs} ms`)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

test('a request after server exit starts a fresh session and child process', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'computer-control-bridge-'));
  const executable = join(directory, 'fake-python.mjs');
  const eventsPath = join(directory, 'events.jsonl');
  const exitPath = join(directory, 'first-child-exited');
  writeFileSync(executable, `#!/usr/bin/env node
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
const countPath = ${JSON.stringify(join(directory, 'spawn-count'))};
const eventsPath = ${JSON.stringify(eventsPath)};
const exitPath = ${JSON.stringify(exitPath)};
let count = 0;
try { count = Number(readFileSync(countPath, 'utf8')); } catch {}
count += 1;
writeFileSync(countPath, String(count));
let buffer = '';
let respondedToToolCall = false;
process.on('exit', () => {
  if (count === 1 && respondedToToolCall) writeFileSync(exitPath, 'exited');
});
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf('\\n')) !== -1) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    const message = JSON.parse(line);
    appendFileSync(eventsPath, JSON.stringify({ count, method: message.method }) + '\\n');
    const result = message.method === 'session.start'
      ? { ok: true, session_id: 'fake-session' }
      : { ok: true, result: { generation: count }, meta: {} };
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }) + '\\n');
    if (count === 1 && message.method === 'tools.call') {
      respondedToToolCall = true;
      setTimeout(() => process.exit(0), 10);
    }
  }
});
`);
  chmodSync(executable, 0o700);

  const previousPython = process.env.COMPUTER_CONTROL_PYTHON;
  process.env.COMPUTER_CONTROL_PYTHON = executable;
  let dispose;
  t.after(async () => {
    dispose?.();
    await delay(50);
    if (previousPython === undefined) delete process.env.COMPUTER_CONTROL_PYTHON;
    else process.env.COMPUTER_CONTROL_PYTHON = previousPython;
    rmSync(directory, { recursive: true, force: true });
  });

  const { apply } = await import('../index.js');
  const definitions = new Map();
  apply({
    tools: { register(definition) { definitions.set(definition.name, definition); return () => {}; } },
    effect(callback) { dispose = callback; },
  });
  const tool = definitions.values().next().value;
  assert.ok(tool, 'manifest tools are registered');

  const first = await tool.execute({});
  assert.deepEqual(first.result, { generation: 1 });
  await waitForFile(exitPath);

  const afterExit = await callWithTimeout(tool, 1000);
  let second = afterExit;
  if (!afterExit.ok) {
    assert.match(afterExit.error.message, /computer-control server exited/);
    second = await callWithTimeout(tool, 1000);
  }
  assert.equal(second.ok, true, JSON.stringify(second));
  assert.deepEqual(second.result, { generation: 2 });
  assert.deepEqual(
    readFileSync(eventsPath, 'utf8').trim().split('\n').map((line) => JSON.parse(line)),
    [
      { count: 1, method: 'session.start' },
      { count: 1, method: 'tools.call' },
      { count: 2, method: 'session.start' },
      { count: 2, method: 'tools.call' },
    ],
  );
});

test('a request retries after the initial child process fails to spawn', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'computer-control-bridge-'));
  const executable = join(directory, 'python-that-will-appear');
  const eventsPath = join(directory, 'events.jsonl');
  const previousPython = process.env.COMPUTER_CONTROL_PYTHON;
  process.env.COMPUTER_CONTROL_PYTHON = executable;
  let dispose;
  t.after(async () => {
    dispose?.();
    await delay(50);
    if (previousPython === undefined) delete process.env.COMPUTER_CONTROL_PYTHON;
    else process.env.COMPUTER_CONTROL_PYTHON = previousPython;
    rmSync(directory, { recursive: true, force: true });
  });

  const { apply } = await import('../index.js?spawn-retry');
  const definitions = new Map();
  apply({
    tools: { register(definition) { definitions.set(definition.name, definition); return () => {}; } },
    effect(callback) { dispose = callback; },
  });
  const tool = definitions.values().next().value;
  assert.ok(tool, 'manifest tools are registered');

  const firstResults = await Promise.all([
    callWithTimeout(tool, 1000),
    callWithTimeout(tool, 1000),
  ]);
  for (const first of firstResults) {
    assert.equal(first.ok, false);
    assert.equal(first.error.code, 'bridge_error');
    assert.match(first.error.message, /ENOENT/);
  }

  writeFileSync(executable, `#!/usr/bin/env node
import { appendFileSync } from 'node:fs';
const eventsPath = ${JSON.stringify(eventsPath)};
let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf('\\n')) !== -1) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    const message = JSON.parse(line);
    appendFileSync(eventsPath, JSON.stringify({ method: message.method }) + '\\n');
    const result = message.method === 'session.start'
      ? { ok: true, session_id: 'fake-session' }
      : { ok: true, result: { generation: 1 }, meta: {} };
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }) + '\\n');
  }
});
`);
  chmodSync(executable, 0o700);

  const retry = await callWithTimeout(tool, 1000);
  assert.equal(retry.ok, true, JSON.stringify(retry));
  assert.deepEqual(retry.result, { generation: 1 });
  assert.deepEqual(
    readFileSync(eventsPath, 'utf8').trim().split('\n').map((line) => JSON.parse(line)),
    [{ method: 'session.start' }, { method: 'tools.call' }],
  );
});
