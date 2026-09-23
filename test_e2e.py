#!/usr/bin/env python3
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request, urllib.error

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
                    and any('needle' in str(r) for r in j.get('degraded', [])))
        lm_probe('localmodels: without --no-needle => needle.enabled True + needle degraded reason', lm_needle_enabled)
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
        s, j = req(lmport, '/nope', raw=b'{"x":"' + b'a' * (2 * 1024 * 1024) + b'"}')
        return s == 413 and j.get('ok') is False
    lm_probe('localmodels: oversized POST body (> 1 MiB) => 413', lm_oversized)

    def lm_foreign_post_before_route():
        s, j = req(lmport, '/nope', {}, origin='http://evil.example')
        return s == 403 and j.get('error') == 'origin not permitted'
    lm_probe('localmodels: foreign-Origin POST refused before route logic (403, not 404)',
             lm_foreign_post_before_route)

    # the ONLY file the daemon may write is the ledger
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

print('\n%d FAILURES' % len(fails))
sys.exit(1 if fails else 0)
