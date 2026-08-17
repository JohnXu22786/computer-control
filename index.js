// dsh Cordis bundle bridge for computer-control.
//
// The plugin's real work is done by the Python package (`python -m
// computer_control serve`, a line-delimited JSON-RPC 2.0 server over stdio).
// This module is the thin adapter a dsh profile loads: it spawns that server
// as a child process, exposes every tool declared in manifest.json as a dsh
// tool, and forwards high-risk confirmation outcomes via a session.confirm
// helper tool.
//
// Nothing here re-implements capture/injection logic — it only wires the
// protocol the Python side already speaks. The bridge needs `python` with the
// `computer_control` package importable (see README "Installation").
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'dsh-computer-control';

/** Require the dsh tools service; without it there is nothing to bind. */
export const inject = ['tools'];

const __dirname = dirname(fileURLToPath(import.meta.url));

const PYTHON = process.env.COMPUTER_CONTROL_PYTHON || 'python';

/** manifest.json types -> JSON-schema types (null degrades to string). */
const TYPE_MAP = {
  str: 'string',
  int: 'integer',
  float: 'number',
  number: 'number',
  bool: 'boolean',
  list: 'array',
  region: 'object',
};

function loadManifest() {
  try {
    return JSON.parse(readFileSync(join(__dirname, 'manifest.json'), 'utf8'));
  } catch {
    return null;
  }
}

function paramSchema(param) {
  const schema = { type: TYPE_MAP[param.type] ?? 'string' };
  if (param.description) schema.description = param.description;
  if (param.default !== undefined && param.default !== null) schema.default = param.default;
  if (param.minimum !== undefined) schema.minimum = param.minimum;
  if (param.maximum !== undefined) schema.maximum = param.maximum;
  if (param.choices) schema.enum = param.choices;
  if (param.type === 'region') schema.additionalProperties = true;
  return schema;
}

function toolSchema(tool) {
  const properties = {};
  const required = [];
  for (const param of tool.parameters || []) {
    properties[param.name] = paramSchema(param);
    if (param.required) required.push(param.name);
  }
  const schema = { type: 'object', properties };
  if (required.length) schema.required = required;
  return schema;
}

/**
 * Line-delimited JSON-RPC 2.0 client for `python -m computer_control serve`.
 * The server is spawned lazily on the first tool call so an unavailable
 * Python runtime fails with a readable error instead of at plugin load.
 */
function createClient(config, log) {
  let proc = null;
  let spawned = null;
  let buffer = '';
  let counter = 0;
  let sessionToken = null;
  const pending = new Map();

  function ensureSpawned() {
    if (spawned) return spawned;
    spawned = new Promise((resolve, reject) => {
      const child = spawn(PYTHON, ['-m', 'computer_control', 'serve'], {
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      proc = child;
      child.once('spawn', resolve);
      child.once('error', reject);
      child.on('error', (err) => {
        for (const waiter of pending.values()) waiter.reject(err);
        pending.clear();
      });
      child.on('exit', () => {
        for (const waiter of pending.values())
          waiter.reject(new Error('computer-control server exited; is `python -m computer_control serve` runnable?'));
        pending.clear();
      });
      // The child's pipe streams emit 'error' independently of the child
      // process events; an abrupt server death (EPIPE on a late write) would
      // otherwise surface as an unhandled stream error and crash the host.
      child.stdin.on('error', () => {});
      child.stdout.on('error', () => {});
      child.stderr.on('error', () => {});
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => {
        buffer += chunk;
        drain();
      });
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (chunk) => {
        for (const line of chunk.trim().split('\n')) if (line) log('info', line);
      });
    });
    return spawned;
  }

  function drain() {
    let nl;
    while ((nl = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        continue; // partial or foreign output on stdout; ignore
      }
      if (message && message.method === 'event' && typeof onEvent === 'function') {
        onEvent(message.params);
        continue;
      }
      if (message && message.id !== undefined && pending.has(message.id)) {
        const waiter = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) waiter.reject(new Error(formatRpcError(message.error)));
        else waiter.resolve(message.result);
      }
    }
  }

  /**
   * Open a session lazily (once). The row's config becomes the base overlay
   * for session.start, so a profile can pin safety/format via the patch file.
   */
  function ensureSession(config) {
    if (sessionToken) return sessionToken;
    sessionToken = request('session.start', { config })
      .then((envelope) => {
        if (!envelope || envelope.ok !== true) {
          sessionToken = null;
          throw new Error((envelope && envelope.error && envelope.error.message) || 'session.start failed');
        }
        return envelope;
      });
    return sessionToken;
  }

  function request(method, params) {
    return ensureSpawned().then(() => {
      const id = ++counter;
      const payload = { jsonrpc: '2.0', id, method, params: params ?? {} };
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        proc.stdin.write(JSON.stringify(payload) + '\n');
      });
    });
  }

  async function call(method, params) {
    try {
      await ensureSession(config);
      const envelope = await request(method, params);
      if (!envelope || envelope.ok !== true) {
        return {
          ok: false,
          error: (envelope && envelope.error) || { code: 'server_error', message: 'no envelope from server' },
        };
      }
      return { ok: true, result: envelope.result, meta: envelope.meta };
    } catch (err) {
      return { ok: false, error: { code: 'bridge_error', message: err instanceof Error ? err.message : String(err) } };
    }
  }

  async function teardown() {
    if (!proc) return;
    try {
      await Promise.race([request('session.stop', {}), new Promise((r) => setTimeout(r, 2000))]);
    } catch {
      /* best effort */
    }
    proc.kill();
  }

  let onEvent = null;

  return {
    call,
    setOnEvent(fn) {
      onEvent = fn;
    },
    teardown,
  };
}

function formatRpcError(error) {
  return error && error.message ? `jsonrpc error: ${error.message}` : `jsonrpc error: ${JSON.stringify(error)}`;
}

function makeLogger(ctx) {
  const log = (level, msg) => {
    if (ctx.logger && typeof ctx.logger[level] === 'function') ctx.logger[level](`[computer-control] ${msg}`);
    else if (level === 'error') console.error(`[computer-control] ${msg}`);
    else console.log(`[computer-control] ${msg}`);
  };
  return log;
}

export function apply(ctx, rowConfig = {}) {
  const disposers = [];
  const config = rowConfig && typeof rowConfig === 'object' ? rowConfig : {};
  const log = makeLogger(ctx);
  const client = createClient(config, log);

  if (ctx.emit && typeof ctx.emit === 'function') {
    client.setOnEvent(({ type, payload }) => {
      // Forward the server's lifecycle/safety events into the harness.
      ctx.emit(type, payload ?? {});
    });
  }

  const manifest = loadManifest();
  const tools = manifest && Array.isArray(manifest.tools) ? manifest.tools : [];

  const run = (toolName, arguments_) => client.call('tools.call', { tool: toolName, arguments: arguments_ ?? {} });

  for (const tool of tools) {
    if (!tool || typeof tool.name !== 'string') continue;
    const def = {
      name: tool.name,
      description: tool.summary || `computer-control tool: ${tool.name}`,
      parameters: toolSchema(tool),
      output: { schema: { type: 'object', additionalProperties: true } },
      execute: (args) => run(tool.name, args),
    };
    const ret = ctx.tools && ctx.tools.register ? ctx.tools.register(def) : null;
    if (typeof ret === 'function') disposers.push(ret);
  }

  ctx.tools &&
    ctx.tools.register &&
    disposers.push(
      ctx.tools.register({
        name: 'session.confirm',
        description:
          'Resolve a pending high-risk confirmation surfaced by another tool result (status: "awaiting_confirmation"). Approve to run the action, deny to cancel it.',
        parameters: {
          type: 'object',
          properties: {
            request_id: { type: 'string', description: 'The request_id from the awaiting_confirmation result.' },
            approve: { type: 'boolean', description: 'true approves and runs the action; false denies it.' },
          },
          required: ['request_id', 'approve'],
        },
        output: { schema: { type: 'object', additionalProperties: true } },
        execute: (args) => client.call('session.confirm', args ?? {}),
      }),
    );

  if (!tools.length) log('warn', 'manifest.json missing or empty — no tools registered');

  const dispose = () => {
    for (const fn of disposers) {
      try {
        fn();
      } catch {
        /* unregister must not throw */
      }
    }
    void client.teardown();
  };

  if (ctx.effect && typeof ctx.effect === 'function') ctx.effect(dispose);
  return dispose;
}