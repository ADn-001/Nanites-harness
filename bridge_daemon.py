#!/usr/bin/env python3
# bridge_daemon.py — COGITATOR machine-level bridge supervisor.
# Run ONCE per machine:  python bridge_daemon.py   (or --install-autostart, then forget)
# Owns the lifecycle of per-directory bridge.py workers:
#   POST /pick_directory  native OS folder picker
#   POST /set_workdir     stop old worker, scrub old bridge.py (only if we wrote it),
#                         write fresh bridge.py, optionally respawn
#   POST /start | /stop   spawn | kill the worker
#   GET  /status | /health
import argparse, hmac, json, os, secrets, signal, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_PORT   = 8930
BRIDGE_PORT    = 8931
BRIDGE_MARKER  = '# ==== COGITATOR BRIDGE — managed by bridge_daemon.py ===='
MAX_JSON_BYTES = 1 * 1024 * 1024
#: The Host header must name loopback. The whole value is split on the last
#: colon and the NAME must equal one of these exactly, so neither
#: "localhost.evil.example" nor "127.0.0.1.evil.example" can pass as
#: "localhost". A missing Host is refused: HTTP/1.1 requires one, so its
#: absence means a crafted request rather than a browser. The bracketed IPv6
#: literal "[::1]" is handled separately in _host_allowed, because it carries
#: its port inside the brackets and would not survive a naive split on ":".
LOOPBACK_HOSTS = ('localhost', '127.0.0.1')

# Every daemon route is privileged, so the Origin allow-list is enforced on
# every GET and POST. Missing Origin = curl/native caller (allowed); null is
# what a sandboxed iframe / data: / file:// page sends, so it is refused
# unless explicitly opted in.
ALLOW_ANY_ORIGIN  = False
ALLOW_FILE_ORIGIN = False

def origins_desc():
    if ALLOW_ANY_ORIGIN:
        return 'ANY'
    if ALLOW_FILE_ORIGIN:
        return 'localhost / null (file:// trusted)'
    return 'localhost only (null refused)'

_here = os.path.dirname(os.path.abspath(__file__))
STATE = {'workdir': None, 'proc': None, 'bridge_port': BRIDGE_PORT, 'started_at': None}
_lock = threading.RLock()
#: The per-install token path. The spawned worker is handed the SAME path via
#: --token-file, so one paste into the UI covers the daemon and the worker.
TOKEN_PATH = os.path.join(_here, '.cogitator-token')
TOKEN = ''


def load_or_create_token(path):
    """Per-install token, 0600. Read-or-create so the operator pastes it into the UI once."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(tok + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok

def log(msg):
    line = time.strftime('[%Y-%m-%d %H:%M:%S] ') + msg
    print(line, flush=True)
    try:
        with open(os.path.join(_here, 'bridge_daemon.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass

def pid_file():
    return os.path.join(_here, 'bridge_daemon.pid')

def _pid_alive(pid):
    try:
        os.kill(pid, 0); return True
    except (OSError, ProcessLookupError):
        return False

def single_instance():
    pf = pid_file()
    if os.path.exists(pf):
        try:
            old = int(open(pf).read().strip())
            if _pid_alive(old):
                print('bridge_daemon already running (pid %d). Exiting.' % old, file=sys.stderr)
                sys.exit(1)
        except (ValueError, OSError):
            pass
    with open(pf, 'w') as f:
        f.write(str(os.getpid()))

def read_bridge_source():
    p = os.path.join(_here, 'bridge.py')
    if not os.path.isfile(p):
        raise RuntimeError('bridge.py not found next to bridge_daemon.py — keep both files together.')
    with open(p, 'r', encoding='utf-8') as f:
        return f.read()

def bridge_supports_args(src):
    return ('--port' in src) and ('--root' in src)

def expected_bridge_source():
    """The daemon's own source with BRIDGE_MARKER forced onto LINE 1.

    Unconditional, and line 1 rather than "somewhere in the first 4096 bytes":
    the verify step must not depend on how much of the file a reader happened to
    see, and a source that already carried the marker deep inside it must still
    produce a file whose FIRST line is the marker (CR-Nanites-harness-0024).
    """
    src = read_bridge_source()
    lines = src.splitlines(True)
    while lines and lines[0].strip() == BRIDGE_MARKER:
        lines.pop(0)
    return BRIDGE_MARKER + '\n' + ''.join(lines)

def valid_workdir(path):
    return (isinstance(path, str) and os.path.isabs(path)
            and os.path.isdir(path) and os.access(path, os.W_OK))

def stop_bridge():
    # CR-Nanites-harness-0023: takes _lock so /stop and /start are mutually exclusive
    # with /set_workdir, which holds the same RLock around the whole rebind. It is an
    # RLock, so set_workdir calling this is safe and is not a deadlock.
    with _lock:
        proc = STATE['proc']; STATE['proc'] = None; STATE['started_at'] = None
        if proc and proc.poll() is None:
            try:
                proc.terminate(); proc.wait(timeout=5)
            except Exception:
                try: proc.kill()
                except Exception: pass
            log('bridge worker stopped')
            return True
        return False

def scrub_old_bridge():
    wd = STATE.get('workdir')
    if not wd: return
    target = os.path.join(wd, 'bridge.py')
    try:
        if os.path.isfile(target):
            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                head = f.read(4096)
            if BRIDGE_MARKER in head:
                os.remove(target)
                log('removed managed bridge.py from old workdir: ' + wd)
            else:
                log('left bridge.py in ' + wd + ' (no daemon marker — not ours)')
    except OSError as e:
        log('warning: could not scrub old bridge.py in %s: %s' % (wd, e))

def write_bridge(workdir):
    dest = os.path.join(workdir, 'bridge.py')
    # Back up a foreign file FIRST, and never destroy what we find. A file the
    # daemon itself wrote that the operator later edited is also not byte-equal
    # to the source, so verify_or_plant_bridge re-plants it - and that edit is
    # backed up here rather than lost (CR-Nanites-harness-0016).
    if os.path.exists(dest):
        try:
            with open(dest, 'r', encoding='utf-8', errors='replace') as f:
                on_disk = f.read()
        except OSError:
            on_disk = ''
        if on_disk != expected_bridge_source():
            bak = dest + '.cogitator-bak'
            n = 1
            while os.path.exists(bak):
                n += 1; bak = dest + '.cogitator-bak%d' % n
            os.replace(dest, bak)
            log('existing bridge.py in %s backed up to %s (never destroyed)' % (workdir, bak))
    src = expected_bridge_source()
    # CR-Nanites-harness-0024: atomic. Write to a temp file in the SAME directory
    # (os.replace is only atomic within one filesystem) and replace into position,
    # so a crash mid-write can never leave a truncated bridge.py behind.
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=workdir,
                                         delete=False, prefix='.cogitator-bridge-') as f:
            tmp = f.name
            f.write(src); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, dest)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass
    log('wrote bridge.py -> ' + dest)
    return dest

def verify_or_plant_bridge(workdir):
    """Never spawn a file the daemon did not write (CR-Nanites-harness-0016).

    Returns (ok, msg). The on-disk file must be byte-equal to the daemon's own
    source with BRIDGE_MARKER on line 1; anything else (missing, truncated,
    tampered, hand-edited) is re-planted, with the old file backed up rather than
    destroyed, and the message says so. `start_bridge` spawns only after this
    returns ok, and spawns the path it just verified.
    """
    try:
        expected = expected_bridge_source()
    except RuntimeError as e:
        return False, str(e)
    dest = os.path.join(workdir, 'bridge.py')
    reason = None
    try:
        with open(dest, 'r', encoding='utf-8', errors='replace') as f:
            on_disk = f.read()
    except OSError:
        on_disk, reason = None, 'missing'
    else:
        if on_disk != expected:
            reason = 'differs from the daemon source'
        elif on_disk.splitlines()[:1] != [BRIDGE_MARKER]:
            reason = 'BRIDGE_MARKER is not on line 1'
    if reason is None:
        return True, 'bridge.py verified against the daemon source'
    write_bridge(workdir)
    return True, 're-planted bridge.py in %s (%s); the previous file was backed up' % (workdir, reason)

def start_bridge():
    # CR-Nanites-harness-0023: holds _lock, so /start and /set_workdir are mutually
    # exclusive and the daemon cannot end up bound to one directory while the live
    # worker serves another. _lock is an RLock: set_workdir already holds it and
    # calls this, which is safe precisely because it is reentrant.
    with _lock:
        if STATE['proc'] and STATE['proc'].poll() is None:
            return False, 'already running'
        if not STATE['workdir']:
            return False, 'no working directory bound'
        ok, vmsg = verify_or_plant_bridge(STATE['workdir'])
        if not ok:
            return False, vmsg
        bridge_path = os.path.join(STATE['workdir'], 'bridge.py')
        argv = [sys.executable, bridge_path]
        if bridge_supports_args(read_bridge_source()):
            argv += ['--root', STATE['workdir'], '--port', str(STATE['bridge_port'])]
        # The worker shares the DAEMON's token file, so one paste in the UI covers
        # the whole stack instead of two.
        argv += ['--token-file', TOKEN_PATH]
        kwargs = dict(cwd=STATE['workdir'], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        if os.name == 'nt':
            kwargs['creationflags'] = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                                       | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            kwargs['start_new_session'] = True
        try:
            STATE['proc'] = subprocess.Popen(argv, **kwargs)
            STATE['started_at'] = time.time()
            log('bridge worker spawned (pid %d) in %s' % (STATE['proc'].pid, STATE['workdir']))
            return True, 'spawned; ' + vmsg
        except OSError as e:
            return False, str(e)

def set_workdir(path, autostart=True):
    # The full rebind sequence is atomic: another /set_workdir cannot interleave
    # between stop, scrub, write, and spawn while this rite is in progress.
    with _lock:
        if not valid_workdir(path):
            return False, 'not a writable absolute directory: %r' % (path,)
        path = os.path.abspath(path)
        if STATE['workdir'] == path and STATE['proc'] and STATE['proc'].poll() is None:
            return True, 'already bound to this directory'
        stop_bridge()
        if STATE['workdir'] and STATE['workdir'] != path:
            scrub_old_bridge()
        STATE['workdir'] = path
        write_bridge(path)
        if autostart:
            ok, msg = start_bridge()
            return ok, 'workdir set to %s; %s' % (path, msg)
        return True, 'workdir set to ' + path

def pick_directory_dialog():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
        path = filedialog.askdirectory(title='Choose working directory for the bridge')
        root.destroy()
        return path or None
    except Exception:
        return None

AUTOSTART_NAME = 'CogitatorBridgeDaemon'

def install_autostart():
    script = os.path.abspath(__file__); py = sys.executable
    try:
        if sys.platform == 'win32':
            import winreg
            # pythonw avoids a console-window flash at every Windows login.
            if os.path.basename(py).lower() == 'python.exe':
                py = os.path.join(os.path.dirname(py), 'pythonw.exe')
                if not os.path.exists(py):
                    py = sys.executable
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, '"%s" "%s"' % (py, script))
            winreg.CloseKey(key)
            return 'installed (HKCU Run key)'
        elif sys.platform == 'darwin':
            plist = os.path.expanduser('~/Library/LaunchAgents/com.cogitator.%s.plist' % AUTOSTART_NAME)
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>Label</key><string>com.cogitator.' + AUTOSTART_NAME + '</string>\n'
                    '  <key>ProgramArguments</key><array><string>' + py + '</string><string>' + script + '</string></array>\n'
                    '  <key>RunAtLoad</key><true/>\n</dict></plist>')
            with open(plist, 'w') as f: f.write(body)
            return 'installed (' + plist + ')'
        else:
            desktop = os.path.expanduser('~/.config/autostart'); os.makedirs(desktop, exist_ok=True)
            entry = os.path.join(desktop, AUTOSTART_NAME + '.desktop')
            body = ('[Desktop Entry]\nType=Application\nName=' + AUTOSTART_NAME
                    + '\nExec=' + py + ' ' + script
                    + '\nX-GNOME-Autostart-enabled=true\nHidden=false\nNoDisplay=false\n')
            with open(entry, 'w') as f: f.write(body)
            return 'installed (' + entry + ')'
    except Exception as e:
        return 'FAILED: %s' % e

def remove_autostart():
    try:
        if sys.platform == 'win32':
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE)
            try: winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError: pass
            winreg.CloseKey(key)
            return 'removed'
        elif sys.platform == 'darwin':
            plist = os.path.expanduser('~/Library/LaunchAgents/com.cogitator.%s.plist' % AUTOSTART_NAME)
            if os.path.exists(plist): os.remove(plist)
            return 'removed'
        else:
            entry = os.path.expanduser('~/.config/autostart/%s.desktop' % AUTOSTART_NAME)
            if os.path.exists(entry): os.remove(entry)
            return 'removed'
    except Exception as e:
        return 'FAILED: %s' % e

def bridge_healthy():
    try:
        s = socket.create_connection(('127.0.0.1', STATE['bridge_port']), timeout=1)
        s.close(); return True
    except OSError:
        return False

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _host_allowed(self):
        """The Host header must name loopback, with or without a port."""
        host = (self.headers.get('Host') or '').strip().lower()
        if not host:
            # HTTP/1.1 requires a Host; its absence means a crafted request, not a browser.
            return False
        if host.startswith('['):
            if host == '[::1]':
                return True
            return host.startswith('[::1]:') and host[6:].isdigit()
        name, sep, port = host.partition(':')
        if name not in LOOPBACK_HOSTS:
            return False
        return not sep or port.isdigit()

    def _token_ok(self):
        """Constant-time compare of X-Cogitator-Token against the per-install token.

        Compare the ASCII-safe form: hmac.compare_digest raises TypeError on any
        str holding a byte >= 0x80, and http.server decodes header bytes as
        latin-1, so a malformed token would crash the gate instead of being
        refused with a 403. See bridge.py for the full note.
        """
        sent = self.headers.get('X-Cogitator-Token') or ''
        if not sent or not TOKEN:
            return False
        try:
            return hmac.compare_digest(sent.encode('utf-8'), TOKEN.encode('utf-8'))
        except (UnicodeError, TypeError):
            return False

    def _privileged(self):
        """Every POST on the daemon is privileged; /health and /status are not."""
        return self.command == 'POST'

    def _origin_allowed(self):
        if ALLOW_ANY_ORIGIN:
            return True
        origin = self.headers.get('Origin')
        if not origin:
            # Absent Origin on a PRIVILEGED route is refused (CR-Nanites-harness-0017):
            # curl / native callers send none, and they must carry the token.
            # GET /health and GET /status keep treating it as allowed, so the UI's
            # status chip is alive before the operator has paired.
            return not self._privileged()
        if origin == 'null':
            # Sandboxed iframe, data:/blob: document, or file:// page — any
            # hostile page can obtain an opaque origin. Opt-in only.
            return ALLOW_FILE_ORIGIN
        low = origin.lower()
        return (low.startswith('http://localhost:') or low.startswith('http://127.0.0.1:')
                or low in ('http://localhost', 'http://127.0.0.1'))

    def _auth_gate(self):
        """Return an error string when the request must be refused, else None.

        The three checks are ANDed on a privileged route: a correct token never
        buys a past a foreign Origin, and a local Origin never buys a past a
        missing token.
        """
        if not self._host_allowed():
            return 'host not permitted'
        if self._privileged() and not self._token_ok():
            return 'X-Cogitator-Token required'
        if not self._origin_allowed():
            return 'origin not permitted'
        return None

    def _cors(self, reflect=True):
        """Reflect the SPECIFIC request Origin. Never `*`.

        `reflect=False` on a refusal: a wildcard ACAO on a 403 is part of what
        CR-Nanites-harness-0017 calls out, so no ACAO is emitted at all then.
        ALLOW-Methods/ALLOW-HEADERS stay unconditional - they leak nothing and
        the preflight needs them (X-Cogitator-Token especially, or the browser
        refuses the real request and the UI breaks silently).
        """
        if reflect:
            origin = self.headers.get('Origin')
            if origin and self._origin_allowed():
                self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Cogitator-Token')

    def _json(self, obj, code=200, cors=True):
        body = json.dumps(obj).encode()
        self.send_response(code); self._cors(cors)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _body(self):
        """Read the JSON body as a BOUND, CONTAINED read (CR-Nanites-harness-0021).

        Returns (obj, err). `err` is None, or a (code, message) pair the caller
        answers with verbatim. ONE shape, shared with bridge.py — do_POST below is
        the only call site, and it refuses on it, so no route can be reached by a
        body that was never contained.

        Why each refusal is what it is:

        * Content-Length absent, non-numeric or <= 0 => 400. `int()` used to be
          unwrapped, so "abc" raised ValueError straight out of here into the
          caller's handler — a dropped connection or a 500, i.e. a malformed request
          could take a handler down. A guard that crashes is not a guard that refuses.
        * Content-Length > MAX_JSON_BYTES => 413, not a silent {}. Silently coercing
          an oversized body to {} meant the route RAN on an empty request and could
          answer success-shaped for a request that was never read. 413 outranks the
          404 below, so an oversized body is never routed.
        * The READ itself is capped at MAX_JSON_BYTES bytes, and anything actually
          delivered beyond that is 413. Capping only the DECLARED number leaves the
          read itself trusting the client; this is the read-bound.
        * Not valid JSON => 400, and valid JSON that is not an object => 400. A list
          used to reach `b.get('path')` and raise AttributeError, killing the handler.

        The length check happens BEFORE the read, so an over-limit body is never
        buffered at all.
        """
        raw_len = self.headers.get('Content-Length')
        if raw_len is None or not str(raw_len).strip():
            return None, (400, 'request body required: Content-Length missing')
        try:
            n = int(str(raw_len).strip())
        except (TypeError, ValueError):
            return None, (400, 'invalid Content-Length header')
        if n <= 0:
            return None, (400, 'invalid Content-Length header')
        if n > MAX_JSON_BYTES:
            return None, (413, 'request exceeds %d byte limit' % MAX_JSON_BYTES)
        try:
            raw = self.rfile.read(min(n, MAX_JSON_BYTES + 1))
        except Exception:
            return None, (400, 'could not read request body')
        if raw is None or len(raw) > MAX_JSON_BYTES:
            return None, (413, 'request exceeds %d byte limit' % MAX_JSON_BYTES)
        if not raw.strip():
            return None, (400, 'request body required: empty body')
        try:
            obj = json.loads(raw.decode('utf-8', 'replace'))
        except (json.JSONDecodeError, ValueError):
            return None, (400, 'invalid JSON body')
        if not isinstance(obj, dict):
            return None, (400, 'request body must be a JSON object')
        return obj, None

    def do_OPTIONS(self):
        # A preflight cannot carry the token's VALUE, so the token check is not
        # performed here - the real request is where it is enforced. The Host and
        # Origin checks still gate the 204.
        if not self._host_allowed():
            self._json({'ok': False, 'error': 'host not permitted'}, 403, cors=False); return
        if not self._origin_allowed():
            self._json({'ok': False, 'error': 'origin not permitted'}, 403, cors=False); return
        self.send_response(204); self._cors(True); self.end_headers()

    def do_GET(self):
        denied = self._auth_gate()
        if denied:
            self._json({'ok': False, 'error': denied}, 403, cors=False); return
        u = urlparse(self.path).path
        if u == '/health':
            self._json({'ok': True, 'daemon': True, 'workdir': STATE['workdir'],
                        'allow_any_origin': ALLOW_ANY_ORIGIN,
                        'allow_file_origin': ALLOW_FILE_ORIGIN,
                        'origins': origins_desc()})
        elif u == '/status':
            running = bool(STATE['proc'] and STATE['proc'].poll() is None)
            self._json({'ok': True, 'workdir': STATE['workdir'],
                        'bridge_running': running,
                        'bridge_pid': STATE['proc'].pid if running else None,
                        'bridge_port': STATE['bridge_port'] if running else None,
                        'started_at': STATE['started_at'],
                        'allow_any_origin': ALLOW_ANY_ORIGIN,
                        'allow_file_origin': ALLOW_FILE_ORIGIN,
                        'origins': origins_desc(),
                        'bridge_healthy': bridge_healthy() if running else False})
        else:
            self._json({'ok': False, 'error': 'unknown route'}, 404)

    def do_POST(self):
        denied = self._auth_gate()
        if denied:
            self._json({'ok': False, 'error': denied}, 403, cors=False); return
        u = urlparse(self.path).path
        # ONE contained read, ONE refusal, ahead of routing. Every POST route below
        # (/pick_directory /set_workdir /start /stop /install_autostart
        # /remove_autostart) went through this single call site, so handling the
        # refusal here covers all of them: 413 outranks the 404 for an unknown route,
        # and a route is never reached by a body that was never contained.
        b, berr = self._body()
        if berr is not None:
            self._json({'ok': False, 'error': berr[1]}, berr[0], cors=False); return
        if u == '/pick_directory':
            p = pick_directory_dialog()
            self._json({'ok': bool(p), 'path': p})
        elif u == '/set_workdir':
            ok, msg = set_workdir(b.get('path'), autostart=bool(b.get('autostart', True)))
            self._json({'ok': ok, 'message': msg, 'workdir': STATE['workdir']}, 200 if ok else 400)
        elif u == '/start':
            ok, msg = start_bridge()
            self._json({'ok': ok, 'message': msg}, 200 if ok else 400)
        elif u == '/stop':
            was_running = bool(STATE['proc'] and STATE['proc'].poll() is None)
            stop_bridge()
            # Idempotent operator control: the stop rite succeeds even if the
            # worker was already severed; was_running preserves the old detail.
            self._json({'ok': True, 'stopped': True, 'was_running': was_running})
        elif u == '/install_autostart':
            self._json({'ok': True, 'result': install_autostart()})
        elif u == '/remove_autostart':
            self._json({'ok': True, 'result': remove_autostart()})
        else:
            self._json({'ok': False, 'error': 'unknown route'}, 404)

def _cleanup():
    stop_bridge()
    try: os.remove(pid_file())
    except OSError: pass

def main():
    import atexit
    global ALLOW_ANY_ORIGIN, ALLOW_FILE_ORIGIN, TOKEN, TOKEN_PATH
    atexit.register(_cleanup)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    ap = argparse.ArgumentParser(description='Cogitator bridge supervisor daemon')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT, help='supervisor port (default: %d)' % DEFAULT_PORT)
    ap.add_argument('--allow-any-origin', action='store_true', help='allow non-local web origins (not recommended)')
    ap.add_argument('--allow-file-origin', action='store_true', help='trust a null Origin (file:// page) — not recommended')
    ap.add_argument('--install-autostart', action='store_true', help='install the login autostart entry and exit')
    ap.add_argument('--remove-autostart', action='store_true', help='remove the login autostart entry and exit')
    global TOKEN, TOKEN_PATH
    a = ap.parse_args()
    ALLOW_ANY_ORIGIN = a.allow_any_origin
    ALLOW_FILE_ORIGIN = a.allow_file_origin
    TOKEN = load_or_create_token(TOKEN_PATH)
    port = a.port
    if a.install_autostart:
        print('autostart:', install_autostart()); return
    if a.remove_autostart:
        print('autostart:', remove_autostart()); return
    single_instance()
    log('daemon listening on http://127.0.0.1:%d' % port)
    log('  origins : %s' % origins_desc())
    # The token itself is never logged - only where to read it from.
    log('  token   : %s  (paste into the UI with: cat "%s")' % (TOKEN_PATH, TOKEN_PATH))
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_bridge()
        try: os.remove(pid_file())
        except OSError: pass
        log('daemon shut down')

if __name__ == '__main__':
    main()
