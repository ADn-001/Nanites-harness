#!/usr/bin/env python3
"""
tune_thresholds.py — ledger-driven threshold-tuning report (Phase 15, workstream A).

This tool READS the Local Cortex ledger and prints what the ledger can actually PROVE
about the shipped confidence thresholds. It is a REPORTER, not a validator: it never
re-implements the Phase 7 deterministic validator, never decides whether a repair was
semantically right, and never invents a number it did not measure. The only way it
reports a "good repair" is by pointing at ledger evidence — an `accepted` outcome line
on the same trace_id, a recorded candidate set, a recorded confidence.

Local-only and dependency-free: stdlib only (`argparse`, `collections`, `json`, `os`,
`sys`), no network, no `needle` package, no venv. It runs on a bare system python.

    python3 tools/tune_thresholds.py [--ledger PATH] [--corpus PATH] [--deterministic PATH]
                                     [--json] [--thresholds 0.1,0.2,...]

WHY THE DEFINITIONS ARE THE WAY THEY ARE
----------------------------------------
The ledger records two line shapes (see `localmodels/local_models_daemon.py`):
  * an `op:` record   — {op:'repair'|'select'|'decide', request.candidates:[name…],
                        output, confidence, latency_ms, degraded, action:None}
  * an outcome line   — {trace_id, action, note}, with NO `op` key. This is deliberate
                        (see `_LEDGER_ACTIONS`): the operator's ACCEPT/IGNORE arrives
                        later, on the same trace_id. So an "outcome" is a JOIN, never a
                        field on the op record, and an op record with no matching outcome
                        line has UNOBSERVABLE ground truth: it is excluded from every
                        rate denominator rather than silently counted as a rejection.

* deterministic-pass fix rate — NOT computable from the ledger. It lives in
  `tools/corpus_deterministic_rate.mjs` (it measures the deterministic salvage pass over
  the golden corpus) and is passed in with `--deterministic`. Without that file this
  section says "not supplied" and prints no rate. This tool never shells out to node:
  the integrator runs the two and feeds the result in, so a missing node can never turn
  into a missing number that looks measured.

* repair acceptance rate — accepted outcomes (`accepted` or `accepted_by_operator`, the
  same two aliases `_LEDGER_ACTIONS` folds into `accepted`) over the op records that
  produced ANY outcome at all. Numerator and denominator are both printed: a percentage
  alone hides a denominator of 2, and threshold tuning on 2 samples is how a bad
  threshold ships.

* false-repair rate — the number the plan cares most about. It counts only defects the
  LEDGER CAN PROVE, and only among ACCEPTED repairs (a repair the operator rejected was
  never acted on, so it cannot have done harm):
    1. `name_outside_candidates` — the emitted rite name is not among the names recorded
       in that same request's `request.candidates`. The daemon flattens the candidates to
       bare names before writing them, so this is an exact set-membership test, not a
       fuzzy one: salvage inventing a rite the request never offered is a provable false
       repair.
    2. `no_confidence` / `low_confidence` — the accepted repair recorded a missing or
       non-numeric confidence, or one BELOW the threshold in force for its model
       (0.75 needle/dispatcher, 0.70 laya). Either way something was accepted that the
       shipped gate would not have accepted: the gate and the ledger disagree.
    3. `empty_output` — the accepted repair emitted nothing. An accepted empty repair is
       either a silent pass-through or a lost call; from the ledger both look identical
       and both are counted, because the ledger cannot tell them apart and pretending
       otherwise would under-report.
  Everything else — whether the model "meant" the right thing, whether the arguments were
  useful, whether the human was happy — is NOT observable from the ledger and is
  reported as such. An empty numerator with a real denominator prints `0.0%`; with NO
  denominator at all it prints `no data`, because `0.0` from zero records is a
  fabricated measurement wearing a measurement's clothes.

* per-threshold precision/recall — a sweep, computed ONLY over records that HAVE a
  numeric confidence AND an outcome. Predicted-accept = `confidence >= t`; actual-accept
  = the record's outcome was accepted. precision = TP/(TP+FP), recall = TP/(TP+FN), each
  `null` when its denominator is 0 (never NaN, never a bare 0 that reads as measured).
  This is the sweep that answers "where should DEF_SETTINGS.needle.minConfidence sit?".

* latency percentiles — p50/p95/p99 of `latency_ms` on a NEAREST-RANK percentile
  (`ceil(p*n)`-th smallest, 1-indexed) over the sorted sample. Nearest-rank rather than
  interpolation because every sample here is an integer millisecond count from a real
  call, so an interpolated p95 would report a millisecond nobody ever measured.

HARD RULES this file obeys
--------------------------
* A missing, empty, truncated or all-malformed ledger produces a clean report and exit
  code 0 — never a traceback, never a fabricated 0.0. Every malformed line is SKIPPED,
  the same tolerance `local_models_daemon._ledger_counts` already has, so this tool
  cannot be the thing that breaks on a ledger the daemon considers merely untidy.
* It WRITES NOTHING. It appends to no file, creates no directory, and drops no
  `__pycache__` (`sys.dont_write_bytecode`), so running it can never dirty a share-ready
  working tree.
* It is a READER of the ledger. No new action words, no new field names, no writes.
"""
import sys

# Never drop a __pycache__ into a share-ready working tree (see localmodels/ledger.py):
# this script lives inside the repo and may be imported by a suite.
sys.dont_write_bytecode = True

import argparse
import collections
import json
import math
import os

DEFAULT_LEDGER = 'var/local-models.jsonl'
DEFAULT_CORPUS = os.path.join('tests', 'fixtures', 'toolcall-corpus', 'cases.json')
OPS = ('repair', 'select', 'decide')

# The action aliases the daemon itself folds together (`_LEDGER_ACTIONS`). A repair the
# operator ACCEPTED by hand is as accepted as one the gate auto-accepted; counting only
# the machine word would under-report acceptance on exactly the runs a human intervened
# in, which are the interesting ones.
ACCEPTED_ACTIONS = ('accepted', 'accepted_by_operator')
KNOWN_ACTIONS = ('accepted', 'accepted_by_operator', 'ignored_by_operator', 'rejected',
                 'passed_through', 'timeout')
# The same alias folding `local_models_daemon._LEDGER_ACTIONS` applies, duplicated here on
# purpose: this tool must run on a bare system python with no `localmodels` import, and a
# reader that silently disagrees with the daemon's own /health counters is worse than
# useless for threshold tuning.
ACTION_FOLDER = {'accepted': 'accepted', 'accepted_by_operator': 'accepted',
                 'ignored_by_operator': 'ignored', 'rejected': 'rejected',
                 'passed_through': 'passed_through', 'timeout': 'timeout'}

# The thresholds currently shipped in `CogCore.localModels.DEFAULTS` in appcore.js
# (needle.minConfidence 0.75, dispatcher.minConfidence 0.75, laya.minConfidence 0.70).
# The default sweep always includes them so the report answers "are the values we ship
# actually any good?" even when the operator only cares about a different grid.
SHIPPED_THRESHOLDS = (0.75, 0.70)
MODEL_THRESHOLD = {'needle': 0.75, 'laya': 0.70}
OP_MODEL = {'repair': 'needle', 'select': 'needle', 'decide': 'laya'}


# --------------------------------------------------------------------------- ledger IO

def read_ledger(path):
    """Parse the JSONL ledger into (records, stats). NEVER raises.

    Tolerance is deliberately identical to `local_models_daemon._ledger_counts`: a blank
    line, a line that is not JSON, and a JSON scalar/array are all SKIPPED, because a
    half-flushed line from a daemon that was killed mid-append must not be able to break
    a reporting tool. Every skip is COUNTED, so a ledger that is 90% garbage is visible
    as such rather than hiding behind a smaller, healthier-looking sample.

    Returns (records, stats) where stats carries path/exists/total_lines/records/skipped.
    """
    stats = {'path': path, 'exists': False, 'total_lines': 0, 'records': 0, 'skipped': 0}
    records = []
    try:
        if not path or not os.path.isfile(path):
            return records, stats
        stats['exists'] = True
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                # Every physical line counts toward `total_lines` — including a blank one.
                # A blank line is not a record, but it IS a line the file contains, and a
                # reader that hides it makes "lines read" disagree with `wc -l`.
                stats['total_lines'] += 1
                if not line:
                    stats['skipped'] += 1
                    continue                        # a blank line is not a record
                try:
                    rec = json.loads(line)
                except Exception:
                    stats['skipped'] += 1
                    continue                        # not JSON (e.g. a truncated tail)
                if not isinstance(rec, dict):
                    stats['skipped'] += 1
                    continue                        # a JSON array/scalar is not a record
                records.append(rec)
        stats['records'] = len(records)
    except Exception:
        # An unreadable ledger is an empty ledger with the same honest report.
        return [], stats
    return records, stats


def split_records(records):
    """Partition into (op_records, outcome_lines).

    The split is the daemon's own convention: an outcome line carries an `action` and NO
    `op` key. Keeping them apart is what makes "did this repair get accepted?" a join on
    trace_id rather than a field read, and therefore what makes a missing outcome
    detectable as missing.
    """
    ops = collections.defaultdict(list)
    outcomes = []
    for rec in records:
        op = rec.get('op')
        if isinstance(op, str) and op in OPS:
            ops[op].append(rec)
        elif isinstance(rec.get('action'), str) and rec.get('action'):
            outcomes.append(rec)
    return ops, outcomes


def outcome_index(outcomes):
    """trace_id -> the list of actions recorded against it (last write is not assumed to
    win: an operator may accept then later reject, and both are evidence)."""
    idx = collections.defaultdict(list)
    for rec in outcomes:
        tid = rec.get('trace_id')
        if isinstance(tid, str) and tid:
            idx[tid].append(rec.get('action'))
    return idx


# --------------------------------------------------------------------------- primitives

def is_number(value):
    """True only for a real, finite number. `bool` is excluded on purpose: Python says
    isinstance(True, int), and a confidence of `true` is a malformed record, not a 1.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float('inf'), float('-inf'))


def percentile(sorted_sample, p):
    """Nearest-rank percentile of an ALREADY SORTED sample: the ceil(p*n)-th smallest,
    1-indexed. Returns None for an empty sample (there is no p50 of nothing)."""
    n = len(sorted_sample)
    if not n:
        return None
    rank = int(math.ceil(p * n))
    if rank < 1:
        rank = 1
    if rank > n:
        rank = n
    return sorted_sample[rank - 1]


def ratio(num, den):
    """A rate, or None when the denominator is 0. None is the whole point: a percentage
    printed from zero records is indistinguishable from a real measurement once it is
    copied into a settings file, and that is exactly the bug this avoids."""
    if not den:
        return None
    return round(float(num) / float(den), 6)


def candidate_names(rec):
    """The candidate names recorded on this request, as a set.

    The daemon writes `request.candidates` as a LIST OF BARE NAMES (it flattens each
    `{type:'function',function:{name}}` entry before appending), so membership is an exact
    string test. Returns None when the field is absent or empty, which the caller must
    treat as "not observable" rather than "no candidates were allowed" — those are very
    different claims and only the first one is provable from the record.
    """
    request = rec.get('request')
    if not isinstance(request, dict):
        return None
    cands = request.get('candidates')
    if not isinstance(cands, list) or not cands:
        return None
    names = set()
    for c in cands:
        if isinstance(c, str) and c:
            names.add(c)
        elif isinstance(c, dict):
            fn = c.get('function')
            inner = fn if isinstance(fn, dict) else c
            name = inner.get('name')
            if isinstance(name, str) and name:
                names.add(name)
    return names or None


def emitted_names(rec):
    """The rite names an op record's `output` carries.

    `output` is the model's `calls` list (a degraded call stores `{degraded: reason}`
    instead), so this is the name of the thing that was actually PROPOSED. A decide
    record's output is `{answers:{…}}` and carries no rite at all, so an empty result
    there means "not applicable", not "empty repair" — the caller checks the op.
    """
    out = rec.get('output')
    names = []
    if isinstance(out, list):
        calls = out
    elif isinstance(out, dict) and isinstance(out.get('calls'), list):
        calls = out['calls']
    else:
        calls = []
    for call in calls:
        if isinstance(call, dict):
            fn = call.get('function')
            inner = fn if isinstance(fn, dict) else call
            name = inner.get('name')
            if isinstance(name, str) and name:
                names.append(name)
        elif isinstance(call, str) and call:
            names.append(call)
    return names


def output_empty(rec):
    """True when the record emitted NOTHING that could have been acted on.

    `output` of None, [], {} or '' is empty. A `{degraded: reason}` dict is ALSO empty in
    the sense that matters — no call came out of it — and the ledger cannot distinguish a
    degraded call from a silent pass-through, so both are reported (the degraded case is
    additionally counted in `outcomes.degraded`).
    """
    out = rec.get('output')
    if out is None:
        return True
    if isinstance(out, (list, dict, str)):
        return len(out) == 0 or (isinstance(out, dict) and set(out) <= {'degraded'})
    return False


# --------------------------------------------------------------------------- metrics

def false_repair_reasons(rec, threshold):
    """The PROVABLE defects of one accepted op record, as a list of reason names.

    Only things the ledger can prove are returned; a repair the ledger says nothing wrong
    about yields []. That asymmetry is deliberate: this list is the "we can prove this
    was wrong" list, and pretending a quiet record is a verified-good one would make the
    false-repair rate a claim about semantics that the ledger cannot support.
    """
    bad = []
    # An empty output is a false repair whether or not the record says WHY it is empty.
    # A `{degraded: 'disabled'}` output emitted no call, so accepting it is exactly the
    # silent-pass-through this metric exists to catch; the `degraded` flag is counted
    # separately in `outcomes.degraded` rather than used to excuse it. Suppressing it here
    # would have made every --no-needle run report a clean 0.0% false-repair rate, which is
    # the single most flattering lie this tool could tell.
    if output_empty(rec):
        bad.append('empty_output')
    cands = candidate_names(rec)
    if cands is not None:
        for name in emitted_names(rec):
            if name not in cands:
                bad.append('name_outside_candidates')
                break
    else:
        # No candidate set on the record: membership is not observable, and is reported
        # as such rather than counted as a pass.
        bad.append('candidates_unobservable')
    conf = rec.get('confidence')
    if not is_number(conf):
        bad.append('no_confidence')
    elif threshold is not None and conf < threshold:
        bad.append('low_confidence')
    return bad


def op_metrics(records, outcomes_by_tid, thresholds):
    """Every metric for one `op`, as the stable JSON block for that op.

    Only JOINABLE records are counted. A record with no `trace_id` can never be matched to
    an outcome line, so it can contribute to no rate, no percentile and no sweep cell — it
    is a line the ledger parsed but the daemon's writer never really produced. Counting it
    would let a junk line (`{"op":"repair","confidence":"high"}`) move `records` and make
    the report depend on ledger debris. It is still counted at LEDGER level (`ledger.records`),
    which is the honest "lines that parsed" number.
    """
    action_counts = collections.Counter()
    reasons = collections.Counter()
    folded = collections.Counter()
    degraded = 0
    confidences = []
    latencies = []
    with_outcome = 0
    accepted = 0
    false_count = 0
    scored = []                      # (confidence, actually_accepted) for the sweep
    unobservable_ground_truth = 0
    cand_unobservable = 0
    usable = [r for r in records if isinstance(r.get('trace_id'), str) and r.get('trace_id')]

    for rec in usable:
        actions = outcomes_by_tid.get(rec.get('trace_id') or '', [])
        for action in actions:
            if action in KNOWN_ACTIONS:
                action_counts[action] += 1
                folded[ACTION_FOLDER.get(action, action)] += 1
        if rec.get('degraded'):
            degraded += 1
        if is_number(rec.get('confidence')):
            confidences.append(rec['confidence'])
        if is_number(rec.get('latency_ms')):
            latencies.append(rec['latency_ms'])
        is_accepted = any(a in ACCEPTED_ACTIONS for a in actions)
        if actions:
            with_outcome += 1
        else:
            # No outcome line: the ledger cannot say whether this proposal was used, so it
            # is excluded from every rate denominator (counted, never assumed rejected).
            unobservable_ground_truth += 1
        if not actions:
            continue
        if is_accepted:
            accepted += 1
            threshold = MODEL_THRESHOLD.get(rec.get('model') or OP_MODEL.get('', ''), None)
            if threshold is None:
                threshold = MODEL_THRESHOLD.get(OP_MODEL.get(rec.get('op'), ''), None)
            bad = false_repair_reasons(rec, threshold)
            if 'candidates_unobservable' in bad:
                cand_unobservable += 1
            provable = [b for b in bad if b != 'candidates_unobservable']
            for reason in provable:
                reasons[reason] += 1
            if provable:
                false_count += 1
        # Only a record with BOTH a numeric confidence and a known outcome can be scored
        # by the sweep; anything else would put a fabricated label on the confusion matrix.
        if is_number(rec.get('confidence')) and actions:
            scored.append((rec['confidence'], is_accepted))

    sweep = []
    for t in thresholds:
        tp = sum(1 for c, a in scored if c >= t and a)
        fp = sum(1 for c, a in scored if c >= t and not a)
        fn = sum(1 for c, a in scored if c < t and a)
        tn = sum(1 for c, a in scored if c < t and not a)
        sweep.append({'threshold': t, 'n': len(scored), 'tp': tp, 'fp': fp,
                      'fn': fn, 'tn': tn,
                      'precision': ratio(tp, tp + fp), 'recall': ratio(tp, tp + fn)})

    latencies.sort()
    return {
        'records': len(usable),
        # `outcomes` counts each action VALUE as written, so `accepted` and
        # `accepted_by_operator` stay visible separately; `outcomes_folded` is the same
        # tally the daemon's own /health counters report (`_LEDGER_ACTIONS` folds the two
        # accepted aliases into one bucket). Both are published because the raw split is
        # what tells you a human intervened, and the folded one is what compares against
        # the health endpoint.
        'outcomes': dict(action_counts),
        'outcomes_folded': folded,
        'accepted': accepted,
        'outcomes_recorded': with_outcome,
        'unobservable_ground_truth': unobservable_ground_truth,
        'degraded': degraded,
        'confidence_n': len(confidences),
        'acceptance': {
            'accepted': accepted,
            'denominator': with_outcome,
            'rate': ratio(accepted, with_outcome),
        },
        'false_repair': {
            'count': false_count,
            'denominator': accepted,
            'rate': ratio(false_count, accepted),
            'by_reason': dict(reasons),
            'candidates_unobservable': cand_unobservable,
        },
        'latency_ms': {
            'p50': percentile(latencies, 0.50),
            'p95': percentile(latencies, 0.95),
            'p99': percentile(latencies, 0.99),
            'n': len(latencies),
        },
        'per_threshold': sweep,
    }


# --------------------------------------------------------------------------- report

def parse_thresholds(text):
    """Parse a `--thresholds` grid. A malformed entry is a loud error, not a silent
    skip: a threshold grid that quietly lost half its points would produce a report whose
    gaps nobody notices."""
    if text is None:
        return None
    out = []
    for chunk in str(text).split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = float(chunk)
        except Exception:
            raise SystemExit('[tune_thresholds] --thresholds: %r is not a number' % chunk)
        out.append(round(value, 4))
    if not out:
        raise SystemExit('[tune_thresholds] --thresholds: no numbers given')
    return out


def default_grid():
    """0.05..0.95 step 0.05, PLUS every currently shipped value so the report always
    answers the question the integrator actually has: is what we ship any good?"""
    grid = [round(0.05 * i, 2) for i in range(1, 20)]
    for shipped in SHIPPED_THRESHOLDS:
        if shipped not in grid:
            grid.append(shipped)
    return sorted(set(grid))


def load_deterministic(path):
    """The deterministic section. Reads the JSON `tools/corpus_deterministic_rate.mjs`
    emitted. A missing/unreadable/malformed file is reported as such and NEVER as a rate."""
    if not path:
        return {'source': 'not supplied', 'total': None, 'fixed': None,
                'unchanged_correct': None, 'rate': None,
                'note': 'not supplied: run `node tools/corpus_deterministic_rate.mjs` '
                        'and pass the file with --deterministic'}
    out = {'source': path, 'total': None, 'fixed': None, 'unchanged_correct': None,
           'rate': None, 'note': None}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        out['note'] = 'unreadable: %s' % type(e).__name__
        return out
    if not isinstance(data, dict):
        out['note'] = 'malformed: not a JSON object'
        return out
    for key in ('total', 'fixed', 'unchanged_correct', 'rate'):
        value = data.get(key)
        if is_number(value):
            out[key] = value
    out['by_category'] = data.get('by_category') if isinstance(data.get('by_category'), dict) else None
    if out['rate'] is None:
        out['note'] = 'the supplied file carries no rate'
    return out


def build_report(ledger_path, corpus_path, deterministic_path, thresholds):
    records, stats = read_ledger(ledger_path)
    ops, outcomes = split_records(records)
    outcomes_by_tid = outcome_index(outcomes)

    corpus = {'path': corpus_path, 'exists': False, 'cases': None, 'tools': None}
    try:
        with open(corpus_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            corpus['exists'] = True
            cases = data.get('cases')
            corpus['cases'] = len(cases) if isinstance(cases, list) else None
            tools = data.get('tools')
            corpus['tools'] = len(tools) if isinstance(tools, list) else None
    except Exception:
        pass       # a missing corpus is a missing corpus, not a crash

    report = {
        'ledger': stats,
        'corpus': corpus,
        'deterministic': load_deterministic(deterministic_path),
        'per_threshold': thresholds,
    }
    for op in OPS:
        report[op] = op_metrics(ops.get(op, []), outcomes_by_tid, thresholds)
    return report


def fmt_rate(rate):
    """A rate for humans: '80.0%' or 'no data'. NEVER a bare '0.0%' standing in for
    'nothing was measured'."""
    if rate is None:
        return 'no data'
    return '%.1f%%' % (rate * 100.0)


def render(report):
    led = report['ledger']
    out = []
    out.append('== tune_thresholds: ledger-driven threshold report ==')
    out.append('ledger: %s' % led['path'])
    if not led['exists']:
        out.append('  MISSING: no such file -> no data (nothing below is measured)')
    out.append('  lines read %d | records %d | skipped %d'
               % (led['total_lines'], led['records'], led['skipped']))
    if not led['records']:
        out.append('  no usable records -> every rate below is no data')
    corpus = report['corpus']
    if corpus['exists']:
        out.append('corpus: %s (%s cases, %s tools)'
                   % (corpus['path'], corpus['cases'], corpus['tools']))
    else:
        out.append('corpus: %s (not found)' % corpus['path'])
    det = report['deterministic']
    out.append('-- deterministic-pass fix rate --')
    if det['source'] == 'not supplied':
        out.append('  no data: %s' % det['note'])
    elif det['rate'] is None:
        out.append('  no data: %s' % det['note'])
    else:
        out.append('  %s of %s repairable corpus cases fixed = %s (unchanged-correct %s, '
                   'false repairs %s)'
                   % (det['fixed'], det.get('repairable', det['total']), fmt_rate(det['rate']),
                      det['unchanged_correct'], det.get('false_repairs', 'n/a')))
    for op in OPS:
        m = report[op]
        out.append('-- op: %s --' % op)
        if not m['records']:
            out.append('  no data: no %s records in this ledger' % op)
            continue
        acc = m['acceptance']
        fr = m['false_repair']
        out.append('  records %d | outcomes recorded for %d | degraded %d | confidence %d'
                   % (m['records'], m['outcomes_recorded'], m['degraded'], m['confidence_n']))
        out.append('  outcomes: %s  (folded: %s)'
                   % (json.dumps(m['outcomes'], sort_keys=True),
                      json.dumps(m['outcomes_folded'], sort_keys=True)))
        out.append('  acceptance: %d/%d = %s'
                   % (acc['accepted'], acc['denominator'], fmt_rate(acc['rate'])))
        out.append('  false repair: %d/%d = %s  %s'
                   % (fr['count'], fr['denominator'], fmt_rate(fr['rate']),
                      json.dumps(fr['by_reason'], sort_keys=True)))
        if fr['candidates_unobservable']:
            out.append('  candidate-set membership: %d accepted record(s) not observable '
                       'from the ledger' % fr['candidates_unobservable'])
        lat = m['latency_ms']
        out.append('  latency_ms: n=%d p50=%s p95=%s p99=%s'
                   % (lat['n'], 'n/a' if lat['p50'] is None else lat['p50'],
                      'n/a' if lat['p95'] is None else lat['p95'],
                      'n/a' if lat['p99'] is None else lat['p99']))
        if m['unobservable_ground_truth']:
            out.append('  %d record(s) produced no outcome line: excluded from every rate'
                       % m['unobservable_ground_truth'])
        out.append('  threshold sweep (n=%d):' % (m['per_threshold'][0]['n'] if m['per_threshold'] else 0))
        for row in m['per_threshold']:
            out.append('    t=%.2f tp=%d fp=%d fn=%d tn=%d precision=%s recall=%s'
                       % (row['threshold'], row['tp'], row['fp'], row['fn'], row['tn'],
                          fmt_rate(row['precision']) if row['precision'] is not None else 'n/a',
                          fmt_rate(row['recall']) if row['recall'] is not None else 'n/a'))
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='tune_thresholds.py',
        description='Report what the Local Cortex ledger can prove about the shipped '
                    'confidence thresholds. Reads only; writes nothing; never fabricates '
                    'a rate from zero records.')
    parser.add_argument('--ledger', default=None,
                        help="ledger JSONL path (default: $LEDGER_PATH, else %s)"
                             % DEFAULT_LEDGER)
    parser.add_argument('--corpus', default=DEFAULT_CORPUS,
                        help='golden tool-call corpus (default: %s)' % DEFAULT_CORPUS)
    parser.add_argument('--deterministic', default=None,
                        help='JSON emitted by tools/corpus_deterministic_rate.mjs; '
                             'without it the deterministic section reports "not supplied"')
    parser.add_argument('--json', action='store_true',
                        help='emit ONE machine-readable JSON object (stable keys)')
    parser.add_argument('--thresholds', default=None,
                        help='comma-separated sweep grid (default: 0.05..0.95 step 0.05 '
                             'plus the shipped 0.75/0.70)')
    args = parser.parse_args(argv)

    # Same precedence as `ledger.default_path()`: the environment wins, then the
    # repo-relative default. `os.path.expanduser` so a `~`-prefixed $LEDGER_PATH works
    # and no absolute personal path is ever baked into a tracked file.
    ledger_path = args.ledger or os.environ.get('LEDGER_PATH') or DEFAULT_LEDGER
    ledger_path = os.path.expanduser(ledger_path)
    corpus_path = os.path.expanduser(args.corpus or DEFAULT_CORPUS)
    thresholds = parse_thresholds(args.thresholds) or default_grid()

    report = build_report(ledger_path, corpus_path, args.deterministic, thresholds)
    if args.json:
        # One object, nothing else on stdout: the integrator scripts against these keys.
        sys.stdout.write(json.dumps(report, sort_keys=True) + '\n')
    else:
        sys.stdout.write(render(report) + '\n')
    # Always 0: a missing/empty/garbage ledger is a REPORT, not a tool failure.
    return 0


if __name__ == '__main__':
    sys.exit(main())
