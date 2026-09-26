#!/usr/bin/env node
/*
 * laya_child.mjs — the LOCAL CORTEX Laya decision child (Phase 13, workstream A).
 *
 * A long-lived Node process speaking newline-delimited JSON over stdio. The Python
 * sidecar (localmodels/local_models_daemon.py) spawns it lazily, sends one request per
 * line and reads one JSON reply per line. Laya runs in Node because @receptron/laya is
 * ESM-only and needs onnxruntime-node; the daemon never contacts a network endpoint —
 * the ONLY network use is the model's own one-time weight download on the first load.
 *
 * Inbound  (one JSON object per line on stdin):
 *   {id, op:'health'}                    -> {id, ok:true, loaded:<bool>, pid, cache, node}
 *   {id, op:'load'}                      -> {id, ok:true, loaded:true, load_ms}
 *   {id, op:'decide', state, questions}  -> {id, ok:true, answers, usage, latency_ms}
 *   {id, op:'close'}                     -> {id, ok:true} then exit 0
 * Any other op  -> {id, ok:false, error:'unknown op: <op>'}
 * Handler error -> {id, ok:false, error:'<message>'} (the process stays alive)
 * Unparseable line -> {id:null, ok:false, error:'bad json'} (keeps reading)
 * EOF on stdin -> exit 0.
 *
 * HARD RULE: stdout carries protocol JSON and NOTHING else — the daemon parses it as
 * NDJSON. Every console channel is re-routed to stderr below, and the package is
 * imported lazily inside the load path so a missing/incomplete node_modules is reported
 * as a JSON error instead of a startup crash. Download progress goes to stderr as JSON
 * lines (`{"type":"progress", file, received, total}`).
 *
 * Load options are EXACTLY these: the package has no `precision`/`float16` option
 * (plan §1 "must verify" item 3 — do not invent one).
 */
import readline from 'node:readline';

// Re-route every console channel to stderr BEFORE anything can log: a stray line on
// stdout would corrupt the protocol.
const toStderr = (...args) => {
  const text = args
    .map((a) => (typeof a === 'string' ? a : a instanceof Error ? a.stack || a.message : JSON.stringify(a)))
    .join(' ');
  try { process.stderr.write(text + '\n'); } catch (e) { /* never crash on a log */ }
};
for (const name of ['log', 'info', 'warn', 'debug', 'trace']) console[name] = toStderr;

const DEFAULT_CACHE = '~/.cache/receptron-laya';

function emit(obj, cb) {
  process.stdout.write(JSON.stringify(obj) + '\n', cb);
}

function emitStderr(obj) {
  try { process.stderr.write(JSON.stringify(obj) + '\n'); } catch (e) { /* ignore */ }
}

let laya = null;          // the loaded Laya instance (null until the first load/decide)
let loading = null;       // in-flight load promise (so concurrent requests share one load)

async function getLaya() {
  if (laya) return laya;
  if (!loading) {
    loading = (async () => {
      // Lazy import: a missing/incomplete node_modules becomes a JSON error, not a crash.
      const mod = await import('@receptron/laya');
      const instance = await mod.Laya.load({
        cacheDir: process.env.LAYA_CACHE || undefined, // undefined => the package default
        executionProviders: ['cpu'],
        onProgress: (p) => emitStderr({
          type: 'progress',
          file: p && p.file,
          received: p && p.received,
          total: p && p.total,
        }),
      });
      laya = instance;
      return instance;
    })().catch((e) => {
      loading = null; // let a later request try again
      throw e;
    });
  }
  return loading;
}

async function handle(msg) {
  const op = msg.op;
  if (op === 'health') {
    // Never triggers a load: a status probe must not start a 1.7 GB download.
    return { id: msg.id, ok: true, loaded: laya !== null, pid: process.pid,
             cache: process.env.LAYA_CACHE || DEFAULT_CACHE, node: process.version };
  }
  if (op === 'load') {
    const t0 = Date.now();
    await getLaya();
    return { id: msg.id, ok: true, loaded: true, load_ms: Date.now() - t0 };
  }
  if (op === 'decide') {
    const instance = await getLaya();
    const t0 = Date.now();
    // The package's own envelope IS the contract (plan §4): state/questions go straight
    // through and answers/usage come back untouched.
    const result = await instance.systemOne(msg.state, msg.questions);
    return { id: msg.id, ok: true,
             answers: (result && result.answers) || {},
             usage: (result && result.usage) !== undefined ? result.usage : null,
             latency_ms: Date.now() - t0 };
  }
  if (op === 'close') {
    try { if (laya && typeof laya.close === 'function') await laya.close(); } catch (e) { /* ignore */ }
    return { id: msg.id, ok: true, _exit: true };
  }
  return { id: msg.id, ok: false, error: 'unknown op: ' + op };
}

async function processLine(line) {
  const trimmed = line.trim();
  if (!trimmed) return;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch (e) {
    emit({ id: null, ok: false, error: 'bad json' });
    return;
  }
  if (!msg || typeof msg !== 'object' || Array.isArray(msg) || typeof msg.op !== 'string') {
    emit({ id: null, ok: false, error: 'bad json' });
    return;
  }
  let out;
  try {
    out = await handle(msg);
  } catch (e) {
    // One bad request must not kill the child: report it and stay alive.
    out = { id: msg.id !== undefined ? msg.id : null, ok: false, error: String((e && e.message) || e) };
  }
  const shouldExit = out._exit === true;
  delete out._exit;
  if (shouldExit) emit(out, () => process.exit(0));
  else emit(out);
}

// One request at a time, in arrival order (the package keeps ONE instance and its
// systemOne call is not documented as re-entrant).
let queue = Promise.resolve();
const enqueue = (fn) => (queue = queue.then(fn, fn));

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => enqueue(() => processLine(line)));
// EOF on stdin => exit 0, but let an in-flight request land first (a caller that pipes a
// request and closes the pipe must still get its answer). Bounded, so a wedged load can
// never keep the process alive.
rl.on('close', () => {
  const timer = setTimeout(() => process.exit(0), 1000);
  const done = () => { clearTimeout(timer); process.exit(0); };
  queue.then(done, done);
});
process.on('SIGTERM', () => process.exit(0));
process.on('SIGINT', () => process.exit(0));
process.on('unhandledRejection', (e) => {
  emitStderr({ type: 'unhandledRejection', error: String((e && e.message) || e) });
  process.exit(1);
});
