#!/usr/bin/env python3
# bridge_daemon.py — COGITATOR machine-level bridge supervisor.
# Run ONCE per machine:  python bridge_daemon.py   (or --install-autostart, then forget)
# Owns the lifecycle of per-directory bridge.py workers:
#   POST /pick_directory  native OS folder picker
#   POST /set_workdir     stop old worker, scrub old bridge.py (only if we wrote it),
#                         write fresh bridge.py, optionally respawn
#   POST /start | /stop   spawn | kill the worker
#   GET  /status | /health
import json, os, signal, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_PORT   = 8930
BRIDGE_PORT    = 8931
BRIDGE_MARKER  = '# ==== COGITATOR BRIDGE — managed by bridge_daemon.py ===='
MAX_JSON_BYTES = 1 * 1024 * 1024

_here = os.path.dirname(os.path.abspath(__file__))
STATE = {'workdir': None, 'proc': None, 'bridge_port': BRIDGE_PORT, 'started_at': None}
_lock = threading.RLock()

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

def valid_workdir(path):
    return (isinstance(path, str) and os.path.isabs(path)
            and os.path.isdir(path) and os.access(path, os.W_OK))

def stop_bridge():
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
    src = read_bridge_source()
    if BRIDGE_MARKER not in src:
        src = BRIDGE_MARKER + '\n' + src
    dest = os.path.join(workdir, 'bridge.py')
    if os.path.exists(dest):
        try:
            with open(dest, 'r', encoding='utf-8', errors='replace') as f:
                foreign = BRIDGE_MARKER not in f.read(4096)
        except OSError:
            foreign = True
        if foreign:
            bak = dest + '.cogitator-bak'
            n = 1
            while os.path.exists(bak):
                n += 1; bak = dest + '.cogitator-bak%d' % n
            os.replace(dest, bak)
            log('foreign bridge.py in %s backed up to %s (never destroyed)' % (workdir, bak))
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(src)
    log('wrote bridge.py -> ' + dest)
    return dest

def start_bridge():
    if STATE['proc'] and STATE['proc'].poll() is None:
        return False, 'already running'
    if not STATE['workdir']:
        return False, 'no working directory bound'
    bridge_path = os.path.join(STATE['workdir'], 'bridge.py')
    if not os.path.isfile(bridge_path):
        write_bridge(STATE['workdir'])
    argv = [sys.executable, bridge_path]
    if bridge_supports_args(read_bridge_source()):
        argv += ['--root', STATE['workdir'], '--port', str(STATE['bridge_port'])]
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
        return True, 'spawned'
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

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > MAX_JSON_BYTES: return {}
        try: return json.loads(self.rfile.read(n) or b'{}')
        except json.JSONDecodeError: return {}

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path).path
        if u == '/health':
            self._json({'ok': True, 'daemon': True, 'workdir': STATE['workdir']})
        elif u == '/status':
            running = bool(STATE['proc'] and STATE['proc'].poll() is None)
            self._json({'ok': True, 'workdir': STATE['workdir'],
                        'bridge_running': running,
                        'bridge_pid': STATE['proc'].pid if running else None,
                        'bridge_port': STATE['bridge_port'] if running else None,
                        'started_at': STATE['started_at'],
                        'bridge_healthy': bridge_healthy() if running else False})
        else:
            self._json({'ok': False, 'error': 'unknown route'}, 404)

    def do_POST(self):
        u = urlparse(self.path).path
        b = self._body()
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
    atexit.register(_cleanup)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    args = sys.argv[1:]; port = DEFAULT_PORT
    if '--port' in args: port = int(args[args.index('--port') + 1])
    if '--install-autostart' in args:
        print('autostart:', install_autostart()); return
    if '--remove-autostart' in args:
        print('autostart:', remove_autostart()); return
    single_instance()
    log('daemon listening on http://127.0.0.1:%d' % port)
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
