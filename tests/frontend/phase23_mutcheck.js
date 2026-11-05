'use strict';
/*
 * phase23_mutcheck.js — prove the phase-23 suite has teeth.
 *
 * A green suite is a claim about the CODE. This is the claim about the SUITE: apply each
 * mutation that removes one guarantee Phase 23 established, and record which tests notice.
 *
 * The harness REFUSES to score: it captures output to a file and asserts the real summary
 * line is present before reading any result out of it, because a run that silently did
 * nothing scores as a clean sweep — the most dangerous way this technique can lie.
 * It also asserts the patch actually changed the file, so "caught" can never be a
 * pattern that matched nothing.
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const TARGET = path.join(ROOT, 'index.html');
const TEST = path.join(__dirname, 'phase23_probe_and_timeout.test.js');

/* Each mutation reverts ONE guarantee. `find` must be unique in the file — if it is not,
 * the harness aborts rather than scoring a mutation it did not actually apply. */
const MUTANTS = [
  {
    name: 'timeout reported as an endpoint fault again (drops the TimeoutError branch)',
    find: "else if(e.name==='TimeoutError'){",
    replace: "else if(false){"
  },
  {
    name: 'soul-load retry suppressed for ALL faults (over-broad guard)',
    find: "else{\n      m.content+='\\n\\n[ RITE FAILED: '",
    replace: "else if(currentBackend()!=='lmstudio'){\n      m.content+='\\n\\n[ RITE FAILED: '"
  },
  {
    name: 'auth header removed from the /api/v0/models probe (CR-0029 reverted)',
    find: "const r=await fetch(api('/api/v0/models'),{signal:apiSig(),headers:authHeaders()});",
    replace: "const r=await fetch(api('/api/v0/models'),{signal:apiSig()});"
  },
  {
    name: 'per-iteration budget reverted to the single turn-wide signal',
    find: "try{iterSig=streamSignal();}catch(e){iterSig=sig;}",
    replace: "try{iterSig=sig;}catch(e){iterSig=sig;}"
  },
  /* The two an independent review caught AFTER the first green. Both were invisible to the
     shape assertions: the per-iteration signal existed but was never the binding constraint,
     and the loop guards hardcoded 'AbortError' so a budget expiry blamed the operator. The
     behavioural multi-iteration test is what kills these — a source-shape check passes on
     both. */
  {
    name: 'the turn-wide signal carries the budget again (per-iteration signal is inert)',
    find: "try{generating=true;abortCtl=new AbortController();sig=operatorSignal();}catch(e){}",
    replace: "try{generating=true;abortCtl=new AbortController();sig=streamSignal();}catch(e){}"
  },
  {
    name: 'loop guards hardcode AbortError again (a budget expiry blames the operator)',
    find: "    if(sig.aborted)throwAbort(sig);\n    /* PHASE 14",
    replace: "    if(sig.aborted)throw new DOMException('Aborted','AbortError');\n    /* PHASE 14",
    /* DOCUMENTED EQUIVALENT — do not "fix" this by writing a test.
       `sig` is now `operatorSignal()` (abortCtl.signal with NO timer composed onto it), and
       the only thing that ever aborts it is `halt()` -> `abortCtl.abort()` with no reason
       argument. So `abortCause(sig)` can only ever return 'AbortError' at these guards, and
       `throwAbort(sig)` is behaviourally identical to throwing AbortError. Verified by
       reading the only call site of `.abort()`; there is no path that attaches a
       TimeoutError reason to `sig`.

       It is kept rather than simplified because it is the correct thing to write at an
       abort guard, and it is what makes the code correct if a future change composes a
       budget back onto the turn-wide signal. A test built for this would have to mock a
       reason onto `sig` to make the branches differ — that asserts the shape of the code,
       not the behaviour, and passes just as happily against either implementation.

       The REAL guarantee this phase had to pin — that a budget expiry is never reported as
       an operator halt — is pinned behaviourally by tests 0005(f) and 0005(g), which drive a
       genuinely slow model call and assert the report is a timeout. */
    equivalent: true
  },
  {
    name: 'the plain (non-agent) chat path loses its budget entirely',
    find: "try{plainSig=streamSignal();}catch(e){plainSig=sig;}",
    replace: "try{plainSig=sig;}catch(e){plainSig=sig;}"
  }
];

/* Mutate a THROWAWAY COPY of the project, never the real tree.
   An earlier version of this harness rewrote index.html in place and restored it in a
   `finally`. That is a live-mutation sweep on a tracked file: a SIGKILL, a power loss, or a
   second session committing mid-run leaves a MUTATED index.html in the repository — the
   symptom is a suite that goes green on weakened code, noticed several phases later.
   helpers.js resolves ROOT as ../.. from this file, so a copy of the whole project gives an
   isolated tree with no plumbing changes and nothing for a crash to leave behind. */
const SANDBOX = fs.mkdtempSync(path.join(os.tmpdir(), 'phase23-mut-'));

function buildSandbox() {
  for (const entry of ['index.html', 'appcore.js', 'sw.js', 'manifest.webmanifest']) {
    const src = path.join(ROOT, entry);
    if (fs.existsSync(src)) fs.copyFileSync(src, path.join(SANDBOX, entry));
  }
  fs.mkdirSync(path.join(SANDBOX, 'tests', 'frontend'), { recursive: true });
  fs.copyFileSync(path.join(ROOT, 'tests', 'frontend', 'helpers.js'), path.join(SANDBOX, 'tests', 'frontend', 'helpers.js'));
  fs.copyFileSync(TEST, path.join(SANDBOX, 'tests', 'frontend', 'phase23_probe_and_timeout.test.js'));
  /* node_modules must resolve for `require('jsdom')`. */
  const nm = path.join(SANDBOX, 'node_modules');
  if (!fs.existsSync(nm)) {
    try { fs.symlinkSync(path.join(ROOT, 'node_modules'), nm); } catch (e) { /* best effort */ }
  }
}

function runSuite() {
  const outFile = path.join(SANDBOX, 'run.log');
  try {
    execFileSync('node', ['tests/frontend/phase23_probe_and_timeout.test.js'],
      { cwd: SANDBOX, stdio: ['ignore', fs.openSync(outFile, 'w'), 'pipe'], timeout: 180000 });
  } catch (e) { /* non-zero is a normal mutant outcome; the file has the output */ }
  let out = '';
  try { out = fs.readFileSync(outFile, 'utf8'); } catch (e) { /* handled below */ }
  // REFUSE TO SCORE a run that did not really run.
  if (!/phase23: \d+ FAILURES/.test(out)) {
    throw new Error('harness refused to score: no summary line in output.\n--- tail ---\n' + out.slice(-600));
  }
  const m = out.match(/phase23: (\d+) FAILURES/);
  return { fails: parseInt(m[1], 10), out: out };
}

const original = fs.readFileSync(TARGET, 'utf8');
buildSandbox();
let caught = 0, escaped = [], notApplied = [], equivalents = [];

try {
  const base = runSuite();
  console.log('CONTROL (unmutated): ' + base.fails + ' failures');
  if (base.fails !== 0) {
    console.error('ABORT: the unmutated tree is not green, so a mutation result would mean nothing.');
    process.exit(2);
  }

  for (const mut of MUTANTS) {
    const hits = original.split(mut.find).length - 1;
    if (hits !== 1) {
      notApplied.push(mut.name + ' (pattern matched ' + hits + ' times, expected exactly 1)');
      console.log('NOT-APPLIED  ' + mut.name + ' — matched ' + hits + ' times');
      continue;
    }
    const mutated = original.split(mut.find).join(mut.replace);
    if (mutated === original) {
      notApplied.push(mut.name + ' (replacement was a no-op)');
      console.log('NOT-APPLIED  ' + mut.name + ' — no-op replacement');
      continue;
    }
    // Write into the SANDBOX, never the repo. Assert it landed there before scoring.
    fs.writeFileSync(path.join(SANDBOX, 'index.html'), mutated);
    const onDisk = fs.readFileSync(path.join(SANDBOX, 'index.html'), 'utf8');
    if (onDisk === original) {
      notApplied.push(mut.name + ' (write did not change the sandbox file)');
      console.log('NOT-APPLIED  ' + mut.name + ' — write did not change the file');
      continue;
    }
    let res;
    try { res = runSuite(); }
    catch (e) { res = { fails: -1, out: String(e.message) }; }
    fs.writeFileSync(path.join(SANDBOX, 'index.html'), original);

    /* An equivalent mutant is a RESULT, not a defect: the patch applied and the program
       genuinely behaves identically, so no test could have caught it. Score it separately
       rather than letting it inflate the escape count, and say why in the summary. */
    if (mut.equivalent) {
      equivalents.push(mut.name + (res.fails > 0 ? ' (BUT it went red — the equivalence claim is WRONG, investigate)' : ''));
      console.log((res.fails > 0 ? 'EQUIV?      ' : 'EQUIVALENT  ') + mut.name + '  (applied, suite ' + (res.fails > 0 ? 'went RED' : 'stayed green') + ')');
      continue;
    }

    const wentRed = res.fails > 0 || res.fails === -1;
    if (wentRed) {
      caught++;
      console.log('CAUGHT       ' + mut.name + '  (' + res.fails + ' failures)');
    } else {
      escaped.push(mut.name);
      console.log('ESCAPED      ' + mut.name + '  — the suite stayed GREEN');
    }
  }
} finally {
  /* The real tree was never written to, so there is nothing to restore — but PROVE it
     rather than assume it. A harness that fails to restore leaves every later result
     describing a file nobody intended, and that failure is invisible in the output. */
  const onDisk = fs.readFileSync(TARGET, 'utf8');
  if (onDisk !== original) {
    console.error('FATAL: index.html in the REPO differs from what was read at start.');
    process.exit(3);
  }
  try { fs.rmSync(SANDBOX, { recursive: true, force: true }); } catch (e) { /* best effort */ }
}

const scoreable = MUTANTS.length - equivalents.length;
console.log('\nMUTATION SUMMARY: ' + caught + '/' + scoreable + ' caught, ' +
  escaped.length + ' escaped, ' + equivalents.length + ' documented-equivalent, ' +
  notApplied.length + ' not applied');
if (escaped.length) console.log('ESCAPED:\n  - ' + escaped.join('\n  - '));
if (equivalents.length) console.log('DOCUMENTED EQUIVALENT (applied, behaviour identical — no test could catch it):\n  - ' + equivalents.join('\n  - '));
if (notApplied.length) console.log('NOT APPLIED:\n  - ' + notApplied.join('\n  - '));
/* A not-applied mutation is a FALSE PASS and must not read as success. An escape is a real
   gap. A documented equivalent is neither — but it must never be silently absorbed. */
process.exit((escaped.length || notApplied.length) ? 1 : 0);