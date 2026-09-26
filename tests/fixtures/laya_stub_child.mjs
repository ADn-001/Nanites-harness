#!/usr/bin/env node
/*
 * laya_stub_child.mjs — TEST FIXTURE (Phase 13, workstream A).
 *
 * A model-free stand-in for localmodels/laya_child.mjs: it speaks the exact same
 * newline-delimited-JSON stdio protocol, but it never imports @receptron/laya, never
 * downloads anything and answers deterministic values. It exists so `test_e2e.py` can
 * prove the daemon's /decide route, the idle reaper, the timeout path and the
 * child_gone path with no model, no Node packages and no preload shim.
 *
 * Knobs (env):
 *   LAYA_STUB_HANG=1              answer NOTHING (force the daemon's timeout path)
 *   LAYA_STUB_EXIT_AFTER_MS=<ms>  on the first `decide`, die mid-flight without answering
 *                                 (force the daemon's child_gone path)
 *
 * stdout carries protocol JSON and NOTHING else (the daemon parses it as NDJSON).
 */
import readline from 'node:readline';

// A stray log must never corrupt the protocol: route every console channel to stderr.
for (const name of ['log', 'info', 'warn', 'debug', 'trace']) {
  console[name] = (...args) =>
    process.stderr.write(args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ') + '\n');
}

const HANG = process.env.LAYA_STUB_HANG === '1';
const EXIT_AFTER_MS = parseInt(process.env.LAYA_STUB_EXIT_AFTER_MS || '0', 10) || 0;

let loaded = false;
let died = false;

function emit(obj, cb) {
  process.stdout.write(JSON.stringify(obj) + '\n', cb);
}

/** Deterministic answers derived only from the question types. */
function answersFor(questions) {
  const out = {};
  const qs = questions && typeof questions === 'object' ? questions : {};
  for (const [key, q] of Object.entries(qs)) {
    const type = q && q.type;
    if (type === 'noul') {
      out[key] = { noul: 0.9 };
    } else if (type === 'score') {
      out[key] = { score: 1 };
    } else if (type === 'choice') {
      const criteria = q && q.criteria;
      let keys = [];
      if (Array.isArray(criteria)) keys = criteria.map((c, i) => String(typeof c === 'string' ? c : i));
      else if (criteria && typeof criteria === 'object') keys = Object.keys(criteria);
      if (!keys.length) keys = ['a'];
      // Uniform shares, with the LAST one absorbing the rounding so the sum is exactly 1.
      const probs = {};
      const share = 1 / keys.length;
      keys.forEach((k, i) => {
        probs[k] = i === keys.length - 1 ? 1 - share * (keys.length - 1) : share;
      });
      out[key] = { choice: keys[0], probabilities: probs };
    }
  }
  return out;
}

function handle(msg) {
  const op = msg && msg.op;
  if (op === 'health') {
    return { id: msg.id, ok: true, loaded: loaded, pid: process.pid, cache: 'stub', node: process.version };
  }
  if (op === 'load') {
    loaded = true;
    return { id: msg.id, ok: true, loaded: true, load_ms: 1 };
  }
  if (op === 'decide') {
    if (EXIT_AFTER_MS > 0 && !died) {
      // Die mid-flight WITHOUT answering: the daemon must report child_gone.
      died = true;
      setTimeout(() => process.exit(1), EXIT_AFTER_MS);
      return null; // never answers
    }
    loaded = true;
    return { id: msg.id, ok: true, answers: answersFor(msg.questions),
             usage: { input_tokens: 42 }, latency_ms: 3 };
  }
  if (op === 'close') {
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
  if (!msg || typeof msg !== 'object' || typeof msg.op !== 'string') {
    emit({ id: null, ok: false, error: 'bad json' });
    return;
  }
  if (HANG) return; // answer nothing at all
  let out;
  try {
    out = handle(msg);
  } catch (e) {
    out = { id: msg.id, ok: false, error: String((e && e.message) || e) };
  }
  if (out === null) return; // this request never gets an answer (mid-flight death)
  const shouldExit = out._exit === true;
  delete out._exit;
  if (shouldExit) emit(out, () => process.exit(0));
  else emit(out);
}

// One request at a time, in arrival order.
let queue = Promise.resolve();
const enqueue = (fn) => (queue = queue.then(fn, fn));

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => enqueue(() => processLine(line)));
rl.on('close', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
process.on('unhandledRejection', () => process.exit(1));
