#!/usr/bin/env python3
"""
LIVE Laya decision suite (Phase 13, workstream A) - opt-in, runs the REAL model.

Skipped unless COG_LIVE_MODELS=1, and skipped cleanly (never an error) when
`localmodels/node_modules/@receptron/laya` or the weights cache (~/.cache/receptron-laya,
LAYA_CACHE overrides) is absent. NEVER run by `npm test` - it exists as the owner's "it
genuinely works" evidence: the CI-reproducible mocked suites stay the gates.

Unlike the Needle live suite this one drives the REAL child through the REAL daemon:
the daemon is spawned with `--laya-child <localmodels/laya_child.mjs>`, and the test
POSTs /decide over HTTP, so the whole path (Origin guard -> route -> child spawn ->
NDJSON -> Laya.systemOne -> ledger) is exercised exactly as the frontend will use it.

Contract asserted - three different `state` values, one question of EACH type per state:
  - the answer key set is exactly the question key set,
  - `choice` has `choice` in the criteria keys and `probabilities` summing to ~1,
  - `score` is a number inside the rubric's index range,
  - `noul` is a number in [0, 1],
  - `usage.input_tokens` is present.
Measured latencies + token counts are PRINTED so the gatelog can quote them.

If every prerequisite is present (package, node, child script, cached weights) and the engine
STILL cannot serve, this suite FAILS LOUDLY - a real environment blocker must not masquerade as a
green skip. Set COG_LIVE_LAYA_ALLOW_UNSERVABLE=1 to skip deliberately on a machine that cannot run
the ONNX runtime (e.g. a glibc-only onnxruntime-node prebuild on a musl host).

Run:  COG_LIVE_MODELS=1 python3 -m unittest tests/live/test_laya_live.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAEMON = os.path.join(BASE, 'localmodels', 'local_models_daemon.py')
CHILD = os.path.join(BASE, 'localmodels', 'laya_child.mjs')
LAYA_PKG = os.path.join(BASE, 'localmodels', 'node_modules', '@receptron', 'laya')

LIVE = os.environ.get('COG_LIVE_MODELS') == '1'

# Prerequisites present but the engine still cannot serve (e.g. a glibc-only onnxruntime-node
# prebuild on a musl host): FAIL loudly by default, skip only on an explicit opt-out.
ALLOW_UNSERVABLE = os.environ.get('COG_LIVE_LAYA_ALLOW_UNSERVABLE') == '1'

# Generous budgets: the FIRST /decide pays for the model load (tens of seconds even with
# the weights cached; a cold cache also downloads ~1.7 GB).
LOAD_TIMEOUT_MS = int(os.environ.get('COG_LIVE_LAYA_LOAD_MS', '600000'))
CALL_TIMEOUT_MS = int(os.environ.get('COG_LIVE_LAYA_CALL_MS', '120000'))

STATES = [
    {'utterance': 'write the cleaned log to out/final.log',
     'tool': 'write_file',
     'arguments': {'path': 'out/final.log', 'content': 'log data'},
     'schema': 'write_file(path: string required, content: string required)'},
    {'ticket': 'Refund not received',
     'body': 'I cancelled two weeks ago and still have no refund. This is the third time I write.'},
    {'reply': 'I cannot help with that request.',
     'tool_calls': []},
]

QUESTIONS = {
    'gate': {'type': 'noul',
             'instructions': 'Do these arguments plausibly satisfy the tool schema description?',
             'criteria': 'true when the arguments look like a valid invocation of the described tool'},
    'kind': {'type': 'choice',
             'instructions': 'Which kind of operation is this?',
             'criteria': {'read': 'reads data only',
                          'write': 'writes or mutates data',
                          'exec': 'executes commands'}},
    'severity': {'type': 'score',
                 'instructions': 'How severe would this operation be?',
                 'criteria': ['harmless', 'mild', 'risky', 'destructive']},
}


def _laya_weights_cached():
    """The package caches under LAYA_CACHE or ~/.cache/receptron-laya."""
    cache = os.environ.get('LAYA_CACHE') or os.path.expanduser(os.path.join('~', '.cache', 'receptron-laya'))
    if not os.path.isdir(cache):
        return False
    for _root, _dirs, files in os.walk(cache):
        for name in files:
            if name.startswith('laya.') or name == 'laya_config.json':
                return True
    return False


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(port, path, payload, timeout=30.0):
    data = json.dumps(payload).encode()
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


@unittest.skipUnless(LIVE, 'COG_LIVE_MODELS=1 not set (live model suite is opt-in)')
class TestLayaLive(unittest.TestCase):
    """Real child, real daemon, real weights - the 'it genuinely works' evidence."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(LAYA_PKG):
            raise unittest.SkipTest('@receptron/laya not installed under localmodels/node_modules '
                                    '(run localmodels/setup.sh)')
        if not os.path.isfile(CHILD):
            raise unittest.SkipTest('child script absent: %s' % CHILD)
        if shutil.which(os.environ.get('LAYA_NODE') or 'node') is None:
            raise unittest.SkipTest('node not on PATH')
        if not _laya_weights_cached():
            raise unittest.SkipTest('Laya weights not cached (run the first Laya.load once)')

        cls.tmp = tempfile.mkdtemp(prefix='coglaya-live-')
        cls.port = _free_port()
        ledger = os.path.join(cls.tmp, 'live.jsonl')
        cls.ledger = ledger
        cls.daemon = subprocess.Popen(
            [sys.executable, DAEMON, '--port', str(cls.port), '--ledger', ledger,
             '--no-needle', '--laya-child', CHILD,
             '--laya-timeout-ms', str(LOAD_TIMEOUT_MS), '--laya-idle-s', '0'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=cls.tmp)
        # Wait for the socket (the daemon binds before it can answer).
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            cls._stop()
            raise unittest.SkipTest('daemon did not bind port %d' % cls.port)

        cls.results = []
        for i, state in enumerate(STATES):
            t0 = time.time()
            code, body = _post(cls.port, '/decide',
                               {'state': state, 'questions': QUESTIONS,
                                'trace_id': 'live_state_%d' % i},
                               timeout=(LOAD_TIMEOUT_MS / 1000.0) + 30)
            dt = (time.time() - t0) * 1000.0
            cls.results.append({'index': i, 'status': code, 'body': body, 'wall_ms': round(dt, 1)})
            print('[laya-live] state %d: HTTP %s in %.0f ms -> %s'
                  % (i, code, dt, json.dumps(body)[:400]))
        first = cls.results[0]['body'] if cls.results else {}
        if not first.get('ok'):
            reason = first.get('reason') or first.get('error')
            cls._stop()
            msg = ('the real child could not serve /decide (%s)' % reason)
            if ALLOW_UNSERVABLE:
                raise unittest.SkipTest(msg + ' [COG_LIVE_LAYA_ALLOW_UNSERVABLE=1]')
            # Every PREREQUISITE was present (package, node, child, cached weights) and the
            # engine STILL could not serve: that is a real environment blocker, not a missing
            # prerequisite, and it must be LOUD. Do not degrade it back to a silent skip —
            # a green "skipped" here would read as "verified" to the next agent.
            raise RuntimeError(
                msg + ' — the package, node, the child script and the cached weights are all '
                      'present, so this is a FAILURE, not a skip. Known cause: a glibc-only '
                      'onnxruntime-node prebuild on a musl host (Alpine/postmarketOS) crashes in '
                      'session creation even under a gcompat + symbol shim. Set '
                      'COG_LIVE_LAYA_ALLOW_UNSERVABLE=1 to skip deliberately on such a machine.')

    @classmethod
    def _stop(cls):
        proc = getattr(cls, 'daemon', None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        tmp = getattr(cls, 'tmp', None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_answer_type_contract(self):
        """One question of each type, three states: keys, types, ranges, probabilities."""
        for result in self.results:
            i = result['index']
            self.assertEqual(result['status'], 200, 'state %d: HTTP %s' % (i, result['status']))
            body = result['body']
            self.assertTrue(body.get('ok'), 'state %d: %r' % (i, body))
            answers = body.get('answers') or {}
            print('[laya-live] state %d answers: %s' % (i, json.dumps(answers)))
            self.assertEqual(sorted(answers), sorted(QUESTIONS),
                             'state %d: answer keys %r != question keys %r'
                             % (i, sorted(answers), sorted(QUESTIONS)))

            gate = answers.get('gate') or {}
            self.assertIsInstance(gate.get('noul'), (int, float), 'state %d: noul missing' % i)
            self.assertTrue(0.0 <= gate['noul'] <= 1.0,
                            'state %d: noul out of [0,1]: %r' % (i, gate['noul']))

            kind = answers.get('kind') or {}
            criteria = QUESTIONS['kind']['criteria']
            self.assertIn(kind.get('choice'), criteria,
                          'state %d: choice %r not in criteria' % (i, kind.get('choice')))
            probs = kind.get('probabilities')
            self.assertIsInstance(probs, dict, 'state %d: probabilities missing' % i)
            self.assertEqual(sorted(probs), sorted(criteria),
                             'state %d: probability keys %r != criteria %r'
                             % (i, sorted(probs), sorted(criteria)))
            self.assertAlmostEqual(sum(probs.values()), 1.0, delta=0.02,
                                   msg='state %d: probabilities do not sum to 1: %r' % (i, probs))
            self.assertAlmostEqual(max(probs, key=probs.get), kind['choice'], delta=1e-9,
                                   msg='state %d: choice != argmax(probabilities)' % i)

            sev = answers.get('severity') or {}
            levels = QUESTIONS['severity']['criteria']
            self.assertIsInstance(sev.get('score'), (int, float), 'state %d: score missing' % i)
            self.assertTrue(0.0 <= sev['score'] <= float(len(levels) - 1),
                            'state %d: score %r outside 0..%d' % (i, sev['score'], len(levels) - 1))

    def test_usage_and_latency_recorded(self):
        """Print the measurements the gatelog will quote, and assert usage came back."""
        for result in self.results:
            body = result['body']
            usage = body.get('usage') or {}
            print('[laya-live] state %d: child latency %s ms, wall %.0f ms, input_tokens %s'
                  % (result['index'], body.get('latency_ms'), result['wall_ms'],
                     usage.get('input_tokens')))
            self.assertIsInstance(usage.get('input_tokens'), int,
                                  'state %d: usage.input_tokens missing' % result['index'])
            self.assertIsInstance(body.get('latency_ms'), int)

    def test_ledger_carries_probabilities(self):
        """Every live decision is ledgered with its probability distribution."""
        with open(self.ledger, encoding='utf-8') as f:
            records = [json.loads(line) for line in f if line.strip()]
        decides = [r for r in records if r.get('op') == 'decide' and r.get('model') == 'laya']
        self.assertEqual(len(decides), len(STATES),
                         'expected one ledger record per decision, got %d' % len(decides))
        for rec in decides:
            kind = ((rec.get('output') or {}).get('answers') or {}).get('kind') or {}
            probs = kind.get('probabilities')
            self.assertIsInstance(probs, dict, 'ledger lost the probabilities: %r' % rec)
            self.assertAlmostEqual(sum(probs.values()), 1.0, delta=0.02,
                                   msg='ledger probabilities do not sum to 1: %r' % probs)
            self.assertIsInstance(rec.get('confidence'), (int, float),
                                  'ledger confidence (max noul) missing: %r' % rec)
        print('[laya-live] ledger records: %s' % json.dumps(decides))


if __name__ == '__main__':
    unittest.main()
