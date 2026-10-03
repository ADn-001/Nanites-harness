#!/usr/bin/env python3
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error
import inspect, re

BASE = os.path.dirname(os.path.abspath(__file__))
fails = []

def check(cond, msg):
    print(('ok  - ' if cond else 'FAIL- ') + msg)
    if not cond:
        fails.append(msg)

AMBIENT = object()
"""Sentinel: "use the token registered for this port".

Phase 19 made X-Cogitator-Token mandatory on every privileged route. The
pre-phase-19 call sites test something else entirely (git policy, jail escapes,
needle salvage) and were written when no token existed, so rather than editing
~60 call sites by hand each server registers its own token against its port
below and `req()` sends it by default. Passing token=None explicitly still
sends NO header — which is how the phase-19 cases assert the refusal, and how
the pre-existing foreign-Origin cases still assert a 403 for the Origin reason.

Phase 19 deliberately does NOT register its ports, so its "no token" cases stay
honest: the negative direction cannot be satisfied by an ambient default.
"""

PORT_TOKENS = {}

# The one allowed browser Origin the ambient default uses. Defined here rather
# than beside the phase-19 block because _send() needs it at call time.
P19_ORIGIN = 'http://localhost:8080'


def register_port_token(port, token):
    PORT_TOKENS[port] = token


def read_server_token(path, deadline=15.0):
    """Read a server's per-install token, waiting (bounded) for it to be created.

    Phase 19 made the token mandatory on privileged routes, so every pre-phase-19
    case needs the real one. The suite never invents a token: it reads the file the
    server itself wrote, which is also why these cases stay honest - a fabricated
    constant would pass nothing and fail everything.
    """
    end = time.time() + deadline
    while time.time() < end:
        try:
            with open(path, encoding='utf-8') as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError('no token file at %s after %.0fs - the server never wrote one'
                         % (path, deadline))


def _send(port, path, payload=None, origin=None, raw=None, token=AMBIENT, host=None, method=None):
    """One HTTP round-trip, returning (status, parsed_body, response_headers_lower).

    `token` sets X-Cogitator-Token and `host` overrides Host. token=AMBIENT (the
    default) resolves via PORT_TOKENS; token=None sends no header at all.
    """
    if token is AMBIENT:
        token = PORT_TOKENS.get(port)
        if origin is None:
            # Phase 19 also made "no Origin header" mean REFUSED on a privileged
            # route (that was CR-0017's actual hole). So the ambient default has
            # to carry an allowed Origin too, or every pre-phase-19 rite would
            # be refused for a reason it is not testing. An explicit origin=
            # always wins, so the foreign-Origin cases keep asserting 403.
            origin = P19_ORIGIN
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    headers = {'Content-Type': 'application/json'}
    if origin:
        headers['Origin'] = origin
    if token:
        headers['X-Cogitator-Token'] = token
    if host is not None:
        headers['Host'] = host
    r = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, headers=headers,
                               method=method or ('POST' if data is not None else 'GET'))
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            body = resp.read() or b'{}'
            status = resp.status
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        body = e.read() or b'{}'
        status = e.code
        hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        obj = {'raw': body.decode('utf-8', 'replace')}
    return status, obj, hdrs


def req(port, path, payload=None, origin=None, raw=None, token=AMBIENT, host=None):
    s, j, _ = _send(port, path, payload, origin, raw, token, host)
    return s, j


def req_cors(port, path, payload=None, origin=None, token=AMBIENT, host=None, method=None):
    """Like req(), but also returns the response headers - CORS assertions need them."""
    return _send(port, path, payload, origin, None, token, host, method)


def req_absent_host(port, path, payload=None, token=None, origin=None, timeout=10):
    """POST with NO Host header at all.

    urllib always synthesises one, and HTTP/1.1 requires it, so a crafted
    request that omits it can only be written by hand. This is the "absent Host
    must be refused" half of the Host pin (spec section 3).
    """
    import socket
    body = json.dumps(payload if payload is not None else {}).encode()
    lines = ['POST %s HTTP/1.1' % path,
             'Content-Type: application/json',
             'Content-Length: %d' % len(body),
             'Connection: close']
    if token:
        lines.append('X-Cogitator-Token: %s' % token)
    if origin:
        lines.append('Origin: %s' % origin)
    wire = ('\r\n'.join(lines) + '\r\n\r\n').encode() + body
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        sock.sendall(wire)
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
    finally:
        sock.close()
    head = b''.join(chunks).split(b'\r\n', 1)[0].decode('latin-1')
    try:
        return int(head.split()[1])
    except (IndexError, ValueError):
        return -1


def req_non_ascii_token(port, path, payload=None, timeout=10):
    """POST a token header containing bytes >= 0x80, via a raw socket.

    urllib would encode the value as UTF-8 and the header would never arrive as
    the single latin-1 char http.server decodes it to, so this has to be written
    by hand. The point is the STATUS LINE: an auth check that raises drops the
    connection, and -1 here is how that crash reports itself.
    """
    body = json.dumps(payload if payload is not None else {}).encode()
    lines = ['POST %s HTTP/1.1' % path,
             'Host: 127.0.0.1:%d' % port,
             'Origin: http://localhost:8080',
             'Content-Type: application/json',
             'Content-Length: %d' % len(body),
             'X-Cogitator-Token: caf%s-x' % chr(0xE9),   # 'e-acute', one latin-1 char
             'Connection: close']
    wire = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + body
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        sock.sendall(wire)
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
    finally:
        sock.close()
    head = b''.join(chunks).split(b'\r\n', 1)[0].decode('latin-1')
    try:
        return int(head.split()[1])
    except (IndexError, ValueError):
        return -1                                 # no status line: the handler died

# ---------------- bridge worker ----------------
# PHASE 16: the bridge's git policy tables are the single source the frontend's policy is
# generated from (CR-Nanites-harness-0009), so the test asserts against the real tables
# rather than a literal restatement of them.
sys.path.insert(0, BASE)
import bridge as bridge_mod  # noqa: E402

proj = tempfile.mkdtemp(prefix='cogtest-')
open(os.path.join(proj, 'hello.py'), 'w', encoding='utf-8').write('print("hi")\n# TODO fix me\n')
os.makedirs(os.path.join(proj, 'venv'))
open(os.path.join(proj, 'venv', 'ignored.txt'), 'w', encoding='utf-8').write('TODO\n')
port = 18931
br = subprocess.Popen([sys.executable, os.path.join(BASE, 'bridge.py'), '--root', proj, '--port', str(port)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
register_port_token(port, read_server_token(os.path.join(BASE, '.cogitator-token')))
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
    # ---- PHASE 16: the bridge jail is closed on git's file-writing flags (CR-0001) ----
    #
    # RED when written: `_git_flag_audit` returned the subcommand for ANY args on a GIT_READ
    # member, so `git log --output=<file>` reached subprocess.run and wrote outside ROOT.
    # The assertion is the FILE, not just the status: a 403 that still wrote the file would
    # pass a status-only check, and the write is the whole finding.
    outside = os.path.join(tempfile.gettempdir(), 'cog-phase16-escape.txt')
    if os.path.exists(outside):
        os.remove(outside)
    for args in ['log --output=' + outside, 'diff --output=' + outside, 'show --output=' + outside,
                 'blame --output=' + outside, 'log --output-indicator-new=x', 'log --exec-path=/tmp',
                 'format-patch -1', 'archive --output=' + outside]:
        s, j = req(port, '/tools/execute', {'name': 'git', 'arguments': {'args': args}})
        check(s == 403, 'git read subcommand with a file-writing flag refused: ' + args)
        check(not os.path.exists(outside),
              'refused git invocation wrote NO file outside ROOT: ' + args)
        if os.path.exists(outside):
            os.remove(outside)
    # The inverse: the ordinary read subcommands must STILL work. A guard that refuses
    # everything would satisfy every case above.
    for args in ['status --short', 'log --oneline -1', 'diff --stat', 'show --stat HEAD']:
        s, j = req(port, '/tools/execute', {'name': 'git', 'arguments': {'args': args}})
        check(s in (200, 404), 'ordinary read subcommand still accepted: ' + args)
    # ---- PHASE 16: t_grep jails every file it opens (CR-0002) ----
    #
    # The existing symlink case above covers list_dir and a symlinked DIRECTORY. This one
    # plants a symlinked FILE, which is the actual gap: os.walk(followlinks=False) already
    # protects directories, so grep's missing per-file jail() was invisible to that test.
    if hasattr(os, 'symlink'):
        secret_dir = tempfile.mkdtemp(prefix='cog-phase16-outside-')
        try:
            secret_path = os.path.join(secret_dir, 'secret.txt')
            with open(secret_path, 'w', encoding='utf-8') as fh:
                fh.write('TOPSECRET_MARKER_9931\n')
            os.makedirs(os.path.join(proj, 'sub'), exist_ok=True)
            link = os.path.join(proj, 'sub', 'link.txt')
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(secret_path, link)
            s, j = req(port, '/tools/execute', {'name': 'grep', 'arguments': {'pattern': 'TOPSECRET_MARKER_9931', 'path': 'sub'}})
            check(s == 200 and 'TOPSECRET_MARKER_9931' not in j.get('result', ''),
                  'grep does NOT match a symlinked file pointing outside the jail')
            # read_file/list_dir must still refuse it -- the fix must not weaken them.
            s, j = req(port, '/tools/execute', {'name': 'read_file', 'arguments': {'path': 'sub/link.txt'}})
            check(s == 403, 'read_file still refuses the symlinked file')
            s, j = req(port, '/tools/execute', {'name': 'list_dir', 'arguments': {'path': 'sub'}})
            check(s == 200, 'list_dir still lists the directory containing the symlink')
            os.remove(link)
        finally:
            shutil.rmtree(secret_dir, ignore_errors=True)
    # ---- PHASE 16: the frontend policy is GENERATED from the bridge (CR-0009) ----
    #
    # The block in index.html is emitted by tools/gen_git_policy.py from bridge.py's
    # tables. Without this check the two copies drift silently and the frontend starts
    # approving something the bridge refuses (or the reverse) with every suite still
    # green — which is the exact condition 0009 was filed about.
    r = subprocess.run([sys.executable, os.path.join(BASE, 'tools', 'gen_git_policy.py'), '--check'],
                       capture_output=True, text=True, cwd=BASE)
    check(r.returncode == 0, 'the frontend git policy is not stale w.r.t. bridge.py '
                             '(run tools/gen_git_policy.py --write): ' + (r.stdout + r.stderr).strip()[:300])
    # And the generator must actually be derived from the bridge, not a private copy:
    # a subcommand in one table and not the other has to be visible here.
    r = subprocess.run([sys.executable, os.path.join(BASE, 'tools', 'gen_git_policy.py')],
                       capture_output=True, text=True, cwd=BASE)
    gen = r.stdout
    for sub in sorted(bridge_mod.GIT_READ):
        check(("'%s'" % sub) in gen, 'generator emits the bridge read subcommand: ' + sub)
    for flag in bridge_mod.GIT_JAIL_FLAGS:
        check(flag in gen, 'generator emits the bridge output flag: ' + flag)
    for sub in sorted(bridge_mod.GIT_REFUSED_WRITE):
        check(("'%s'" % sub) in gen, 'generator emits the refused-write subcommand: ' + sub)
    # ---- PHASE 21: the service worker's asset digests are GENERATED (CR-0007) ----
    #
    # sw.js's CACHE is derived from ASSET_DIGESTS, which tools/gen_sw_cache.py emits
    # from the real precached files. Without this check a changed asset ships with a
    # stale digest, the cache name does not change, and the browser keeps serving the
    # OLD asset from cache indefinitely — cache-first, no revalidation, no TTL — with
    # every other suite still green. That is the exact condition 0007 was filed about,
    # so the check is the guard, not a nicety.
    r = subprocess.run([sys.executable, os.path.join(BASE, 'tools', 'gen_sw_cache.py'), '--check'],
                       capture_output=True, text=True, cwd=BASE)
    check(r.returncode == 0, 'sw.js asset digests are not stale w.r.t. the real files '
                             '(run tools/gen_sw_cache.py --write): ' + (r.stdout + r.stderr).strip()[:300])
    # The digests must be derived from the FILES, not be plausible-looking literals:
    # recompute one here and compare. A generator that emits a constant would satisfy
    # --check forever while invalidation silently stopped working.
    import hashlib as _hl
    with open(os.path.join(BASE, 'appcore.js'), 'rb') as fh:
        want = _hl.sha256(fh.read()).hexdigest()[:16]
    r = subprocess.run([sys.executable, os.path.join(BASE, 'tools', 'gen_sw_cache.py')],
                       capture_output=True, text=True, cwd=BASE)
    check(("'./appcore.js': '%s'" % want) in r.stdout,
          'the generated appcore.js digest matches the real appcore.js bytes (derivation is real)')
    # CR-0018 part (1): appcore.js must be precached. It is the module index.html loads
    # via <script src>; without it the FIRST offline visit — the case offline support
    # exists for — cannot get it at all, because the runtime cache-write needs a prior
    # successful online fetch. Parsed from the SHELL declaration itself (not a substring
    # of the whole file, which a comment could satisfy) via the generator's own reader,
    # so this check and sw.js can never disagree about what is precached.
    sys.path.insert(0, os.path.join(BASE, 'tools'))
    import gen_sw_cache as _gen
    check('./appcore.js' in _gen.read_shell(),
          "sw.js's SHELL precaches ./appcore.js (CR-0018)")
    # And the navigation fallback must be scoped to navigations. This is asserted
    # BEHAVIOURALLY, not by looking for a substring: a `in sw_src` check would pass on
    # an INVERTED guard (`if (isNavigation(e.request)) throw err;`) containing the very
    # same token, which is the defect re-spelled. A tiny vm harness executes sw.js's real
    # fetch listener with a real offline script request and asserts what it returns.
    # (tests/frontend/phase21_service_worker.test.js is the full behavioural suite; this
    # is the python-side smoke of the same property, so a python-only run still fails.)
    import subprocess as _sp
    _probe = (
        "const fs=require('fs'),vm=require('vm');"
        "const s={self:{addEventListener(t,f){(s._h||(s._h={}))[t]=f},skipWaiting(){},"
        "clients:{claim(){}}},caches:{open:async()=>({}),match:async()=>undefined,"
        "keys:async()=>[],delete:async()=>{}},location:{origin:'https://x'},"
        "fetch:async()=>{throw new Error('offline')},URL,Promise,console};"
        "s.self.location=s.location;vm.createContext(s);"
        "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),s);"
        "const req={method:'GET',url:'https://x/appcore.js',mode:'no-cors',destination:'script'};"
        "let out=null;s._h.fetch({request:req,respondWith(p){out=Promise.resolve(p)}});"
        "out.then(v=>{console.log('RESOLVED:'+(v&&v.body?'body':'undefined'))},"
        "()=>console.log('REJECTED'));"
    )
    r = subprocess.run(['node', '-e', _probe, os.path.join(BASE, 'sw.js')],
                       capture_output=True, text=True, cwd=BASE)
    check('REJECTED' in r.stdout,
          'sw.js rejects a failed SCRIPT fetch instead of answering it with HTML (CR-0018): '
          + (r.stdout + r.stderr).strip()[:200])

    # A symlinked DIRECTORY must not be descended into either. This case is GREEN both
    # with and without the explicit `islink` prune in bridge.py, because os.walk's default
    # is followlinks=False — so it DOCUMENTS the property rather than pinning the prune.
    # The prune's real job is to stop a future edit that flips that kwarg from silently
    # turning grep into a walk out of the jail; `tools/mutate_phase16.py` records M6 as a
    # documented equivalent for exactly this reason. The walk below is called with
    # followlinks=False EXPLICITLY so the property does not rest on a default.
    if hasattr(os, 'symlink'):
        out_dir = tempfile.mkdtemp(prefix='cog-phase16-outdir-')
        try:
            with open(os.path.join(out_dir, 'bigsecret.txt'), 'w', encoding='utf-8') as fh:
                fh.write('DIRSECRET_MARKER_4471\n')
            os.makedirs(os.path.join(proj, 'sub'), exist_ok=True)
            dlink = os.path.join(proj, 'sub', 'dirlink')
            if os.path.lexists(dlink):
                os.remove(dlink)
            os.symlink(out_dir, dlink)
            s, j = req(port, '/tools/execute', {'name': 'grep', 'arguments': {'pattern': 'DIRSECRET_MARKER_4471', 'path': 'sub'}})
            check(s == 200 and 'DIRSECRET_MARKER_4471' not in j.get('result', ''),
                  'grep does not descend into a symlinked directory pointing outside the jail')
            os.remove(dlink)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
    # The walk is called with followlinks=False EXPLICITLY. Asserted structurally because
    # no behavioural test can distinguish it from the default — the default IS False, so a
    # suite asserting the behaviour passes whether or not the kwarg is written. This is
    # the property the islink prune above is defending.
    with open(os.path.join(BASE, 'bridge.py'), encoding='utf-8') as fh:
        bridge_src = fh.read()
    check('os.walk(p, followlinks=False)' in bridge_src,
          't_grep calls os.walk with followlinks=False EXPLICITLY, not by default')
    # ---- the suite must name the subcommand, so the more specific refusal wins ----
    # `archive --output=x` carries BOTH a refused-write subcommand and an output flag. The
    # subcommand check is the more specific of the two and must be evaluated first, or the
    # generic flag message shadows it and the refusal stops naming what it refused.
    for args, named in (('format-patch -1', 'format-patch'), ('archive --output=/tmp/x.tar', 'archive')):
        s, j = req(port, '/tools/execute', {'name': 'git', 'arguments': {'args': args}})
        check(s == 403 and named in json.dumps(j),
              'the file-writing refusal NAMES the subcommand: ' + args)
    # ---- PHASE 16: `git diff --no-index` reads arbitrary paths outside the jail ----
    #
    # Found by the phase-16 independent review, and confirmed by a direct probe: `--no-index`
    # makes diff compare two ARBITRARY paths instead of repo contents, so
    # `git diff --no-index /etc/passwd /dev/null` returns the contents of a file outside
    # ROOT — the same class of escape as CR-0002, reached through the git tool instead of
    # grep. It was auto-approved as a read in BOTH layers, so no operator ever saw it.
    # The negative case is the load-bearing half: an ALLOW-list keyed on the subcommand
    # alone would call this a read, since `diff` is in GIT_READ.
    for args in ['diff --no-index /etc/passwd /dev/null',
                 'diff --no-index -- /etc/passwd /dev/null',
                 'diff --no-index /etc/hostname /dev/null',
                 'log --no-index']:
        s, j = req(port, '/tools/execute', {'name': 'git', 'arguments': {'args': args}})
        check(s == 403, 'git diff --no-index (reads arbitrary out-of-jail paths) refused: ' + args)
        check('root:' not in json.dumps(j),
              'refused --no-index returned no out-of-jail file content: ' + args)
    # And the inverse: ordinary diffs, including --no-color and --stat, still work.
    for args in ['diff --stat', 'diff --no-color', 'diff HEAD~1 HEAD --stat']:
        s, j = req(port, '/tools/execute', {'name': 'git', 'arguments': {'args': args}})
        check(s in (200, 404), 'ordinary diff still accepted: ' + args)
    s, j = req(port, '/tools/execute', raw=b'{"name":"read_file","arguments":{"path":"' + b'a' * (2 * 1024 * 1024) + b'"}}')
    check(s == 413, 'oversized POST refused')
    # --allow-file-origin is the explicit opt-in that re-trusts a null Origin.
    port2 = 18932
    br2 = subprocess.Popen([sys.executable, os.path.join(BASE, 'bridge.py'), '--root', proj,
                            '--port', str(port2), '--allow-file-origin'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    register_port_token(port2, read_server_token(os.path.join(BASE, '.cogitator-token')))
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
register_port_token(dport, read_server_token(os.path.join(BASE, '.cogitator-token')))
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
register_port_token(lmport, read_server_token(os.path.join(BASE, 'localmodels', '.cogitator-token')))
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
    register_port_token(lmport2, read_server_token(os.path.join(BASE, 'localmodels', '.cogitator-token')))
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
    p = subprocess.Popen([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
                             '--port', str(port), '--ledger', ledger_path] + list(extra),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=laya_dir, env=env)
    register_port_token(port, read_server_token(os.path.join(BASE, 'localmodels', '.cogitator-token')))
    return p


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
    p = subprocess.Popen([sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
                             '--port', str(port), '--ledger', ledger_path] + list(extra),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=sel_dir, env=env)
    register_port_token(port, read_server_token(os.path.join(BASE, 'localmodels', '.cogitator-token')))
    return p


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

# ---------------- PHASE 15 workstream A: tools/tune_thresholds.py ----------------
# The tool is a REPORTER, never a validator: it reads the ledger and the JSON the
# deterministic-rate script emits and prints only what the ledger can actually PROVE.
# Every expected number below is computed BY HAND in the comment beside it, so a drift
# in the tool is a FAIL rather than a silent redefinition of the metric.
TUNE_DIR = tempfile.mkdtemp(prefix='cogtune-')
TUNE_TOOL = os.path.join(BASE, 'tools', 'tune_thresholds.py')
TUNE_MJS = os.path.join(BASE, 'tools', 'corpus_deterministic_rate.mjs')
TUNE_CORPUS = os.path.join(BASE, 'tests', 'fixtures', 'toolcall-corpus', 'cases.json')
# The repo top level, snapshotted BEFORE the tool ever runs, so "the tool writes nothing"
# can be asserted against the whole tree and not just the temp dir.
BASE_BEFORE = sorted(os.listdir(BASE))
with open(TUNE_CORPUS, encoding='utf-8') as _f:
    TUNE_CASES = len(json.load(_f)['cases'])


def tune_line(op, tid, cands, out, conf, lat, **extra):
    """One ledger record in the REAL shape local_models_daemon.py writes (op records carry
    `op` + request.candidates as NAMES; outcome lines carry `action` and NO `op`)."""
    rec = {'trace_id': tid, 'model': 'laya' if op == 'decide' else 'needle', 'op': op,
           'input_redacted': True,
           'request': {'candidates': cands} if op != 'decide' else {'state': {'seen': 1}},
           'output': out, 'confidence': conf, 'latency_ms': lat,
           'degraded': False, 'action': None}
    rec.update(extra)
    return rec


def tune_outcome(tid, action):
    return {'trace_id': tid, 'action': action, 'note': 'phase15 fixture'}


def tune_call(name):
    return [{'name': name, 'arguments': {'path': 'a.py'}}]


# 7 repair + 2 select + 1 decide records, and 8 outcome lines on 7 of those trace_ids.
# The needle threshold in force for every repair/select record is 0.75 (the shipped
# DEF_SETTINGS.localModels.needle.minConfidence), and 0.70 for the laya decide record.
TUNE_RECORDS = [
    # r1 accepted, name inside the candidate set, conf 0.9 >= 0.75, output non-empty: CLEAN.
    tune_line('repair', 'tr_r1', ['read_file', 'grep'], tune_call('read_file'), 0.9, 10),
    # r2 accepted, emitted write_file but the candidate set was only [read_file]: FALSE
    # (name_outside_candidates) - a rite the request never offered.
    tune_line('repair', 'tr_r2', ['read_file'], tune_call('write_file'), 0.8, 20),
    # r3 rejected: a rejected repair is never a false repair, whatever it emitted. Its
    # 0.8 is above 0.75, so it is also a clean positive in the sweep.
    tune_line('repair', 'tr_r3', ['grep'], tune_call('grep'), 0.8, 30),
    # r4 accepted_by_operator with an EMPTY output: FALSE (empty_output).
    tune_line('repair', 'tr_r4', ['read_file'], [], 0.95, 40),
    # r5 accepted with NO confidence at all: FALSE (no_confidence - a repair nobody can
    # grade, and the gate would have refused it).
    tune_line('repair', 'tr_r5', ['read_file'], tune_call('read_file'), None, 50),
    # r6 has a confidence but NO outcome line: unobservable ground truth, excluded from
    # every rate. A 0.99 here must NOT be allowed to flatter the acceptance rate.
    tune_line('repair', 'tr_r6', ['read_file'], tune_call('read_file'), 0.99, 60),
    # r7 accepted at conf 0.6, BELOW the 0.75 in force: FALSE (low_confidence - the gate
    # and the ledger disagree about the same call), and a genuine false negative at t=.75.
    tune_line('repair', 'tr_r7', ['read_file'], tune_call('read_file'), 0.6, 70),
    # s1 accepted cleanly; s2 was ignored by the operator, so it is not a false repair
    # even though it emitted a name outside its candidate set.
    tune_line('select', 'tr_s1', ['read_file', 'list_dir'], tune_call('read_file'), 0.75, 15),
    tune_line('select', 'tr_s2', ['read_file'], tune_call('grep'), 0.6, 25),
    # d1 never produced an outcome at all: there is nothing to accept and nothing to
    # sweep - that must read as "no data", never as 0.0.
    tune_line('decide', 'tr_d1', None, {'answers': {'a': {'noul': 0.7}}}, 0.7, 8),
    tune_outcome('tr_r1', 'accepted'),
    tune_outcome('tr_r2', 'accepted'),
    tune_outcome('tr_r3', 'rejected'),
    tune_outcome('tr_r4', 'accepted_by_operator'),
    tune_outcome('tr_r5', 'accepted'),
    tune_outcome('tr_r7', 'accepted'),
    tune_outcome('tr_s1', 'accepted'),
    tune_outcome('tr_s2', 'ignored_by_operator'),
]
TUNE_TOTAL_LINES = 18           # 10 op records + 8 outcome lines
TUNE_REPAIR_RECORDS = 7
TUNE_REPAIR_OUTCOMED = 6        # r6 never produced an outcome
TUNE_REPAIR_ACCEPTED = 5        # r1, r2, r4, r5, r7  -> 5/6 = 83.3%
TUNE_REPAIR_FALSE = 4           # r2 name, r4 empty output, r5 no conf, r7 low conf
TUNE_REPAIR_CONF_N = 6          # every repair but r5 carries a numeric confidence
TUNE_REPAIR_LAT = [10, 20, 30, 40, 50, 60, 70]   # n=7: p50 -> rank 4 -> 40; p95/p99 -> rank 7 -> 70
TUNE_SWEEP_N = 5                # numeric confidence AND an outcome: r1,r2,r3,r4,r7 (r6 unscored)


def tune_write(name, records, extra_lines=()):
    path = os.path.join(TUNE_DIR, name)
    with open(path, 'w', encoding='utf-8') as f:
        for rec in list(records) + list(extra_lines):
            f.write(rec if isinstance(rec, str) else json.dumps(rec))
            f.write('\n')
    return path


def tune_run(args, cwd=None, env=None):
    """Run the tool. Never raises into the suite: a non-zero exit is a value to assert on."""
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, TUNE_TOOL] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=120,
                          cwd=cwd or TUNE_DIR, env=e)


def tune_json(path, args=(), **kw):
    r = tune_run(['--ledger', path, '--json'] + list(args), **kw)
    if r.returncode != 0 or not r.stdout.strip():
        return r, None
    try:
        return r, json.loads(r.stdout)
    except Exception:
        return r, None


def tune_rows(j, op):
    return {(row['threshold']): row for row in (j.get(op) or {}).get('per_threshold') or []}


try:
    # ---- the CLI exists ----
    def tune_help():
        r = tune_run(['--help'], cwd=BASE)
        return (r.returncode == 0
                and all(flag in r.stdout for flag in
                        ('--ledger', '--corpus', '--deterministic', '--json', '--thresholds')))
    lm_probe('localmodels (phase15): tune_thresholds.py --help works and documents every flag',
             tune_help)

    # ---- the arithmetic, pinned by hand ----
    def tune_arithmetic():
        path = tune_write('a.jsonl', TUNE_RECORDS)
        r, j = tune_json(path)
        if j is None:
            print('   no JSON: rc=%s stderr=%s' % (r.returncode, r.stderr[-400:]))
            return False
        led = j.get('ledger') or {}
        # "records read" counts every well-formed record line - the 10 op records AND the
        # 8 outcome lines, because the outcome lines are records too. Per-op record counts
        # live under each op key.
        if led.get('total_lines') != TUNE_TOTAL_LINES or led.get('records') != 18 \
                or led.get('skipped') != 0 or led.get('exists') is not True:
            print('   ledger accounting wrong: %r' % led)
            return False
        rp = j.get('repair') or {}
        if rp.get('records') != TUNE_REPAIR_RECORDS:
            print('   repair.records %r != %r' % (rp.get('records'), TUNE_REPAIR_RECORDS))
            return False
        if (rp.get('outcomes') or {}).get('accepted') != 4 \
                or (rp.get('outcomes') or {}).get('accepted_by_operator') != 1 \
                or (rp.get('outcomes') or {}).get('rejected') != 1:
            print('   repair.outcomes wrong: %r' % rp.get('outcomes'))
            return False
        # The FOLDED tally matches the daemon's own /health counters: 4 accepted +
        # 1 accepted_by_operator -> accepted:5.
        if (rp.get('outcomes_folded') or {}).get('accepted') != TUNE_REPAIR_ACCEPTED:
            print('   repair.outcomes_folded wrong: %r' % rp.get('outcomes_folded'))
            return False
        acc = rp.get('acceptance') or {}
        # 5 accepted (accepted + accepted_by_operator) over the 6 repairs that produced an
        # outcome at all -> 83.3%. r6 never produced one, so it is NOT in the denominator:
        # a proposal nobody ever answered for is not evidence of acceptance.
        if acc.get('accepted') != TUNE_REPAIR_ACCEPTED or acc.get('denominator') != TUNE_REPAIR_OUTCOMED \
                or abs((acc.get('rate') or 0) - (5.0 / 6.0)) > 1e-6:
            print('   repair acceptance wrong: %r' % acc)
            return False
        if (rp.get('unobservable_ground_truth') or 0) != 1:
            print('   repair.unobservable_ground_truth %r != 1'
                  % rp.get('unobservable_ground_truth'))
            return False
        fr = rp.get('false_repair') or {}
        # 4 of the 5 accepted repairs are provably defective, one for each reason the
        # definition allows: r2 name outside candidates, r4 empty output, r5 no
        # confidence, r7 confidence 0.6 below the 0.75 in force.
        if fr.get('count') != TUNE_REPAIR_FALSE or fr.get('denominator') != 5 \
                or abs((fr.get('rate') or 0) - 0.8) > 1e-9:
            print('   false repair wrong: %r' % fr)
            return False
        if (fr.get('by_reason') or {}) != {'name_outside_candidates': 1, 'empty_output': 1,
                                            'no_confidence': 1, 'low_confidence': 1}:
            print('   false_repair.by_reason wrong: %r' % fr.get('by_reason'))
            return False
        lat = rp.get('latency_ms') or {}
        if lat.get('n') != 7 or lat.get('p50') != 40 or lat.get('p95') != 70 or lat.get('p99') != 70:
            print('   repair percentiles wrong: %r' % lat)
            return False
        if rp.get('confidence_n') != TUNE_REPAIR_CONF_N:
            print('   repair.confidence_n %r != %r' % (rp.get('confidence_n'), TUNE_REPAIR_CONF_N))
            return False
        rows = tune_rows(j, 'repair')
        # t=0.75 (the value we SHIP): predicted r1(.9) r2(.8) r3(.8) r4(.95); r7(.6) and
        # the unscored r6 are below it. r1/r2/r4 were accepted, r3 was rejected, r7 was
        # accepted -> tp=3 fp=1 fn=1 tn=0 -> precision 0.750, recall 0.750.
        row = rows.get(0.75) or {}
        if (row.get('tp'), row.get('fp'), row.get('fn')) != (3, 1, 1) \
                or abs((row.get('precision') or 0) - 0.75) > 1e-9 \
                or abs((row.get('recall') or 0) - 0.75) > 1e-9:
            print('   sweep row 0.75 wrong: %r' % row)
            return False
        # t=0.85: predicted r1(.9) r4(.95) only -> tp=2 fp=0 fn=2 (r2 .8 and r7 .6 were
        # both accepted) -> precision 1.000, recall 0.500.
        row = rows.get(0.85) or {}
        if (row.get('tp'), row.get('fp'), row.get('fn')) != (2, 0, 2) \
                or abs((row.get('precision') or 0) - 1.0) > 1e-9 \
                or abs((row.get('recall') or 0) - 0.5) > 1e-9:
            print('   sweep row 0.85 wrong: %r' % row)
            return False
        if row.get('n') != TUNE_SWEEP_N:
            print('   sweep row scored %r records, expected %r' % (row.get('n'), TUNE_SWEEP_N))
            return False
        sel = j.get('select') or {}
        sacc = sel.get('acceptance') or {}
        # s1 accepted, s2 ignored -> 1/2. Neither is a false repair: s2 was REJECTED, and
        # a rejected proposal cannot have done harm.
        if (sel.get('records') != 2 or sacc.get('accepted') != 1 or sacc.get('denominator') != 2
                or (sel.get('latency_ms') or {}).get('p50') != 15
                or ((sel.get('false_repair') or {}).get('count') or 0) != 0):
            print('   select wrong: %r' % sel)
            return False
        dec = j.get('decide') or {}
        # d1 never produced an outcome, so there is nothing to accept: no data, not 0.0.
        if (dec.get('acceptance') or {}).get('rate') is not None \
                or (dec.get('acceptance') or {}).get('denominator') != 0:
            print('   decide acceptance should be no-data: %r' % dec.get('acceptance'))
            return False
        if (dec.get('latency_ms') or {}).get('p50') != 8:
            print('   decide percentiles wrong: %r' % dec.get('latency_ms'))
            return False
        return True
    lm_probe('localmodels (phase15): the hand-computed acceptance rate, false-repair count, '
             'per-threshold tp/fp/fn and p50/p95/p99 all match the synthetic ledger',
             tune_arithmetic)

    # ---- a zero denominator is null, never a division and never 0.0 ----
    def tune_zero_denominator():
        path = tune_write('a.jsonl', TUNE_RECORDS)
        r, j = tune_json(path, ['--thresholds', '0.96,0.75'])
        if j is None:
            return False
        row = (tune_rows(j, 'repair') or {}).get(0.96) or {}
        # Nothing scores >= 0.96 among the five scored records -> precision has a 0
        # denominator. It must be null, NOT 0.0 (which would read as "measured, and bad")
        # and not a crash. All four accepted records with a numeric confidence are missed.
        if row.get('tp') != 0 or row.get('fp') != 0 or row.get('fn') != 4:
            print('   t=0.96 counts wrong: %r' % row)
            return False
        if row.get('precision') is not None:
            print('   precision should be null on a zero denominator, got %r' % row.get('precision'))
            return False
        if abs((row.get('recall') or 0)) > 1e-9:
            print('   recall 0/4 should be 0.0: %r' % row.get('recall'))
            return False
        # decide has no outcome at all, so EVERY row of its sweep is undefined.
        drow = (tune_rows(j, 'decide') or {}).get(0.75) or {}
        if drow.get('precision') is not None or drow.get('recall') is not None:
            print('   decide sweep row should be all-null: %r' % drow)
            return False
        r2 = tune_run(['--ledger', path, '--thresholds', '0.96'])
        if r2.returncode != 0 or 'Traceback' in r2.stderr:
            return False
        return ('n/a' in r2.stdout)
    lm_probe('localmodels (phase15): a zero denominator prints/returns null ("n/a"), never a '
             'divide and never a 0.0 that reads like a measurement', tune_zero_denominator)

    # ---- a missing ledger is an honest no-data, not a traceback and not a rate ----
    def tune_missing():
        missing = os.path.join(TUNE_DIR, 'not-here.jsonl')
        r = tune_run(['--ledger', missing], cwd=BASE)
        if r.returncode != 0 or 'Traceback' in r.stderr:
            print('   rc=%s stderr=%s' % (r.returncode, r.stderr[-400:]))
            return False
        if 'no data' not in r.stdout.lower():
            print('   a missing ledger did not say "no data":\n%s' % r.stdout)
            return False
        # A rate computed from zero records must not be printed AT ALL: no percentage.
        if '%' in r.stdout:
            print('   a missing ledger printed a percentage anyway:\n%s' % r.stdout)
            return False
        r2, j = tune_json(missing, cwd=BASE)
        if j is None:
            return False
        return ((j.get('ledger') or {}).get('exists') is False
                and (j.get('ledger') or {}).get('records') == 0
                and ((j.get('repair') or {}).get('acceptance') or {}).get('rate') is None
                and ((j.get('repair') or {}).get('latency_ms') or {}).get('p50') is None)
    lm_probe('localmodels (phase15): a MISSING ledger exits 0, says "no data" and prints no '
             'rate at all', tune_missing)

    # ---- an all-malformed ledger is the same honest no-data ----
    def tune_all_malformed():
        # Five lines, none of which is a JSON OBJECT: prose, a truncated record, a JSON
        # array, a JSON string and a bare number. All five must be skipped, never fatal.
        path = tune_write('junk.jsonl', ['not json at all', '{"op":"repair",',
                                         '[1,2,3]', '"a string"', '42'])
        r = tune_run(['--ledger', path], cwd=BASE)
        if r.returncode != 0 or 'Traceback' in r.stderr or 'no data' not in r.stdout.lower():
            print('   rc=%s stdout=%s' % (r.returncode, r.stdout[-500:]))
            return False
        if '%' in r.stdout:
            print('   an all-malformed ledger printed a percentage:\n%s' % r.stdout)
            return False
        r2, j = tune_json(path, cwd=BASE)
        if j is None:
            return False
        return ((j.get('ledger') or {}).get('records') == 0
                and (j.get('ledger') or {}).get('skipped') == 5
                and ((j.get('repair') or {}).get('false_repair') or {}).get('rate') is None)
    lm_probe('localmodels (phase15): an all-malformed ledger is the same clean "no data" '
             'report, never a traceback and never a fabricated 0.0', tune_all_malformed)

    # ---- the .mjs runs on plain node and reports the corpus it was given ----
    def tune_mjs():
        try:
            r = subprocess.run(['node', TUNE_MJS], capture_output=True, text=True,
                               timeout=120, cwd=BASE)
        except FileNotFoundError:
            print('   node not on PATH - skipping the corpus rate check')
            return True
        except subprocess.TimeoutExpired:
            print('   node timed out on the corpus')
            return False
        if r.returncode != 0:
            print('   rc=%s stderr=%s' % (r.returncode, r.stderr[-400:]))
            return False
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if len(lines) != 1:
            print('   expected ONE json line on stdout, got %d' % len(lines))
            return False
        try:
            j = json.loads(lines[0])
        except Exception as e:
            print('   stdout is not json: %s' % e)
            return False
        if j.get('total') != TUNE_CASES:
            print('   total %r != the %d cases in cases.json' % (j.get('total'), TUNE_CASES))
            return False
        rate = j.get('rate')
        if not isinstance(rate, (int, float)) or not 0.0 <= rate <= 1.0:
            print('   rate %r is not a real rate' % rate)
            return False
        cats = j.get('by_category')
        if not isinstance(cats, dict) or not cats or 'narration-recovery' not in cats:
            print('   by_category missing/empty: %r' % cats)
            return False
        for name, row in cats.items():
            if (not isinstance(row, dict) or row.get('total', 0) < 1
                    or not isinstance(row.get('fixed'), int)):
                print('   by_category[%r] malformed: %r' % (name, row))
                return False
        if not isinstance(j.get('fixed'), int) or not isinstance(j.get('unchanged_correct'), int):
            print('   fixed/unchanged_correct must be integers: %r' % j)
            return False
        if 'false_repairs' in j and j['false_repairs'] != 0:
            print('   the deterministic pass reported false repairs: %r' % j['false_repairs'])
            return False
        print('   corpus rate => %r' % lines[0][:400])
        return True
    lm_probe('localmodels (phase15): tools/corpus_deterministic_rate.mjs runs on plain node, '
             'exits 0 and emits one parseable JSON line covering all %d cases' % TUNE_CASES,
             tune_mjs)

    # ---- malformed lines are SKIPPED, and skipping changes nothing else ----
    def tune_malformed_tolerance():
        clean = tune_write('clean.jsonl', TUNE_RECORDS)
        _, jc = tune_json(clean)
        dirty = tune_write('dirty.jsonl', TUNE_RECORDS,
                           extra_lines=['', '   ', 'not json at all', '{"op":"repair",',
                                        '[1,2,3]', '"a string"', '42', 'null', '{}',
                                        json.dumps({'op': 'repair', 'confidence': 'high'})])
        _, jd = tune_json(dirty)
        if jc is None or jd is None:
            print('   clean=%s dirty=%s' % (jc is not None, jd is not None))
            return False
        # The good records must produce BYTE-IDENTICAL metrics; only the line counters move.
        for key in ('records', 'outcomes', 'outcomes_folded', 'accepted',
                    'outcomes_recorded', 'unobservable_ground_truth', 'degraded',
                    'confidence_n', 'acceptance', 'false_repair', 'latency_ms',
                    'per_threshold'):
            if (jc['repair'].get(key) != jd['repair'].get(key)
                    or jc['select'].get(key) != jd['select'].get(key)
                    or jc['decide'].get(key) != jd['decide'].get(key)):
                print('   a malformed line moved repair.%s: %r -> %r'
                      % (key, jc['repair'].get(key), jd['repair'].get(key)))
                return False
        # 9 junk lines, one of which ('{}') is a valid but contentless record.
        ld, lc = jd['ledger'], jc['ledger']
        if ld['total_lines'] != lc['total_lines'] + 10 or ld['records'] != lc['records'] + 2:
            print('   line accounting wrong: %r vs %r' % (ld, lc))
            return False
        if ld['skipped'] != lc['skipped'] + 8:
            print('   skipped count wrong: %r' % ld)
            return False
        # And a non-numeric confidence must not be counted as one.
        return True
    lm_probe('localmodels (phase15): blank / garbage / truncated / non-object lines are '
             'SKIPPED and the good records still produce byte-identical metrics',
             tune_malformed_tolerance)

    # ---- a name outside the recorded candidate set is called out as a false repair ----
    def tune_false_repair_named():
        # One accepted repair whose emitted name is NOT in the candidate set the same
        # request recorded, and one clean accepted repair. The tool must count 1 of 2 and
        # name the reason, and must print both counts - a bare "50%" hides n=2.
        path = tune_write('cand.jsonl', [
            tune_line('repair', 'tr_c1', ['read_file'], tune_call('write_file'), 0.9, 5),
            tune_line('repair', 'tr_c2', ['read_file', 'grep'], tune_call('read_file'), 0.9, 6),
            tune_outcome('tr_c1', 'accepted'),
            tune_outcome('tr_c2', 'accepted'),
        ])
        r, j = tune_json(path)
        if j is None:
            return False
        fr = (j.get('repair') or {}).get('false_repair') or {}
        if fr.get('count') != 1 or fr.get('denominator') != 2 \
                or (fr.get('by_reason') or {}).get('name_outside_candidates') != 1:
            print('   name_outside_candidates not detected: %r' % fr)
            return False
        # A record with NO recorded candidate set is unobservable, not a pass.
        path2 = tune_write('nocand.jsonl', [
            tune_line('repair', 'tr_n1', None, tune_call('read_file'), 0.9, 5),
            tune_outcome('tr_n1', 'accepted'),
        ])
        _, j2 = tune_json(path2)
        fr2 = ((j2 or {}).get('repair') or {}).get('false_repair') or {}
        if fr2.get('candidates_unobservable') != 1:
            print('   a record with no candidate set should be reported unobservable: %r' % fr2)
            return False
        # The human report must say which rite escaped the candidate set.
        h = tune_run(['--ledger', path])
        return (h.returncode == 0 and 'name_outside_candidates' in h.stdout
                and '1/2' in h.stdout)
    lm_probe('localmodels (phase15): a repair whose rite name falls OUTSIDE the recorded '
             'candidate set is counted as a false repair and named in the report',
             tune_false_repair_named)

    # ---- the --json contract the integrator scripts against ----
    def tune_json_contract():
        path = tune_write('a.jsonl', TUNE_RECORDS)
        det = os.path.join(TUNE_DIR, 'det.json')
        r = subprocess.run(['node', TUNE_MJS], capture_output=True, text=True, timeout=120,
                           cwd=BASE)
        if r.returncode == 0 and r.stdout.strip():
            with open(det, 'w', encoding='utf-8') as f:
                f.write(r.stdout)
        r2, j = tune_json(path, ['--deterministic', det] if os.path.isfile(det) else [])
        if j is None:
            return False
        if set(j) != {'ledger', 'corpus', 'deterministic', 'per_threshold',
                      'repair', 'select', 'decide'}:
            print('   top-level keys: %r' % sorted(j))
            return False
        for op in ('repair', 'select', 'decide'):
            block = j[op]
            for key in ('records', 'outcomes', 'confidence_n', 'acceptance', 'false_repair',
                        'latency_ms', 'per_threshold'):
                if key not in block:
                    print('   %s is missing the stable key %r' % (op, key))
                    return False
            lat = block['latency_ms']
            if set(lat) < {'p50', 'p95', 'p99', 'n'}:
                print('   %s.latency_ms keys: %r' % (op, sorted(lat)))
                return False
            for row in block['per_threshold']:
                for key in ('threshold', 'tp', 'fp', 'fn', 'precision', 'recall'):
                    if key not in row:
                        print('   %s.per_threshold row missing %r: %r' % (op, key, row))
                        return False
                for key in ('precision', 'recall'):
                    v = row[key]
                    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                        print('   %s.%s must be a number or null, got %r' % (op, key, v))
                        return False
        # No NaN/Infinity anywhere: those are not valid JSON and would silently survive
        # json.loads() in a consumer while meaning nothing.
        raw = tune_run(['--ledger', path, '--json']).stdout
        if 'NaN' in raw or 'Infinity' in raw:
            print('   the JSON contains NaN/Infinity')
            return False
        # --thresholds must REPLACE the grid, not extend it.
        _, j3 = tune_json(path, ['--thresholds', '0.3,0.9'])
        grid = [(row['threshold']) for row in j3['repair']['per_threshold']]
        if grid != [0.3, 0.9]:
            print('   --thresholds grid wrong: %r' % grid)
            return False
        # ... and the DEFAULT grid must always contain the two shipped values.
        _, j4 = tune_json(path)
        default_grid_thresholds = j4['per_threshold']
        for shipped in (0.75, 0.70):
            if shipped not in default_grid_thresholds:
                print('   the default grid is missing the shipped %r' % shipped)
                return False
        if len(default_grid_thresholds) != 19:
            print('   the default grid is %d points, expected 19 (0.05..0.95)'
                  % len(default_grid_thresholds))
            return False
        # A malformed --thresholds entry is a loud error, not a silently shorter grid.
        bad = tune_run(['--ledger', path, '--thresholds', '0.5,oops'])
        return (bad.returncode != 0 and 'oops' in (bad.stderr + bad.stdout))
    lm_probe('localmodels (phase15): --json carries every stable key with null (never NaN) '
             'where unobservable, and --thresholds replaces the grid while the default keeps '
             'the shipped 0.75/0.70', tune_json_contract)

    # ---- the deterministic section is fed in, never invented ----
    def tune_deterministic_section():
        path = tune_write('a.jsonl', TUNE_RECORDS)
        # (a) not supplied: the DETERMINISTIC section says "no data" and prints no rate.
        # Scope the check to that section only. A bare `'%' in r.stdout` is a false
        # positive: a real ledger legitimately prints acceptance / sweep percentages
        # elsewhere in the same report, which has nothing to do with this section.
        r = tune_run(['--ledger', path])
        det_block = r.stdout.split('-- deterministic-pass fix rate --', 1)[-1].split('--', 1)[0]
        if 'no data' not in det_block.lower() or '%' in det_block:
            print('   an unsupplied deterministic section printed a rate:\n%s'
                  % det_block[:600])
            return False
        # (b) supplied with the real .mjs output: the rate comes through verbatim.
        det = os.path.join(TUNE_DIR, 'det.json')
        try:
            mjs = subprocess.run(['node', TUNE_MJS], capture_output=True, text=True,
                                 timeout=120, cwd=BASE)
        except FileNotFoundError:
            print('   node not on PATH - skipping the deterministic feed-in check')
            return True
        if mjs.returncode != 0:
            return False
        with open(det, 'w', encoding='utf-8') as f:
            f.write(mjs.stdout)
        r2, j = tune_json(path, ['--deterministic', det])
        src = json.loads(mjs.stdout)
        if j is None or (j.get('deterministic') or {}).get('rate') != src['rate'] \
                or (j.get('deterministic') or {}).get('total') != TUNE_CASES:
            print('   the supplied rate did not come through: %r' % (j or {}).get('deterministic'))
            return False
        h = tune_run(['--ledger', path, '--deterministic', det])
        # (c) an unreadable --deterministic file is no data, not a crash and not 0.0.
        bad = tune_run(['--ledger', path, '--deterministic',
                        os.path.join(TUNE_DIR, 'no-such-det.json')])
        _, j3 = tune_json(path, ['--deterministic', os.path.join(TUNE_DIR, 'nope.json')])
        bad_block = bad.stdout.split('-- deterministic-pass fix rate --', 1)[-1].split('--', 1)[0]
        return (h.returncode == 0 and '%' in h.stdout
                and bad.returncode == 0 and 'no data' in bad_block.lower()
                and (j3.get('deterministic') or {}).get('rate') is None)
    lm_probe('localmodels (phase15): the deterministic section reports "not supplied" '
             'plainly, feeds a supplied rate through verbatim, and turns an unreadable '
             '--deterministic file into no data', tune_deterministic_section)

    # ---- $LEDGER_PATH resolution, exactly ledger.default_path() precedence ----
    def tune_ledger_path_env():
        path = tune_write('env.jsonl', TUNE_RECORDS)
        # With the env var set and no --ledger, the tool must read THAT file.
        rr = tune_run(['--json'], cwd=BASE, env={'LEDGER_PATH': path})
        if rr.returncode != 0:
            print('   rc=%s stderr=%s' % (rr.returncode, rr.stderr[-300:]))
            return False
        try:
            jj = json.loads(rr.stdout)
        except Exception as e:
            print('   stdout not json: %s' % e)
            return False
        if (jj.get('ledger') or {}).get('path') != path \
                or (jj.get('repair') or {}).get('records') != TUNE_REPAIR_RECORDS:
            print('   $LEDGER_PATH was not honoured: %r' % (jj.get('ledger') or {}).get('path'))
            return False
        # An explicit --ledger must WIN over the env var.
        other = tune_write('other.jsonl', TUNE_RECORDS[:1])
        rr2 = tune_run(['--ledger', other, '--json'], cwd=BASE, env={'LEDGER_PATH': path})
        if (json.loads(rr2.stdout).get('ledger') or {}).get('path') != other:
            print('   --ledger did not override $LEDGER_PATH')
            return False
        # With neither, the default is the repo-relative path ledger.default_path() uses -
        # and it may well not exist, which must be a clean no-data, not a crash.
        rr3 = tune_run(['--json'], cwd=BASE, env={'LEDGER_PATH': ''})
        if rr3.returncode != 0 or 'Traceback' in rr3.stderr:
            print('   the default path did not degrade cleanly: %r' % rr3.stderr[-300:])
            return False
        jd = json.loads(rr3.stdout)
        return (jd['ledger']['path'] == 'var/local-models.jsonl'
                or jd['ledger']['path'].endswith(os.path.join('var', 'local-models.jsonl')))
    lm_probe('localmodels (phase15): the default ledger path follows ledger.default_path() '
             'precedence ($LEDGER_PATH, then var/local-models.jsonl) and --ledger overrides it',
             tune_ledger_path_env)

    # ---- the tool is a READER: running it must not create a single file ----
    def tune_writes_nothing():
        path = tune_write('a.jsonl', TUNE_RECORDS)
        det = os.path.join(TUNE_DIR, 'det.json')
        before = sorted(os.listdir(TUNE_DIR))
        before_stat = os.stat(path)
        r = tune_run(['--ledger', path, '--corpus', TUNE_CORPUS], cwd=TUNE_DIR)
        r2 = tune_run(['--ledger', path, '--json'], cwd=TUNE_DIR)
        after = sorted(os.listdir(TUNE_DIR))
        if before != after:
            print('   the tool created %r' % (set(after) - set(before)))
            return False
        # The ledger must be untouched, not even re-sorted or re-flushed.
        after_stat = os.stat(path)
        if (before_stat.st_size, before_stat.st_mtime) != (after_stat.st_size, after_stat.st_mtime):
            print('   the tool modified the ledger it was asked to read')
            return False
        if sorted(os.listdir(BASE)) != BASE_BEFORE:
            print('   the tool created %r in the repo'
                  % (set(os.listdir(BASE)) - set(BASE_BEFORE)))
            return False
        # A missing ledger must not be CREATED either - that is the classic way a "reader"
        # silently becomes a second writer.
        gone = os.path.join(TUNE_DIR, 'gone.jsonl')
        tune_run(['--ledger', gone], cwd=TUNE_DIR)
        # NOTE: do NOT assert `det.json` is absent from TUNE_DIR. An EARLIER phase-15 case
        # writes that file there on purpose, so it is present in `before` too; asserting its
        # absence tested a shared-fixture assumption, not the tool. "This test's runs created
        # nothing" is exactly the `before != after` check above.
        return r.returncode == 0 and r2.returncode == 0 and not os.path.exists(gone)
    lm_probe('localmodels (phase15): running the tool writes NO file anywhere - not in the '
             'temp dir, not in the repo, and it never creates the ledger it was asked to read',
             tune_writes_nothing)

    # ---- a real daemon's ledger, read back through the tool ----
    def tune_real_daemon_ledger():
        # The strongest check available: boot the real --no-needle daemon, POST a /repair
        # and a /select so the daemon appends REAL records in the REAL shape, then read
        # them with the tool. The synthetic fixture cannot catch a field-name drift
        # between this tool and local_models_daemon.py; this can.
        d = tempfile.mkdtemp(prefix='cogtunedaemon-')
        led = os.path.join(d, 'real.jsonl')
        port = 18973
        proc = subprocess.Popen(
            [sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
             '--port', str(port), '--ledger', led, '--no-needle', '--no-laya'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=d)
        register_port_token(port, read_server_token(os.path.join(BASE, 'localmodels', '.cogitator-token')))
        try:
            time.sleep(1.2)
            req(port, '/repair', {'suspect': {'name': 'read-file', 'args': '{"path":"a.py"}'},
                                  'candidates': SEL_TOOLS, 'trace_id': 'tr_real_repair'})
            req(port, '/select', {'input': 'read main.py', 'candidates': SEL_TOOLS,
                                  'trace_id': 'tr_real_select'})
            # A real operator ACCEPT on the repair, and IGNORE on the select.
            req(port, '/ledger', {'trace_id': 'tr_real_repair', 'action': 'accepted',
                                  'note': 'phase15 e2e'})
            req(port, '/ledger', {'trace_id': 'tr_real_select', 'action': 'ignored_by_operator'})
            rr, j = tune_json(led, cwd=BASE)
            if j is None:
                print('   the daemon ledger did not parse: rc=%s' % rr.returncode)
                return False
            if (j.get('ledger') or {}).get('records') != 4:
                print('   expected 4 real records, got %r' % (j.get('ledger') or {}))
                return False
            rp = j.get('repair') or {}
            # A --no-needle repair degrades: the output is {degraded:...} with no call, so
            # an ACCEPTED degraded repair is exactly the empty_output false repair.
            if (rp.get('outcomes_folded') or {}).get('accepted') != 1 \
                    or (rp.get('acceptance') or {}).get('denominator') != 1:
                print('   real repair acceptance wrong: %r / %r'
                      % (rp.get('outcomes_folded'), rp.get('acceptance')))
                return False
            fr = rp.get('false_repair') or {}
            if fr.get('count') != 1 or (fr.get('by_reason') or {}).get('empty_output') != 1:
                print('   a real accepted degraded repair was not flagged: %r' % fr)
                return False
            if rp.get('degraded') != 1:
                print('   degraded not counted from a real record: %r' % rp.get('degraded'))
                return False
            # The ignored select is NOT a false repair: nothing was acted on.
            if ((j.get('select') or {}).get('false_repair') or {}).get('count') != 0:
                print('   an ignored proposal was counted as a false repair')
                return False
            # r5-style: a real degraded record has confidence None, so confidence_n is 0
            # and every sweep row must be null rather than a fabricated 0.
            if rp.get('confidence_n') != 0 or rp['per_threshold'][0]['precision'] is not None:
                print('   a real null-confidence record produced a measurable rate')
                return False
            h = tune_run(['--ledger', led], cwd=BASE)
            return h.returncode == 0 and 'Traceback' not in h.stderr
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(d, ignore_errors=True)
    lm_probe('localmodels (phase15): a REAL --no-needle daemon ledger (repair + select + a '
             'real accepted/ignored outcome join) reads back with the right acceptance, '
             'false-repair and degraded counts', tune_real_daemon_ledger)
finally:
    shutil.rmtree(TUNE_DIR, ignore_errors=True)

# ============================ PHASE 18 =====================================
# Sidecar concurrency: the model lock must be reentrant, the model pool must be
# BOUNDED (a stale work item cannot occupy the single worker forever), and a
# DYING child's teardown must not fail the LIVE child's pending requests.
#
# CR-0010 (critical): `_LOCK` was a plain Lock and `_build` re-acquires it while
# `repair` already holds it, so every tuned-weights /repair deadlocked forever.
# `NEEDLE_WEIGHTS` had 0 occurrences in the whole suite, which is precisely why a
# guaranteed deadlock shipped green.
# CR-0019 (high): MODEL_POOL had an unbounded queue and the timeout path never
# called future.cancel(), so a stale item held the single worker indefinitely.
# CR-0020 (high): `_read_stdout`'s finally ran `_fail_pending` unconditionally, so
# an OLD child's EOF failed the NEW child's in-flight request.
sys.path.insert(0, os.path.join(BASE, 'localmodels'))


def _stub_needle_module():
    """Install a minimal `needle` module so the backend's model path is reachable.

    needle is not installed on this machine, and `_build` returns 'tool_unavailable'
    BEFORE it ever takes the lock when the import fails - so without a stub the
    deadlock is unreachable and the test would pass vacuously. The stub records its
    construction so the RLock test can prove `build()` actually ran.
    """
    import types
    if 'needle' in sys.modules:
        return sys.modules['needle']
    stub = types.ModuleType('needle')
    # NB: a plain list built OUTSIDE the class body. `stub.__x` written inside a class
    # body is name-mangled to `stub._Agent__x` and raises AttributeError.
    built = []
    stub.built = built

    class _Agent:
        def __init__(self, **kw):
            self.kw = kw
            built.append(kw)

        def complete(self, text, max_new_tokens=384):
            return {'function_calls': [], 'confidence': None, 'reasoning': '',
                    'success': True}

    stub.Needle = _Agent
    sys.modules['needle'] = stub
    return stub


def p18_weights_file():
    f = tempfile.NamedTemporaryFile(prefix='p18-weights-', suffix='.bin', delete=False)
    f.write(b'\x00' * 16)
    f.close()
    return f.name


def p18_repair_with_weights(timeout_s=8.0):
    """CR-0010: /repair with NEEDLE_WEIGHTS set to a REAL file must RETURN.

    The timeout is the assertion: a deadlock must FAIL rather than stall the suite.
    """
    w = p18_weights_file()
    _stub_needle_module()
    import needle_backend as nb

    old_w = os.environ.get('NEEDLE_WEIGHTS')
    os.environ['NEEDLE_WEIGHTS'] = w
    try:
        b = nb.NeedleBackend()
        res = {}

        def go():
            try:
                res['r'] = b.repair('repair this call',
                                    [{'name': 'read_file', 'description': 'Read a file.',
                                      'parameters': {}}])
            except BaseException as e:            # a raised error is a failure too
                res['exc'] = '%s: %s' % (type(e).__name__, e)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            print('   DEADLOCK: repair() with tuned weights never returned in %ss '
                  '(CR-0010: _LOCK is not reentrant)' % timeout_s)
            return False
        if 'exc' in res:
            print('   repair() raised %s' % res['exc'])
            return False
        r = res.get('r') or {}
        if not r.get('ok'):
            print('   repair() returned a degraded answer: %r' % (r,))
            return False
        return True
    finally:
        if old_w is None:
            os.environ.pop('NEEDLE_WEIGHTS', None)
        else:
            os.environ['NEEDLE_WEIGHTS'] = old_w
        try:
            os.unlink(w)
        except OSError:
            pass


def p18_lock_is_reentrant():
    """CR-0010, structural half: the module lock must admit the same thread twice.

    Asserted against the RULE (a plain Lock cannot be re-entered) rather than the
    spelling, so swapping Lock for any non-reentrant primitive still fails.
    """
    import needle_backend as nb
    acquired_inner = []

    def outer():
        nb._LOCK.acquire()
        try:
            # Same thread, second acquire. A plain Lock parks here forever.
            got = nb._LOCK.acquire(timeout=2.0)
            acquired_inner.append(got)
            if got:
                nb._LOCK.release()
        finally:
            nb._LOCK.release()

    t = threading.Thread(target=outer, daemon=True)
    t.start()
    t.join(6.0)
    if t.is_alive():
        print('   DEADLOCK: _LOCK is not reentrant (repair->_build would hang forever)')
        return False
    if not acquired_inner or not acquired_inner[0]:
        print('   _LOCK could not be re-acquired by the thread that already holds it')
        return False
    return True


def p18_pool_is_bounded():
    """CR-0019: MODEL_POOL must have a queue bound AND cancel on the timeout path.

    Both halves are load-bearing and both are asserted, because either alone leaves
    the failure: an unbounded queue grows forever, and an uncancelled stale item
    occupies the single worker even when the queue itself is bounded.

    The cancel half is asserted PER CALL SITE, not as "the file mentions .cancel()".
    A whole-file substring scan passes as soon as ONE of the two timeout paths is
    fixed - which is exactly the half-done fix that leaves /select starving /repair,
    since they share the one worker. Parsed per function, so fixing one and forgetting
    the other is a red, not a green.
    """
    import ast
    import local_models_daemon as lmd
    path = os.path.join(BASE, 'localmodels', 'local_models_daemon.py')
    src = open(path, encoding='utf-8').read()

    problems = []
    bound = getattr(lmd, 'MODEL_POOL_MAX_QUEUE', None)
    if not isinstance(bound, int) or bound < 1:
        problems.append('no finite MODEL_POOL_MAX_QUEUE (got %r)' % (bound,))

    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for fname in ('_repair_call', '_select_call'):
        fn = funcs.get(fname)
        if fn is None:
            problems.append('%s not found' % fname)
            continue
        calls = {n.func.attr for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        if 'cancel' not in calls:
            problems.append('%s never cancels its work item on the timeout path' % fname)
        if 'submit_model_call' not in {n.func.id for n in ast.walk(fn)
                                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}:
            problems.append('%s bypasses the bounded admission (submit_model_call)' % fname)
    if problems:
        print('   ' + '; '.join(problems))
        return False
    return True


def p18_pool_refuses_over_saturation():
    """CR-0019, behavioural half: the (N+1)th concurrent call is REFUSED, not queued.

    Saturates the single worker, then submits past the bound and asserts the daemon
    answers degraded/busy instead of accepting unbounded work.
    """
    import local_models_daemon as lmd
    bound = getattr(lmd, 'MODEL_POOL_MAX_QUEUE', None)
    if not isinstance(bound, int) or bound < 1:
        print('   no finite MODEL_POOL_MAX_QUEUE to saturate against (got %r)' % (bound,))
        return False
    # A PRIVATE pool, not the global MODEL_POOL: saturating the real one would leave
    # work queued behind `gate`, and a later in-process case using _repair_call could
    # transiently see a spurious 'engine busy' that has nothing to do with itself.
    pool = lmd._BoundedModelPool(max_workers=1, max_queue=bound,
                                 thread_name_prefix='p18-probe')
    gate = threading.Event()
    try:
        # occupy the single worker
        pool.submit(lambda: gate.wait(30))
        time.sleep(0.3)
        refusals = []
        for i in range(bound + 3):
            try:
                pool.submit(lambda: 'never runs')
                refusals.append(False)
            except Exception:
                refusals.append(True)
        gate.set()
    finally:
        pool.shutdown(wait=False)
    # At least one submission past the bound must have been refused rather than queued.
    if not any(refusals):
        print('   all %d submissions past the bound were accepted - the queue is unbounded'
              % (bound + 3))
        return False
    return True


def p18_stale_child_teardown_spares_live_child():
    """CR-0020: a DYING child's teardown must not fail the LIVE child's pending work.

    Deterministic and process-free, but it drives the REAL `_spawn_locked` (with a
    fake Popen) rather than hand-setting the counter. That matters: a generation that
    never increments - `self._generation = 1` instead of `+= 1` - makes every child
    look current forever, and a fixture that assigns the counter itself would sail
    straight past it. So the two spawns here are the assertion that it is MONOTONIC.
    """
    import local_models_daemon as lmd

    child = lmd.LayaChild.__new__(lmd.LayaChild)
    child._fut_lock = threading.RLock()
    child._proc_lock = threading.RLock()
    child._write_lock = threading.Lock()
    child._futures = {}
    child._inflight = 0
    child._next_id = 0
    child._generation = 0
    child.loaded = False
    child.pid = None
    child._proc = None
    child.stderr_tail = __import__('collections').deque(maxlen=20)
    child.node_bin = 'node'
    child.child_path = '/nonexistent/laya_child.mjs'
    child.last_used = time.monotonic()

    class _FakeProc:
        """Stands in for a spawned child: empty stdio, alive until told otherwise."""

        def __init__(self):
            self.pid = 4242
            self.stdout = iter(())
            self.stderr = iter(())
            self.stdin = None

        def poll(self):
            return None

    real_popen = lmd.subprocess.Popen
    real_thread = threading.Thread
    spawned = []

    class _NoThread:
        """Swallow the reader threads; the fake stdio is empty anyway.

        Records (target, args) so the case can assert WHICH reader was told WHICH
        generation. Asserting only "some thread got gen1" is too loose - the stderr
        reader also receives a generation, so it would satisfy a check that the stdout
        reader (the actual CR-0020 call site) was left in the dark.
        """

        def __init__(self, target=None, args=(), **kw):
            spawned.append((getattr(target, '__name__', str(target)), args))

        def start(self):
            pass

    try:
        lmd.subprocess.Popen = lambda *a, **kw: _FakeProc()
        lmd.threading.Thread = _NoThread
        child._spawn_locked()
        gen1 = child._generation
        if gen1 != 1:
            print('   first spawn left the generation at %r (expected 1)' % gen1)
            return False
        # The child dies and a NEW one is spawned in its place.
        child._spawn_locked()
        gen2 = child._generation
        if gen2 <= gen1:
            print('   the generation did not advance across a respawn (%r -> %r); every '
                  'child would look current and a stale teardown would kill live work'
                  % (gen1, gen2))
            return False
        # The request is issued against the LIVE child (#2). This is the case that
        # matters: a request registered against the DYING child would legitimately be
        # failed by its own teardown, and asserting otherwise would pin the old bug.
        rid, fut = child._register()
        # The DYING child #1 hits stdout EOF. It must NOT fail child #2's request.
        child._fail_pending(lmd.ChildGone('old child stdout closed'), gen1)
        if fut.done():
            print('   a DYING child\'s teardown failed the LIVE child\'s request %r' % rid)
            return False
        # ...and the CURRENT child's own teardown must still fail it, so a guard that
        # refuses everything cannot pass this case.
        child._fail_pending(lmd.ChildGone('current child closed'), gen2)
        if not fut.done():
            print('   the CURRENT child\'s teardown no longer fails its own pending request')
            return False
        # The STDOUT reader - the actual CR-0020 call site - must be told which child it
        # belongs to. Checked by name, not "some thread got the generation".
        stdout_readers = [a for name, a in spawned if name == '_read_stdout']
        if not stdout_readers:
            print('   the stdout reader thread was never started')
            return False
        if not any(gen1 in (a or ()) for a in stdout_readers):
            print('   the stdout reader was not given the child generation %r' % gen1)
            return False
        return True
    finally:
        lmd.subprocess.Popen = real_popen
        lmd.threading.Thread = real_thread


lm_probe('localmodels (phase18): the model lock is REENTRANT (a plain Lock deadlocks '
         'repair->_build)', p18_lock_is_reentrant)

lm_probe('localmodels (phase18): /repair with NEEDLE_WEIGHTS set to a real file RETURNS '
         'instead of deadlocking [CR-0010]', p18_repair_with_weights)

lm_probe('localmodels (phase18): MODEL_POOL has a finite queue bound AND cancels the work '
         'item on the timeout path [CR-0019]', p18_pool_is_bounded)

lm_probe('localmodels (phase18): the (N+1)th concurrent model call is REFUSED, not queued '
         '[CR-0019]', p18_pool_refuses_over_saturation)

lm_probe('localmodels (phase18): a DYING child\'s teardown does not fail the LIVE child\'s '
         'pending request, and the current child\'s own teardown still does [CR-0020]',
         p18_stale_child_teardown_spares_live_child)


def p18_respawn_cannot_slip_through_the_fence():
    """CR-0020, the TOCTOU half: the fence and the drain must be ONE critical section.

    The single-threaded case above cannot tell a correct fence from a check performed
    outside the lock, because nothing interleaves. This case forces the interleaving
    deterministically: the respawn is made to happen exactly when the teardown takes
    `_fut_lock` - i.e. precisely inside the window a non-atomic fence leaves open.

      fence OUTSIDE the lock: check passes (gen still matches) -> respawn bumps the
        generation and registers fresh work -> drain kills that fresh work. Dead.
      fence INSIDE the lock: the respawn happens first, so the check now sees a
        generation that no longer matches and the teardown is a no-op. Alive.
    """
    import local_models_daemon as lmd

    child = lmd.LayaChild.__new__(lmd.LayaChild)
    child._proc_lock = threading.RLock()
    child._write_lock = threading.Lock()
    child._futures = {}
    child._inflight = 0
    child._next_id = 0
    child._generation = 1
    child.loaded = True
    child.pid = None
    child._proc = None
    child.stderr_tail = __import__('collections').deque(maxlen=20)
    child.node_bin = 'node'
    child.child_path = '/nonexistent/laya_child.mjs'
    child.last_used = time.monotonic()

    fresh = {}

    class _RespawnOnAcquire:
        """A lock whose acquisition performs the respawn - landing the interleaving
        exactly in the window between an outside-the-lock fence and the drain.

        Armed explicitly, because `_register` also takes this lock: arming it during
        setup would fire the respawn long before the teardown it is meant to interrupt.
        """

        def __init__(self):
            self._real = threading.RLock()
            self.armed = False

        def __enter__(self):
            self._real.acquire()
            if self.armed:
                self.armed = False
                # The dying child is replaced while the teardown is mid-flight.
                with self._real:
                    child._generation += 1        # child #2 is now current
                    from concurrent.futures import Future as _F
                    fresh['fut'] = _F()
                    # registered the way _register does it: (generation, future)
                    child._futures['ly%d' % (child._next_id + 1)] = (child._generation,
                                                                     fresh['fut'])
                    child._next_id += 1
                    child._inflight += 1
            return self

        def __exit__(self, *a):
            self._real.release()
            return False

    fence_lock = _RespawnOnAcquire()
    child._fut_lock = fence_lock

    # Work in flight against child #1.
    rid_old, fut_old = child._register()

    fence_lock.armed = True
    # Child #1's stdout reader hits EOF, believing it is still the current child.
    child._fail_pending(lmd.ChildGone('child stdout closed'), 1)

    new_fut = fresh.get('fut')
    if new_fut is None:
        print('   the forced respawn never happened - the case did not exercise the race')
        return False
    if new_fut.done():
        print('   a respawn that happened DURING the teardown had its fresh request killed '
              '(the fence was evaluated outside _fut_lock - CR-0020 reopens)')
        return False
    if not fut_old.done():
        print('   the dying child\'s own request was not failed - the fence is too strict '
              'and would leave real work hanging')
        return False
    return True


def p18_teardown_signature_is_fenced():
    """CR-0020, structurally: every teardown must NAME the child it is about.

    Two properties, both asserted because either alone leaves the invariant unenforceable:
      - `_fail_pending` takes `generation` with NO default, so a call site that forgets it
        is a TypeError at review time rather than a silent reinstatement of the old bug;
      - every call site in the module passes one.
    """
    import ast
    import local_models_daemon as lmd
    path = os.path.join(BASE, 'localmodels', 'local_models_daemon.py')
    tree = ast.parse(open(path, encoding='utf-8').read())

    problems = []
    cls = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == 'LayaChild':
            cls = n
    if cls is None:
        return False
    fp = None
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == '_fail_pending':
            fp = n
    if fp is None:
        problems.append('_fail_pending not found')
    else:
        args = fp.args.args[1:]                # skip self
        names = [a.arg for a in args]
        if 'generation' not in names:
            problems.append('_fail_pending has no `generation` parameter')
        elif fp.args.defaults:
            problems.append('_fail_pending\'s `generation` has a default - the unscoped '
                            'teardown path is reachable again')
    # Every call site must pass a generation.
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == '_fail_pending':
            if len(n.args) < 2:
                problems.append('line %d: _fail_pending called without a generation'
                                % n.lineno)
    if problems:
        print('   ' + '; '.join(problems))
        return False
    return True


lm_probe('localmodels (phase18): a respawn DURING a teardown does not have its fresh '
         'request killed, and the dying child\'s own request is still failed [CR-0020]',
         p18_respawn_cannot_slip_through_the_fence)

lm_probe('localmodels (phase18): _fail_pending requires a generation and every teardown '
         'call site names its child [CR-0020]', p18_teardown_signature_is_fenced)


def _bare_child(lmd):
    """A LayaChild wired up exactly as the other phase-18 cases wire it."""
    child = lmd.LayaChild.__new__(lmd.LayaChild)
    child._fut_lock = threading.RLock()
    child._proc_lock = threading.RLock()
    child._write_lock = threading.Lock()
    child._futures = {}
    child._inflight = 0
    child._next_id = 0
    child._generation = 0
    child.loaded = False
    child.pid = None
    child._proc = None
    child.stderr_tail = __import__('collections').deque(maxlen=20)
    child.node_bin = 'node'
    child.child_path = '/nonexistent/laya_child.mjs'
    child.last_used = time.monotonic()
    return child


def p18_teardown_clears_loaded_flag():
    """A child that dies with NO request in flight must still be marked not-loaded.

    `_fail_pending` returned False on an empty drain BEFORE resetting `loaded`, so an
    idle child that was reaped - or one that crashed between two requests - stayed
    `loaded=True` forever. `health_obj()` then reports `laya.loaded: true` for a child
    that does not exist, and nothing else in the daemon clears it until a successful
    `_laya_decide`. The flag describes THE CHILD, so it must follow the child's death,
    not the fate of the requests that happened to be in flight.

    Caught by an independent review of the phase-18 diff; the mutation harness could
    not see it because every other phase-18 case leaves a request registered, so the
    early return never fired.
    """
    import local_models_daemon as lmd

    child = _bare_child(lmd)
    child._generation = 1
    child.loaded = True                       # the engine had finished loading
    child._futures = {}                       # ... and nothing is in flight right now

    child._fail_pending(lmd.ChildGone('child stdout closed'), 1)

    if child.loaded:
        print('   a child that died with no request in flight stayed loaded=True - '
              '/health would report a live engine that no longer exists')
        return False
    return True


def p18_stale_teardown_leaves_live_child_loaded():
    """The CR-0020 fence must cover the `loaded` FLAG, not only the futures.

    The generation fence was applied to the future drain only. `self.loaded = False`
    still fired unconditionally, so a dying child's teardown that happened to match at
    least one stale entry cleared the flag belonging to a NEWER, live, loaded child -
    the same class of defect as the one this phase exists to close, one line away.

    `loaded` is only set True again by a successful `_laya_decide`, so /health lies
    about the engine until the next decide happens to succeed.
    """
    import local_models_daemon as lmd
    from concurrent.futures import Future as _F

    child = _bare_child(lmd)
    # Child #1 had one request in flight; it has since died.
    child._generation = 1
    stale = _F()
    child._futures = {'ly1': (1, stale)}
    child._inflight = 1
    # Child #2 respawned and finished loading - it is the live, loaded engine.
    child._generation = 2
    child.loaded = True

    # Child #1's stdout reader now hits EOF and tears itself down.
    child._fail_pending(lmd.ChildGone('child stdout closed'), 1)

    if not stale.done():
        print('   the dying child\'s own in-flight request was not failed - the fence is '
              'too strict and real work would hang until its caller timed out')
        return False
    if not child.loaded:
        print('   a DYING child\'s teardown cleared `loaded` on the LIVE current child - '
              'the generation fence does not cover the loaded flag (CR-0020 half-open)')
        return False
    return True


def p18_stamp_binds_to_the_receiving_child():
    """`_stamp` must bind a request to the child that RECEIVED the bytes.

    `_stamp` re-read `self._generation` at stamp time, after both `_proc_lock` and
    `_write_lock` had been released. A respawn between the write and the stamp (any
    other thread reaching `_write` while the dead child is being replaced) therefore
    stamps the request with the NEW child's generation - so the teardown of the child
    that actually received, and was serving, the request does not match it, and the
    caller waits out its entire timeout instead of being told the child is gone.

    This is exactly the leak the `_stamp` re-stamp was added to close, reopened by
    reading the counter instead of carrying the identity of the process the bytes went
    to. The fix is to capture the generation under `_proc_lock`, next to the proc
    handle that IS the identity, and hand it to `_stamp`.
    """
    import local_models_daemon as lmd
    from concurrent.futures import Future as _F

    child = _bare_child(lmd)
    child._generation = 1
    fut = _F()
    child._futures = {'ly1': (1, fut)}
    child._next_id = 1
    child._inflight = 1

    class _LiveProc:
        pid = 4242
        stdout = iter(())
        stderr = iter(())
        stdin = None

        def poll(self):
            return None

    # The write lands on child #1 ...
    child._proc = _LiveProc()
    # ... and a concurrent respawn replaces it BEFORE the stamp runs.
    child._generation = 2
    child._proc = _LiveProc()

    # `_stamp` must be handed the identity of the child the bytes went to. Call it the
    # way the fixed `_write` will - with that generation - and assert the binding.
    child._stamp('ly1', 1)

    if child._futures.get('ly1', (None, None))[0] != 1:
        print('   the request was not bound to the child that received it (generation %r '
              'recorded, expected 1) - a respawn between the write and the stamp '
              're-opens the CR-0020 request leak'
              % (child._futures.get('ly1'),))
        return False
    return True


def p18_stdout_pop_is_locked():
    """Every mutation of `_futures` must hold `_fut_lock` (CR-0020 structural half).

    `_fail_pending` drains `_futures` under `_fut_lock` and its whole atomicity claim
    rests on that: the fence and the drain must be one critical section. But
    `_read_stdout` pops from the SAME dict with no lock at all, so the reader thread
    that resolves a request can interleave with the drain's iteration. The lock
    protects the dict against every writer except the one that matters most.

    Asserted with AST rather than behaviourally: the interleaving is a narrow window,
    and a test that cannot reliably provoke it would report coverage that does not
    exist. The rule is the property - every `_futures` mutation is inside a `with
    self._fut_lock` - and it is checked at every call site, so a future reader added
    without the lock fails as itself.
    """
    import ast
    path = os.path.join(BASE, 'localmodels', 'local_models_daemon.py')
    tree = ast.parse(open(path, encoding='utf-8').read())

    cls = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == 'LayaChild':
            cls = n
    if cls is None:
        print('   LayaChild not found')
        return False

    def locked_regions(fn):
        """Byte-ranges of `fn`'s body that run inside `with self._fut_lock`."""
        spans = []
        for node in ast.walk(fn):
            if isinstance(node, ast.With):
                for item in node.items:
                    expr = item.context_expr
                    if (isinstance(expr, ast.Attribute)
                            and expr.attr == '_fut_lock'
                            and isinstance(expr.value, ast.Name)
                            and expr.value.id == 'self'):
                        spans.append((node.lineno, max(getattr(s, 'end_lineno', node.lineno)
                                                      for s in ast.walk(node))))
        return spans

    problems = []
    checked = 0
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        spans = locked_regions(fn)
        for node in ast.walk(fn):
            # A subscript assignment or a `.pop()` on self._futures is a mutation.
            hit = None
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                v = node.value
                if (isinstance(v, ast.Attribute) and v.attr == '_futures'
                        and isinstance(v.value, ast.Name) and v.value.id == 'self'):
                    hit = 'assignment to self._futures'
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'pop'):
                v = node.func.value
                if (isinstance(v, ast.Attribute) and v.attr == '_futures'
                        and isinstance(v.value, ast.Name) and v.value.id == 'self'):
                    hit = 'self._futures.pop(...)'
            if hit is None:
                continue
            checked += 1
            if not any(lo <= node.lineno <= hi for lo, hi in spans):
                problems.append('%s(): line %d does %s outside `with self._fut_lock`'
                                % (fn.name, node.lineno, hit))

    if not checked:
        print('   no self._futures mutation found at all - the AST walk is broken, so '
               'this case proved nothing')
        return False
    if problems:
        print('   ' + '; '.join(problems))
        return False
    return True


lm_probe('localmodels (phase18): a child that dies with NO request in flight is still '
         'marked not-loaded', p18_teardown_clears_loaded_flag)

lm_probe('localmodels (phase18): a DYING child\'s teardown does not clear `loaded` on '
         'the LIVE current child [CR-0020 flag half]', p18_stale_teardown_leaves_live_child_loaded)

lm_probe('localmodels (phase18): _stamp binds a request to the child that RECEIVED the '
         'bytes, not to whichever is current at stamp time [CR-0020]',
         p18_stamp_binds_to_the_receiving_child)

lm_probe('localmodels (phase18): EVERY self._futures mutation holds _fut_lock, including '
         'the stdout reader\'s pop [CR-0020 atomicity]', p18_stdout_pop_is_locked)

def p18_http_repair_with_weights():
    """CR-0010, the SEAM: a REAL daemon process answering a REAL POST /repair.

    The in-process probe above proves the lock; this proves the shipped route. They can
    disagree - the handler thread, the pool and the lock are three different hops - and a
    deadlock anywhere along that path is what an operator actually hits. The daemon runs
    on its own port in a temp dir (never in the repo: it writes a pid/log beside itself),
    with a stub `needle` on PYTHONPATH so the tuned-weights build path is really taken.
    """
    w = p18_weights_file()
    stubdir = tempfile.mkdtemp(prefix='p18-stub-')
    with open(os.path.join(stubdir, 'needle.py'), 'w', encoding='utf-8') as fh:
        fh.write(
            'class Needle:\n'
            '    def __init__(self, **kw):\n'
            '        self.kw = kw\n'
            '    def complete(self, text, max_new_tokens=384):\n'
            '        return {"function_calls": [], "confidence": None, "reasoning": "",\n'
            '                "success": True}\n')
    d = tempfile.mkdtemp(prefix='p18-http-')
    # An EPHEMERAL port, not a hardcoded one: if something else already held a fixed
    # port, /health would succeed against the WRONG process and this case would pass
    # without ever exercising the daemon under test.
    import socket
    with socket.socket() as _s:
        _s.bind(('127.0.0.1', 0))
        port = _s.getsockname()[1]
    env = dict(os.environ)
    env['NEEDLE_WEIGHTS'] = w                       # the documented tuned-weights config
    env['PYTHONPATH'] = stubdir + os.pathsep + env.get('PYTHONPATH', '')
    pb = subprocess.Popen(
        [sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
         '--port', str(port), '--ledger', os.path.join(d, 'l.jsonl')],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=d, env=env)
    register_port_token(port, read_server_token(os.path.join(BASE, 'localmodels', '.cogitator-token')))
    try:
        deadline = time.time() + 15
        up = False
        while time.time() < deadline:
            try:
                if req(port, '/health')[0] == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.3)
        if not up:
            return False
        # The repair must ANSWER. The whole point of the case is the timeout: with the
        # plain Lock the handler thread parks inside MODEL_POOL's single worker forever
        # and this request never comes back.
        try:
            s, j = req(port, '/repair', {'suspect': {'name': 'raed_file', 'arguments':
                                                      {'path': 'a.py'}},
                                         'candidates': [{'name': 'read_file',
                                                         'description': 'Read a file.',
                                                         'parameters': {}}]})
        except Exception as e:
            print('   POST /repair never returned (%s) - the model worker is wedged' % e)
            return False
        if s != 200:
            print('   POST /repair answered %s: %r' % (s, j))
            return False
        if not (isinstance(j, dict) and j.get('ok') is True):
            print('   POST /repair returned a non-ok envelope: %r' % (j,))
            return False
        return True
    finally:
        pb.terminate()
        try:
            pb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pb.kill()
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(stubdir, ignore_errors=True)
        try:
            os.unlink(w)
        except OSError:
            pass


lm_probe('localmodels (phase18): a REAL daemon answers POST /repair with tuned weights '
         'set - the shipped route, not just the in-process lock [CR-0010]',
         p18_http_repair_with_weights)

# Never leave the stub behind for a later phase to inherit silently.
sys.modules.pop('needle', None)

# =====================================================================
# PHASE 19 — per-install auth token + Host pin + CORS-on-refusal +
# daemon lifecycle (CR-Nanites-harness-0017, 0016, 0023, 0024)
# =====================================================================
#
# Every case below is a real behavioural assertion against a real server
# process, not a source-shape check. Three rules this block obeys:
#
#  * Each server is launched from a COPY in a temp dir. bridge_daemon.py
#    writes bridge_daemon.pid and bridge_daemon.log NEXT TO ITSELF, so a
#    suite that launched it in-place would litter the repo (finding 0008).
#  * Every token is READ FROM THAT SERVER'S OWN TOKEN FILE. The suite knows
#    the path because the suite chose the directory the server runs in, so
#    the test never hardcodes a secret and never invents one.
#  * Every live/concurrent case is bounded, so a deadlock FAILS the suite
#    instead of stalling the run (phase 18's lesson).

P19_WRONG_TOKEN = 'p19-not-the-token-' + 'x' * 40
P19_BRIDGE_MARKER = '# ==== COGITATOR BRIDGE — managed by bridge_daemon.py ===='


def p19_probe(msg, fn):
    """Run one phase-19 case; an exception is a FAIL for that case, not an abort."""
    try:
        check(bool(fn()), msg)
    except Exception as e:
        check(False, '%s [%s: %s]' % (msg, type(e).__name__, e))


def p19_wait_port(port, deadline=20.0):
    """Wait for a server to accept connections. Bounded so a boot failure FAILS.

    Also proves the port was FREE before we started: a phase that reuses a port
    an earlier phase still holds never binds its own server, yet /health answers
    from the leftover process and every case silently tests the wrong program.
    `p19_assert_ports_free` is the real guard; this just confirms liveness.
    """
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=2) as r:
                r.read()
                return True
        except urllib.error.HTTPError:
            return True                      # a 403 is still a live server
        except Exception:
            time.sleep(0.2)
    return False


def p19_assert_ports_free(ports):
    """Refuse to run a phase whose ports are already held.

    Without this the collision is invisible: our own server exits with
    'address already in use', someone else's answers, and the phase reports a
    pass for behaviour it never exercised. This is the loud version of the same
    failure, and it is the reason the 18970-18972 comment above exists.
    """
    for p in ports:
        try:
            s = socket.create_connection(('127.0.0.1', p), timeout=1)
            s.close()
            check(False, 'phase19: port %d was ALREADY IN USE before launch - another '
                         'phase still owns it, so every case here would test that server '
                         'instead of ours' % p)
            return False
        except OSError:
            pass                             # nothing listening: the good case
    return True


def p19_read_token(path, label, deadline=15.0):
    """Read a server's per-install token, waiting (bounded) for it to be created.

    Returns None if the server never wrote one — which is itself the RED
    signal, and is reported as a failure of its own below.
    """
    end = time.time() + deadline
    tok = None
    while time.time() < end:
        try:
            with open(path, encoding='utf-8') as f:
                tok = f.read().strip()
            if tok:
                break
        except OSError:
            pass
        time.sleep(0.2)
    check(tok is not None, 'phase19 %s: created its per-install token file at %s'
          % (label, os.path.basename(path)))
    if tok is None:
        return P19_WRONG_TOKEN              # placeholder: cases still run and still fail
    check(len(tok) >= 32, 'phase19 %s: the token carries 32+ bytes of entropy' % label)
    if hasattr(os, 'stat'):
        mode = os.stat(path).st_mode & 0o777
        check(mode == 0o600, 'phase19 %s: the token file is mode 0600, not %o' % (label, mode))
    return tok


def p19_expected_bridge(src):
    """What bridge_daemon must write: its own source with BRIDGE_MARKER on line 1."""
    if src.startswith(P19_BRIDGE_MARKER):
        return src
    return P19_BRIDGE_MARKER + '\n' + src


def p19_stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


p19_servers = []

# ---- the three servers, each from a temp-dir copy ---------------------
# bridge worker: its own copy, so ITS token file lands in the temp dir too.
p19_bdir = tempfile.mkdtemp(prefix='cogp19-bridge-')
p19_proj = tempfile.mkdtemp(prefix='cogp19-proj-')
with open(os.path.join(p19_proj, 'hello.py'), 'w', encoding='utf-8') as f:
    f.write('print("phase19")\n')
shutil.copy2(os.path.join(BASE, 'bridge.py'), os.path.join(p19_bdir, 'bridge.py'))
# Ports are 18970-18972 on purpose. The obvious choice (18940-18942) is
# ALREADY TAKEN by phase 13/14 above (laya_port_f, laya_port_g, sel_port_a),
# and the collisions were silent: the phase-19 sidecar never bound, so
# p19_wait_port() saw a live server and every case was answered by a leftover
# phase-14 daemon on the same port. Two of those cases went red only after an
# unrelated edit changed timing. A port that another phase still holds is a
# collision you cannot see from the phase that owns it.
p19_bport = 18970

# supervisor: bridge_daemon.py AND bridge.py together, because the daemon
# refuses to spawn a bridge.py it cannot find next to itself.
p19_ddir = tempfile.mkdtemp(prefix='cogp19-daemon-')
shutil.copy2(os.path.join(BASE, 'bridge_daemon.py'), os.path.join(p19_ddir, 'bridge_daemon.py'))
shutil.copy2(os.path.join(BASE, 'bridge.py'), os.path.join(p19_ddir, 'bridge.py'))
p19_dport = 18971

# sidecar: launched from the repo (its token file is specified as living in
# localmodels/ next to the script), but its runtime state is all in a temp dir.
p19_ldir = tempfile.mkdtemp(prefix='cogp19-lm-')
p19_lport = 18972
p19_ledger = os.path.join(p19_ldir, 'local-models.jsonl')

# All three ports are declared BEFORE anything launches, so this guard can see
# all of them. A collision is silent otherwise (see p19_assert_ports_free).
p19_assert_ports_free([p19_bport, p19_dport, p19_lport])

p19_bproc = subprocess.Popen(
    [sys.executable, os.path.join(p19_bdir, 'bridge.py'), '--root', p19_proj, '--port', str(p19_bport)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=p19_bdir)
p19_servers.append(p19_bproc)

p19_dproc = subprocess.Popen(
    [sys.executable, os.path.join(p19_ddir, 'bridge_daemon.py'), '--port', str(p19_dport)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=p19_ddir)
p19_servers.append(p19_dproc)

p19_lproc = subprocess.Popen(
    [sys.executable, os.path.join(BASE, 'localmodels', 'local_models_daemon.py'),
     '--port', str(p19_lport), '--ledger', p19_ledger, '--no-needle', '--no-laya'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=p19_ldir)
p19_servers.append(p19_lproc)

P19_SUSPECT = {'name': 'read-file', 'arguments': '{"path": "a.py"}'}
P19_CANDIDATES = [{'type': 'function',
                   'function': {'name': 'read_file', 'description': 'Read a file.',
                                'parameters': {'type': 'object',
                                               'properties': {'path': {'type': 'string'}},
                                               'required': ['path']}}}]

if sys.platform == 'win32':
    p19_autostart_entry = None
elif sys.platform == 'darwin':
    p19_autostart_entry = os.path.expanduser('~/Library/LaunchAgents/com.cogitator.CogitatorBridgeDaemon.plist')
else:
    p19_autostart_entry = os.path.expanduser('~/.config/autostart/CogitatorBridgeDaemon.desktop')

try:
    p19_wait_port(p19_bport)
    p19_wait_port(p19_dport)
    p19_wait_port(p19_lport)

    p19_btok = p19_read_token(os.path.join(p19_bdir, '.cogitator-token'), 'bridge worker')
    p19_dtok = p19_read_token(os.path.join(p19_ddir, '.cogitator-token'), 'bridge_daemon')
    p19_ltok = p19_read_token(os.path.join(BASE, 'localmodels', '.cogitator-token'), 'local_models_daemon')

    # ---------------- 1-6: the bridge's privileged route --------------
    def p19_bridge_read():
        s, j = req(p19_bport, '/tools/execute',
                   {'name': 'read_file', 'arguments': {'path': 'hello.py'}})
        return s, j
    p19_probe('phase19 bridge: POST /tools/execute with NO token and NO Origin => 403',
              lambda: p19_bridge_read()[0] == 403)
    p19_probe('phase19 bridge: POST /tools/execute with NO token but a localhost Origin => 403 '
              '(the gate: a privileged route is refused even from loopback)',
              lambda: req(p19_bport, '/tools/execute',
                          {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                          origin=P19_ORIGIN)[0] == 403)
    p19_probe('phase19 bridge: POST /tools/execute with the RIGHT token and an allowed Origin => 200',
              lambda: req(p19_bport, '/tools/execute',
                          {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                          origin=P19_ORIGIN, token=p19_btok)[0] == 200)
    # Spec section 4: on a privileged route an ABSENT Origin is refused, even
    # with the right token. This is the deliberate behaviour change of the
    # phase (the `if not origin: return True` hole). Asserted explicitly
    # because it is the single line that makes the whole token gate meaningful.
    p19_probe('phase19 bridge: POST /tools/execute with the RIGHT token but NO Origin => 403 '
              '(absent Origin is refused on a privileged route)',
              lambda: req(p19_bport, '/tools/execute',
                          {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                          token=p19_btok)[0] == 403)
    p19_probe('phase19 bridge: POST /tools/execute with a WRONG token => 403',
              lambda: req(p19_bport, '/tools/execute',
                          {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                          origin=P19_ORIGIN, token=P19_WRONG_TOKEN)[0] == 403)
    p19_probe('phase19 bridge: the RIGHT token plus a FOREIGN Origin => 403 '
              '(the token does not buy past the Origin check — both must hold)',
              lambda: req(p19_bport, '/tools/execute',
                          {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                          origin='https://evil.example.com', token=p19_btok)[0] == 403)
    p19_probe('phase19 bridge: GET /health with NO token => 200 (non-privileged route stays open, '
              'so the UI status chip is alive before pairing)',
              lambda: req(p19_bport, '/health', origin=P19_ORIGIN)[0] == 200)

    # ---------------- 7-9: the daemon's privileged routes --------------
    p19_atk_wd = tempfile.mkdtemp(prefix='cogp19-atkwd-')

    def p19_daemon_set_workdir_no_token():
        s, j = req(p19_dport, '/set_workdir', {'path': p19_atk_wd, 'autostart': True})
        # The negative side effect, not just the status code: a 403 that still
        # wrote bridge.py would pass a status-only check, and the write is the
        # whole finding.
        return s == 403 and not os.path.exists(os.path.join(p19_atk_wd, 'bridge.py'))
    p19_probe('phase19 daemon: POST /set_workdir with NO token => 403 AND no bridge.py written '
              'to the target directory', p19_daemon_set_workdir_no_token)

    def p19_daemon_autostart_no_token():
        try:
            s, j = req(p19_dport, '/install_autostart', {})
            if p19_autostart_entry and os.path.exists(p19_autostart_entry):
                return False, s, 'an autostart entry WAS created'
            return s == 403, s, 'no entry created'
        finally:
            # The RED run really does reach install_autostart on an
            # unimplemented daemon, so clean up after ourselves unconditionally.
            if p19_autostart_entry and os.path.exists(p19_autostart_entry):
                try:
                    os.remove(p19_autostart_entry)
                except OSError:
                    pass
    _ok, _s, _why = (None, None, None)
    try:
        _r = p19_daemon_autostart_no_token()
        _ok, _s, _why = _r
    except Exception as e:
        _ok, _s, _why = False, 'exception', '%s: %s' % (type(e).__name__, e)
    check(bool(_ok), 'phase19 daemon: POST /install_autostart with NO token => 403 AND no '
                    'autostart entry created (answered %s; %s)' % (_s, _why))

    p19_wd_ok = tempfile.mkdtemp(prefix='cogp19-wdok-')
    p19_probe('phase19 daemon: POST /set_workdir with the RIGHT token => 200',
              lambda: req(p19_dport, '/set_workdir', {'path': p19_wd_ok, 'autostart': False},
                          origin=P19_ORIGIN, token=p19_dtok)[0] == 200)

    # ---------------- 10-11: the sidecar's privileged / non-privileged ----
    p19_probe('phase19 localmodels: POST /repair with NO token => 403',
              lambda: req(p19_lport, '/repair',
                          {'suspect': P19_SUSPECT, 'candidates': P19_CANDIDATES},
                          origin=P19_ORIGIN)[0] == 403)
    p19_probe('phase19 localmodels: GET /health with NO token => 200',
              lambda: req(p19_lport, '/health', origin=P19_ORIGIN)[0] == 200)

    # ---------------- a malformed token must be REFUSED, not crash the gate --
    # hmac.compare_digest refuses to compare str containing non-ASCII, and
    # http.server decodes header bytes as latin-1, so a single byte >= 0x80 in
    # the token raised TypeError inside the auth check. The request was refused
    # in the sense that nothing was served, but the CLIENT GOT NO RESPONSE: an
    # unauthenticated caller could drop the connection on every server. A guard
    # that crashes is not a guard that refuses - the status line is the assertion,
    # and -1 (no status line at all) is how the crash shows up here.
    for _label, _port, _path in (('bridge', p19_bport, '/tools/execute'),
                                 ('daemon', p19_dport, '/stop'),
                                 ('localmodels', p19_lport, '/repair')):
        p19_probe('phase19 %s: a non-ASCII token is REFUSED with a status line, '
                  'not a dropped connection' % _label,
                  lambda _p=_port, _pa=_path: req_non_ascii_token(
                      _p, _pa, {'name': 'read_file', 'arguments': {'path': 'hello.py'}}
                      if _pa == '/tools/execute' else {}) == 403)

    # ---------------- 12-13: the Host pin, on all three servers --------
    # The right token is sent on every one of these, so the ONLY thing that can
    # produce a 403 is the Host. A Host pin that also demanded a wrong token
    # would pass these cases for the wrong reason.
    p19_host_cases = (
        ('bridge', p19_bport, '/tools/execute',
         {'name': 'read_file', 'arguments': {'path': 'hello.py'}}, p19_btok),
        ('daemon', p19_dport, '/stop', {}, p19_dtok),
        ('localmodels', p19_lport, '/repair',
         {'suspect': P19_SUSPECT, 'candidates': P19_CANDIDATES}, p19_ltok),
    )
    for label, prt, path, payload, tok in p19_host_cases:
        p19_probe('phase19 %s: Host pin — privileged route with the RIGHT token and '
                  'Host: evil.example.com => 403' % label,
                  lambda prt=prt, path=path, payload=payload, tok=tok:
                  req(prt, path, payload, origin=P19_ORIGIN, token=tok,
                      host='evil.example.com')[0] == 403)
        p19_probe('phase19 %s: Host pin — privileged route with the RIGHT token and NO Host '
                  'header at all => 403 (a crafted request, never a browser)' % label,
                  lambda prt=prt, path=path, payload=payload, tok=tok:
                  req_absent_host(prt, path, payload=payload, token=tok,
                                  origin=P19_ORIGIN) == 403)
    # Positive half: the loopback forms the pin must ACCEPT, or the pin is just
    # a ban on everything.
    p19_probe('phase19 bridge: Host pin — Host: localhost:PORT with the right token is accepted',
              lambda: req(p19_bport, '/tools/execute',
                          {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                          origin=P19_ORIGIN, token=p19_btok,
                          host='localhost:%d' % p19_bport)[0] != 403)
    p19_probe('phase19 bridge: Host pin — Host: 127.0.0.1:PORT with the right token is accepted',
              lambda: req(p19_bport, '/tools/execute',
                          {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                          origin=P19_ORIGIN, token=p19_btok,
                          host='127.0.0.1:%d' % p19_bport)[0] != 403)
    p19_probe('phase19 daemon: Host pin — Host: localhost:PORT with the right token is accepted',
              lambda: req(p19_dport, '/stop', {}, origin=P19_ORIGIN, token=p19_dtok,
                          host='localhost:%d' % p19_dport)[0] != 403)
    p19_probe('phase19 localmodels: Host pin — Host: 127.0.0.1:PORT with the right token is accepted',
              lambda: req(p19_lport, '/repair',
                          {'suspect': P19_SUSPECT, 'candidates': P19_CANDIDATES},
                          origin=P19_ORIGIN, token=p19_ltok,
                          host='127.0.0.1:%d' % p19_lport)[0] != 403)

    # ---------------- 14: _cors stops lying on a refusal ---------------
    # The load-bearing half: a refusal must carry NO Access-Control-Allow-Origin.
    # A wildcard ACAO on a 403 is part of what CR-0017 calls out.
    def p19_cors_absent_on_refusal():
        s, j, h = req_cors(p19_bport, '/tools/execute',
                           {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                           origin=P19_ORIGIN)
        return s == 403 and 'access-control-allow-origin' not in h
    p19_probe('phase19 bridge: a 403 refusal carries NO Access-Control-Allow-Origin header at all',
              p19_cors_absent_on_refusal)
    p19_probe('phase19 daemon: a 403 refusal carries NO Access-Control-Allow-Origin header at all',
              lambda: (lambda r: r[0] == 403 and 'access-control-allow-origin' not in r[2])(
                  req_cors(p19_dport, '/set_workdir', {'path': p19_atk_wd}, origin=P19_ORIGIN)))
    p19_probe('phase19 localmodels: a 403 refusal carries NO Access-Control-Allow-Origin header at all',
              lambda: (lambda r: r[0] == 403 and 'access-control-allow-origin' not in r[2])(
                  req_cors(p19_lport, '/repair',
                           {'suspect': P19_SUSPECT, 'candidates': P19_CANDIDATES},
                           origin=P19_ORIGIN)))

    def p19_cors_reflects():
        s, j, h = req_cors(p19_bport, '/tools/execute',
                           {'name': 'read_file', 'arguments': {'path': 'hello.py'}},
                           origin=P19_ORIGIN, token=p19_btok)
        acao = h.get('access-control-allow-origin')
        return s == 200 and acao is not None and acao != '*' and acao == P19_ORIGIN
    p19_probe('phase19 bridge: an ALLOWED response reflects the specific Origin, never `*`',
              p19_cors_reflects)

    # The header the UI needs: without X-Cogitator-Token in Allow-Headers the
    # browser preflight fails and the product breaks while every other test
    # stays green (spec section 2 calls this load-bearing).
    def p19_preflight():
        s, j, h = req_cors(p19_bport, '/tools/execute', origin=P19_ORIGIN,
                           token=p19_btok, method='OPTIONS')
        acao = h.get('access-control-allow-origin')
        allow = (h.get('access-control-allow-headers') or '').lower()
        return (s == 204 and acao is not None and acao != '*' and acao == P19_ORIGIN
                and 'x-cogitator-token' in allow)
    p19_probe('phase19 bridge: OPTIONS preflight answers 204, reflects the Origin, and lists '
              'X-Cogitator-Token in Access-Control-Allow-Headers', p19_preflight)

    # ---------------- 15-17: daemon lifecycle -------------------------
    # CR-0016: never spawn a file the daemon did not write. The planted
    # bridge.py has an OBSERVABLE side effect (it writes PWNED into its own
    # cwd), so "the planted code never ran" is asserted as a FILE, not a
    # status code — a daemon that re-plants and then still spawns the tampered
    # copy would pass a message-only assertion.
    p19_tamper_wd = tempfile.mkdtemp(prefix='cogp19-tamper-')
    p19_src = open(os.path.join(p19_ddir, 'bridge.py'), encoding='utf-8').read()
    p19_expected = p19_expected_bridge(p19_src)
    p19_tampered = ('import os, time\n'
                    'open(os.path.join(os.getcwd(), "PWNED"), "w").write("x")\n'
                    'time.sleep(5)\n')

    def p19_setup_tamper():
        # Bind without autostart, so nothing is running when the file is planted.
        if req(p19_dport, '/set_workdir', {'path': p19_tamper_wd, 'autostart': False},
               origin=P19_ORIGIN, token=p19_dtok)[0] != 200:
            return False
        req(p19_dport, '/stop', {}, origin=P19_ORIGIN, token=p19_dtok)
        with open(os.path.join(p19_tamper_wd, 'bridge.py'), 'w', encoding='utf-8') as f:
            f.write(p19_tampered)
        return True

    p19_probe('phase19 daemon: staged a tampered bridge.py in the bound workdir', p19_setup_tamper)

    p19_start_result = {}

    def p19_do_start():
        p19_start_result['resp'] = req(p19_dport, '/start', {}, origin=P19_ORIGIN, token=p19_dtok)
        return True
    p19_probe('phase19 daemon: POST /start over a tampered bridge.py answered', p19_do_start)
    # Give a wrongly-spawned planted process time to drop its sentinel.
    time.sleep(1.2)

    def p19_cr0016_replanted():
        s, j = p19_start_result.get('resp', (0, {}))
        return s == 200 and 'plant' in json.dumps(j).lower()
    p19_probe('phase19 daemon [CR-0016]: /start over a tampered bridge.py says it re-planted',
              p19_cr0016_replanted)
    p19_probe('phase19 daemon [CR-0016]: the PLANTED code never ran (no PWNED sentinel in the '
              'workdir) — asserted as the file, not the status code',
              lambda: not os.path.exists(os.path.join(p19_tamper_wd, 'PWNED')))
    p19_probe('phase19 daemon [CR-0016]: the on-disk bridge.py is byte-equal to the daemon\'s own '
              'source after the re-plant',
              lambda: open(os.path.join(p19_tamper_wd, 'bridge.py'), encoding='utf-8').read()
                      == p19_expected)
    p19_probe('phase19 daemon [CR-0016]: the tampered file was BACKED UP, never destroyed',
              lambda: os.path.exists(os.path.join(p19_tamper_wd, 'bridge.py.cogitator-bak')))

    # CR-0024: the marker is LINE 1 — not "within the first 4096 bytes", which
    # is the weaker property the old code was checked against.
    def p19_marker_line_one():
        with open(os.path.join(p19_tamper_wd, 'bridge.py'), encoding='utf-8') as f:
            lines = f.read().splitlines()
        return bool(lines) and P19_BRIDGE_MARKER in lines[0]
    p19_probe('phase19 daemon [CR-0024]: BRIDGE_MARKER is on LINE 1 of the written bridge.py',
              p19_marker_line_one)

    # CR-0023: /set_workdir and /start must be mutually exclusive, or the
    # daemon ends up bound to one directory while the live worker serves
    # another. The assertion is the INVARIANT — the two sources agree — not
    # which of the two racing requests won.
    p19_race_wd_a = tempfile.mkdtemp(prefix='cogp19-raceA-')
    p19_race_wd_b = tempfile.mkdtemp(prefix='cogp19-raceB-')

    def p19_wait_port_free(bp, deadline=10.0):
        """Wait until nothing accepts on the daemon's bridge_port.

        The daemon's bridge_port is fixed (no CLI flag), so a retry issued while
        the previous attempt's worker is still dying races it for the same port:
        the new worker loses the bind, exits, and the attempt looks like a daemon
        defect when it is really a port collision created by the retry itself.
        """
        import socket as _s
        end = time.time() + deadline
        while time.time() < end:
            try:
                c = _s.create_connection(('127.0.0.1', bp), timeout=1)
                c.close()
                time.sleep(0.3)
            except OSError:
                return True
        return False

    def p19_do_race():
        # Two shapes, both required, because they exercise DIFFERENT things.
        #
        # 'spec' — bind A with autostart=True, then race /set_workdir(B) against
        #   /start, exactly as the spec describes. Measured on the UNLOCKED daemon
        #   this shape is mostly a NO-OP: a worker is already up, so /start
        #   short-circuits to 'already running' (400) and never touches STATE.
        #   It passes against broken code, so it cannot be the only shape.
        #
        # 'cold' — bind A with autostart=False and no worker running, so /start
        #   does REAL work and genuinely interleaves with set_workdir's
        #   stop/write/spawn. This is where CR-0023 bites: measured over 8 trials
        #   each, the unlocked daemon ends the cold race with NO live worker 6
        #   times out of 8, while the same daemon with _lock taken in
        #   start_bridge is 8 of 8 clean.
        #
        # Both shapes assert the SAME invariant (the two sources agree); they
        # differ only in how much work /start has to do.
        #
        # Retried up to 5 times per shape, as the spec requires, but the two
        # failure modes are NOT alike:
        #   running=True but not yet healthy -> the worker is still binding.
        #     Retryable; that is the flakiness the retry budget is for.
        #   running=False                     -> the daemon was told to serve a
        #     directory and nothing is serving it. Retrying cannot fix that.
        outcomes = []
        for shape in ('spec', 'cold'):
            verdict = 'no attempt was made'
            for attempt in range(5):
                req(p19_dport, '/stop', {}, origin=P19_ORIGIN, token=p19_dtok)
                bport_try = req(p19_dport, '/status', origin=P19_ORIGIN)[1].get('bridge_port') or 8931
                p19_wait_port_free(bport_try)
                autostart = (shape == 'spec')
                s, j = req(p19_dport, '/set_workdir', {'path': p19_race_wd_a, 'autostart': autostart},
                           origin=P19_ORIGIN, token=p19_dtok)
                if s != 200:
                    verdict = 'could not bind workdir A (answered %s: %r)' % (s, j)
                    print('   phase19 race[%s]: %s; retrying' % (shape, verdict))
                    time.sleep(1.0)
                    continue
                deadline = time.time() + 15
                st = {}
                while autostart and time.time() < deadline:
                    st = req(p19_dport, '/status', origin=P19_ORIGIN)[1]
                    if st.get('bridge_running') and st.get('bridge_healthy'):
                        break
                    time.sleep(0.3)

                results = {}
                barrier = threading.Barrier(2, timeout=30)

                def rebind():
                    barrier.wait()
                    results['set_workdir'] = req(p19_dport, '/set_workdir',
                                                 {'path': p19_race_wd_b, 'autostart': True},
                                                 origin=P19_ORIGIN, token=p19_dtok)
                def restart():
                    barrier.wait()
                    results['start'] = req(p19_dport, '/start', {}, origin=P19_ORIGIN, token=p19_dtok)

                threads = [threading.Thread(target=rebind), threading.Thread(target=restart)]
                for t in threads:
                    t.daemon = True
                    t.start()
                # ALWAYS join, with a bound: a wedge here must fail the suite, not hang it.
                for t in threads:
                    t.join(timeout=45)
                alive = [t for t in threads if t.is_alive()]
                if alive:
                    verdict = ('%d thread(s) still alive after 45s - a /set_workdir or /start '
                               'never returned' % len(alive))
                    print('   phase19 race[%s]: %s' % (shape, verdict))
                    return False
                print('   phase19 race[%s]: /set_workdir => %s, /start => %s'
                      % (shape, results.get('set_workdir', ('?',))[0],
                         results.get('start', ('?',))[0]))

                deadline = time.time() + 10
                st = {}
                while time.time() < deadline:
                    st = req(p19_dport, '/status', origin=P19_ORIGIN)[1]
                    if st.get('bridge_running') and st.get('bridge_healthy'):
                        break
                    if not st.get('bridge_running'):
                        break
                    time.sleep(0.4)

                bport = st.get('bridge_port')
                if not st.get('bridge_running'):
                    verdict = ('the daemon completed the race bound to %s with NO live worker '
                               '(bridge_running=False), although /set_workdir was asked to '
                               'autostart and /start also ran — the racing pair tore the '
                               'worker down after recording it'
                               % st.get('workdir'))
                    print('   phase19 race[%s]: %s' % (shape, verdict))
                    return False
                if not bport:
                    verdict = 'the daemon reports a running worker but no bridge_port'
                    print('   phase19 race[%s]: %s; retrying' % (shape, verdict))
                    time.sleep(1.0)
                    continue
                try:
                    hs, hj = req(bport, '/health', origin=P19_ORIGIN)
                except Exception as e:
                    verdict = ('the daemon claims a worker on %s but nothing answers /health '
                               'there (%s: %s)' % (bport, type(e).__name__, e))
                    print('   phase19 race[%s]: %s; retrying' % (shape, verdict))
                    time.sleep(1.0)
                    continue
                if hs != 200 or 'root' not in hj:
                    verdict = 'the worker on %s answered /health with %s' % (bport, hs)
                    print('   phase19 race[%s]: %s; retrying' % (shape, verdict))
                    time.sleep(1.0)
                    continue
                print('   phase19 race[%s]: the daemon says workdir %s, the LIVE worker on %s '
                      'says root %s' % (shape, st.get('workdir'), bport, hj.get('root')))
                if st.get('workdir') != hj.get('root'):
                    verdict = ('the daemon is bound to %s while the live worker on %s serves %s'
                               % (st.get('workdir'), bport, hj.get('root')))
                    print('   phase19 race[%s]: DISAGREEMENT - %s' % (shape, verdict))
                    return False
                outcomes.append('%s: agreed' % shape)
                break
            else:
                print('   phase19 race[%s]: gave up after 5 attempts; last outcome: %s'
                      % (shape, verdict))
                return False
        print('   phase19 race: %s' % ' | '.join(outcomes))
        return True
    p19_probe('phase19 daemon [CR-0023]: after a racing /set_workdir + /start, the daemon\'s '
              'workdir equals the LIVE worker\'s own root (the two sources agree)', p19_do_race)
finally:
    for _p in p19_servers:
        try:
            p19_stop(_p)
        except Exception:
            pass
    if p19_autostart_entry and os.path.exists(p19_autostart_entry):
        try:
            os.remove(p19_autostart_entry)
        except OSError:
            pass

# ---------------- PHASE 22 workstream B: sidecar ledger integrity ----------------
# CR-Nanites-harness-0022 (candidate shape + telemetry isolation), 0006 (action
# vocabulary + unbounded growth), 0025 (byte-bounded tail), 0004 (a docstring rule no
# code path provides).
#
# The sidecar is started ONCE for this phase, in its own temp dir, with stderr
# captured to a FILE: two of these cases assert that a traceback never reaches
# stderr, which DEVNULL cannot show.
P22_DIR = tempfile.mkdtemp(prefix='cogp22-')
P22_LEDGER = os.path.join(P22_DIR, 'local-models.jsonl')
P22_STDERR = os.path.join(P22_DIR, 'stderr.log')
P22_PORT = 18980

P22_SUSPECT = {'name': 'raed_file', 'arguments': '{"path": "main.py"}'}
# The three candidate shapes a caller may legitimately send. The FLAT one is the
# bug: `(c.get('function') or {}).get('name')` assumes function is a dict, and a
# plain string raises AttributeError in the ledger-record argument expression -
# i.e. in the HTTP handler, before append_record's own try/except ever runs.
P22_NESTED = {'type': 'function',
              'function': {'name': 'read_file', 'description': 'Read a file from disk.',
                           'parameters': {'type': 'object',
                                          'properties': {'path': {'type': 'string'}},
                                          'required': ['path']}}}
# The flattened shape, `function` already being the name. Carries read_file here so
# case C reads exactly the shape in the finding; case D builds its own copy carrying
# list_dir so the three shapes are checked against three DIFFERENT names.
P22_FLAT_FUNCTION = {'type': 'function', 'function': 'read_file'}
P22_FLAT_NAME = {'name': 'grep', 'description': 'Search a directory.',
                 'parameters': {'type': 'object',
                                'properties': {'pattern': {'type': 'string'}}}}


def p22_daemon(port, ledger_path, extra=(), env=None):
    """Start the sidecar with stderr captured to a file, in the phase-22 temp dir."""
    errf = open(P22_STDERR, 'ab')
    try:
        p = subprocess.Popen([sys.executable, os.path.join(BASE, 'localmodels',
                                                            'local_models_daemon.py'),
                              '--port', str(port), '--ledger', ledger_path] + list(extra),
                             stdout=subprocess.DEVNULL, stderr=errf, cwd=P22_DIR,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', **(env or {})))
    finally:
        errf.close()
    register_port_token(port, read_server_token(os.path.join(BASE, 'localmodels',
                                                             '.cogitator-token')))
    return p


def p22_stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def p22_ledger_lines(path=P22_LEDGER):
    if not os.path.isfile(path):
        return []
    return [json.loads(ln) for ln in open(path, encoding='utf-8') if ln.strip()]


def p22_raw_post(port, path, header_lines, body, timeout=10, half_close=False):
    """POST with HAND-WRITTEN header lines; return (status_code, body_text).

    urllib will not emit `Content-Length: -1` or `Content-Length: banana`, so the
    two malformed-length cases can only be written on a raw socket - the same
    technique `req_absent_host` and `req_non_ascii_token` already use above, copied
    rather than imported. The body comes back too because a status code alone
    cannot tell a Content-Length refusal from any other 400.

    `half_close=True` sends `shutdown(SHUT_WR)` after the payload, which is what a
    client that was cut off mid-body does. It is REQUIRED for the short-body case:
    the server's `self.rfile.read(n)` only returns fewer than `n` bytes when the
    peer half-closes, so without it a truncated request would just block until the
    timeout instead of reproducing anything.
    """
    lines = ['POST %s HTTP/1.1' % path,
             'Host: 127.0.0.1:%d' % port,
             'Origin: %s' % P19_ORIGIN,
             'X-Cogitator-Token: %s' % PORT_TOKENS[port],
             'Content-Type: application/json',
             'Connection: close'] + list(header_lines)
    wire = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + body
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        sock.sendall(wire)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
    finally:
        sock.close()
    raw = b''.join(chunks)
    head, _, rest = raw.partition(b'\r\n\r\n')
    first = head.split(b'\r\n', 1)[0].decode('latin-1')
    try:
        status = int(first.split()[1])
    except (IndexError, ValueError):
        return -1, raw.decode('latin-1', 'replace')   # no status line: the handler died
    return status, rest.decode('utf-8', 'replace')


def p22_stderr_text():
    return open(P22_STDERR, encoding='utf-8', errors='replace').read() \
        if os.path.isfile(P22_STDERR) else ''


p22_proc = p22_daemon(P22_PORT, P22_LEDGER, ['--no-needle', '--no-laya'])
time.sleep(1.0)
try:
    # ---- A: a negative Content-Length is REFUSED, not read ----
    # `if n > MAX_JSON_BYTES` does not reject n < 0, and `self.rfile.read(-1)`
    # reads until EOF: an unbounded read of whatever the client keeps sending, on
    # a route whose size cap is supposed to be the thing that stops it. The
    # timeout here is short on purpose - a hang IS the symptom.
    #
    # /repair is the route used for both length cases because it is the one that
    # answers 200 on an EMPTY body (degraded:true), so a dropped body is visible
    # as a success rather than hidden behind the route's own 400.
    def p22_negative_content_length():
        body = json.dumps({'suspect': P22_SUSPECT,
                           'candidates': [P22_NESTED]}).encode()
        t0 = time.time()
        st, _ = p22_raw_post(P22_PORT, '/repair', ['Content-Length: -1'], body, timeout=6)
        dt = time.time() - t0
        if st != 400:
            print('   Content-Length: -1 => %s after %.1fs (want 400; read(-1) reads to '
                  'EOF, so this HANGS while the client holds the socket open)' % (st, dt))
        return st == 400
    lm_probe('phase22 sidecar: a NEGATIVE Content-Length is refused with 400, not read '
             '(rfile.read(-1) would read to EOF)', p22_negative_content_length)

    # ---- B: a non-numeric Content-Length is REFUSED, not DROPPED ----
    # "Dropped" is load-bearing: the old code caught the ValueError, set n = 0 and
    # returned ({}, None), so /repair answered 200 {ok:false, degraded:true} for a
    # request whose body was never read - a success-shaped answer for a request
    # that did not arrive. The status alone is not enough to tell a length refusal
    # from any other 400, so the error text is asserted too.
    def p22_non_numeric_content_length():
        body = json.dumps({'suspect': P22_SUSPECT,
                           'candidates': [P22_NESTED]}).encode()
        st, resp = p22_raw_post(P22_PORT, '/repair', ['Content-Length: banana'], body)
        s, j = req(P22_PORT, '/repair', {'suspect': P22_SUSPECT, 'candidates': [P22_NESTED]})
        if st != 400 or 'content-length' not in resp.lower():
            print('   Content-Length: banana => %s %r (want 400 naming the Content-Length; '
                  'the old path answered 200 on an empty {} body, and the well-formed '
                  'control answered %s %r)' % (st, resp, s, j))
        return st == 400 and 'content-length' in resp.lower()
    lm_probe('phase22 sidecar: a NON-NUMERIC Content-Length is refused with 400 naming the '
             'length, not dropped into an empty {} body that /repair answers 200 on',
             p22_non_numeric_content_length)

    # The positive half, or "refuse everything" would pass both cases above: an
    # absent Content-Length and a well-formed one must both still be served.
    def p22_content_length_positive():
        before = len(p22_ledger_lines())
        s, j = req(P22_PORT, '/ledger', {'trace_id': 'tr_p22_ok', 'action': 'accepted'})
        after = p22_ledger_lines()
        return (s == 200 and j.get('ok') is True and len(after) == before + 1
                and after[-1].get('trace_id') == 'tr_p22_ok')
    lm_probe('phase22 sidecar: a well-formed POST /ledger still answers 200 and writes '
             'its line (the refusals above are not a blanket ban)', p22_content_length_positive)

    # ---- B2: a SHORT body under a VALID positive Content-Length is REFUSED ----
    # The third length case, and the one the suite was structurally unable to catch:
    # every length case above exercises a length value that is UNUSABLE (negative,
    # non-numeric), never one that is perfectly valid but whose bytes never arrive.
    # `_read_body` mapped `not raw` - a read of n > 0 bytes that came back short, and
    # a read that RAISED - onto the same value as a legitimately empty body, ({}, None),
    # so /repair proceeded and answered 200 {"ok": false, "degraded": true, ...} AND
    # wrote a full op:'repair' ledger record for a request whose body was never read.
    # That is precisely the defect this phase exists to close, so the status alone is
    # not enough: the LEDGER FILE is asserted too, because a refusal that still writes
    # an audit record is not a refusal.
    #
    # `half_close=True` is what makes this reproducible: `self.rfile.read(n)` only
    # returns fewer than n bytes once the peer shuts down its write side, which is
    # what a client cut off mid-flight does. /repair is the route on purpose - it is
    # the one that answers 200 on an empty body, so a dropped body is visible as a
    # SUCCESS rather than hidden behind the route's own 400.
    def p22_short_body_refused():
        full = json.dumps({'suspect': P22_SUSPECT,
                           'candidates': [P22_NESTED]}).encode()
        # Three shapes of "the bytes never arrived", because they fail differently:
        # nothing at all, a complete JSON payload whose declared length was larger,
        # and a payload cut in the middle of an object.
        shapes = (('Content-Length: 107, zero body bytes sent', 107, b''),
                  ('declared %d, sent %d (a COMPLETE JSON body)' % (len(full) + 50, len(full)),
                   len(full) + 50, full),
                  ('declared 107, sent %d (cut mid-object)' % min(20, len(full)),
                   107, full[:20]))
        problems = []
        for label, declared, sent in shapes:
            before = p22_ledger_lines()
            st, resp = p22_raw_post(P22_PORT, '/repair',
                                    ['Content-Length: %d' % declared], sent,
                                    timeout=6, half_close=True)
            grew = p22_ledger_lines()[len(before):]
            if st == 200:
                problems.append('%s => 200 %s (a success-shaped answer for a body that '
                                'never arrived)' % (label, resp[:100]))
            if grew:
                problems.append('%s => %d ledger record(s) written anyway: %r'
                                % (label, len(grew), grew[:1]))
            # A framing failure and a malformed payload are different diagnoses, and
            # "invalid JSON body" is the wrong one here: the JSON may be perfectly
            # well formed and merely INCOMPLETE, so say so.
            if 'truncat' not in resp.lower():
                problems.append('%s => %s %r (want a 400 naming the truncated body, so '
                                'the caller can tell framing from payload)'
                                % (label, st, resp[:100]))
        if problems:
            print('   ' + '\n   '.join(problems))
            return False
        return True
    lm_probe('phase22 sidecar: a body SHORTER than a valid positive Content-Length is a '
             '400, not a success - for zero bytes, a complete-but-short body, and one '
             'cut mid-object, and NONE of them writes a ledger record',
             p22_short_body_refused)

    # ---- B3: the control - a length that MATCHES the bytes is still served ----
    # Without this, a `_read_body` that refuses every positive length passes B2 and
    # the whole phase-22 block above. Same raw socket, same route, same hand-written
    # header: only the arithmetic differs.
    def p22_matching_length_still_served():
        payload = json.dumps({'suspect': P22_SUSPECT,
                              'candidates': [P22_NESTED]}).encode()
        before = p22_ledger_lines()
        st, resp = p22_raw_post(P22_PORT, '/repair',
                                ['Content-Length: %d' % len(payload)], payload, timeout=6)
        after = p22_ledger_lines()
        grew = after[len(before):]
        problems = []
        if st != 200:
            problems.append('a body whose declared length MATCHES got %s %r (want 200)'
                            % (st, resp[:120]))
        if len(grew) != 1:
            problems.append('...and wrote %d ledger record(s), want 1: %r'
                            % (len(grew), grew[:1]))
        elif grew[0].get('op') != 'repair':
            problems.append('...and wrote the wrong record: %r' % grew[0])
        if problems:
            print('   ' + '; '.join(problems))
            return False
        return True
    lm_probe('phase22 sidecar: the CONTROL - a raw-socket POST whose declared length '
             'matches the bytes sent is still served with 200 and its ledger record '
             '(the short-body refusal is not a blanket ban)', p22_matching_length_still_served)

    # ---- C: a FLAT candidate shape gets a CLEAN response, not a 500 ----
    # CR-0022. `(c.get('function') or {}).get('name')` assumes `function` is a dict.
    # The flattened OpenAI-ish {"function": "read_file"} raises AttributeError ON THE
    # STRING - and it raises in the ARGUMENT EXPRESSION of append_record, which is
    # evaluated BEFORE the call, so append_record's own blanket except never sees it
    # and the exception escapes into the HTTP handler.
    def p22_flat_function_candidate_clean():
        before_err = p22_stderr_text()
        s, j = req(P22_PORT, '/repair', {'suspect': P22_SUSPECT,
                                         'candidates': [P22_FLAT_FUNCTION],
                                         'trace_id': 'tr_p22_flat'})
        new_err = p22_stderr_text()[len(before_err):]
        if s != 200:
            print('   flat candidate POST /repair => %s %r' % (s, j))
            print('   stderr: %s' % new_err.strip()[-400:])
            return False
        # "Clean" means three things: the same envelope shape a nested candidate
        # gets, no traceback in stderr, and a ledger record that carries the name.
        envelope = (j.get('ok') is False and j.get('degraded') is True
                    and j.get('reason') == 'disabled' and j.get('calls') == []
                    and isinstance(j.get('latency_ms'), int)
                    and j.get('trace_id') == 'tr_p22_flat'
                    and 'confidence' in j and 'reasoning' in j)
        rec = [r for r in p22_ledger_lines()
               if r.get('trace_id') == 'tr_p22_flat' and r.get('op') == 'repair']
        if 'Traceback' in new_err:
            print('   flat candidate wrote a traceback to stderr: %s'
                  % new_err.strip()[-400:])
            return False
        if not envelope:
            print('   flat candidate envelope wrong: %r' % j)
            return False
        if len(rec) != 1:
            print('   flat candidate wrote %d ledger records, want 1: %r' % (len(rec), rec))
            return False
        if rec[0].get('request', {}).get('candidates') != ['read_file']:
            print('   flat candidate name was not extracted: %r'
                  % rec[0].get('request'))
            return False
        return True
    lm_probe('phase22 sidecar [CR-0022]: a candidate {"function": "read_file"} gets a CLEAN '
             '200 - no 500, no traceback, and its name still reaches the ledger',
             p22_flat_function_candidate_clean)

    # ---- D: the POSITIVE direction of the extractor ----
    # Without this, a helper that returned None for EVERY shape passes case C. Both
    # shapes a real caller sends must still yield their name.
    def p22_nested_and_flat_name_candidates():
        # The expected name is paired with the shape that CARRIES it - the nested
        # schema is read_file, the flat {"name":...} is grep, and the flattened
        # {"function": "<name>"} is list_dir, so each shape is checked against a
        # different name and a helper that returned the first candidate's name for
        # all three cannot pass.
        shapes = ((P22_NESTED, 'read_file'), (P22_FLAT_NAME, 'grep'),
                  ({'type': 'function', 'function': 'list_dir'}, 'list_dir'))
        out = {}
        for i, (cand, want) in enumerate(shapes):
            tid = 'tr_p22_shape%d' % i
            s, j = req(P22_PORT, '/repair', {'suspect': P22_SUSPECT,
                                             'candidates': [cand], 'trace_id': tid})
            rec = [r for r in p22_ledger_lines()
                   if r.get('trace_id') == tid and r.get('op') == 'repair']
            out[want] = (s, rec[0].get('request', {}).get('candidates')
                         if len(rec) == 1 else None)
        bad = {k: v for k, v in out.items() if v != (200, [k])}
        if bad:
            print('   candidate shapes that did not yield their name: %r (all: %r)' % (bad, out))
        return not bad
    lm_probe('phase22 sidecar [CR-0022]: the nested {"function":{"name":...}}, the flat '
             '{"name":...} and the flat {"function":"..."} shapes ALL still extract their '
             'name', p22_nested_and_flat_name_candidates)

    # ---- E: an unusable candidate shape is None, never an exception ----
    # The helper's contract in isolation: whatever nonsense arrives, it returns
    # None rather than raising, because the caller builds a ledger record from it.
    def p22_candidate_extractor_never_raises():
        sys.path.insert(0, os.path.join(BASE, 'localmodels'))
        import local_models_daemon as lmd22
        if not hasattr(lmd22, '_candidate_name'):
            print('   local_models_daemon has no _candidate_name helper')
            return False
        cases = [({'function': 42}, None), ({'function': ['read_file']}, None),
                 ({'function': None, 'name': 'grep'}, 'grep'),
                 ({'function': {'name': ''}}, None), ({'function': {}}, None),
                 ({}, None), ({'name': 'write_file'}, 'write_file'),
                 ({'function': {'name': 'nested'}}, 'nested'),
                 ({'function': 'flat'}, 'flat')]
        bad = []
        for cand, want in cases:
            try:
                got = lmd22._candidate_name(cand)
            except Exception as e:
                bad.append('%r raised %s: %s' % (cand, type(e).__name__, e))
                continue
            if got != want:
                bad.append('%r => %r, want %r' % (cand, got, want))
        if bad:
            print('   ' + '; '.join(bad))
        return not bad
    lm_probe('phase22 sidecar [CR-0022]: _candidate_name returns None for every unusable '
             'candidate shape instead of raising (the value the ledger record is built from)',
             p22_candidate_extractor_never_raises)

    # ---- F: ONE extractor, used at EVERY call site ----
    # A helper that exists and is not called is a dormant guard: the code keeps
    # raising and every helper-level test still passes.
    def p22_every_extraction_site_uses_the_helper():
        # Tokenise rather than grep the raw text: the helper's own docstring QUOTES
        # the old expression to explain why it exists, and a text grep would report
        # the fix as unfinished forever. `tokenize` sees CODE - it drops comments and
        # every string literal, including docstrings - so what is left is exactly the
        # expressions that actually run. There is no raw-text fallback, because any
        # such fallback would match the prose this comment is talking about.
        import tokenize
        path = os.path.join(BASE, 'localmodels', 'local_models_daemon.py')
        offenders = []
        with open(path, 'rb') as f:
            for tok in tokenize.tokenize(f.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                if "get('function')" in tok.string and '_candidate_name' not in tok.string:
                    offenders.append((tok.start[0], tok.string))
        if offenders:
            print('   candidate-extraction sites still inline the shape assumption: %r'
                  % offenders)
        return not offenders
    lm_probe('phase22 sidecar [CR-0022]: no candidate-extraction call site inlines '
             "get('function') any more - one helper, used everywhere", p22_every_extraction_site_uses_the_helper)

    # ---- G: an action OUTSIDE the vocabulary is refused AND not written ----
    # CR-0006. The old gate was `isinstance(action, str) and action`, so "accepted "
    # (a trailing space) and "ACCEPTED" both landed in the ledger. `_ledger_counts`
    # ignores them, but tools/tune_thresholds.py has its OWN copy of the vocabulary
    # and this is the acceptance rate Phase 15's tuning rests on.
    #
    # Both the status AND the file are asserted: a refusal that still writes is not a
    # refusal, and that is exactly the failure mode a status-only check cannot see.
    P22_BAD_ACTIONS = ('accepted ', 'ACCEPTED', 'Accept', 'nope', 'accepted_by_opeRator',
                       'accepted\n', ' ignore')

    def p22_out_of_vocabulary_refused():
        before = p22_ledger_lines()
        seen = {}
        for i, act in enumerate(P22_BAD_ACTIONS):
            s, j = req(P22_PORT, '/ledger', {'trace_id': 'tr_p22_bad%d' % i, 'action': act})
            seen[act] = (s, j)
        after = p22_ledger_lines()
        wrote = [r for r in after[len(before):]]
        bad = {k: v for k, v in seen.items() if v[0] != 400}
        if bad or wrote:
            print('   out-of-vocabulary actions: %r' % bad)
            print('   ledger grew by %d line(s) despite the refusals: %r'
                  % (len(wrote), wrote))
            return False
        # The refusal must be a vocabulary refusal, not some unrelated 400.
        s, j = req(P22_PORT, '/ledger', {'trace_id': 'tr_p22_badv', 'action': 'ACCEPTED'})
        return s == 400 and 'action' in str(j).lower() and j.get('ok') is False
    lm_probe('phase22 sidecar [CR-0006]: every action outside _LEDGER_ACTIONS is refused '
             'with 400 AND writes nothing to the ledger file', p22_out_of_vocabulary_refused)

    # ---- H: the positive direction - every key in the vocabulary still works ----
    # "Refuse everything" passes case G. Each real action word must still be accepted,
    # written, and counted - including both accepted aliases, which fold to one bucket.
    def p22_every_known_action_accepted():
        sys.path.insert(0, os.path.join(BASE, 'localmodels'))
        import local_models_daemon as lmd22
        bad = []
        before = p22_ledger_lines()
        # The /health counters are CUMULATIVE over the whole file, and earlier cases
        # in this phase have already written lines - so the assertion is a DELTA.
        base = dict(req(P22_PORT, '/health')[1]['ledger']['counts'])
        for i, act in enumerate(sorted(lmd22._LEDGER_ACTIONS)):
            tid = 'tr_p22_known_%d' % i
            s, j = req(P22_PORT, '/ledger', {'trace_id': tid, 'action': act})
            rec = [r for r in p22_ledger_lines()
                   if r.get('trace_id') == tid and r.get('op') is None]
            if s != 200 or j.get('ok') is not True or len(rec) != 1 or rec[0].get('action') != act:
                bad.append('%r => %s %r, %d record(s)' % (act, s, j, len(rec)))
        counts = req(P22_PORT, '/health')[1]['ledger']['counts']
        grew = p22_ledger_lines()[len(before):]
        # By hand: the two accepted aliases fold to ONE 'accepted' counter, so the
        # delta must be 2 for accepted and 1 for each of the other three keys.
        want_delta = {}
        for key, val in lmd22._LEDGER_ACTIONS.items():
            want_delta[val] = want_delta.get(val, 0) + 1
        delta = {k: counts.get(k, 0) - base.get(k, 0) for k in set(counts) | set(base)}
        delta = {k: v for k, v in delta.items() if v}
        if delta != want_delta:
            bad.append('health counter delta %r, want %r' % (delta, want_delta))
        if len(grew) != len(lmd22._LEDGER_ACTIONS):
            bad.append('%d lines written for %d valid actions'
                       % (len(grew), len(lmd22._LEDGER_ACTIONS)))
        if bad:
            print('   ' + '; '.join(bad))
        return not bad
    lm_probe('phase22 sidecar [CR-0006]: every action in the vocabulary is still accepted, '
             'written, and counted by /health (the refusal is not a blanket ban)',
             p22_every_known_action_accepted)

    # ---- I: the daemon and tools/tune_thresholds.py agree on the vocabulary ----
    # The CR-0006 assertion itself. Asserted in BOTH directions and on the FOLD map,
    # because a reader that silently disagrees with the daemon's own /health counters
    # is worse than useless for threshold tuning. `action` is the whole finding: the
    # daemon now REFUSES anything outside its keys, so if this set were ever smaller
    # than the tool's, the daemon would reject a word the tool counts, and if it were
    # larger, the daemon would accept a word the tool ignores.
    def p22_vocabularies_agree():
        sys.path.insert(0, os.path.join(BASE, 'localmodels'))
        sys.path.insert(0, os.path.join(BASE, 'tools'))
        import local_models_daemon as lmd22
        import tune_thresholds as tt22
        daemon_keys = set(lmd22._LEDGER_ACTIONS)
        tool_keys = set(tt22.KNOWN_ACTIONS)
        problems = []
        if daemon_keys != tool_keys:
            problems.append('daemon-only=%r tool-only=%r'
                            % (sorted(daemon_keys - tool_keys), sorted(tool_keys - daemon_keys)))
        if lmd22._LEDGER_ACTIONS != tt22.ACTION_FOLDER:
            problems.append('fold maps differ: daemon=%r tool=%r'
                            % (lmd22._LEDGER_ACTIONS, tt22.ACTION_FOLDER))
        accepted = set(tt22.ACCEPTED_ACTIONS)
        if {k for k, v in lmd22._LEDGER_ACTIONS.items() if v == 'accepted'} != accepted:
            problems.append('the daemon folds %r into "accepted" but the tool counts %r'
                            % (sorted(k for k, v in lmd22._LEDGER_ACTIONS.items()
                                      if v == 'accepted'), sorted(accepted)))
        # The direction that actually matters, asserted by behaviour and not by
        # set arithmetic: anything the daemon ACCEPTS is a word the tool folds in.
        for act in sorted(daemon_keys):
            if tt22.ACTION_FOLDER.get(act) != lmd22._LEDGER_ACTIONS[act]:
                problems.append('%r folds to %r in the tool but %r in the daemon'
                                % (act, tt22.ACTION_FOLDER.get(act), lmd22._LEDGER_ACTIONS[act]))
        if problems:
            print('   ' + '; '.join(problems))
        return not problems
    lm_probe('phase22 sidecar [CR-0006]: _LEDGER_ACTIONS and tools/tune_thresholds.py '
             'KNOWN_ACTIONS are the SAME set, fold the same way, and agree on which '
             'actions count as accepted', p22_vocabularies_agree)

    # ---- J: an injected unknown action word does NOT move the measured rate ----
    # The end-to-end claim behind CR-0006. The tool is driven over a ledger written
    # BY HAND - a proposal plus a mix of real actions plus the injected typos - so the
    # daemon's 400 is not what is being tested here; the measurement is. The assertion
    # is the measured number, not the exit code.
    def p22_injected_action_not_in_rate():
        led = os.path.join(P22_DIR, 'injected.jsonl')
        rows = [tune_line('repair', 'tr_i1', ['read_file'], tune_call('read_file'), 0.9, 10),
                tune_line('repair', 'tr_i2', ['read_file'], tune_call('read_file'), 0.9, 10),
                tune_line('repair', 'tr_i3', ['read_file'], tune_call('read_file'), 0.9, 10),
                # Two real outcomes and three injected words that are NOT in the
                # vocabulary: a trailing space, a case variant, and a near-miss alias.
                tune_outcome('tr_i1', 'accepted'),
                tune_outcome('tr_i2', 'rejected'),
                tune_outcome('tr_i3', 'accepted '),          # trailing space
                tune_outcome('tr_i1', 'ACCEPTED'),           # case variant
                tune_outcome('tr_i2', 'accepted_by_opeRator')]  # near-miss alias
        with open(led, 'w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row) + '\n')
        r, j = tune_json(led, cwd=BASE)
        if j is None:
            print('   tune_thresholds produced no JSON: rc=%s stderr=%r'
                  % (r.returncode, r.stderr[-300:]))
            return False
        rep = (j.get('repair') or {})
        acc = rep.get('acceptance') or {}
        # By hand: 3 op records, all three carry an outcome line, exactly ONE of them
        # carries a REAL accepted word. So 1/3, and the three injected words must
        # appear in no counter at all.
        if acc.get('accepted') != 1 or acc.get('denominator') != 3 or acc.get('rate') != round(1/3, 6):
            print('   acceptance measured as %r, want accepted=1 denominator=3 rate=%r'
                  % (acc, round(1 / 3, 6)))
            return False
        outcomes = rep.get('outcomes') or {}
        for word in ('accepted ', 'ACCEPTED', 'accepted_by_opeRator'):
            if word in outcomes:
                print('   the injected word %r landed in the measured outcomes: %r'
                      % (word, outcomes))
                return False
        if set(outcomes) != {'accepted', 'rejected'}:
            print('   measured outcomes are %r, want exactly accepted + rejected' % (outcomes,))
            return False
        if (rep.get('outcomes_folded') or {}) != {'accepted': 1, 'rejected': 1}:
            print('   folded outcomes are %r, want accepted=1 rejected=1'
                  % (rep.get('outcomes_folded'),))
            return False
        return True
    lm_probe('phase22 [CR-0006]: an injected unknown action word does NOT land in the '
             'measured acceptance rate (1/3 here, and no injected word in any counter)',
             p22_injected_action_not_in_rate)

    # ---- K: the daemon REFUSES the words the tool would ignore ----
    # Ties the two halves together: the same three injected words, posted to the
    # live daemon, must each be a 400 and add no line.
    def p22_daemon_refuses_the_injected_words():
        before = len(p22_ledger_lines())
        for i, word in enumerate(('accepted ', 'ACCEPTED', 'accepted_by_opeRator')):
            s, j = req(P22_PORT, '/ledger', {'trace_id': 'tr_p22_inj%d' % i, 'action': word})
            if s != 400:
                print('   the daemon ACCEPTED the injected word %r => %s %r' % (word, s, j))
                return False
        return len(p22_ledger_lines()) == before
    lm_probe('phase22 sidecar [CR-0006]: the daemon itself refuses the exact words the '
             'tuning tool ignores, and writes nothing for them', p22_daemon_refuses_the_injected_words)
finally:
    p22_stop(p22_proc)

# ---- PHASE 22, the pure-helper half: rotation and the byte-bounded tail ----
# These need no live daemon: they are about `ledger.append_record` and
# `_ledger_tail`, both pure with respect to the filesystem. Asserting them directly
# is what makes "the bound holds" a fact about the code rather than about one
# request's timing.
sys.path.insert(0, os.path.join(BASE, 'localmodels'))
import local_models_daemon as P22_LMD   # noqa: E402
import ledger as P22_LEDGER             # noqa: E402

# ---- L: size-based rotation keeps the file bounded and the NEWEST content ----
# CR-0006. `append_record` appended forever and nothing rotated. The direction that
# matters: rotating must DISCARD THE OLDEST and keep the most recent, because a
# rotation that keeps the oldest data and throws the new away is worse than no
# rotation at all - it silently destroys the records the operator just made.
P22_ROT_DIR = tempfile.mkdtemp(prefix='cogp22-rot-')
P22_ROT_LIMIT = 64 * 1024


def p22_rotation_bounded():
    path = os.path.join(P22_ROT_DIR, 'rot.jsonl')
    fat = 'x' * 4096
    # Append well past the limit, tagging every line with its ordinal so "which data
    # survived" is a fact and not an inference from file size.
    for i in range(60):
        if not P22_LEDGER.append_record(
                {'trace_id': 'tr_rot_%03d' % i, 'op': 'select', 'blob': fat},
                path=path, max_bytes=P22_ROT_LIMIT):
            print('   append %d returned False' % i)
            return False
    size = os.path.getsize(path)
    recs = P22_LEDGER.read_records(path) if hasattr(P22_LEDGER, 'read_records') else None
    lines = [json.loads(ln) for ln in open(path, encoding='utf-8') if ln.strip()]
    tids = [r.get('trace_id') for r in lines]
    problems = []
    if size > P22_ROT_LIMIT:
        problems.append('the file is %d bytes after rotation, limit is %d' % (size, P22_ROT_LIMIT))
    if tids != sorted(tids):
        problems.append('the surviving lines are not in append order')
    # The MOST RECENT record must be there; the very first must be gone.
    if 'tr_rot_059' not in tids:
        problems.append('the newest record tr_rot_059 was rotated away (%r ... %r)'
                        % (tids[:2], tids[-2:]))
    if 'tr_rot_000' in tids:
        problems.append('the oldest record tr_rot_000 survived; rotation kept the wrong end')
    if len(tids) >= 60:
        problems.append('nothing was rotated at all (%d lines retained)' % len(tids))
    if problems:
        print('   ' + '; '.join(problems))
        return False
    return True


def p22_rotation_keeps_append_working():
    """After a rotation the ledger is still a valid append target and /health's size
    report tracks the file. A rotation that leaves a truncated or unreadable file
    would pass the bound check and break every write after it."""
    path = os.path.join(P22_ROT_DIR, 'rot2.jsonl')
    for i in range(40):
        P22_LEDGER.append_record({'trace_id': 'tr_r2_%02d' % i, 'blob': 'y' * 4096},
                                 path=path, max_bytes=32 * 1024)
    ok = P22_LEDGER.append_record({'trace_id': 'tr_r2_final'}, path=path,
                                  max_bytes=32 * 1024)
    lines = [json.loads(ln) for ln in open(path, encoding='utf-8') if ln.strip()]
    if not ok or not lines or lines[-1].get('trace_id') != 'tr_r2_final':
        print('   after rotation: append ok=%r, last line=%r'
              % (ok, lines[-1] if lines else None))
        return False
    return os.path.getsize(path) <= 32 * 1024


def p22_rotation_no_stray_files():
    # The existing phase-10 case `lm_no_stray_files` allows exactly
    # {local-models.jsonl, local-models-2.jsonl} in the daemon's own directory, so
    # rotation must NOT create a third filename. This asserts that directly on the
    # rotation directory, which is otherwise the only place a rotation artefact could
    # show up unnoticed.
    #
    # It runs its OWN rotation first rather than depending on the cases above having
    # already created files - a case that passes because the directory happens to be
    # empty has proved nothing.
    path = os.path.join(P22_ROT_DIR, 'stray.jsonl')
    for i in range(40):
        P22_LEDGER.append_record({'trace_id': 'tr_st_%02d' % i, 'blob': 'z' * 4096},
                                 path=path, max_bytes=32 * 1024)
    if not os.path.isfile(path):
        print('   the stray-file probe never wrote %s' % path)
        return False
    files = set(os.listdir(P22_ROT_DIR))
    allowed = {'rot.jsonl', 'rot2.jsonl', 'stray.jsonl', 'fat.jsonl', 'normal.jsonl'}
    extra = files - allowed
    if extra:
        print('   rotation left extra files behind: %r (allowed: %r)'
              % (sorted(extra), sorted(allowed)))
        return False
    return True


def p22_health_reports_size():
    """`/health`'s ledger block must surface the size, so an operator can see a ledger
    approaching its bound without reading the file. Asserted on the pure helper the
    daemon calls, and on the live shape."""
    block = {'path': 'var/local-models.jsonl', 'writable': True,
             'bytes': P22_LMD.ledger_size(os.path.join(P22_ROT_DIR, 'rot.jsonl')),
             'max_bytes': P22_LEDGER.MAX_BYTES, 'counts': {}}
    size = block['bytes']
    if size != os.path.getsize(os.path.join(P22_ROT_DIR, 'rot.jsonl')):
        print('   ledger_size reported %r, the file is %d'
              % (size, os.path.getsize(os.path.join(P22_ROT_DIR, 'rot.jsonl'))))
        return False
    if not isinstance(size, int) or size <= 0:
        print('   ledger_size must be a positive int, got %r' % (size,))
        return False
    return 'bytes' in block and 'max_bytes' in block


lm_probe('phase22 ledger [CR-0006]: size rotation bounds the file AND keeps the MOST '
         'RECENT records (the oldest are the ones dropped)', p22_rotation_bounded)
lm_probe('phase22 ledger [CR-0006]: after a rotation the ledger is still appendable and '
         'its last line is the newest record', p22_rotation_keeps_append_working)
lm_probe('phase22 ledger [CR-0006]: rotation introduces NO third filename (the phase-10 '
         'two-file set must keep holding)', p22_rotation_no_stray_files)
lm_probe('phase22 ledger [CR-0006]: the ledger size is surfaced for /health, as a real '
         'byte count of the active file', p22_health_reports_size)

# ---- M: a ~10-fat-record ledger does not defeat the /health read bound ----
# CR-0025. `_ledger_tail` doubled its 64 KiB window without limit until it held 2000
# lines, so 10 fat records (100 KB each) walked the window up over a megabyte.
# The bound is asserted two ways, because either alone is weak: the returned tail
# must be no bigger than the ceiling, AND the read must TERMINATE (measured, not
# assumed - the old loop was unbounded, so a test that only checked the answer would
# have passed against an implementation that spent ten seconds getting there).
P22_FAT_RECORDS = 10
P22_FAT_BYTES = 100 * 1024
P22_TAIL_TIME_BUDGET_S = 3.0


def p22_fat_ledger_tail_bounded():
    path = os.path.join(P22_ROT_DIR, 'fat.jsonl')
    with open(path, 'w', encoding='utf-8') as f:
        for i in range(P22_FAT_RECORDS):
            f.write(json.dumps({'op': 'select', 'trace_id': 'fat_%02d' % i,
                                'action': None, 'note': 'z' * P22_FAT_BYTES}) + '\n')
    total = os.path.getsize(path)
    ceiling = P22_LMD.LEDGER_TAIL_MAX_BYTES
    if not isinstance(ceiling, int) or ceiling <= 0:
        print('   LEDGER_TAIL_MAX_BYTES is not a positive int: %r' % (ceiling,))
        return False
    t0 = time.time()
    tail = P22_LMD._ledger_tail(path)
    dt = time.time() - t0
    read_bytes = sum(len(ln.encode('utf-8')) + 1 for ln in tail)
    problems = []
    if dt > P22_TAIL_TIME_BUDGET_S:
        problems.append('_ledger_tail took %.2fs (budget %.1fs)' % (dt, P22_TAIL_TIME_BUDGET_S))
    # The ceiling, plus the one partial line the window necessarily starts mid-way on.
    if read_bytes > ceiling + P22_FAT_BYTES:
        problems.append('read %d bytes, ceiling is %d (+one %d-byte line tolerance)'
                        % (read_bytes, ceiling, P22_FAT_BYTES))
    if total <= ceiling:
        problems.append('the fixture is only %d bytes - it never exceeded the %d ceiling, '
                        'so this case proved nothing' % (total, ceiling))
    # It must be the MOST RECENT lines and it must not be empty.
    if not tail:
        problems.append('_ledger_tail returned [] on a %d-byte ledger' % total)
    elif 'fat_%02d' % (P22_FAT_RECORDS - 1) not in tail[-1]:
        problems.append('the newest line is not last: %r' % tail[-1][:80])
    if problems:
        print('   ' + '; '.join(problems))
        print('   (ledger %d bytes, read %d, %d lines, %.3fs)'
              % (total, read_bytes, len(tail), dt))
        return False
    return True


def p22_fat_ledger_counts_terminate():
    """The same bound through `_ledger_counts`, which is what /health actually calls,
    plus the counts still being sane rather than all-zero."""
    path = os.path.join(P22_ROT_DIR, 'fat.jsonl')
    t0 = time.time()
    counts = P22_LMD._ledger_counts(path)
    dt = time.time() - t0
    if dt > P22_TAIL_TIME_BUDGET_S:
        print('   _ledger_counts took %.2fs on a fat ledger (budget %.1fs)'
              % (dt, P22_TAIL_TIME_BUDGET_S))
        return False
    # 10 records, all op:'select' -> proposals. The byte ceiling may cut the window,
    # so only "some real number, not a fabrication" is asserted here; the tail case
    # above pins the exact bound.
    if not isinstance(counts.get('proposals'), int) or counts['proposals'] <= 0:
        print('   _ledger_counts on a 10-record ledger gave %r' % (counts,))
        return False
    return True


def p22_tail_bound_does_not_under_report():
    """The byte ceiling must not silently under-report a NORMAL ledger.

    This is the regression the original doubling loop was written to avoid (a fixed
    64 KiB window held only 1489 of 2000 lines). A byte ceiling that broke the
    phase-14 guarantee would make the ceiling a second version of the same bug, so
    the existing 2000-line property is re-asserted against the bounded helper."""
    path = os.path.join(P22_ROT_DIR, 'normal.jsonl')
    with open(path, 'w', encoding='utf-8') as f:
        for i in range(2500):
            f.write(json.dumps({'op': 'select', 'trace_id': 'p%d' % i}) + '\n')
        for i in range(2500):
            f.write(json.dumps({'action': 'accepted', 'trace_id': 'a%d' % i,
                                'note': 'x' * 60}) + '\n')
    size = os.path.getsize(path)
    ceiling = P22_LMD.LEDGER_TAIL_MAX_BYTES
    t0 = time.time()
    counts = P22_LMD._ledger_counts(path)
    dt = time.time() - t0
    # This fixture is deliberately UNDER the ceiling: the point is that an ordinary
    # ledger is served entirely by the LINE bound, untouched by the byte backstop. So
    # the assertion is the phase-14 answer, not that the ceiling was reached - a guard
    # demanding this fixture exceed the ceiling would be asserting the opposite of what
    # the case is for.
    if not (counts.get('accepted') == 2000 and counts.get('proposals') == 0):
        print('   bounded tail changed the phase-14 answer: %r (%.2fs, %d-byte file, '
              'ceiling %d)' % (counts, dt, size, ceiling))
        return False
    if size > ceiling:
        print('   the normal fixture (%d bytes) is now OVER the %d ceiling, so this case '
              'no longer proves the ordinary path is unaffected' % (size, ceiling))
        return False
    return dt <= P22_TAIL_TIME_BUDGET_S


def p22_tail_bound_documented():
    """The ceiling must be a NAMED, DOCUMENTED constant and the behaviour at the
    ceiling must be stated - not left for a reader to infer from the code."""
    if not hasattr(P22_LMD, 'LEDGER_TAIL_MAX_BYTES'):
        print('   no LEDGER_TAIL_MAX_BYTES constant')
        return False
    src = inspect.getsource(P22_LMD._ledger_tail)
    problems = []
    if 'LEDGER_TAIL_MAX_BYTES' not in src:
        problems.append('_ledger_tail does not mention the ceiling by name')
    low = src.lower()
    if 'most recent' not in low:
        problems.append('_ledger_tail does not document that it returns the MOST RECENT '
                        'lines when the ceiling is hit first')
    if 'truncat' not in low and 'ceil' not in low:
        problems.append('_ledger_tail does not document what happens at the ceiling')
    if problems:
        print('   ' + '; '.join(problems))
        return False
    return True


lm_probe('phase22 sidecar [CR-0025]: a %d-record x %d KB ledger does NOT defeat the '
         '/health read bound - the tail is capped at LEDGER_TAIL_MAX_BYTES and '
         'terminates inside %.1fs'
         % (P22_FAT_RECORDS, P22_FAT_BYTES // 1024, P22_TAIL_TIME_BUDGET_S),
         p22_fat_ledger_tail_bounded)
lm_probe('phase22 sidecar [CR-0025]: _ledger_counts - what /health actually calls - '
         'terminates on the same fat ledger and reports real counts',
         p22_fat_ledger_counts_terminate)
lm_probe('phase22 sidecar [CR-0025]: the byte ceiling does NOT under-report a NORMAL '
         'ledger (the phase-14 "exactly the last 2000 lines" guarantee still holds)',
         p22_tail_bound_does_not_under_report)
lm_probe('phase22 sidecar [CR-0025]: the ceiling is a NAMED constant and its '
         'truncate-and-terminate behaviour is documented in _ledger_tail',
         p22_tail_bound_documented)

# ---- N: the CR-0004 resolution, asserted ----
# The finding: ledger.py's docstring promised "the configured provider API key is
# replaced with '[REDACTED-KEY]'", but no production call site ever passes `api_key`.
# Investigation (recorded in the report): the frontend holds the key in
# `settings.apiKey` and builds `Authorization: Bearer` headers for the PROVIDER
# endpoint only (index.html `authHeaders()`); appcore.js states outright that the
# `/repair` and `/ledger` requests "carry NO Authorization header and never the
# provider API key"; and `grep -n "api_key" localmodels/local_models_daemon.py` returns
# nothing. There is no env var, no CLI flag, no request field and no config file that
# carries a provider key to the sidecar.
#
# So the honest resolution is (b): correct the docstring so it stops asserting a rule
# no code path provides - and say the guarantee is ABSENT rather than deleting the line,
# because a reader must be able to tell "not implemented" from "not mentioned".
#
# These cases assert the DOCSTRING and the CODE agree, in the direction that matters:
# if a future change really does wire a key through, the docstring must claim it again.
def p22_docstring_does_not_overclaim():
    """The docstring must not state rule 1 as a rule IN FORCE.

    Scoped to the SENTENCE carrying the claim, deliberately. A window of surrounding
    characters is not enough: the original docstring contains the word "never" in
    "never leave the machine" three lines above the claim, so a context-window check
    passes the overclaim it exists to catch. That was measured, not assumed - see the
    report - so the check here is a single sentence or list item.
    """
    doc = P22_LEDGER.__doc__ or ''
    problems = []
    claims = ('the configured provider api key is replaced',
              'the configured provider api key is redacted')
    negations = ('not ', 'never', 'no code path', 'absent', 'does not', "isn't",
                 'unavailable', 'cannot', 'not enforced', 'not provided')
    # Split into sentence-ish units: a numbered rule, or a line of prose.
    units = re.split(r'(?:\n\s*(?:\d+\.\s*)?)|\.\s+', doc)
    for unit in units:
        low = unit.lower()
        if not any(c in low for c in claims):
            continue
        if not any(neg in low for neg in negations):
            problems.append('states %r as a rule in force, with no negation in the '
                            'same sentence: %r' % (unit.strip()[:90], unit.strip()[:120]))
    if problems:
        print('   ' + '; '.join(problems))
    return not problems


def p22_docstring_documents_the_absence():
    doc = (P22_LEDGER.__doc__ or '')
    low = doc.lower()
    problems = []
    # It must name the rule (so a reader can see what is missing) AND say it is absent.
    if 'api key' not in low:
        problems.append('the docstring no longer mentions the api key at all, so the '
                        'absence of that guarantee is invisible')
    if not any(neg in low for neg in ('not ', 'never', 'no code path', 'absent',
                                      'does not', "isn't", 'unavailable', 'cannot')):
        problems.append('the docstring does not state that any guarantee is absent')
    if problems:
        print('   ' + '; '.join(problems))
        return False
    return True


def p22_no_production_call_site_passes_a_key():
    """The code half of the same claim: if the docstring says no code path supplies a
    key, then no production call site may supply one either. Tokenised, so the
    docstring's own prose about this cannot satisfy or break the assertion."""
    import tokenize
    path = os.path.join(BASE, 'localmodels', 'local_models_daemon.py')
    offenders = []
    with open(path, 'rb') as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            if tok.string == 'api_key' or tok.string == 'API_KEY':
                offenders.append((tok.start[0], tok.string))
    if offenders:
        print('   local_models_daemon.py references a provider key at %r - if that is now '
              'a REAL configuration path, the docstring must claim the guarantee again'
              % (offenders,))
        return False
    return True


def p22_redaction_still_works_when_a_key_is_supplied():
    """The mechanism itself must be intact: `api_key=` still redacts when a caller
    passes one. Correcting a docstring must not quietly break the capability the
    docstring used to describe."""
    out = P22_LEDGER.redact({'note': 'the key is supersecretvalue123 in this string'},
                            api_key='supersecretvalue123')
    return (P22_LEDGER.REDACTED_KEY in out['note']
            and 'supersecretvalue123' not in out['note'])


lm_probe('phase22 ledger [CR-0004]: the docstring does NOT assert the provider-key '
         'redaction as a rule in force (it was provided by no code path)',
         p22_docstring_does_not_overclaim)
lm_probe('phase22 ledger [CR-0004]: the docstring still NAMES the provider-key rule and '
         'states plainly that the guarantee is ABSENT, so a reader can tell it is '
         'unimplemented rather than merely unmentioned', p22_docstring_documents_the_absence)
lm_probe('phase22 ledger [CR-0004]: no production call site in local_models_daemon.py '
         'passes an api_key - the docstring and the code agree on the absence',
         p22_no_production_call_site_passes_a_key)
lm_probe('phase22 ledger [CR-0004]: api_key= redaction still WORKS when a caller does '
         'pass one (the capability was corrected in the docstring, not removed)',
         p22_redaction_still_works_when_a_key_is_supplied)

# ---- O: a read that RAISES is a refusal, not an empty body (LE-1) ----
# The socket-level case above is the primary evidence; this one covers the sibling
# path in the same function that a socket cannot easily reach. `_read_body` did
#   except Exception: raw = b''
# and then returned ({}, None) for `not raw` - so a socket error was indistinguishable
# from "this route accepts an empty body", which is a 200 and a ledger record for a
# body that was never read. Driven through the unbound method with a double for
# `rfile`, because Handler.__init__ wants a real connection.
def p22_read_exception_is_refused():
    problems = []

    class _RFile:
        def __init__(self, exc):
            self._exc = exc

        def read(self, n):
            raise self._exc

    class _Self:
        # `_read_body` only ever touches self.headers.get() and self.rfile.read(),
        # so this double needs nothing else. Declared (rather than assigned in
        # __init__) so the attributes exist on the type, not just the instance.
        headers = {}
        rfile = object()

    for label, exc in (('ConnectionResetError', ConnectionResetError('peer reset')),
                       ('OSError', OSError('connection broken')),
                       ('TimeoutError', TimeoutError('read timed out'))):
        me = _Self()
        me.headers = {'Content-Length': '107'}
        me.rfile = _RFile(exc)
        try:
            got = P22_LMD.Handler._read_body(me)
        except Exception as e:
            problems.append('a read raising %s ESCAPED _read_body as %s: %s'
                            % (label, type(e).__name__, e))
            continue
        if got == ({}, None):
            problems.append('a read raising %s => ({}, None) - the same value as a '
                            'legitimately empty body, so the route answers 200'
                            % label)
        elif not (got[0] is None and got[1]):
            problems.append('a read raising %s => %r, want (None, <error code>)'
                            % (label, got))

    # The control, same double, same declared length: a read that returns FEWER bytes
    # than declared is refused for the same reason, and one that returns exactly n is
    # not. Without this a `_read_body` that refused every read would pass the above.
    class _ShortRFile:
        def __init__(self, data):
            self._data = data

        def read(self, n):
            return self._data[:n]

    good = json.dumps({'suspect': 'x', 'candidates': []}).encode()
    for label, declared, data, want_refused in (
            ('exactly n bytes', len(good), good, False),
            ('n - 1 bytes', len(good), good[:-1], True),
            ('zero bytes under a positive length', 107, b'', True)):
        me = _Self()
        me.headers = {'Content-Length': str(declared)}
        me.rfile = _ShortRFile(data)
        got = P22_LMD.Handler._read_body(me)
        refused = got[0] is None and bool(got[1])
        if refused != want_refused:
            problems.append('%s => %r, want %s' % (label, got,
                                                   'a refusal' if want_refused else 'a body'))
    if problems:
        print('   ' + '\n   '.join(problems))
        return False
    return True


lm_probe('phase22 sidecar [LE-1]: a read that RAISES is a 400 refusal, not an empty '
         'body - and, on the same doubles, a read that returns exactly n bytes is still '
         'a body while one that returns fewer is refused',
         p22_read_exception_is_refused)

# ---- P: concurrent appends + rotation lose NOTHING (LE-2) ----
# `append_record` took no lock and then called `_rotate_if_oversize`, which does
# read-size -> seek -> read a window -> write target+'.rotating' -> os.replace. Any
# record another thread appended between that size read and the os.replace was
# DISCARDED, because the replace swaps in a file that does not contain it; two
# concurrent rotations also shared one temp path, so one os.replace consumed the
# other's file and raised FileNotFoundError into a blanket except. `append_record`
# returned True either way: a record reported as written, absent from the audit trail.
# The daemon is a ThreadingHTTPServer, so this is reachable in production.
#
# The CONTROL ARM is the point of the whole case. Rotation disabled (a huge max_bytes)
# must lose exactly nothing; if it did, the harness - not rotation - would be the
# thing under suspicion, and a with-rotation failure could not be told from flakiness.
# Both numbers are printed on every run.
P22_CONC_DIR = tempfile.mkdtemp(prefix='cogp22-conc-')
P22_CONC_THREADS = 8
P22_CONC_PER_THREAD = 25
#: Small enough that the seeded ledger is over it, so rotation is armed from the very
#: first append and stays armed on every one after (a compaction leaves the file at
#: ~max_bytes, which is still not <= max_bytes).
P22_CONC_MAX_BYTES = 64 * 1024
#: The control's limit: large enough that rotation can never fire.
P22_CONC_CONTROL_MAX_BYTES = 512 * 1024 * 1024
#: Seed size, chosen so the retained window always covers EVERY concurrent record.
#:
#: This is the whole design constraint, and getting it wrong produces a test that
#: fails for the right reason on the wrong code: rotation is SUPPOSED to drop the
#: oldest lines, so if the concurrent records together exceed max_bytes then losing
#: them is correct behaviour, not silent loss. The seed is therefore fat enough to
#: arm rotation and is always the OLDER content, while the concurrent records
#: (~90 bytes each, ~18 KB total) all sit inside the newest 64 KB window and so must
#: survive. The seed records are never asserted on: their disappearance is the
#: documented direction of rotation.
P22_CONC_SEED_LINES = 150
P22_CONC_SEED_FAT = 512


def _p22_concurrent_arm(name, max_bytes):
    """One arm: seed an oversize ledger, append N*N unique records concurrently.

    Each arm gets its OWN directory. Sharing one would make the "no stray temp file"
    assertion meaningless - the control arm would see the armed arm's ledger and
    report a stray that is not its own - and the two arms would also be able to
    disturb each other's rotation.

    Returns (missing_markers, all_returned_true, strays).
    """
    arm_dir = os.path.join(P22_CONC_DIR, name)
    os.makedirs(arm_dir, exist_ok=True)
    path = os.path.join(arm_dir, name + '.jsonl')
    # Seed PAST the limit so rotation is armed from the very first append; a ledger
    # that has to grow to the limit first would let most of the run finish unrotated.
    fat = 'p' * P22_CONC_SEED_FAT
    with open(path, 'w', encoding='utf-8') as f:
        for i in range(P22_CONC_SEED_LINES):
            f.write(json.dumps({'trace_id': 'seed_%03d' % i, 'blob': fat}) + '\n')
    seeded_bytes = os.path.getsize(path)
    if seeded_bytes <= P22_CONC_MAX_BYTES:
        # Loudly, because an unarmed arm passes trivially and would make the whole
        # case vacuous - the reviewer's control arm is the armed one in miniature.
        raise AssertionError('the %s arm was never armed: the seed is %d bytes and the '
                             'limit is %d' % (name, seeded_bytes, P22_CONC_MAX_BYTES))
    small = 'q' * 32
    want = set()
    for t in range(P22_CONC_THREADS):
        for i in range(P22_CONC_PER_THREAD):
            want.add('conc_%d_%03d' % (t, i))
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(P22_CONC_THREADS)

    def worker(tid):
        # The barrier makes the appends genuinely simultaneous rather than staggered
        # by thread start-up, which is what opens the rotation window at all.
        barrier.wait()
        local = [P22_LEDGER.append_record(
            {'trace_id': 'conc_%d_%03d' % (tid, i), 'blob': small},
            path=path, max_bytes=max_bytes)
            for i in range(P22_CONC_PER_THREAD)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker, args=(t,))
               for t in range(P22_CONC_THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    alive = [th for th in threads if th.is_alive()]
    if alive:
        return None, False, ['%d thread(s) did not finish' % len(alive)]
    with open(path, encoding='utf-8') as f:
        got = set()
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                got.add(json.loads(ln).get('trace_id'))
            except ValueError:
                pass
    strays = sorted(set(os.listdir(arm_dir)) - {name + '.jsonl'})
    return sorted(want - got), all(results), strays


def p22_concurrent_rotation_loses_nothing():
    """With rotation ARMED: every concurrently appended record must survive, every
    call must have returned True, and no temp file may be left behind."""
    missing, all_true, strays = _p22_concurrent_arm('armed', P22_CONC_MAX_BYTES)
    print('   [LE-2] with rotation ARMED (max_bytes=%d, %d threads x %d appends): '
          '%d of %d records LOST, append_record returned True for all: %s, strays: %r'
          % (P22_CONC_MAX_BYTES, P22_CONC_THREADS, P22_CONC_PER_THREAD,
             len(missing) if missing is not None else -1,
             P22_CONC_THREADS * P22_CONC_PER_THREAD, all_true, strays))
    problems = []
    if missing is None:
        return False
    if missing:
        problems.append('%d of %d records were REPORTED WRITTEN (append_record returned '
                        'True) but are ABSENT from the ledger: %r'
                        % (len(missing), P22_CONC_THREADS * P22_CONC_PER_THREAD,
                           missing[:8]))
    if not all_true:
        problems.append('at least one append_record returned False')
    if strays:
        problems.append('a rotation temp file survived the run: %r' % (strays,))
    if problems:
        print('   ' + '\n   '.join(problems))
        return False
    return True


def p22_concurrent_control_arm_loses_nothing():
    """Rotation DISABLED: the same harness must lose nothing either.

    This is the arm that makes the case above mean something. Without it, a loss
    observed with rotation on could be the harness's fault, and a pass could be
    luck. It is asserted, not merely printed: if the harness ever starts losing
    records on its own, this case fails loudly rather than quietly discredited.
    """
    missing, all_true, strays = _p22_concurrent_arm('control', P22_CONC_CONTROL_MAX_BYTES)
    print('   [LE-2] CONTROL arm, rotation DISABLED (max_bytes=%d, %d threads x %d '
          'appends): %d of %d records LOST, append_record returned True for all: %s'
          % (P22_CONC_CONTROL_MAX_BYTES, P22_CONC_THREADS, P22_CONC_PER_THREAD,
             len(missing) if missing is not None else -1,
             P22_CONC_THREADS * P22_CONC_PER_THREAD, all_true))
    problems = []
    if missing is None:
        return False
    if missing:
        problems.append('the CONTROL arm lost %d record(s) with rotation unable to fire '
                        '- so the harness itself is lossy and the armed arm proves '
                        'nothing: %r' % (len(missing), missing[:8]))
    if not all_true:
        problems.append('the CONTROL arm had an append_record return False')
    if strays:
        problems.append('the CONTROL arm left stray files: %r' % (strays,))
    if problems:
        print('   ' + '\n   '.join(problems))
        return False
    return True


lm_probe('phase22 ledger [LE-2]: %d threads x %d appends onto an ALREADY-oversize '
         'ledger lose ZERO records with rotation armed - every call returned True, and '
         'no temp file survives'
         % (P22_CONC_THREADS, P22_CONC_PER_THREAD),
         p22_concurrent_rotation_loses_nothing)
lm_probe('phase22 ledger [LE-2]: the CONTROL arm - identical harness, rotation DISABLED '
         '(max_bytes=512 MiB) - also loses zero records, so a loss in the armed arm '
         'would be rotation and not the test', p22_concurrent_control_arm_loses_nothing)

# ---- Q: the docstring must not claim rotation introduces NO filename (LE-3) ----
# `append_record` justified in-place compaction on the grounds that it "introduces no
# filename at all", citing the phase-10 `lm_no_stray_files` invariant. `_rotate_if_
# oversize` then wrote `target + '.rotating'` IN THAT SAME DIRECTORY, and the
# neighbouring comment - "the temp name is removed by the rename itself, so no extra
# file survives" - is true only if os.replace runs. The window between open(tmp,'w')
# and os.replace is exactly the crash window, and in it the third filename survives.
# The existing p22_rotation_no_stray_files cannot see this: it only inspects the
# directory after SUCCESSFUL rotations.
#
# Sentence-scoped, like the CR-0004 docstring case above: the word "never" occurs
# elsewhere in this docstring ("never raises"), so a context window would both pass
# the overclaim it exists to catch and break on unrelated prose.
def p22_rotation_docstring_does_not_overclaim():
    doc = inspect.getdoc(P22_LEDGER.append_record) or ''
    units = re.split(r'(?:\n\s*(?:\*\s*)?|\.\s+)', doc)
    # Negations are checked in the SAME sentence unit, for the same reason the CR-0004
    # case above checks them there: a window of context would let the word "never"
    # elsewhere in this docstring ("never raises") satisfy the check, and the honest
    # replacement has to be able to QUOTE the claim in order to retract it.
    negations = ('not true', 'never', 'not ', 'no code path', 'absent', 'does not',
                 "isn't", 'used to say', 'but it is not', 'wrong')
    overclaim = []
    for unit in units:
        low = unit.lower()
        if 'no filename' not in low:
            continue
        if not any(neg in low for neg in negations):
            overclaim.append(unit.strip())
    problems = []
    for unit in overclaim:
        problems.append('claims %r with no retraction in the same sentence; rotation DOES '
                        'create one short-lived temp file in the same directory'
                        % unit[:110])
    # ...and the honest replacement must actually be there, or the claim has merely
    # been deleted: a reader must be able to see what happens during a crash.
    low = doc.lower()
    if 'short-lived' not in low and 'short lived' not in low:
        problems.append('the docstring no longer says the temp file is short-lived')
    if 'finally' not in low:
        problems.append('the docstring does not say the temp file is removed in a '
                        'finally, which is the only thing that makes it short-lived')
    if 'crash' not in low:
        problems.append('the docstring no longer names the crash window in which the '
                        'temp file CAN survive - the honest version has to admit it')
    if problems:
        print('   ' + '\n   '.join(problems))
        return False
    return True


def p22_stale_rotating_file_is_cleaned_up():
    """A crash between open(tmp) and os.replace leaves the temp name behind, and
    `lm_no_stray_files` then fails permanently for that install. With per-rotation
    unique names the leftover is unpredictable, so ANY stale `target + '.rotating*'`
    must be swept on the next append - the old fixed name included, because that is
    the shape a crash from the PREVIOUS build leaves."""
    path = os.path.join(P22_CONC_DIR, 'stale', 'stale.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seeds = [path + '.rotating',                      # the old fixed name
             path + '.rotating.31337-1',              # a unique-temp name
             path + '.rotating.31337-2']
    for p_ in seeds:
        with open(p_, 'w', encoding='utf-8') as f:
            f.write('{"trace_id": "half-written compaction"}\n')
    ok = P22_LEDGER.append_record({'trace_id': 'tr_after_stale'}, path=path,
                                  max_bytes=P22_CONC_CONTROL_MAX_BYTES)
    left = sorted(p_ for p_ in seeds if os.path.exists(p_))
    problems = []
    if not ok:
        problems.append('the append after a crash left-over returned False')
    if left:
        problems.append('stale temp file(s) survived the next append: %r' % (left,))
    if not os.path.isfile(path):
        problems.append('the ledger itself was not written')
    if problems:
        print('   ' + '\n   '.join(problems))
        return False
    return True


lm_probe('phase22 ledger [LE-3]: the rotation docstring does NOT claim that compaction '
         'introduces "no filename at all" - it states the short-lived temp file and its '
         'removal instead', p22_rotation_docstring_does_not_overclaim)
lm_probe('phase22 ledger [LE-3]: a stale .rotating temp file left by a crash - both the '
         'old fixed name and a per-rotation unique one - is swept by the next append, so '
         'lm_no_stray_files recovers instead of failing forever',
         p22_stale_rotating_file_is_cleaned_up)

print('\n%d FAILURES' % len(fails))
sys.exit(1 if fails else 0)
