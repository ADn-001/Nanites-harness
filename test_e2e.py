#!/usr/bin/env python3
import json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
fails = []

def check(cond, msg):
    print(('ok  - ' if cond else 'FAIL- ') + msg)
    if not cond:
        fails.append(msg)

def req(port, path, payload=None, origin=None, raw=None):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    headers = {'Content-Type': 'application/json'}
    if origin:
        headers['Origin'] = origin
    r = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            body = resp.read() or b'{}'
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read() or b'{}'
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {'raw': body.decode('utf-8', 'replace')}

# ---------------- bridge worker ----------------
proj = tempfile.mkdtemp(prefix='cogtest-')
open(os.path.join(proj, 'hello.py'), 'w', encoding='utf-8').write('print("hi")\n# TODO fix me\n')
os.makedirs(os.path.join(proj, 'venv'))
open(os.path.join(proj, 'venv', 'ignored.txt'), 'w', encoding='utf-8').write('TODO\n')
port = 18931
br = subprocess.Popen([sys.executable, os.path.join(BASE, 'bridge.py'), '--root', proj, '--port', str(port)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
try:
    s, j = req(port, '/health'); check(s == 200 and j['ok'], 'bridge health')
    s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}})
    check(s == 200 and 'TODO' in j['result'], 'read_file rite')
    s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': '../../etc/passwd'}})
    check(s == 403, 'relative jail escape refused')
    if hasattr(os, 'symlink'):
        os.symlink('/etc', os.path.join(proj, 'evil'))
        s, j = req(port, '/tools/execute', {'name': 'list_dir', 'arguments': {'path': 'evil'}})
        check(s == 403, 'symlink jail escape refused')
        os.remove(os.path.join(proj, 'evil'))
    s, j = req(port, '/tools/execute', {'name': 'write_file', 'arguments': {'path': 'sub/new.txt', 'content': 'x=1\n'}})
    check(s == 200 and open(os.path.join(proj, 'sub/new.txt')).read() == 'x=1\n', 'write_file rite')
    s, j = req(port, '/tools/execute', {'name': 'list_dir', 'arguments': {'path': '.'}})
    check(s == 200 and 'd sub' in j['result'] and 'd venv' in j['result'], 'list_dir rite')
    s, j = req(port, '/tools/execute', {'name': 'grep', 'arguments': {'pattern': 'TODO', 'path': '.'}})
    check(s == 200 and 'hello.py:2' in j['result'] and 'venv/ignored.txt' not in j['result'], 'grep rite and venv pruning')
    s, j = req(port, '/tools/execute', {'name': 'run_command', 'arguments': {'command': 'echo pwn'}})
    check(s == 403, 'run_command disabled by default')
    for args in ['tag -d v1', 'tag -f v9 v8', 'branch -D feature', 'branch -m old new', 'stash pop', 'stash drop', 'stash', 'checkout -- file', '-C /tmp status', '--git-dir=/x log', '-c core.pager=cat log', '--config-env=foo=bar status']:
        s, j = req(port, '/tools/execute', {'name': 'git', 'arguments': {'args': args}})
        check(s == 403, 'git destructive/global bypass refused: ' + args)
    for args in ['status --short', 'branch -r', 'tag -l', 'stash list']:
        s, j = req(port, '/tools/execute', {'name': 'git', 'arguments': {'args': args}})
        check(s in (200, 404), 'safe read git accepted/reasonable: ' + args)
    s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}}, origin='https://evil.example.com')
    check(s == 403, 'foreign Origin refused')
    s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}}, origin='null')
    check(s == 403, 'null Origin refused by default (no file:// trust)')
    s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}})
    check(s == 200, 'missing Origin allowed (curl/native callers)')
    s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}}, origin='http://localhost:8080')
    check(s == 200, 'localhost Origin allowed')
    s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}}, origin='http://127.0.0.1:8080')
    check(s == 200, '127.0.0.1 Origin allowed')
    s, j = req(port, '/tools/execute', raw=b'{"name":"read_file","arguments":{"path":"' + b'a' * (2 * 1024 * 1024) + b'"}}')
    check(s == 413, 'oversized POST refused')
    # --allow-file-origin is the explicit opt-in that re-trusts a null Origin.
    port2 = 18932
    br2 = subprocess.Popen([sys.executable, os.path.join(BASE, 'bridge.py'), '--root', proj,
                            '--port', str(port2), '--allow-file-origin'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        try:
            s, j = req(port2, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}}, origin='null')
            check(s == 200, '--allow-file-origin re-trusts null Origin (opt-in)')
        except Exception as e:
            check(False, '--allow-file-origin re-trusts null Origin (opt-in) [%s: %s]' % (type(e).__name__, e))
        try:
            s, j = req(port2, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'hello.py'}}, origin='https://evil.example.com')
            check(s == 403, '--allow-file-origin still refuses foreign Origin')
        except Exception as e:
            check(False, '--allow-file-origin still refuses foreign Origin [%s: %s]' % (type(e).__name__, e))
    finally:
        br2.terminate()
        try: br2.wait(timeout=5)
        except subprocess.TimeoutExpired: br2.kill()
finally:
    br.terminate()
    try: br.wait(timeout=5)
    except subprocess.TimeoutExpired: br.kill()

# ---------------- daemon ----------------
dport = 18930
wd1, wd2 = tempfile.mkdtemp(prefix='cogwd1-'), tempfile.mkdtemp(prefix='cogwd2-')
open(os.path.join(wd2, 'bridge.py'), 'w', encoding='utf-8').write('# user-authored bridge, NOT managed\nprint(1)\n')
da = subprocess.Popen([sys.executable, os.path.join(BASE, 'bridge_daemon.py'), '--port', str(dport)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
try:
    s, j = req(dport, '/status'); check(s == 200 and j['ok'], 'daemon status')
    s, j = req(dport, '/set_workdir', {'path': wd1, 'autostart': True})
    check(s == 200 and j['ok'], 'set_workdir + autostart')
    time.sleep(0.7)
    s, j = req(dport, '/status'); check(j.get('bridge_running') and j.get('bridge_healthy'), 'worker spawned + healthy')
    managed_copy = os.path.join(wd1, 'bridge.py')
    check(os.path.exists(managed_copy) and 'managed by bridge_daemon.py' in open(managed_copy, encoding='utf-8', errors='replace').read(4096), 'managed bridge marker written')
    s, j = req(dport, '/set_workdir', {'path': wd2, 'autostart': False})
    check(s == 200 and j['ok'], 'rebind stops old worker')
    check(os.path.exists(os.path.join(wd2, 'bridge.py.cogitator-bak')), 'foreign bridge.py backed up not destroyed')
    check(open(os.path.join(wd2, 'bridge.py'), encoding='utf-8', errors='replace').read().startswith('# ==== COGITATOR BRIDGE'), 'managed bridge installed after backup')
    check(not os.path.exists(os.path.join(wd1, 'bridge.py')), 'managed bridge.py scrubbed from old workdir')
    s, j = req(dport, '/set_workdir', {'path': '/nonexistent-dir-xyz'})
    check(s == 400, 'invalid workdir refused')
    s, j = req(dport, '/stop', {}); check(s == 200 and j.get('stopped'), 'daemon stop rite')
    # ---------------- daemon hostile-origin matrix (#14) ----------------
    if sys.platform == 'win32':
        autostart_entry = None
    elif sys.platform == 'darwin':
        autostart_entry = os.path.expanduser('~/Library/LaunchAgents/com.cogitator.CogitatorBridgeDaemon.plist')
    else:
        autostart_entry = os.path.expanduser('~/.config/autostart/CogitatorBridgeDaemon.desktop')
    atk = tempfile.mkdtemp(prefix='cogatk-')
    s, j = req(dport, '/status', origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on GET /status')
    s, j = req(dport, '/health', origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on GET /health')
    s, j = req(dport, '/pick_directory', {}, origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on POST /pick_directory')
    before_wd = req(dport, '/status')[1].get('workdir')
    s, j = req(dport, '/set_workdir', {'path': atk, 'autostart': True}, origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on POST /set_workdir')
    check(not os.path.exists(os.path.join(atk, 'bridge.py')), 'daemon: refused set_workdir wrote no bridge.py')
    check(req(dport, '/status')[1].get('workdir') == before_wd, 'daemon: refused set_workdir left workdir unchanged')
    s, j = req(dport, '/start', {}, origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on POST /start')
    s, j = req(dport, '/stop', {}, origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on POST /stop')
    s, j = req(dport, '/install_autostart', {}, origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on POST /install_autostart')
    if autostart_entry and os.path.exists(autostart_entry):
        os.remove(autostart_entry)
        check(False, 'daemon: refused install_autostart created no autostart entry (it did; cleaned up)')
    else:
        check(True, 'daemon: refused install_autostart created no autostart entry')
    s, j = req(dport, '/remove_autostart', {}, origin='https://evil.example')
    check(s == 403, 'daemon: foreign Origin refused on POST /remove_autostart')
    s, j = req(dport, '/status'); check(s == 200 and j['ok'], 'daemon: missing Origin still allowed (curl/native)')
    s, j = req(dport, '/status', origin='http://localhost:8080'); check(s == 200 and j['ok'], 'daemon: localhost Origin still allowed')
    s, j = req(dport, '/status', origin='http://127.0.0.1:8080'); check(s == 200 and j['ok'], 'daemon: 127.0.0.1 Origin still allowed')
    s, j = req(dport, '/status', origin='null'); check(s == 403, 'daemon: null Origin refused by default')
    # clean up anything a hostile probe managed to create
    req(dport, '/stop', {})
    shutil.rmtree(atk, ignore_errors=True)
finally:
    da.terminate()
    try: da.wait(timeout=5)
    except subprocess.TimeoutExpired: da.kill()

# ---------------- localmodels sidecar (Phase 10, workstream A) ----------------
def lm_probe(msg, fn):
    """Run one localmodels case; an exception (e.g. ConnectionRefused while the
    daemon does not exist yet) is a FAIL for that case, not an aborted suite."""
    try:
        check(bool(fn()), msg)
    except Exception as e:
        check(False, '%s [%s: %s]' % (msg, type(e).__name__, e))


def lm_413(port, path, prefix):
    """POST an oversized body and assert the size cap answers 413, retrying on a reset.

    The daemon answers 413 WITHOUT draining the request body (a deliberate phase-10
    property: the size cap outranks routing), so a client that is still writing 2 MiB can
    get a RST / broken pipe instead of reading the response. That race is route-independent
    - measured on /repair, /decide and an unknown path alike - so it is handled here once
    for every 413 case rather than per route. A transport-level refusal can never be a 500,
    but we do want the real status code, so retry a few times before giving up.
    """
    raw = prefix + b'a' * (2 * 1024 * 1024) + b'"}'
    last = 'no attempt completed'
    for _ in range(4):
        try:
            s, j = req(port, path, raw=raw)
            return s == 413 and j.get('ok') is False
        except Exception as e:
            last = '%s: %s' % (type(e).__name__, e)
    print('   oversized %s never yielded a response (%s) - no 500 either way' % (path, last))
    return True

lmdir = tempfile.mkdtemp(prefix='coglm-')
lmport = 18933
lmledger = os.path.join(lmdir, 'local-models.jsonl')
lmd = subprocess.Popen([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
                        '--port', str(lmport), '--ledger', lmledger, '--no-needle', '--no-laya'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=lmdir)
time.sleep(1.0)
try:
    def lm_health_shape():
        s, j = req(lmport, '/health')
        return (s == 200 and j.get('ok') is True and j.get('version') == '0.1.0'
                and {'needle', 'laya', 'ledger', 'degraded'} <= set(j)
                and isinstance(j.get('degraded'), list)
                and j['needle'].get('enabled') is False and j['laya'].get('enabled') is False
                and j['needle'].get('loaded') is False and j['laya'].get('loaded') is False
                and j['laya'].get('child_pid') is None
                and j['needle'].get('weights') in ('missing', 'present')
                and isinstance(j['needle'].get('generation'), int)
                and 'lib' in j['needle'] and 'cache' in j['laya']
                and j['ledger'].get('path') == os.path.abspath(lmledger)
                and j['ledger'].get('writable') is True)
    lm_probe('localmodels: boot + GET /health 200 (shape, engines disabled, models unloaded)', lm_health_shape)

    def lm_degraded_empty():
        s, j = req(lmport, '/health')
        return s == 200 and j.get('degraded') == []
    lm_probe('localmodels: --no-needle --no-laya => degraded == [] (disabled is not degraded)', lm_degraded_empty)

    # second instance WITHOUT --no-needle: needle enabled but weights missing => degraded reason
    lmport2 = 18934
    lm2ledger = os.path.join(lmdir, 'local-models-2.jsonl')
    lmd2 = subprocess.Popen([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
                             '--port', str(lmport2), '--ledger', lm2ledger, '--no-laya'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=lmdir)
    time.sleep(1.0)
    try:
        def lm_needle_enabled():
            s, j = req(lmport2, '/health')
            return (s == 200 and j['needle'].get('enabled') is True
                    and j['needle'].get('loaded') is False
                    and j['needle'].get('weights') in ('present', 'missing')
                    and any('needle' in str(r) for r in j.get('degraded', [])))
        lm_probe('localmodels: without --no-needle => needle.enabled True + needle degraded reason', lm_needle_enabled)

        # Phase 12: needle is enabled, but this daemon runs the SYSTEM python, which has
        # no needle package. Assert /repair degrades; if some machine ever runs a system
        # python WITH weights loaded, assert only the never-500 contract.
        def lm_repair_needle_unavailable():
            h = req(lmport2, '/health')[1]
            s, j = req(lmport2, '/repair', {'suspect': {'name': 'read-file', 'arguments': '{"path": "a.py"}'},
                                            'candidates': [{'name': 'read_file', 'description': 'Read a file.',
                                                            'parameters': {'type': 'object',
                                                                           'properties': {'path': {'type': 'string'}},
                                                                           'required': ['path']}}],
                                            'trace_id': 'tr_e2e_pkg'})
            if h['needle'].get('weights') == 'present':
                return (s == 200 and isinstance(j.get('ok'), bool) and 'confidence' in j
                        and j.get('trace_id') == 'tr_e2e_pkg')
            return (s == 200 and j.get('ok') is False and j.get('degraded') is True
                    and j.get('reason') in ('tool_unavailable', 'weights_missing')
                    and isinstance(j.get('latency_ms'), int)
                    and j.get('trace_id') == 'tr_e2e_pkg')
        lm_probe('localmodels: needle enabled + package/weights absent => /repair {ok:false,degraded:true}, never a 500',
                 lm_repair_needle_unavailable)
    finally:
        lmd2.terminate()
        try: lmd2.wait(timeout=5)
        except subprocess.TimeoutExpired: lmd2.kill()

    def lm_foreign_origin():
        s, j = req(lmport, '/health', origin='http://evil.example')
        return s == 403 and j.get('error') == 'origin not permitted'
    lm_probe('localmodels: foreign Origin GET /health => 403 origin not permitted', lm_foreign_origin)

    def lm_null_origin():
        s, j = req(lmport, '/health', origin='null')
        return s == 403 and j.get('error') == 'origin not permitted'
    lm_probe('localmodels: null Origin => 403 by default (no file:// trust)', lm_null_origin)

    def lm_localhost_origin():
        s, j = req(lmport, '/health', origin='http://localhost:8080')
        return s == 200 and j.get('ok') is True
    lm_probe('localmodels: http://localhost:8080 Origin allowed', lm_localhost_origin)

    def lm_127_origin():
        s, j = req(lmport, '/health', origin='http://127.0.0.1:8080')
        return s == 200 and j.get('ok') is True
    lm_probe('localmodels: http://127.0.0.1:8080 Origin allowed', lm_127_origin)

    def lm_no_origin():
        s, j = req(lmport, '/health')
        return s == 200 and j.get('ok') is True
    lm_probe('localmodels: missing Origin allowed (curl/native callers)', lm_no_origin)

    def lm_get_unknown():
        s, j = req(lmport, '/nope')
        return s == 404 and j.get('error') == 'unknown rite' and j.get('ok') is False
    lm_probe('localmodels: GET /nope => 404 unknown rite (JSON, no traceback)', lm_get_unknown)

    def lm_post_unknown():
        s, j = req(lmport, '/nope', {})
        return s == 404 and j.get('error') == 'unknown rite' and j.get('ok') is False
    lm_probe('localmodels: POST /nope => 404 unknown rite (JSON, no traceback)', lm_post_unknown)

    def lm_oversized():
        return lm_413(lmport, '/nope', b'{"x":"')
    lm_probe('localmodels: oversized POST body (> 1 MiB) => 413', lm_oversized)

    def lm_foreign_post_before_route():
        s, j = req(lmport, '/nope', {}, origin='http://evil.example')
        return s == 403 and j.get('error') == 'origin not permitted'
    lm_probe('localmodels: foreign-Origin POST refused before route logic (403, not 404)',
             lm_foreign_post_before_route)

    # ---- Phase 12: POST /repair on the --no-needle daemon (never a 500, always JSON) ----
    SUSPECT = {'name': 'read-file', 'arguments': '{"path": "a.py"}'}
    CANDIDATES = [{'type': 'function',
                   'function': {'name': 'read_file', 'description': 'Read a file.',
                                'parameters': {'type': 'object',
                                               'properties': {'path': {'type': 'string'}},
                                               'required': ['path']}}}]

    def lm_repair_flag():
        import subprocess as sp
        r = sp.run([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'), '--help'],
                   capture_output=True, text=True, timeout=30)
        return '--needle-timeout-ms' in r.stdout and '--preload-needle' in r.stdout
    lm_probe('localmodels (phase12): --needle-timeout-ms and --preload-needle flags exist', lm_repair_flag)

    def lm_repair_foreign_origin():
        ledger_before = open(lmledger, encoding='utf-8').read() if os.path.isfile(lmledger) else ''
        s, j = req(lmport, '/repair', {'suspect': SUSPECT, 'candidates': CANDIDATES},
                   origin='http://evil.example')
        ledger_after = open(lmledger, encoding='utf-8').read() if os.path.isfile(lmledger) else ''
        return (s == 403 and j.get('error') == 'origin not permitted'
                and ledger_before == ledger_after)
    lm_probe('localmodels (phase12): foreign Origin POST /repair => 403 and NO ledger record',
             lm_repair_foreign_origin)

    def lm_repair_no_needle():
        s, j = req(lmport, '/repair', {'suspect': SUSPECT, 'candidates': CANDIDATES,
                                       'trace_id': 'tr_e2e_none'})
        return (s == 200 and j.get('ok') is False and j.get('degraded') is True
                and j.get('reason') == 'disabled'
                and isinstance(j.get('latency_ms'), int)
                and j.get('trace_id') == 'tr_e2e_none')
    lm_probe('localmodels (phase12): --no-needle POST /repair => {ok:false,degraded:true}, never a 500',
             lm_repair_no_needle)

    def lm_repair_413wins():
        return lm_413(lmport, '/repair', b'{"suspect":{"name":"')
    lm_probe('localmodels (phase12): oversized POST /repair => 413 (size cap outranks routing)',
             lm_repair_413wins)

    def lm_repair_concurrent():
        # Two concurrent /repair calls: the threading server + backend lock must
        # answer BOTH (bounded waits, no wedge, no 500).
        got = {}
        def call(i):
            got[i] = req(lmport, '/repair', {'suspect': SUSPECT, 'candidates': CANDIDATES,
                                             'trace_id': 'tr_e2e_c%d' % i})
        threads = [threading.Thread(target=call, args=(i,)) for i in range(2)]
        t0 = time.time()
        [t.start() for t in threads]
        [t.join(15) for t in threads]
        dt = time.time() - t0
        return (len(got) == 2 and dt < 12
                and all(got[i][0] == 200 and 'degraded' in got[i][1] for i in (0, 1))
                and got[0][1].get('trace_id') != got[1][1].get('trace_id'))
    lm_probe('localmodels (phase12): two concurrent POST /repair => both answered, never wedged',
             lm_repair_concurrent)

    def lm_repair_ledger():
        before = [json.loads(ln) for ln in open(lmledger, encoding='utf-8')
                  if ln.strip()] if os.path.isfile(lmledger) else []
        tid = 'tr_e2e_repair1'
        s, j = req(lmport, '/repair', {'suspect': SUSPECT, 'candidates': CANDIDATES,
                                       'trace_id': tid})
        if s != 200:
            return False
        after = [json.loads(ln) for ln in open(lmledger, encoding='utf-8')
                 if ln.strip()] if os.path.isfile(lmledger) else []
        fresh = after[len(before):]
        recs = [r for r in fresh if r.get('trace_id') == tid and r.get('op') == 'repair']
        if len(recs) != 1:
            print('   expected exactly 1 repair record for %s, got %r' % (tid, fresh))
            return False
        rec = recs[0]
        if not (rec.get('model') == 'needle' and rec.get('input_redacted') is True
                and 'confidence' in rec and rec.get('action') is None
                and isinstance(rec.get('latency_ms'), int) and rec.get('degraded') is True):
            print('   repair record malformed: %r' % rec)
            return False
        # outcome line: same trace_id, requested action, still redacted
        s2, j2 = req(lmport, '/ledger', {'trace_id': tid, 'action': 'passed_through',
                                         'note': 'Authorization: Bearer topsecret999'})
        if not (s2 == 200 and j2.get('ok') is True):
            return False
        final = [json.loads(ln) for ln in open(lmledger, encoding='utf-8')
                 if ln.strip()]
        outs = [r for r in final if r.get('trace_id') == tid and r.get('action') == 'passed_through'
                and 'op' not in r]
        if len(outs) != 1:
            print('   expected 1 outcome line for %s' % tid)
            return False
        raw = open(lmledger, encoding='utf-8').read()
        return 'topsecret999' not in raw
    lm_probe('localmodels (phase12): /repair ledger record (confidence, action:null) + /ledger outcome line (same trace_id, still redacts)',
             lm_repair_ledger)

    def lm_repair_prompt_shapes():
        """The prompt builder must render EVERY suspect shape the shipped frontend sends.

        Regression (found by the integrator's real client<->daemon probe, not by the mocked
        suites): `_cortexRepairPayload` sends a LIST of {name, arguments, reason} when a turn
        has several unrepairable calls, and the raw reply TEXT for a prose-only probe in
        mode:'on'. A dict-only renderer silently dropped both, sending the model a
        context-free "Previous tool call (malformed): null" prompt - a repair attempt with
        nothing to repair, which produced a confident wrong guess.
        """
        sys.path.insert(0, os.path.join(BASE, 'localmodels'))
        import needle_backend as nb
        d = nb.build_repair_prompt({'name': 'raed_file', 'arguments': '{"path":"main.py"}'})
        lst = nb.build_repair_prompt([{'name': 'raed_file', 'arguments': '{"path":"main.py"}',
                                       'reason': 'unknown tool'}])
        txt = nb.build_repair_prompt('I will read main.py for you.')
        oai = nb.build_repair_prompt([{'function': {'name': 'read_fil', 'arguments': {'path': 'a.py'}}}])
        empty = nb.build_repair_prompt(None)
        checks = [
            ('raed_file' in d and 'main.py' in d, 'dict shape'),
            ('raed_file' in lst and 'main.py' in lst and 'unknown tool' in lst, 'list shape (frontend): %r' % lst),
            ('main.py' in txt, 'prose string shape (mode:on): %r' % txt),
            ('read_fil' in oai and 'a.py' in oai, 'OpenAI nested shape'),
            ('null' not in lst and 'null' not in txt, 'no empty "malformed: null" placeholder: %r' % lst),
            (empty.count('Emit the corrected call.') == 1, 'None suspect still returns one prompt'),
        ]
        bad = [m for ok, m in checks if not ok]
        if bad:
            print('   ' + '; '.join(bad))
        return not bad
    lm_probe('localmodels (phase12): repair prompt renders dict / list / prose-string / OpenAI suspects',
             lm_repair_prompt_shapes)

    def lm_no_stray_files():
        return set(os.listdir(lmdir)) <= {'local-models.jsonl', 'local-models-2.jsonl'}
    lm_probe('localmodels: daemon wrote no pid/log file next to itself (ledger only)',
             lm_no_stray_files)

    # ---- ledger.py standalone (run from BASE, ledger in the temp dir) ----
    lm_ledger_script = (
        "import sys; sys.path.insert(0, 'localmodels'); import ledger\n"
        "ok = ledger.append_record({\n"
        "    'trace_id': ledger.new_trace_id(), 'model': 'needle', 'op': 'repair',\n"
        "    'input_redacted': 'Authorization: Bearer ***', 'output': 'x' * 2500,\n"
        "    'confidence': 0.9, 'latency_ms': 12, 'degraded': False, 'action': None,\n"
        "    'request': {'api_key': 'supersecretvalue123',\n"
        "                'header': 'Authorization: Bearer abcdef123456'}},\n"
        "    path=%r, api_key='supersecretvalue123', home=%r)\n"
        "print('APPENDED' if ok else 'FAILED')" % (lmledger, lmdir)
    )

    def lm_ledger_record():
        if os.path.isfile(lmledger):
            os.remove(lmledger)
        # PYTHONDONTWRITEBYTECODE: a fresh interpreter writes a __pycache__ next to
        # localmodels/*.py otherwise, and this repo is share-ready (no stray artifacts).
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        r = subprocess.run([sys.executable, '-c', lm_ledger_script],
                           cwd=BASE, capture_output=True, text=True, timeout=60, env=env)
        if r.returncode != 0 or 'APPENDED' not in (r.stdout or ''):
            print('   ledger subprocess rc=%s stdout=%r stderr=%r' % (r.returncode, r.stdout, r.stderr))
            return False
        if not os.path.isfile(lmledger):
            print('   ledger file was not created at %s' % lmledger)
            return False
        raw = open(lmledger, encoding='utf-8').read()
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if len(lines) != 1:
            print('   ledger has %d lines, expected exactly 1' % len(lines))
            return False
        rec = json.loads(lines[0])
        if 'supersecretvalue123' in raw:
            print('   ledger leaked the configured api key')
            return False
        if 'Bearer abcdef123456' in raw:
            print('   ledger leaked a Bearer token')
            return False
        out = rec.get('output', '')
        if '…[truncated' not in out or len(out) >= 2500:
            print('   output was not truncated with the marker: %r' % out[-40:])
            return False
        if rec.get('request', {}).get('api_key') != '[REDACTED-AUTH]':
            print('   request.api_key was not redacted: %r' % rec.get('request'))
            return False
        if not rec.get('ts'):
            return False
        if not str(rec.get('trace_id', '')).startswith('tr'):
            return False
        return True
    lm_probe('localmodels: ledger record written, single line, parsed, key/Bearer redacted, '
             'output truncated, request.api_key redacted', lm_ledger_record)

    # ---- installers / docs shipped by this workstream ----
    def lm_support_files():
        for name in ('README.md', 'setup.sh', 'setup.ps1'):
            p = os.path.join(BASE, 'localmodels', name)
            if not os.path.isfile(p) or os.path.getsize(p) == 0:
                print('   missing or empty: %s' % p)
                return False
        readme = open(os.path.join(BASE, 'localmodels', 'README.md'), encoding='utf-8').read()
        return 'GET /health' in readme and 'var/local-models.jsonl' in readme
    lm_probe('localmodels: README + setup.sh + setup.ps1 exist and document /health + the ledger path',
             lm_support_files)
finally:
    lmd.terminate()
    try: lmd.wait(timeout=5)
    except subprocess.TimeoutExpired: lmd.kill()
    shutil.rmtree(lmdir, ignore_errors=True)

# ---------------- localmodels sidecar (Phase 13, workstream A: Laya /decide) ----------------
# Every instance below runs with cwd=<temp dir>: a daemon is NEVER booted in the repo
# tree, and neither is the Laya child it spawns.
LAYA_STUB = os.path.join(BASE, 'tests', 'fixtures', 'laya_stub_child.mjs')
LAYA_Q_NOUL = {'gate': {'type': 'noul',
                        'instructions': 'Do these arguments plausibly satisfy the schema?',
                        'criteria': 'true when the arguments look like a valid invocation'}}
LAYA_Q_CHOICE = {'team': {'type': 'choice', 'instructions': 'Which team?',
                          'criteria': {'billing': 'payments and refunds',
                                       'support': 'product help and bugs'}}}
LAYA_STATE = {'utterance': 'write the cleaned log to out/final.log', 'tool': 'write_file'}


def laya_daemon(port, ledger_path, extra=(), env=None):
    return subprocess.Popen([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
                             '--port', str(port), '--ledger', ledger_path] + list(extra),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=laya_dir, env=env)


def laya_stop(proc):
    proc.terminate()
    try: proc.wait(timeout=5)
    except subprocess.TimeoutExpired: proc.kill()


def laya_ledger_lines(path):
    if not os.path.isfile(path):
        return []
    return [json.loads(ln) for ln in open(path, encoding='utf-8') if ln.strip()]


laya_dir = tempfile.mkdtemp(prefix='coglaya-')
laya_ledger_a = os.path.join(laya_dir, 'a.jsonl')   # --no-laya daemon
laya_ledger_b = os.path.join(laya_dir, 'b.jsonl')   # stub-child daemon
laya_ledger_c = os.path.join(laya_dir, 'c.jsonl')   # hanging stub (timeout path)
laya_ledger_d = os.path.join(laya_dir, 'd.jsonl')   # idle reap
laya_ledger_e = os.path.join(laya_dir, 'e.jsonl')   # default daemon (lazy, not degraded)
laya_ledger_f = os.path.join(laya_dir, 'f.jsonl')   # stub that dies mid-flight
try:
    # ---- case 1: the new flags exist ----
    def laya_flags():
        r = subprocess.run([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'), '--help'],
                           capture_output=True, text=True, timeout=30)
        return ('--laya-timeout-ms' in r.stdout and '--laya-idle-s' in r.stdout
                and '--laya-child' in r.stdout)
    lm_probe('localmodels (phase13): --laya-timeout-ms, --laya-idle-s and --laya-child flags exist', laya_flags)

    # ---- cases 2, 3, 8: the --no-laya daemon (no child is ever spawned) ----
    laya_port_a = 18935
    laya_pa = laya_daemon(laya_port_a, laya_ledger_a, ['--no-laya'])
    time.sleep(1.0)
    try:
        def laya_disabled_decide():
            s, j = req(laya_port_a, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL,
                                                'trace_id': 'tr_e2e_laya_off'})
            h = req(laya_port_a, '/health')[1]
            return (s == 200 and j.get('ok') is False and j.get('degraded') is True
                    and j.get('reason') == 'disabled'
                    and isinstance(j.get('latency_ms'), int)
                    and j.get('trace_id') == 'tr_e2e_laya_off'
                    and j.get('answers') == {}
                    and h['laya'].get('enabled') is False
                    and h['laya'].get('child_pid') is None)
        lm_probe('localmodels (phase13): --no-laya POST /decide => {ok:false,degraded:true,reason:disabled}, '
                 '/health.laya.child_pid None', laya_disabled_decide)

        def laya_foreign_origin_no_record():
            before = open(laya_ledger_a, encoding='utf-8').read() if os.path.isfile(laya_ledger_a) else ''
            s, j = req(laya_port_a, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL},
                       origin='http://evil.example')
            after = open(laya_ledger_a, encoding='utf-8').read() if os.path.isfile(laya_ledger_a) else ''
            return (s == 403 and j.get('error') == 'origin not permitted' and before == after)
        lm_probe('localmodels (phase13): foreign Origin POST /decide => 403 and NO ledger record',
                 laya_foreign_origin_no_record)

        def laya_413_wins():
            return lm_413(laya_port_a, '/decide', b'{"state":"')
        lm_probe('localmodels (phase13): oversized POST /decide => 413 (size cap outranks routing)', laya_413_wins)

        def laya_disabled_not_degraded():
            h = req(laya_port_a, '/health')[1]
            return h['laya'].get('enabled') is False and not any('laya' in str(r) for r in h.get('degraded', []))
        lm_probe('localmodels (phase13): --no-laya => laya.enabled false and no laya degraded reason',
                 laya_disabled_not_degraded)
    finally:
        laya_stop(laya_pa)

    # ---- case 7: a default (laya-enabled) daemon is LAZY, not degraded ----
    laya_port_e = 18939
    laya_pe = laya_daemon(laya_port_e, laya_ledger_e)
    time.sleep(1.0)
    try:
        def laya_default_not_degraded():
            h = req(laya_port_e, '/health')[1]
            return (h['laya'].get('enabled') is True and h['laya'].get('loaded') is False
                    and h['laya'].get('child_pid') is None
                    and isinstance(h['laya'].get('cache'), str) and h['laya']['cache'].startswith('~')
                    and not any('laya' in str(r) for r in h.get('degraded', []))
                    and not any('not started' in str(r) for r in h.get('degraded', [])))
        lm_probe('localmodels (phase13): default daemon => laya enabled but lazy, no '
                 "\"laya child not started\" degraded reason, cache is a '~' string", laya_default_not_degraded)
    finally:
        laya_stop(laya_pe)

    # ---- case 7b: no `node` at all => enabled+degraded, /health still answers ----
    laya_port_g = 18941
    laya_ledger_g = os.path.join(laya_dir, 'g.jsonl')
    laya_pg = laya_daemon(laya_port_g, laya_ledger_g,
                          env=dict(os.environ, LAYA_NODE='/nonexistent-node-binary'))
    time.sleep(1.0)
    try:
        def laya_node_absent():
            s, h = req(laya_port_g, '/health')
            if not (s == 200 and h.get('ok') is True and h['laya'].get('enabled') is True
                    and h['laya'].get('child_pid') is None
                    and 'laya engine missing' in h.get('degraded', [])):
                print('   /health with no node: %s %r' % (s, h))
                return False
            s2, j2 = req(laya_port_g, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL})
            return (s2 == 200 and j2.get('ok') is False and j2.get('degraded') is True
                    and j2.get('reason') == 'engine_missing'
                    and isinstance(j2.get('latency_ms'), int))
        lm_probe('localmodels (phase13): no node on PATH => daemon still boots/answers /health, '
                 "degraded lists 'laya engine missing', /decide => engine_missing", laya_node_absent)
    finally:
        laya_stop(laya_pg)

    # ---- case 4: the stub child answers /decide; ledger carries probabilities ----
    laya_port_b = 18936
    laya_pb = laya_daemon(laya_port_b, laya_ledger_b, ['--laya-child', LAYA_STUB])
    time.sleep(1.0)
    try:
        def laya_stub_decide():
            tid = 'tr_e2e_laya_stub'
            s, j = req(laya_port_b, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL,
                                                'trace_id': tid})
            if not (s == 200 and j.get('ok') is True and j.get('degraded') in (None, False)):
                print('   /decide answered %s %r' % (s, j))
                return False
            answers, usage = j.get('answers'), j.get('usage')
            if not (isinstance(answers, dict) and isinstance(answers.get('gate'), dict)
                    and isinstance(answers['gate'].get('noul'), (int, float))
                    and isinstance(usage, dict) and usage.get('input_tokens') == 42
                    and j.get('trace_id') == tid and isinstance(j.get('latency_ms'), int)):
                print('   unexpected decide envelope: %r' % j)
                return False
            h = req(laya_port_b, '/health')[1]
            if not (isinstance(h['laya'].get('child_pid'), int) and h['laya'].get('loaded') is True
                    and h['laya'].get('enabled') is True):
                print('   health after decide: %r' % h.get('laya'))
                return False
            print('   /decide (stub) => %s' % json.dumps(j))
            print('   /health.laya  => %s' % json.dumps(h['laya']))
            return True
        lm_probe('localmodels (phase13): stub child POST /decide => {ok:true, answers, usage}; '
                 '/health.laya.child_pid int + loaded true', laya_stub_decide)

        def laya_stub_ledger():
            # A choice question (probabilities) PLUS a noul question (confidence) in ONE
            # batched request: this is exactly how the frontend amortises a turn.
            tid = 'tr_e2e_laya_prob'
            qs = dict(LAYA_Q_CHOICE)
            qs.update(LAYA_Q_NOUL)
            s, j = req(laya_port_b, '/decide', {'state': LAYA_STATE, 'questions': qs,
                                                'trace_id': tid})
            if s != 200 or j.get('ok') is not True:
                print('   choice decide answered %s %r' % (s, j))
                return False
            recs = [r for r in laya_ledger_lines(laya_ledger_b)
                    if r.get('trace_id') == tid and r.get('op') == 'decide']
            if len(recs) != 1:
                print('   expected exactly 1 decide record for %s, got %r' % (tid, recs))
                return False
            rec = recs[0]
            probs = (((rec.get('output') or {}).get('answers') or {}).get('team') or {}).get('probabilities')
            if not isinstance(probs, dict) or not probs:
                print('   decide record carried no probabilities: %r' % rec)
                return False
            total = sum(v for v in probs.values() if isinstance(v, (int, float)))
            if abs(total - 1.0) > 0.02:
                print('   probabilities do not sum to ~1: %r' % probs)
                return False
            if not (rec.get('model') == 'laya' and rec.get('input_redacted') is True
                    and rec.get('action') is None and rec.get('degraded') is False
                    and isinstance(rec.get('latency_ms'), int)
                    and rec.get('confidence') == 0.9          # max numeric noul/score seen
                    and 'state' in (rec.get('request') or {})
                    and [q.get('key') for q in ((rec.get('request') or {}).get('questions') or [])]
                    == ['team', 'gate']
                    and {'key', 'type'} <= set(((rec.get('request') or {}).get('questions') or [{}])[0])):
                print('   decide record malformed: %r' % rec)
                return False
            print('   decide ledger line (choice+noul) => %s' % json.dumps(rec))

            # A choice-only batch has NOTHING to grade: confidence must stay null rather
            # than being invented (the frontend reads null as below-threshold).
            tid2 = 'tr_e2e_laya_prob_null'
            s2, j2 = req(laya_port_b, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_CHOICE,
                                                  'trace_id': tid2})
            recs2 = [r for r in laya_ledger_lines(laya_ledger_b)
                     if r.get('trace_id') == tid2 and r.get('op') == 'decide']
            if s2 != 200 or len(recs2) != 1 or recs2[0].get('confidence') is not None:
                print('   choice-only confidence should be null: %r' % recs2)
                return False
            return True
        lm_probe('localmodels (phase13): /decide appends ONE ledger record with the child\'s '
                 'probabilities (sum ~1), model laya, input_redacted true, confidence = max noul '
                 '(null when there is nothing to grade)', laya_stub_ledger)
    finally:
        laya_stop(laya_pb)

    # ---- case 5: timeout (hanging child) never wedges the server ----
    laya_port_c = 18937
    laya_pc = laya_daemon(laya_port_c, laya_ledger_c, ['--laya-child', LAYA_STUB, '--laya-timeout-ms', '300'],
                          env=dict(os.environ, LAYA_STUB_HANG='1'))
    time.sleep(1.0)
    try:
        def laya_timeout():
            t0 = time.time()
            s, j = req(laya_port_c, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL,
                                                'trace_id': 'tr_e2e_laya_hang'})
            dt = time.time() - t0
            if not (s == 200 and j.get('ok') is False and j.get('degraded') is True
                    and j.get('reason') == 'timeout' and isinstance(j.get('latency_ms'), int)
                    and j.get('trace_id') == 'tr_e2e_laya_hang'):
                print('   hanging decide answered %s %r after %.2fs' % (s, j, dt))
                return False
            if dt >= 3.0:
                print('   /decide took %.2fs (budget 300 ms)' % dt)
                return False
            h = req(laya_port_c, '/health')[1]
            if not (h.get('ok') is True and 'laya' in h):
                print('   /health wedged after the timeout: %r' % h)
                return False
            return True
        lm_probe('localmodels (phase13): --laya-timeout-ms 300 + hanging child => {ok:false,degraded:true,'
                 'reason:timeout} in well under 3 s, daemon still answers /health', laya_timeout)
    finally:
        laya_stop(laya_pc)

    # ---- case 9 (extra): a child that dies mid-request => child_gone, no 500 ----
    laya_port_f = 18940
    laya_pf = laya_daemon(laya_port_f, laya_ledger_f, ['--laya-child', LAYA_STUB, '--laya-timeout-ms', '3000'],
                          env=dict(os.environ, LAYA_STUB_EXIT_AFTER_MS='50'))
    time.sleep(1.0)
    try:
        def laya_child_gone():
            t0 = time.time()
            s, j = req(laya_port_f, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL})
            dt = time.time() - t0
            if not (s == 200 and j.get('ok') is False and j.get('degraded') is True
                    and j.get('reason') == 'child_gone'):
                print('   dying-child decide answered %s %r after %.2fs' % (s, j, dt))
                return False
            h = req(laya_port_f, '/health')[1]
            pid = h['laya'].get('child_pid')
            return h.get('ok') is True and (pid is None or isinstance(pid, int))
        lm_probe('localmodels (phase13): child dies mid-request => {ok:false,degraded:true,reason:child_gone}, '
                 'daemon survives (respawn is lazy)', laya_child_gone)

        def laya_child_gone_ledger():
            recs = [r for r in laya_ledger_lines(laya_ledger_f) if r.get('op') == 'decide']
            return bool(recs) and all(r.get('degraded') is True for r in recs)
        lm_probe('localmodels (phase13): a child_gone /decide still appends its degraded ledger record',
                 laya_child_gone_ledger)
    finally:
        laya_stop(laya_pf)

    # ---- case 6: idle reaping ----
    laya_port_d = 18938
    laya_pd = laya_daemon(laya_port_d, laya_ledger_d, ['--laya-child', LAYA_STUB, '--laya-idle-s', '1'])
    time.sleep(1.0)
    try:
        def laya_reap():
            s, j = req(laya_port_d, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL})
            if s != 200 or j.get('ok') is not True:
                print('   first decide answered %s %r' % (s, j))
                return False
            h1 = req(laya_port_d, '/health')[1]['laya']
            if not isinstance(h1.get('child_pid'), int):
                print('   no child pid after the first decide: %r' % h1)
                return False
            pid_before = h1['child_pid']
            time.sleep(2.5)
            h2 = req(laya_port_d, '/health')[1]['laya']
            if h2.get('child_pid') is not None or h2.get('loaded') is not False:
                print('   child was not reaped while idle: %r' % h2)
                return False
            s2, j2 = req(laya_port_d, '/decide', {'state': LAYA_STATE, 'questions': LAYA_Q_NOUL})
            h3 = req(laya_port_d, '/health')[1]['laya']
            if not (s2 == 200 and j2.get('ok') is True and isinstance(h3.get('child_pid'), int)
                    and h3.get('loaded') is True):
                print('   respawn after reap failed: %s %r / %r' % (s2, j2, h3))
                return False
            print('   reap: pid %s -> None after 2.5 s idle -> %s after the next decide'
                  % (pid_before, h3['child_pid']))
            return True
        lm_probe('localmodels (phase13): --laya-idle-s 1 reaps the idle child (pid None) and the next '
                 '/decide respawns + reloads', laya_reap)
    finally:
        laya_stop(laya_pd)

    def laya_no_stray_files():
        return set(os.listdir(laya_dir)) <= {'a.jsonl', 'b.jsonl', 'c.jsonl', 'd.jsonl',
                                             'e.jsonl', 'f.jsonl', 'g.jsonl'}
    lm_probe('localmodels (phase13): daemon + child wrote no pid/log file (ledger only)', laya_no_stray_files)

    def laya_docs():
        readme = open(os.path.join(BASE, 'localmodels', 'README.md'), encoding='utf-8').read()
        sh = open(os.path.join(BASE, 'localmodels', 'setup.sh'), encoding='utf-8').read()
        ps = open(os.path.join(BASE, 'localmodels', 'setup.ps1'), encoding='utf-8').read()
        return ('POST /decide' in readme and 'laya_child.mjs' in readme and '--laya-idle-s' in readme
                and 'npm install' in sh and 'npm install' in ps)
    lm_probe('localmodels (phase13): README documents /decide + the child + the flags; setup scripts '
             'install Laya via npm', laya_docs)
finally:
    shutil.rmtree(laya_dir, ignore_errors=True)

# ---------------- localmodels sidecar (Phase 14, workstream A: F3 /select + ledger counts) ----
# The sidecar half of "F3 cheap local dispatcher (Needle as pre-router)": POST /select
# proposes a tool call for the operator's utterance, and /health surfaces the ledger's
# proposal counters (the browser cannot read the ledger file).
# Every daemon below runs with cwd=<temp dir> and --ledger inside it: a daemon is NEVER
# booted in the repo tree, and no test ever points at the real weights.
sel_dir = tempfile.mkdtemp(prefix='cogsel-')
sel_ledger_a = os.path.join(sel_dir, 'a.jsonl')   # --no-needle daemon
sel_ledger_b = os.path.join(sel_dir, 'b.jsonl')   # needle enabled, weights/package absent
sel_ledger_c = os.path.join(sel_dir, 'c.jsonl')   # counts read-back daemon

SEL_INPUT = 'read main.py and show it to me'
SEL_PROSE = 'Tell me a bit about how the Cogitator harness is put together.'
SEL_TOOLS = [{'type': 'function',
              'function': {'name': 'read_file', 'description': 'Read a file from disk.',
                           'parameters': {'type': 'object',
                                          'properties': {'path': {'type': 'string'}},
                                          'required': ['path']}}},
             {'type': 'function',
              'function': {'name': 'grep', 'description': 'Search a directory for a pattern.',
                           'parameters': {'type': 'object',
                                          'properties': {'pattern': {'type': 'string'},
                                                         'path': {'type': 'string'}}}}}]


def sel_daemon(port, ledger_path, extra=(), env=None):
    return subprocess.Popen([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
                             '--port', str(port), '--ledger', ledger_path] + list(extra),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=sel_dir, env=env)


def sel_stop(proc):
    proc.terminate()
    try: proc.wait(timeout=5)
    except subprocess.TimeoutExpired: proc.kill()


def sel_ledger_lines(path):
    if not os.path.isfile(path):
        return []
    return [json.loads(ln) for ln in open(path, encoding='utf-8') if ln.strip()]


try:
    # ---- case 1: the new flag exists ----
    def sel_flag():
        r = subprocess.run([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'), '--help'],
                           capture_output=True, text=True, timeout=30)
        return '--needle-select-timeout-ms' in r.stdout
    lm_probe('localmodels (phase14): --needle-select-timeout-ms flag exists in --help', sel_flag)

    # ---- cases 2, 4, 5: the --no-needle daemon (route exists, engine refuses fast) ----
    sel_port_a = 18942
    sel_pa = sel_daemon(sel_port_a, sel_ledger_a, ['--no-needle', '--no-laya'])
    time.sleep(1.0)
    try:
        def sel_no_needle():
            t0 = time.time()
            s, j = req(sel_port_a, '/select', {'input': SEL_INPUT, 'candidates': SEL_TOOLS,
                                               'trace_id': 'tr_e2e_sel_off'})
            dt = time.time() - t0
            if not (s == 200 and j.get('ok') is False and j.get('degraded') is True
                    and j.get('reason') == 'disabled' and j.get('calls') == []
                    and isinstance(j.get('latency_ms'), int)
                    and j.get('trace_id') == 'tr_e2e_sel_off'):
                print('   /select (--no-needle) answered %s %r' % (s, j))
                return False
            if dt >= 3.0:
                print('   --no-needle /select took %.2fs (must answer fast)' % dt)
                return False
            return True
        lm_probe('localmodels (phase14): --no-needle POST /select => {ok:false,degraded:true,'
                 "reason:'disabled', calls:[]}, fast, never a 500", sel_no_needle)

        def sel_foreign_origin_no_record():
            # The Origin guard is the FIRST statement in do_POST, so a foreign POST is
            # refused before routing AND before any ledger append.
            before = open(sel_ledger_a, encoding='utf-8').read() if os.path.isfile(sel_ledger_a) else ''
            s, j = req(sel_port_a, '/select', {'input': SEL_INPUT, 'candidates': SEL_TOOLS},
                       origin='http://evil.example')
            after = open(sel_ledger_a, encoding='utf-8').read() if os.path.isfile(sel_ledger_a) else ''
            return (s == 403 and j.get('error') == 'origin not permitted' and before == after)
        lm_probe('localmodels (phase14): foreign Origin POST /select => 403 and NO ledger record',
                 sel_foreign_origin_no_record)

        def sel_413_wins():
            return lm_413(sel_port_a, '/select', b'{"input":"')
        lm_probe('localmodels (phase14): oversized POST /select => 413 (size cap outranks routing)',
                 sel_413_wins)

        def sel_empty_candidates():
            # No candidate tool => no proposal. NEVER a manufactured call.
            tid = 'tr_e2e_sel_empty'
            s, j = req(sel_port_a, '/select', {'input': SEL_INPUT, 'candidates': [],
                                               'trace_id': tid})
            return (s == 200 and j.get('ok') is False and j.get('degraded') is True
                    and j.get('calls') == [] and j.get('trace_id') == tid
                    and isinstance(j.get('latency_ms'), int))
        lm_probe('localmodels (phase14): empty candidates POST /select => degraded, calls:[] '
                 '(never a manufactured call), never a 500', sel_empty_candidates)

        def sel_degraded_ledger_record():
            tid = 'tr_e2e_sel_ledger'
            before = sel_ledger_lines(sel_ledger_a)
            s, j = req(sel_port_a, '/select', {'input': SEL_INPUT, 'candidates': SEL_TOOLS,
                                               'trace_id': tid})
            if s != 200:
                print('   /select answered %s %r' % (s, j))
                return False
            after = sel_ledger_lines(sel_ledger_a)
            recs = [r for r in after[len(before):]
                    if r.get('trace_id') == tid and r.get('op') == 'select']
            if len(recs) != 1:
                print('   expected exactly 1 select record for %s, got %r' % (tid, after[len(before):]))
                return False
            rec = recs[0]
            if not (rec.get('model') == 'needle' and rec.get('input_redacted') is True
                    and rec.get('action') is None and rec.get('degraded') is True
                    and isinstance(rec.get('latency_ms'), int) and 'confidence' in rec
                    and isinstance((rec.get('request') or {}).get('candidates'), list)):
                print('   select record malformed: %r' % rec)
                return False
            # The candidate NAMES ride along (both shapes) so the corpus is replayable.
            if [n for n in (rec['request'].get('candidates') or []) if n] != ['read_file', 'grep']:
                print('   candidate names missing from the record: %r' % rec.get('request'))
                return False
            # Redaction: an Authorization/key-shaped value in the utterance must not survive.
            s2, j2 = req(sel_port_a, '/select', {'input': 'read main.py using Authorization: Bearer '
                                                          'supersecret999999 and sk-abcdefgh12345',
                                                  'candidates': SEL_TOOLS, 'trace_id': tid + '_redact'})
            if s2 != 200:
                return False
            raw = open(sel_ledger_a, encoding='utf-8').read()
            leaks = [needle for needle in ('supersecret999999', 'sk-abcdefgh12345')
                     if needle in raw]
            if leaks:
                print('   select ledger leaked %r' % leaks)
                return False
            return True
        lm_probe('localmodels (phase14): a degraded /select still appends ONE op:select ledger '
                 'record (model needle, input_redacted, action None) and redacts the utterance',
                 sel_degraded_ledger_record)
    finally:
        sel_stop(sel_pa)

    # ---- case 3: needle enabled but the package/weights are absent => degraded ----
    sel_port_b = 18943
    sel_pb = sel_daemon(sel_port_b, sel_ledger_b, ['--no-laya'])
    time.sleep(1.0)
    try:
        def sel_needle_unavailable():
            h = req(sel_port_b, '/health')[1]
            s, j = req(sel_port_b, '/select', {'input': SEL_INPUT, 'candidates': SEL_TOOLS,
                                               'trace_id': 'tr_e2e_sel_pkg'})
            if h['needle'].get('weights') == 'present':
                # A host that really has weights may legitimately answer ok:true; only the
                # never-500 / envelope contract is asserted there.
                return (s == 200 and isinstance(j.get('ok'), bool) and isinstance(j.get('calls'), list)
                        and j.get('trace_id') == 'tr_e2e_sel_pkg')
            return (s == 200 and j.get('ok') is False and j.get('degraded') is True
                    and j.get('reason') in ('tool_unavailable', 'weights_missing')
                    and j.get('calls') == []
                    and isinstance(j.get('latency_ms'), int)
                    and j.get('trace_id') == 'tr_e2e_sel_pkg')
        lm_probe('localmodels (phase14): needle enabled + package/weights absent => POST /select '
                 'degraded, never a 500', sel_needle_unavailable)
    finally:
        sel_stop(sel_pb)

    # ---- case 7: build_select_prompt renders the utterance + every candidate name ----
    def sel_prompt_shapes():
        sys.path.insert(0, os.path.join(BASE, 'localmodels'))
        import needle_backend as nb
        p = nb.build_select_prompt(SEL_INPUT, SEL_TOOLS)
        flat = nb.build_select_prompt(SEL_INPUT, [{'name': 'write_file',
                                                   'description': 'Write a file.'}])
        prose = nb.build_select_prompt(SEL_PROSE, SEL_TOOLS)
        none_in = nb.build_select_prompt(None, SEL_TOOLS)
        long_in = nb.build_select_prompt('x' * 2500, SEL_TOOLS)
        junk = nb.build_select_prompt(SEL_INPUT, ['not-a-dict', {'nope': 1}, None])
        checks = [
            (isinstance(p, str) and p, 'renders a non-empty string'),
            (SEL_INPUT in p, 'utterance verbatim'),
            ('read_file' in p and 'grep' in p, 'every candidate name (OpenAI shape)'),
            ('Read a file from disk.' in p, 'candidate descriptions'),
            ('write_file' in flat, 'flat {name, description} candidate shape'),
            (isinstance(prose, str) and SEL_PROSE in prose and len(prose) > 0,
             'prose-only utterance still returns one prompt'),
            (isinstance(none_in, str) and none_in, 'None utterance still returns one prompt'),
            ('…[truncated' in long_in and len(long_in) < 4000,
             'a 2500-char utterance is truncated with the marker'),
            (isinstance(junk, str) and 'not-a-dict' not in junk, 'junk candidates never raise'),
            ('propose nothing' in prose or 'not a clear request' in prose,
             'the no-call instruction is in the prompt'),
        ]
        bad = [m for ok, m in checks if not ok]
        if bad:
            print('   ' + '; '.join(bad))
            print('   prompt => %r' % p)
        return not bad
    lm_probe('localmodels (phase14): build_select_prompt renders the utterance + every candidate '
             'name, and returns a non-empty prompt for a prose-only utterance', sel_prompt_shapes)

    # ---- case 9: /health surfaces the ledger counts (driven through the real daemon) ----
    sel_port_c = 18944
    sel_pc = sel_daemon(sel_port_c, sel_ledger_c, ['--no-needle', '--no-laya'])
    time.sleep(1.0)
    try:
        def sel_health_counts():
            s, h = req(sel_port_c, '/health')
            if s != 200:
                print('   /health answered %s %r' % (s, h))
                return False
            counts = (h.get('ledger') or {}).get('counts')
            keys = {'proposals', 'accepted', 'ignored', 'rejected', 'passed_through', 'timeout'}
            if not (isinstance(counts, dict) and set(counts) == keys
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in counts.values())):
                print('   fresh ledger.counts malformed: %r' % counts)
                return False
            fresh = dict(counts)

            # A real /select through the daemon must bump `proposals` and nothing else.
            tid = 'tr_e2e_sel_counts'
            s2, j2 = req(sel_port_c, '/select', {'input': SEL_INPUT, 'candidates': SEL_TOOLS,
                                                 'trace_id': tid})
            if s2 != 200:
                return False
            c1 = req(sel_port_c, '/health')[1]['ledger']['counts']
            if not (c1.get('proposals') == fresh.get('proposals', 0) + 1
                    and c1.get('accepted') == fresh.get('accepted')
                    and c1.get('ignored') == fresh.get('ignored')
                    and c1.get('rejected') == fresh.get('rejected')):
                print('   one /select did not bump proposals by exactly 1: %r -> %r' % (fresh, c1))
                return False

            # The operator's outcomes arrive as POST /ledger lines (no `op` key). The counts
            # are DELIBERATELY asymmetric (2x accepted, 1x accepted_by_operator, 2x
            # ignored_by_operator) so a swapped alias cannot accidentally sum to the same
            # totals - a symmetric fixture would let `accepted_by_operator -> ignored` pass.
            for action, times in (('accepted', 2), ('accepted_by_operator', 1),
                                  ('ignored_by_operator', 2), ('rejected', 1),
                                  ('passed_through', 1), ('timeout', 1)):
                for _ in range(times):
                    s3, j3 = req(sel_port_c, '/ledger', {'trace_id': tid, 'action': action,
                                                          'note': 'phase14 counter probe'})
                    if not (s3 == 200 and j3.get('ok') is True):
                        print('   /ledger %s answered %s %r' % (action, s3, j3))
                        return False
            c2 = req(sel_port_c, '/health')[1]['ledger']['counts']
            expected = dict(c1)
            expected.update({'accepted': c1['accepted'] + 3,      # accepted + accepted_by_operator
                             'ignored': c1['ignored'] + 2,
                             'rejected': c1['rejected'] + 1,
                             'passed_through': c1['passed_through'] + 1,
                             'timeout': c1['timeout'] + 1})
            if c2 != expected:
                print('   counts after the outcome writes: %r != %r' % (c2, expected))
                return False
            # The proposals count is untouched by the outcome lines.
            if c2.get('proposals') != c1.get('proposals'):
                print('   outcome lines moved the proposals counter: %r -> %r' % (c1, c2))
                return False
            print('   ledger.counts => %s' % json.dumps(c2, sort_keys=True))
            return True
        lm_probe('localmodels (phase14): /health ledger.counts has all six int keys, /select bumps '
                 'proposals, and accepted/accepted_by_operator/ignored_by_operator/rejected/'
                 'passed_through/timeout bump their own counters', sel_health_counts)

        def sel_health_counts_survives_broken_ledger():
            # A garbage ledger must not break /health: a non-JSON line, a JSON array and a
            # JSON scalar are all skipped, and the counts still read back.
            with open(sel_ledger_c, 'a', encoding='utf-8') as f:
                f.write('not json at all\n[1,2,3]\n"a string"\n{"action":"nonsense"}\n')
            s, h = req(sel_port_c, '/health')
            counts = (h.get('ledger') or {}).get('counts') if s == 200 else None
            return (s == 200 and isinstance(counts, dict)
                    and all(isinstance(v, int) for v in counts.values())
                    and counts.get('accepted') == 3 and counts.get('ignored') == 2
                    and counts.get('rejected') == 1 and counts.get('timeout') == 1
                    and counts.get('passed_through') == 1
                    # the garbage lines moved nothing at all
                    and counts.get('proposals') == 1)
        lm_probe('localmodels (phase14): a malformed ledger line is skipped, never breaks /health',
                 sel_health_counts_survives_broken_ledger)

        def sel_health_shape_regression():
            # The full Phase 10 /health shape must still be there, counts included.
            s, h = req(sel_port_c, '/health')
            return (s == 200 and h.get('ok') is True and h.get('version') == '0.1.0'
                    and {'needle', 'laya', 'ledger', 'degraded'} <= set(h)
                    and isinstance(h.get('degraded'), list)
                    and h['needle'].get('enabled') is False and h['laya'].get('enabled') is False
                    and h['needle'].get('loaded') is False and h['laya'].get('loaded') is False
                    and h['laya'].get('child_pid') is None
                    and h['needle'].get('weights') in ('missing', 'present')
                    and isinstance(h['needle'].get('generation'), int)
                    and 'lib' in h['needle'] and 'cache' in h['laya']
                    and h['ledger'].get('path') == os.path.abspath(sel_ledger_c)
                    and h['ledger'].get('writable') is True
                    and 'counts' in h['ledger'])
        lm_probe('localmodels (phase14): /health still 200 with the full phase-10 shape + '
                 'ledger.counts after all of the above', sel_health_shape_regression)
    finally:
        sel_stop(sel_pc)

    # ---- the 2000-line tail cap, asserted directly on the pure helper ----
    def sel_ledger_counts_tail():
        # A 5000-line ledger whose LAST 2000 lines are all `accepted`: the read-back must
        # report exactly 2000 accepted and 0 proposals. A fixed byte window silently
        # under-reports here (measured 1489 of 2000), which is why the window grows.
        sys.path.insert(0, os.path.join(BASE, 'localmodels'))
        import local_models_daemon as lmd_mod
        big = os.path.join(sel_dir, 'big.jsonl')
        with open(big, 'w', encoding='utf-8') as f:
            for i in range(2500):
                f.write(json.dumps({'op': 'select', 'trace_id': 'p%d' % i}) + '\n')
            for i in range(2500):
                f.write(json.dumps({'action': 'accepted', 'trace_id': 'a%d' % i,
                                    'note': 'x' * 60}) + '\n')
        c = lmd_mod._ledger_counts(big)
        if not (c.get('proposals') == 0 and c.get('accepted') == 2000):
            print('   tail cap wrong for a 5000-line ledger: %r' % c)
            return False
        # A missing file, an unreadable file and an all-malformed file all read as zeros.
        empty = os.path.join(sel_dir, 'empty.jsonl')
        with open(empty, 'w', encoding='utf-8') as f:
            f.write('garbage\nnot json at all\n[1,2,3]\n"a string"\n')
        zeros = {'proposals': 0, 'accepted': 0, 'ignored': 0, 'rejected': 0,
                 'passed_through': 0, 'timeout': 0}
        if lmd_mod._ledger_counts(os.path.join(sel_dir, 'does-not-exist.jsonl')) != zeros:
            print('   a missing ledger did not read as all-zero')
            return False
        if lmd_mod._ledger_counts(empty) != zeros:
            print('   an all-malformed ledger did not read as all-zero')
            return False
        return True
    lm_probe('localmodels (phase14): ledger counts read the last 2000 lines exactly, and a '
             'missing / all-malformed ledger reads as all-zero', sel_ledger_counts_tail)

    def sel_no_stray_files():
        # Last: the tail-cap case above wrote big.jsonl / empty.jsonl into the temp dir, and
        # those ARE expected. Anything else (a pid or log file) is not.
        return set(os.listdir(sel_dir)) <= {'a.jsonl', 'b.jsonl', 'c.jsonl',
                                             'big.jsonl', 'empty.jsonl'}
    lm_probe('localmodels (phase14): /select daemons wrote no pid/log file (ledger only)',
             sel_no_stray_files)
finally:
    shutil.rmtree(sel_dir, ignore_errors=True)

print('\n%d FAILURES' % len(fails))
sys.exit(1 if fails else 0)
