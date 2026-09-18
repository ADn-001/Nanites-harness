#!/usr/bin/env python3
"""
COGITATOR BRIDGE v2 - local tool executor for the Cogitator frontend.
Run this INSIDE the project directory you want the agent to operate on:

    python bridge.py                     # jail = current dir, port 8931
    python bridge.py --root D:/code/foo  # jail = D:/code/foo
    python bridge.py --port 9000
    python bridge.py --allow-git-write   # permit git add/commit/restore/...
    python bridge.py --allow-exec        # permit run_command (DANGEROUS)
    python bridge.py --allow-any-origin  # disable localhost/null Origin guard

The frontend sends {name, arguments} to POST /tools/execute and gets back
{ok: true, result: "..."} or {ok: false, error: "..."}.
"""
import argparse, json, os, re, shlex, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_READ   = 512 * 1024
MAX_WRITE  = 2 * 1024 * 1024
MAX_GREP   = 200
MAX_GREP_FILE = 1024 * 1024
MAX_SUB_OUT = 40000
MAX_JSON_BYTES = 1024 * 1024

# Directories that are almost never useful agent targets and can be huge.
PRUNE_DIRS = {
    '.git', '.hg', '.svn', '.idea', '.vscode', '__pycache__', 'node_modules',
    'venv', '.venv', 'env', '.env', 'dist', 'build', 'target', 'out',
    'coverage', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.next', '.nuxt',
    'bin', 'obj', 'vendor', 'Pods', 'DerivedData', '.gradle', '.terraform'
}
GIT_READ  = {"status","diff","log","show","branch","ls-files","ls-tree",
             "rev-parse","stash","blame","describe","tag","shortlog","count-objects"}
GIT_WRITE = {"add","commit","restore","checkout","switch","reset","merge",
             "rebase","rm","mv","clean","fetch","pull","push","cherry-pick"}

ROOT = os.path.abspath(os.getcwd())
ALLOW_EXEC = False
ALLOW_GIT_WRITE = False
ALLOW_ANY_ORIGIN = False
PORT = 8931

def jail(path):
    if not path:
        path = "."
    p = path if os.path.isabs(path) else os.path.join(ROOT, path)
    p = os.path.realpath(p)
    if p != ROOT and not p.startswith(ROOT + os.sep):
        raise PermissionError("path escapes project root: %r" % path)
    return p

def _blocked_git_global_options(args):
    """Reject global options that can redirect git outside the jail."""
    blocked_exact = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    blocked_prefixes = (
        "--git-dir=", "--work-tree=", "--exec-path", "--config-env=",
        "--namespace=", "--git-path=", "--attr-source=", "--config-file=",
        "--config=", "-c=", "--template="
    )
    return any(a in blocked_exact or a.startswith(blocked_prefixes) for a in args)

def _git_flag_audit(args):
    """Return the safe read-only subcommand, or raise."""
    sub = args[0]
    rest = args[1:]
    if sub in ("branch", "tag", "stash"):
        safe_branch = {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose",
                       "--list", "--show-current", "--no-color", "--porcelain"}
        safe_branch_prefix = ("--sort=", "--points-at", "--contains", "--merged=")
        safe_tag = {"-l", "--list", "-n", "-n1", "-n2", "-n3", "-n4", "-n5",
                    "-n6", "-n7", "-n8", "-n9", "--no-color"}
        safe_tag_prefix = ("--sort=", "--contains", "--points-at", "--merged", "-n")
        safe_stash = {"list", "show"}
        if sub == "branch":
            if all(a in safe_branch or a.startswith(safe_branch_prefix) for a in rest):
                return sub
        elif sub == "tag":
            if all(a in safe_tag or a.startswith(safe_tag_prefix) for a in rest):
                return sub
        elif sub == "stash":
            if rest and rest[0] in safe_stash and all(not a.startswith('-') or a == '--no-color' for a in rest[1:]):
                return sub
        raise PermissionError("git %s invocation is not read-only-safe; narrow it or enable --allow-git-write" % sub)
    if sub in GIT_READ:
        return sub
    if ALLOW_GIT_WRITE and sub in GIT_WRITE:
        return sub
    raise PermissionError("git subcommand %r not permitted (read-only: %s; start bridge with --allow-git-write for write rites)"
                          % (sub, ", ".join(sorted(GIT_READ))))

def t_read_file(a):
    p = jail(a.get("path", ""))
    if not os.path.isfile(p):
        raise FileNotFoundError("no such file: %s" % a.get("path"))
    if os.path.getsize(p) > MAX_READ:
        raise ValueError("file exceeds %d byte read limit" % MAX_READ)
    with open(p, "rb") as f:
        raw = f.read()
    if b"\x00" in raw[:4096]:
        raise ValueError("binary file - refusing to read")
    return raw.decode("utf-8", errors="replace")

def t_write_file(a):
    p = jail(a.get("path", ""))
    content = a.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content) > MAX_WRITE:
        raise ValueError("content exceeds %d byte write limit" % MAX_WRITE)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return "WROTE %s (%d bytes, %d lines)" % (os.path.relpath(p, ROOT), len(content), content.count("\n") + 1)

def t_list_dir(a):
    p = jail(a.get("path", "."))
    if not os.path.isdir(p):
        raise NotADirectoryError("not a directory: %s" % a.get("path"))
    entries = []
    for name in sorted(os.listdir(p)):
        if name in PRUNE_DIRS:
            tag = "d"
        else:
            fp = os.path.join(p, name)
            try:
                if os.path.isdir(fp):
                    tag = "d"
                elif os.access(fp, os.X_OK):
                    tag = "x"
                else:
                    tag = "f"
            except OSError:
                tag = "?"
        entries.append("%s %s" % (tag, name))
        if len(entries) >= 500:
            entries.append("...[TRUNCATED AT 500 ENTRIES]")
            break
    return "\n".join(entries) if entries else "[EMPTY DIRECTORY]"

def t_grep(a):
    p = jail(a.get("path", "."))
    pattern = a.get("pattern", "")
    if not os.path.isdir(p):
        raise NotADirectoryError("not a directory: %s" % a.get("path"))
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError("invalid regex: %s" % e)
    hits, skipped = [], 0
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > MAX_GREP_FILE:
                    skipped += 1; continue
                with open(fp, "rb") as f:
                    raw = f.read()
                if b"\x00" in raw[:4096]:
                    skipped += 1; continue
                text = raw.decode("utf-8", errors="replace")
            except OSError:
                skipped += 1; continue
            rel = os.path.relpath(fp, ROOT)
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append("%s:%d:%s" % (rel, n, line[:300]))
                    if len(hits) >= MAX_GREP:
                        return "\n".join(hits) + "\n...[TRUNCATED AT %d HITS]" % MAX_GREP
    if skipped:
        hits.append("...[skipped %d oversized/binary files]" % skipped)
    return "\n".join(hits) if hits else "[NO MATCHES]"

def t_git(a):
    args = shlex.split(a.get("args", ""))
    if not args:
        raise ValueError("empty git invocation")
    if _blocked_git_global_options(args):
        raise PermissionError("git global option not permitted (would bypass the project jail)")
    _git_flag_audit(args)
    r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True, text=True,
                       errors="replace", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > MAX_SUB_OUT:
        out = out[:MAX_SUB_OUT] + "\n...[TRUNCATED]"
    if not out.strip():
        out = "[git %s - no output, exit %d]" % (args[0], r.returncode)
    return out

def t_run_command(a):
    if not ALLOW_EXEC:
        raise PermissionError("run_command is disabled - restart bridge with --allow-exec")
    cmd = a.get("command", "")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("empty command")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       errors="replace", timeout=30, shell=True)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > MAX_SUB_OUT:
        out = out[:MAX_SUB_OUT] + "\n...[TRUNCATED]"
    return out.strip() or ("[exit %d, no output]" % r.returncode)

TOOLS = {
    "read_file":  t_read_file,
    "write_file": t_write_file,
    "list_dir":   t_list_dir,
    "grep":       t_grep,
    "git":        t_git,
    "run_command":t_run_command,
}

class Handler(BaseHTTPRequestHandler):
    def _origin_allowed(self):
        if ALLOW_ANY_ORIGIN:
            return True
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True
        low = origin.lower()
        return low.startswith("http://localhost:") or low.startswith("http://127.0.0.1:") or low in ("http://localhost", "http://127.0.0.1")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}); return
        if self.path.rstrip("/").endswith("health"):
            self._json(200, {"ok": True, "root": ROOT, "port": PORT,
                             "allow_exec": ALLOW_EXEC, "allow_git_write": ALLOW_GIT_WRITE,
                             "allow_any_origin": ALLOW_ANY_ORIGIN, "tools": sorted(TOOLS)})
        else:
            self._json(404, {"ok": False, "error": "unknown rite"})
    def do_POST(self):
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}); return
        if not self.path.rstrip("/").endswith("tools/execute"):
            self._json(404, {"ok": False, "error": "unknown rite"}); return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_JSON_BYTES:
                self._json(413, {"ok": False, "error": "request exceeds %d byte limit" % MAX_JSON_BYTES}); return
            req = json.loads(self.rfile.read(n) or b"{}")
            name, args = req.get("name"), req.get("arguments") or {}
            if name not in TOOLS:
                self._json(400, {"ok": False, "error": "unknown tool: %r" % name}); return
            result = TOOLS[name](args)
            self._json(200, {"ok": True, "result": result})
        except PermissionError as e:
            self._json(403, {"ok": False, "error": str(e)})
        except (FileNotFoundError, NotADirectoryError) as e:
            self._json(404, {"ok": False, "error": str(e)})
        except subprocess.TimeoutExpired:
            self._json(408, {"ok": False, "error": "rite timed out"})
        except Exception as e:
            self._json(400, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
    def log_message(self, fmt, *args):
        sys.stderr.write("[BRIDGE] %s\n" % (fmt % args))

def main():
    global ROOT, ALLOW_EXEC, ALLOW_GIT_WRITE, ALLOW_ANY_ORIGIN, PORT
    ap = argparse.ArgumentParser(description="Cogitator tool bridge")
    ap.add_argument("--root", default=os.getcwd(), help="project root jail (default: cwd)")
    ap.add_argument("--port", type=int, default=8931)
    ap.add_argument("--allow-exec", action="store_true", help="enable run_command tool")
    ap.add_argument("--allow-git-write", action="store_true", help="enable write git rites")
    ap.add_argument("--allow-any-origin", action="store_true", help="allow non-local web origins (not recommended)")
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root); ALLOW_EXEC = a.allow_exec
    ALLOW_GIT_WRITE = a.allow_git_write; ALLOW_ANY_ORIGIN = a.allow_any_origin; PORT = a.port
    print("=" * 56)
    print(" COGITATOR BRIDGE v2 — the machine extends into the physical")
    print("   root       : %s" % ROOT)
    print("   port       : %d" % PORT)
    print("   git write  : %s" % ("ENABLED" if ALLOW_GIT_WRITE else "disabled (read-only)"))
    print("   exec       : %s" % ("ENABLED" if ALLOW_EXEC else "disabled"))
    print("   origins    : %s" % ("ANY" if ALLOW_ANY_ORIGIN else "localhost / null only"))
    print("   health     : http://localhost:%d/health" % PORT)
    print("=" * 56)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
