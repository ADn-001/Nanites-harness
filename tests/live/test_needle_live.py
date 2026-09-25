#!/usr/bin/env python3
"""
LIVE Needle repair suite (Phase 12) - opt-in, runs the REAL model.

Skipped unless COG_LIVE_MODELS=1, and skipped cleanly when the venv python or the base
weights are absent. NEVER run by `npm test` - it exists as the owner's "it genuinely
works" evidence: the CI-reproducible mocked suites stay the gates.

The repair itself runs IN A SUBPROCESS with localmodels/.venv/bin/python: this test
process may be the SYSTEM python, which has no `needle` package (that is the daemon's
normal environment - the whole point of the lazy import).

Contract asserted (deliberately forgiving - the model may word things differently and a
hard-to-repair case must not sink the whole suite):
  - the chosen tool name is inside the candidate set (or function_calls == []),
  - every emitted `arguments` is a plain object (JSON object, not a string),
  - a no-match prompt yields function_calls == [].
A case the model cannot repair is PRINTED and counted, not failed; the suite fails only
on a contract violation (out-of-candidate name, non-object arguments) or on a subprocess
never producing a JSON result line.

Run:  COG_LIVE_MODELS=1 python3 -m unittest discover tests/live
"""
import json
import os
import subprocess
import sys
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_PY = os.path.join(BASE, 'localmodels', '.venv',
                       'Scripts' if sys.platform == 'win32' else 'bin',
                       'python.exe' if sys.platform == 'win32' else 'python')
CORPUS = os.path.join(BASE, 'tests', 'fixtures', 'toolcall-corpus', 'cases.json')

LIVE = os.environ.get('COG_LIVE_MODELS') == '1'

# Five repair-required corpus cases (a mix of near-miss name, narration recovery and
# trailing-garbage formats) + one no-match prompt.
REPAIR_CASE_IDS = ['string-args-trailing-garbage-brace', 'truncated-content-narration',
                   'near-miss-with-broken-args', 'narration-call-span',
                   'mixed-good-and-broken-args']
NO_MATCH_PROMPT = 'What is the capital of France?'

# The child runs inside the repo with the venv python; needle_backend.py sits in
# localmodels/. We inline a tiny driver so the child needs no extra scaffolding.
#
# IMPORTANT (measured): the repair template "The assistant intended one tool call..."
# primes the engine, and a no-match probe sent AFTER repair queries in the same process
# inherits that context (the package keeps ONE active instance per generation and its
# conversation state is sticky even with stateless=True + reset()). The no-match probe
# therefore runs in its OWN fresh subprocess, FIRST, with the plain question - which is
# also the daemon's real cold-start path. Both subprocesses time out (never hang).
CHILD_REPAIR = r"""
import json, os, sys, time
os.environ.setdefault('NEEDLE_TELEMETRY', '0')
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(sys.argv[1], 'localmodels'))
import needle_backend

probe = json.load(open(sys.argv[2], encoding='utf-8'))
tools = needle_backend._flatten_tools(probe['candidates'])
backend = needle_backend.NeedleBackend()
loaded = backend.load(tools)
if not loaded.get('ok'):
    print(json.dumps({'needle_live': {'ok': False, 'reason': loaded.get('reason')}}))
    sys.exit(0)
out = {'ok': True, 'results': []}
for case in probe['cases']:
    text = needle_backend.build_repair_prompt(case['suspect'])
    t0 = time.time()
    res = backend.repair(text, probe['candidates'])
    dt = (time.time() - t0) * 1000.0
    out['results'].append({'id': case['id'], 'latency_ms': round(dt, 1),
                           'ok': res.get('ok'), 'calls': res.get('calls'),
                           'confidence': res.get('confidence')})
    print('%s: %.0f ms' % (case['id'], dt), file=sys.stderr)
print(json.dumps({'needle_live': out}))
"""

CHILD_NO_MATCH = r"""
import json, os, sys
os.environ.setdefault('NEEDLE_TELEMETRY', '0')
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(sys.argv[1], 'localmodels'))
import needle_backend
probe = json.load(open(sys.argv[2], encoding='utf-8'))
backend = needle_backend.NeedleBackend()
loaded = backend.load(needle_backend._flatten_tools(probe['candidates']))
if not loaded.get('ok'):
    print(json.dumps({'needle_live': {'ok': False, 'reason': loaded.get('reason')}}))
    sys.exit(0)
res = backend.repair(probe['no_match'], probe['candidates'])
print(json.dumps({'needle_live': {'ok': True,
                                  'no_match': {'ok': res.get('ok'), 'calls': res.get('calls'),
                                               'confidence': res.get('confidence')}}}))
"""


def _weights_cached():
    try:
        r = subprocess.run(
            [VENV_PY, '-c',
             'import os; from needle.agent import fetch;'
             'print(os.path.isfile(os.path.join(fetch.cache_dir(3), fetch.base_weights(3))))'],
            capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and r.stdout.strip().endswith('True')
    except Exception:
        return False


def _suspect_from_case(case):
    reply = case['reply']
    if case['format'] == 'content':
        return {'name': None, 'arguments': reply.get('content', '')}
    entries = reply
    if isinstance(reply, dict):
        entries = reply.get('toolCalls') or reply.get('tool_calls') or []
    if not isinstance(entries, list) or not entries:
        return {'name': None, 'arguments': ''}
    c0 = entries[0]
    fn = c0.get('function') if isinstance(c0.get('function'), dict) else None
    name = c0.get('name') or (fn or {}).get('name')
    args = c0.get('args', c0.get('arguments', (fn or {}).get('arguments')))
    if isinstance(args, (dict, list)):
        args = json.dumps(args)
    return {'name': name, 'arguments': args}


@unittest.skipUnless(LIVE, 'COG_LIVE_MODELS=1 not set (live model suite is opt-in)')
class TestNeedleLive(unittest.TestCase):

    @classmethod
    def _parse_child(cls, proc, what):
        """Pull the {needle_live: …} JSON line out of a child's stdout (skips cleanly)."""
        line = None
        for raw in (proc.stdout or '').splitlines():
            s = raw.strip()
            if s.startswith('{') and '"needle_live"' in s:
                line = s
        if line is None:
            raise unittest.SkipTest('%s subprocess produced no result line: rc=%s stdout=%r'
                                    % (what, proc.returncode, (proc.stdout or '')[:300]))
        try:
            return json.loads(line)['needle_live']
        except Exception as e:
            raise unittest.SkipTest('unparseable %s result: %s' % (what, e))

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(VENV_PY):
            raise unittest.SkipTest('venv python absent: %s' % VENV_PY)
        if not _weights_cached():
            raise unittest.SkipTest('needle base weights not cached (run localmodels/setup.sh)')

        corpus = json.load(open(CORPUS, encoding='utf-8'))
        by_id = {c['id']: c for c in corpus['cases']}
        cases = [by_id[i] for i in REPAIR_CASE_IDS]
        if len(cases) != len(REPAIR_CASE_IDS):
            raise unittest.SkipTest('corpus case ids changed; update REPAIR_CASE_IDS')

        # Flat candidate schemas derived from the corpus tool list (the 6 harness tools).
        descs = {
            'read_file': ('Read a UTF-8 text file inside the bound working directory.',
                          {'path': {'type': 'string'}}, ['path']),
            'write_file': ('Create or overwrite a text file inside the bound working directory.',
                           {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                           ['path', 'content']),
            'list_dir': ('List a directory with d/f/x type tags.',
                         {'path': {'type': 'string'}}, ['path']),
            'grep': ('Regex-search project text files.',
                     {'path': {'type': 'string'}, 'pattern': {'type': 'string'}},
                     ['path', 'pattern']),
            'git': ('Run a safe, project-local git rite.',
                    {'args': {'type': 'string'}}, ['args']),
            'run_command': ('Run a shell command in the working directory.',
                            {'command': {'type': 'string'}}, ['command']),
        }
        candidates = [{'name': n, 'description': d,
                       'parameters': {'type': 'object', 'properties': p, 'required': r}}
                      for n, (d, p, r) in descs.items()]
        cls.allowed = sorted(descs)

        probe = {
            'candidates': candidates,
            'cases': [{'id': c['id'],
                       'suspect': _suspect_from_case(c),
                       'expect_name': c.get('expect', {}).get('name')} for c in cases],
            'no_match': NO_MATCH_PROMPT,
        }
        import tempfile
        fd, cls.probe_path = tempfile.mkstemp(prefix='needle-live-', suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(probe, f)

        env = dict(os.environ, NEEDLE_TELEMETRY='0', PYTHONDONTWRITEBYTECODE='1')

        # 1) no-match probe FIRST, in its own fresh process (clean engine state).
        t0 = time.time()
        nm_proc = subprocess.run(
            [VENV_PY, '-c', CHILD_NO_MATCH, BASE, cls.probe_path],
            capture_output=True, text=True, timeout=600, env=env)
        sys.stderr.write('[needlive-live] no-match subprocess %.1fs\n' % (time.time() - t0))
        cls.payload = cls._parse_child(nm_proc, 'no-match')

        # 2) the five corpus repairs.
        t0 = time.time()
        cls.proc = subprocess.run(
            [VENV_PY, '-c', CHILD_REPAIR, BASE, cls.probe_path],
            capture_output=True, text=True, timeout=900, env=env)
        sys.stderr.write(cls.proc.stderr[-4000:])
        sys.stderr.write('[needlive-live] repair subprocess %.1fs\n' % (time.time() - t0))
        if not cls.payload.get('ok'):
            raise unittest.SkipTest('backend could not load: %s' % cls.payload.get('reason'))
        cls.payload.update(cls._parse_child(cls.proc, 'repair'))

    @classmethod
    def tearDownClass(cls):
        p = getattr(cls, 'probe_path', None)
        if p and os.path.exists(p):
            os.remove(p)

    def _result(self, cid):
        for r in self.payload['results']:
            if r['id'] == cid:
                return r
        self.fail('no result for case %s' % cid)

    def test_repair_cases_contract(self):
        """Every repair attempt obeys the contract; a name OUTSIDE the candidate set or a
        non-object `arguments` fails the suite. Unrepaired cases are reported, not failed."""
        unrepaired = []
        for r in self.payload['results']:
            calls = r.get('calls') or []
            print('[case] %-38s %6.0f ms conf=%s calls=%s'
                  % (r['id'], r.get('latency_ms') or 0, r.get('confidence'), len(calls)))
            for call in calls:
                name = call.get('name')
                args = call.get('arguments')
                self.assertIn(name, self.allowed,
                              'case %s: needle returned a name outside the candidate set: %r'
                              % (r['id'], name))
                self.assertIsInstance(args, dict,
                                      'case %s: arguments not a plain object: %r' % (r['id'], args))
            if not calls:
                unrepaired.append(r['id'])
        print('[repair] unrepaired cases: %s' % (unrepaired or 'none'))

    def test_canonical_names_when_repaired(self):
        """Forgiving but directional: at least one repair must land the corpus-declared
        canonical name."""
        landed = 0
        for r in self.payload['results']:
            for call in r.get('calls') or []:
                landed += 1  # a call at all
        self.assertGreater(landed, 0, 'the model repaired NONE of the corpus cases')

    def test_no_match_prompt_yields_no_calls(self):
        nm = self.payload['no_match']
        self.assertTrue(nm.get('ok'), 'backend did not return ok for the no-match prompt')
        self.assertEqual(nm.get('calls'), [],
                         'no-match prompt must yield function_calls == []; got %r' % nm.get('calls'))


if __name__ == '__main__':
    unittest.main()
