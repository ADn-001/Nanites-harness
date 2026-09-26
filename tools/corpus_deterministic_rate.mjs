#!/usr/bin/env node
'use strict';
/*
 * PHASE 15 — deterministic-pass fix rate over the golden tool-call corpus.
 *
 * WHAT THIS IS: a MEASUREMENT, not a validator. It drives the SHIPPED
 * `CogCore.salvageToolCalls` (the same `require`-able module every frontend suite
 * loads) over every case in `tests/fixtures/toolcall-corpus/cases.json` and reports
 * how many of the repairable cases the cheap deterministic stage actually fixed.
 * `tools/tune_thresholds.py --deterministic <this file's output>` folds the number
 * into the threshold report, so the two tools share ONE definition of "fixed".
 *
 * WHY THE GRADER LIVES HERE AND NOT IN THE TUNER: `tests/frontend/phase11_salvage.test.js`
 * already defines "fixed" (the expected canonical name came back, with exactly the
 * expected argument VALUES) and "false repair" (an `unchanged: true` case came back
 * altered, or calls were manufactured out of a non-call reply). A second, looser grader
 * in a second tool would make the published rate a lie - the number would drift with
 * whatever definition the second author felt like that day. So the grading below is a
 * faithful port of that suite's rules, case for case, and it deliberately re-uses the
 * same `canon` deep-equality (key order must not matter).
 *
 * CONTRACT:
 *  - one compact JSON object on stdout, nothing else; exit 0.
 *  - READ-ONLY on the repo: it opens cases.json and appcore.js and writes NOTHING.
 *    (No cache, no report file, no __pycache__-alike - a share-ready tree stays clean.)
 *  - no network, no model, no venv, no DOM. `salvageToolCalls` is pure and that purity is
 *    the entire point: this number must measure the DETERMINISTIC pass alone, so it stays
 *    a lower bound that holds even when no local model is installed.
 *  - `rate` is over the REPAIRABLE cases (those declaring `expect.name`), because a case
 *    with nothing to repair cannot be fixed or broken. `total` is every case in the file,
 *    so the denominator of the corpus itself is never quietly redefined.
 */
import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs';

/* ESM has no __dirname; the script's own directory is the tools/ dir either way, and
   deriving it from import.meta.url keeps the paths machine-agnostic. */
const HERE = path.dirname(new URL(import.meta.url).pathname);
const ROOT = path.join(HERE, '..');
const CASES = path.join(ROOT, 'tests', 'fixtures', 'toolcall-corpus', 'cases.json');
const APPCORE = path.join(ROOT, 'appcore.js');

/* The `.mjs` extension makes this file an ES module whatever package.json says, but
   appcore.js is a UMD/CommonJS bundle and the whole point of this tool is to measure the
   SHIPPED copy - the same one `require(path.join(__dirname,'..','appcore.js'))` loads in
   every frontend suite. createRequire is the standard bridge: it loads the identical
   module instance the suites grade, with no re-implementation to drift from. */
const require = createRequire(import.meta.url);
const CogCore = require(APPCORE);

/*
 * The 6 real tool schemas, copied VERBATIM from `TOOL_SCHEMAS` in index.html - the same
 * list the Phase 11 suite compares the corpus against (`corpus.tools`). They are inlined
 * rather than scraped out of index.html because that file is a template full of browser
 * globals; scraping it would need a DOM and this script must stay dependency-free.
 * A per-case `tools` allow-list may name a rite that is NOT one of the six (that is how
 * the corpus pins an ambiguous near-miss pair like ['read_file','read_filx']); for those
 * the schema is synthesized permissively, because salvage only ever reads the NAME out
 * of an allow-list entry and the strict schema gate is the Phase 7 validator's job, not
 * this tool's.
 */
const TOOL_SCHEMAS = [
  { type: 'function', function: { name: 'read_file', description: 'Read a UTF-8 text file inside the bound working directory.', parameters: { type: 'object', properties: { path: { type: 'string', description: 'Project-relative path' } }, required: ['path'] } } },
  { type: 'function', function: { name: 'write_file', description: 'Create or overwrite a text file inside the bound working directory.', parameters: { type: 'object', properties: { path: { type: 'string' }, content: { type: 'string' } }, required: ['path', 'content'] } } },
  { type: 'function', function: { name: 'list_dir', description: 'List a directory with d/f/x type tags.', parameters: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'] } } },
  { type: 'function', function: { name: 'grep', description: 'Regex-search project text files.', parameters: { type: 'object', properties: { path: { type: 'string' }, pattern: { type: 'string' } }, required: ['path', 'pattern'] } } },
  { type: 'function', function: { name: 'git', description: 'Run a safe, project-local git rite.', parameters: { type: 'object', properties: { args: { type: 'string', description: 'Git arguments, e.g. "status --short"' } }, required: ['args'] } } },
  { type: 'function', function: { name: 'run_command', description: 'Run a shell command in the working directory.', parameters: { type: 'object', properties: { command: { type: 'string' } }, required: ['command'] } } }
];
const BY_NAME = new Map(TOOL_SCHEMAS.map((t) => [t.function.name, t]));
function schemasFor(names) {
  return (names || []).map((n) => BY_NAME.get(n) || {
    type: 'function',
    function: { name: n, description: 'corpus allow-list entry with no shipped schema',
      parameters: { type: 'object', properties: {} } }
  });
}

/* Stable deep-equality by canonical serialisation (key order must not matter). */
function canon(v) {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}';
  }
  return JSON.stringify(v === undefined ? null : v);
}

function main() {
  const corpus = JSON.parse(fs.readFileSync(CASES, 'utf8'));
  const cases = corpus.cases || [];
  const byCategory = {};
  let repairable = 0, fixed = 0, unchangedCorrect = 0, unchangedGuarded = 0;
  let falseRepairs = 0, ambiguousGuessed = 0, threw = 0, unrepairableCases = 0;

  const bump = (cat, key) => {
    if (!byCategory[cat]) byCategory[cat] = { total: 0, repairable: 0, fixed: 0, unchanged_correct: 0, false_repairs: 0 };
    byCategory[cat][key] += 1;
  };

  for (const cse of cases) {
    const cat = String(cse.category || 'uncategorised');
    const exp = cse.expect || {};
    const tools = schemasFor(cse.tools || corpus.tools);
    bump(cat, 'total');

    let out = null, err = null;
    try { out = CogCore.salvageToolCalls(cse.reply, tools); } catch (e) { err = e; }
    if (err || !out || !Array.isArray(out.calls) || !Array.isArray(out.changed)
        || !Array.isArray(out.unrepairable)) {
      /* A case salvage cannot even answer for is NOT counted as fixed and NOT counted
         as a false repair: it is a harness fault, surfaced as `threw` so the number
         cannot quietly improve by skipping hard cases. */
      threw += 1;
      continue;
    }

    if (exp.never) {
      for (const forbidden of exp.never) {
        if (out.calls.some((c) => c.name === forbidden)) ambiguousGuessed += 1;
      }
    }
    if (exp.unrepairable) unrepairableCases += 1;

    if (exp.name) {
      repairable += 1;
      bump(cat, 'repairable');
      const got = out.calls.find((c) => c.name === exp.name);
      if (got && (exp.args === undefined || canon(got.args) === canon(exp.args))) {
        fixed += 1;
        bump(cat, 'fixed');
      }
    }
    if (exp.unchanged) {
      unchangedGuarded += 1;
      /* The false-repair guard, ported verbatim from phase11: a legitimate turn must come
         back with changed:[] and with the SAME call (or, when the case declares no name,
         with no call at all). Anything else is a repair invented out of nothing. */
      let clean = out.changed.length === 0;
      if (!exp.name && out.calls.length !== 0) clean = false;
      if (exp.name) {
        const got = out.calls.find((c) => c.name === exp.name);
        if (!got || (exp.args !== undefined && canon(got.args) !== canon(exp.args))) clean = false;
      }
      if (clean) {
        unchangedCorrect += 1;
        bump(cat, 'unchanged_correct');
      } else {
        falseRepairs += 1;
        bump(cat, 'false_repairs');
      }
    }
  }

  const rate = repairable ? Math.round((fixed / repairable) * 1e6) / 1e6 : 0;
  process.stdout.write(JSON.stringify({
    tool: 'corpus_deterministic_rate',
    source: 'tests/fixtures/toolcall-corpus/cases.json',
    total: cases.length,
    repairable: repairable,
    fixed: fixed,
    unchanged_correct: unchangedCorrect,
    unchanged_guarded: unchangedGuarded,
    false_repairs: falseRepairs,
    ambiguous_guessed: ambiguousGuessed,
    unrepairable: unrepairableCases,
    threw: threw,
    rate: rate,
    by_category: byCategory
  }) + '\n');
}

try {
  main();
  process.exit(0);
} catch (e) {
  process.stderr.write('[corpus_deterministic_rate] ' + (e && e.stack ? e.stack : e) + '\n');
  process.exit(1);
}
