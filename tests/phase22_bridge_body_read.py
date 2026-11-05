#!/usr/bin/env python3
"""Phase 22 Workstream A suite — bridge request-body containment (CR-Nanites-harness-0021).

Both servers read the request body with a declared-length form that was neither a
bound nor a contained parse:

  bridge.py          int(Content-Length) unwrapped; n <= 0 accepted, so a NEGATIVE
                     length made rfile.read(n) read until EOF — the unbounded read
                     the byte cap exists to prevent — and a length of 0 was coerced
                     to {} and dispatched with an empty tool name.
  bridge_daemon.py   the same unwrapped int() (a ValueError escaped _body into the
                     handler) plus every bad length SILENTLY coerced to {}, so an
                     oversized or malformed body was dropped and the route still ran
                     on an empty request.

Neither server capped the READ itself, only the declared number.

THE CONTRACT both servers now implement (one shape, chosen once, used in both):

    obj, code = _read_body()

  Content-Length absent / non-numeric / <= 0  ->  400   (the request is malformed)
  Content-Length > MAX_JSON_BYTES             ->  413   (too large; outranks 404)
  more than MAX_JSON_BYTES actually delivered ->  413   (the READ is capped, so a
                                                          lying length cannot make the
                                                          process buffer an unbounded body)
  body is not valid JSON                      ->  400
  body is valid JSON but not an object        ->  400

This suite starts BOTH REAL SERVERS as subprocesses and drives them over real
sockets. urllib cannot send a malformed Content-Length, so every negative here is
hand-written onto a socket — copied in technique from test_e2e.py's
req_absent_host() / req_non_ascii_token(), which is also why nothing is imported
from test_e2e.py (a parallel agent owns that file).

Run:  python3 tests/phase22_bridge_body_read.py     (prints 'N FAILURES', exits 1 on any)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_JSON_BYTES = 1024 * 1024

fails = []


def check(cond, msg):
    print(('ok  - ' if cond else 'FAIL- ') + msg)
    if not cond:
        fails.append(msg)


AMBIENT = object()
PORT_TOKENS = {}
P22_ORIGIN = 'http://localhost:8080'


def register_port_token(port, token):
    PORT_TOKENS[port] = token


def read_server_token(path, deadline=15.0):
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
    raise AssertionError('no token file at %s after %.0fs' % (path, deadline))


def assert_ports_free(ports, deadline=10.0):
    """A port another process already holds is a SILENT collision: the suite would
    be answered by someone else's server and every case would be a lie.

    Retried within a bound, because the previous run's own sockets can still be
    winding down and that is not a collision. The retry window is short and it never
    retries a port some OTHER process is genuinely holding.
    """
    end = time.time() + deadline
    busy = None
    while True:
        busy = None
        for p in ports:
            s = socket.socket()
            # SO_REUSEADDR matches what HTTPServer itself sets, so a socket left in
            # TIME_WAIT by the previous run does not read as a collision. It does NOT
            # mask a real listener: bind still fails with EADDRINUSE if one is up.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', p))
            except OSError as e:
                busy = (p, e)
            finally:
                s.close()
            if busy:
                break
        if not busy or time.time() >= end:
            break
        time.sleep(0.3)
    if busy:
        raise SystemExit('port %d is already in use (%s) after %.0fs - refusing to run '
                         'against a server this suite did not start'
                         % (busy[0], busy[1], deadline))


def req(port, path, payload=None, origin=None, token=AMBIENT, timeout=10):
    """A WELL-FORMED request, over urllib. Used only for the positive direction and
    for confirming a server is still alive after a hostile request."""
    if token is AMBIENT:
        token = PORT_TOKENS.get(port)
        if origin is None:
            origin = P22_ORIGIN
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'}
    if origin:
        headers['Origin'] = origin
    if token:
        headers['X-Cogitator-Token'] = token
    import urllib.request
    import urllib.error
    r = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=data,
                               headers=headers,
                               method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read() or b'{}'
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read() or b'{}'
        status = e.code
    except Exception as e:
        # A timeout / reset / dropped connection is -1, exactly as it is for a
        # hand-written request: a server that does not ANSWER has failed the case,
        # and the suite must REPORT that rather than die on a traceback. A suite
        # that aborts on the first bad server cannot be mutation-scored at all.
        print('   (transport: %s: %s)' % (type(e).__name__, e))
        return -1, {}
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        obj = {'raw': body.decode('utf-8', 'replace')}
    return status, obj


def raw_post(port, path, cl, body, token, host=True, half_close=True, timeout=10):
    """Hand-write the request line and headers so Content-Length can be a lie.

    `cl` is written VERBATIM into the header — None omits the header entirely, which
    is how the absent case is expressed. Returns (status, body_text); status -1 means
    NO status line came back at all, i.e. the handler died or the connection was
    dropped, which is itself a failure of "must be refused".

    `half_close` shuts the write side down after the body. It matters for the
    Content-Length: -1 case: the pre-fix rfile.read(-1) reads until EOF, so without
    the half-close the server would hang rather than answer, and the case would prove
    nothing about whether the request was executed.
    """
    body = body or b''
    lines = ['POST %s HTTP/1.1' % path]
    if host:
        lines.append('Host: 127.0.0.1:%d' % port)
    lines.append('Origin: %s' % P22_ORIGIN)
    lines.append('Content-Type: application/json')
    if cl is not None:
        lines.append('Content-Length: %s' % cl)
    if token:
        lines.append('X-Cogitator-Token: %s' % token)
    lines.append('Connection: close')
    wire = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + body
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        sock.sendall(wire)
        if half_close:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        chunks = []
        while True:
            try:
                part = sock.recv(65536)
            except socket.timeout:
                return -1, ''
            if not part:
                break
            chunks.append(part)
    finally:
        sock.close()
    raw = b''.join(chunks)
    head = raw.split(b'\r\n', 1)[0].decode('latin-1')
    try:
        status = int(head.split()[1])
    except (IndexError, ValueError):
        return -1, raw.decode('latin-1', 'replace')
    return status, raw.split(b'\r\n\r\n', 1)[-1].decode('utf-8', 'replace')


# The refusal has to be a DELIBERATE one. Pre-fix, malformed lengths surfaced as a
# Python dump ("ValueError: invalid literal for int()..." / "AttributeError: 'list'
# object has no attribute 'get'") behind a 400, so a status-only assertion would have
# passed on the broken code. These are the phrases the deliberate refusal uses.
DELIBERATE = ('Content-Length', 'request body required', 'request exceeds',
              'invalid JSON', 'must be a JSON object')
PYTHON_DUMP = ('ValueError', 'AttributeError', 'JSONDecodeError', 'TypeError',
               'IndexError', 'KeyError', 'Traceback')


def is_deliberate_refusal(status, body_text):
    if status != 400:
        return False
    try:
        j = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return False
    if j.get('ok') is not False:
        return False
    err = str(j.get('error') or '')
    if any(d in err for d in PYTHON_DUMP):
        return False
    return any(k in err for k in DELIBERATE)


def refused(status, body_text):
    """A refusal of ANY deliberate kind: 400 with a refusal phrase, or a 413."""
    return status == 413 or is_deliberate_refusal(status, body_text)


def err_of(body_text):
    try:
        return str(json.loads(body_text).get('error') or '')
    except (json.JSONDecodeError, ValueError, AttributeError):
        return body_text[:200]


# --------------------------------------------------------------------------- servers
# Ports 18980/18981: test_e2e.py holds 18930-18944 and 18970-18973. A port another
# phase still holds is a collision you cannot see from the phase that owns it.
B_PORT = 18980
D_PORT = 18981
projs = tempfile.mkdtemp(prefix='cogp22-proj-')
bdir = tempfile.mkdtemp(prefix='cogp22-bridge-')
ddir = tempfile.mkdtemp(prefix='cogp22-daemon-')
servers = []

with open(os.path.join(projs, 'hello.py'), 'w', encoding='utf-8') as f:
    f.write('print("phase22")\n# TODO body containment\n')

# Both files are copied into temp dirs: bridge_daemon.py refuses to spawn a
# bridge.py it cannot find next to itself, and each server writes its
# per-install .cogitator-token next to ITSELF. Copying keeps the repo clean.
shutil.copy2(os.path.join(BASE, 'bridge.py'), os.path.join(bdir, 'bridge.py'))
shutil.copy2(os.path.join(BASE, 'bridge_daemon.py'), os.path.join(ddir, 'bridge_daemon.py'))
shutil.copy2(os.path.join(BASE, 'bridge.py'), os.path.join(ddir, 'bridge.py'))

assert_ports_free([B_PORT, D_PORT])

ENV = dict(os.environ)
ENV['PYTHONDONTWRITEBYTECODE'] = '1'

servers.append(subprocess.Popen(
    [sys.executable, os.path.join(bdir, 'bridge.py'), '--root', projs, '--port', str(B_PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=bdir, env=ENV))
servers.append(subprocess.Popen(
    [sys.executable, os.path.join(ddir, 'bridge_daemon.py'), '--port', str(D_PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ddir, env=ENV))

try:
    BTOK = read_server_token(os.path.join(bdir, '.cogitator-token'))
    DTOK = read_server_token(os.path.join(ddir, '.cogitator-token'))
    register_port_token(B_PORT, BTOK)
    register_port_token(D_PORT, DTOK)

    BPATH = '/tools/execute'
    RITE = b'{"name":"read_file","arguments":{"path":"hello.py"}}'

    # ============================ CASE 7 first: the positive direction =============
    # Without this a handler that refuses EVERYTHING passes every negative below.
    # Written before the negatives on purpose: it is also the baseline that proves the
    # negatives are about the body and not about a server that never came up.
    s, j = req(B_PORT, '/health')
    check(s == 200 and j.get('ok'), 'bridge: /health answers (server is up)')
    s, j = req(B_PORT, BPATH, {'name': 'read_file', 'arguments': {'path': 'hello.py'}})
    check(s == 200 and 'TODO' in j.get('result', ''),
          'bridge: well-formed POST /tools/execute still executes the rite')
    s, j = req(D_PORT, '/health')
    check(s == 200 and j.get('ok'), 'daemon: /health answers (server is up)')
    s, j = req(D_PORT, '/stop', {})
    check(s == 200 and j.get('ok'), 'daemon: well-formed POST /stop with a body still answers')
    wd_before = req(D_PORT, '/status')[1].get('workdir')
    s, j = req(D_PORT, '/set_workdir', {'path': projs, 'autostart': False})
    check(s == 200 and j.get('ok') and j.get('workdir') == projs,
          'daemon: well-formed POST /set_workdir still rebinds the workdir')

    # ============================ CASE 1: non-numeric Content-Length ===============
    # int('abc') raised ValueError. In bridge.py it was caught only by the broad
    # `except Exception` at the bottom of do_POST, surfacing as a 400 whose body was
    # the Python dump — a refusal for the wrong reason. In bridge_daemon.py it escaped
    # _body() into the caller.
    s, b = raw_post(B_PORT, BPATH, 'abc', RITE, BTOK)
    check(is_deliberate_refusal(s, b),
          'bridge: Content-Length: abc => 400 (deliberate), got %s %r' % (s, err_of(b)))
    s, b = raw_post(D_PORT, '/set_workdir', 'abc', json.dumps({'path': projs}).encode(), DTOK)
    check(refused(s, b),
          'daemon: Content-Length: abc => refused with a status line, got %s %r'
          % (s, err_of(b)))

    # ============================ CASE 2: NEGATIVE Content-Length =================
    # THE HEADLINE. rfile.read(-1) reads until EOF: an unbounded read, exactly what
    # MAX_JSON_BYTES exists to prevent — and worse, a perfectly well-formed body
    # behind a negative length was PARSED AND EXECUTED as a real rite.
    s, b = raw_post(B_PORT, BPATH, '-1', RITE, BTOK)
    check(is_deliberate_refusal(s, b),
          'bridge: Content-Length: -1 => 400, the read did not run away and the rite '
          'was NOT executed; got %s %r' % (s, err_of(b)[:120]))
    s, b = raw_post(D_PORT, '/stop', '-1', RITE, DTOK)
    check(is_deliberate_refusal(s, b),
          'daemon: Content-Length: -1 => 400 (was: the body was dropped and the route '
          'ran on {}); got %s %r' % (s, err_of(b)[:120]))

    # ============================ CASE 3: zero / absent ============================
    # A POST that requires a body and has none is malformed, not an empty success.
    s, b = raw_post(B_PORT, BPATH, '0', b'', BTOK)
    check(is_deliberate_refusal(s, b),
          'bridge: Content-Length: 0 => 400 (not dispatched with an empty tool name); '
          'got %s %r' % (s, err_of(b)[:120]))
    s, b = raw_post(B_PORT, BPATH, None, RITE, BTOK)
    check(is_deliberate_refusal(s, b),
          'bridge: absent Content-Length => 400; got %s %r' % (s, err_of(b)[:120]))
    s, b = raw_post(D_PORT, '/set_workdir', '0', b'', DTOK)
    check(is_deliberate_refusal(s, b),
          'daemon: Content-Length: 0 => 400; got %s %r' % (s, err_of(b)[:120]))
    s, b = raw_post(D_PORT, '/set_workdir', None, json.dumps({'path': projs}).encode(), DTOK)
    check(is_deliberate_refusal(s, b),
          'daemon: absent Content-Length => 400; got %s %r' % (s, err_of(b)[:120]))
    # ...and the refused set_workdir must not have rebound anything.
    check(req(D_PORT, '/status')[1].get('workdir') == projs,
          'daemon: a refused /set_workdir left the workdir unchanged')

    # ============================ CASE 4: over the byte limit ======================
    huge = b'{"name":"read_file","arguments":{"path":"' + b'a' * (MAX_JSON_BYTES + 64) + b'"}}'
    s, b = raw_post(B_PORT, BPATH, str(MAX_JSON_BYTES + 64), huge, BTOK, half_close=False)
    check(s == 413, 'bridge: Content-Length > MAX_JSON_BYTES => 413; got %s %r'
          % (s, err_of(b)[:120]))
    s, b = raw_post(D_PORT, '/set_workdir', str(MAX_JSON_BYTES + 64), huge, DTOK,
                    half_close=False)
    check(s == 413,
          'daemon: Content-Length > MAX_JSON_BYTES => 413 (was: silently dropped and '
          'the route ran); got %s %r' % (s, err_of(b)[:120]))

    # ============================ CASE 5: valid size, invalid JSON =================
    s, b = raw_post(B_PORT, BPATH, '5', b'not!!', BTOK)
    check(is_deliberate_refusal(s, b),
          'bridge: Content-Length: 5 body "not!!" => 400 (deliberate), got %s %r'
          % (s, err_of(b)[:120]))
    s, b = raw_post(D_PORT, '/set_workdir', '5', b'not!!', DTOK)
    check(refused(s, b), 'daemon: Content-Length: 5 body "not!!" => 400; got %s %r'
          % (s, err_of(b)[:120]))

    # ============================ CASE 6: valid JSON, not an object =================
    # Pre-fix the list reached .get() and raised AttributeError, which bridge.py
    # answered with a 400 carrying the traceback text and bridge_daemon.py did not
    # answer at all.
    s, b = raw_post(B_PORT, BPATH, '2', b'[]', BTOK)
    check(is_deliberate_refusal(s, b),
          'bridge: Content-Length: 2 body "[]" => 400 (deliberate), got %s %r'
          % (s, err_of(b)[:120]))
    s, b = raw_post(D_PORT, '/set_workdir', '2', b'[]', DTOK)
    check(is_deliberate_refusal(s, b),
          'daemon: Content-Length: 2 body "[]" => 400 with a status line (was: no '
          'response at all); got %s %r' % (s, err_of(b)[:120]))

    # ============================ CASE 8: byte cap ON THE READ ======================
    # What is sent: Content-Length: 19 — a legal, positive, under-the-limit number —
    # but the SOCKET then carries the whole 52-byte read_file request. The first 19
    # bytes are b'{"name":"read_' : truncated, not JSON. So:
    #   - a reader that honours the declared length reads 19 bytes and refuses;
    #   - a reader that over-reads (read to EOF, or cap-without-length) picks up the
    #     remaining 33 bytes and executes a rite that was never legitimately declared.
    # Asserted: it must NOT succeed as a valid body, and no rite may have run.
    lying = b'{"name":"read_' + b'file","arguments":{"path":"hello.py"}}'
    s, b = raw_post(B_PORT, BPATH, '19', lying, BTOK)
    try:
        ran = json.loads(b).get('ok') is True
    except (json.JSONDecodeError, ValueError, AttributeError):
        ran = False
    check(not ran,
          'bridge: declared 19 but sent %d bytes => not executed as a valid body; '
          'got %s %r' % (len(lying), s, err_of(b)[:120]))
    s2, j2 = req(B_PORT, BPATH, {'name': 'read_file', 'arguments': {'path': 'hello.py'}})
    check(s2 == 200 and 'TODO' in j2.get('result', ''),
          'bridge: server still healthy after the lying-length request')
    s, b = raw_post(D_PORT, '/stop', '19', lying, DTOK)
    check(refused(s, b),
          'daemon: declared 19 but sent %d bytes => refused, not processed as a body; '
          'got %s %r' % (len(lying), s, err_of(b)[:120]))
    s, j = req(D_PORT, '/stop', {})
    check(s == 200 and j.get('ok'), 'daemon: server still healthy after the lying-length request')
finally:
    for p in servers:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    for d in (projs, bdir, ddir):
        shutil.rmtree(d, ignore_errors=True)

print('%d FAILURES' % len(fails))
sys.exit(1 if fails else 0)
