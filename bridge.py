#!/usr/bin/env python3
"""
COGITATOR BRIDGE v2 - local tool executor for the Cogitator frontend.
Run this INSIDE the project directory you want the agent to operate on:

    python bridge.py                     # jail = current dir, port 8931
    python bridge.py --root D:/code/foo  # jail = D:/code/foo
    python bridge.py --port 9000
    python bridge.py --allow-git-write   # permit git add/commit/restore/...
    python bridge.py --allow-exec        # permit run_command (DANGEROUS)
    python bridge.py --allow-any-origin  # disable the Origin guard entirely
    python bridge.py --allow-file-origin # trust a null Origin (file:// page)
    python bridge.py --token-file PATH    # use an existing install token (daemon-spawned)

The frontend sends {name, arguments} to POST /tools/execute and gets back
{ok: true, result: "..."} or {ok: false, error: "..."}.
"""
import argparse, hmac, json, os, re, secrets, shlex, subprocess, sys
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
#: Subcommands that are *not* in GIT_READ but still write files, so they are refused
#: by name rather than falling through to the generic "not permitted" error. Listing
#: them makes the refusal legible in the log AND stops a future edit that adds one of
#: them to GIT_READ from silently reopening the hole (CR-Nanites-harness-0001).
GIT_REFUSED_WRITE = {"format-patch", "archive", "bundle", "fast-export", "worktree"}
#: **Flags that take a filesystem path OUTSIDE the repo, denied on EVERY subcommand.**
#:
#: `--output=<file>` redirects stdout to an arbitrary path, so `git log --output=/tmp/x`
#: wrote outside ROOT where `jail()` never sees it (CR-Nanites-harness-0001, critical).
#:
#: `--no-index` is the same class of escape on the READ side and was found by this phase's
#: independent review: it makes `diff` compare two ARBITRARY paths instead of repo
#: contents, so `git diff --no-index /etc/passwd /dev/null` returns the contents of a file
#: outside ROOT. It was permitted here AND classified a read rite by the frontend, so it
#: auto-ran with no operator prompt. Denying the flag is what closes it in both layers.
#:
#: **Why a deny-list and not the allow-list 0001 also suggested.** Inverting to "allow-list
#: the flags each read subcommand may take" is the stronger shape and is right for
#: branch/tag/stash, which this module already does for them. It is not usable for the
#: log/diff/show family: those carry a large and legitimate surface (`--oneline`, `-n`,
#: `--since`, `--author`, `-p`, `--stat`, `--no-color`, path arguments, revision ranges),
#: and an allow-list that omitted one would break ordinary use — a security fix that breaks
#: the tool gets worked around, which is strictly worse than the bug. So the enumerated
#: per-subcommand allow-lists stay for the three subcommands that had them, and the
#: out-of-jail flags are denied uniformly, which is the whole of the escape surface.
GIT_JAIL_FLAGS = ("--output", "--output-indicator-new", "--exec-path", "--no-index")

#: The Host header must name loopback. The whole value is split on the last
#: colon and the NAME must equal one of these exactly, so neither
#: "localhost.evil.example" nor "127.0.0.1.evil.example" can pass as
#: "localhost". A missing Host is refused: HTTP/1.1 requires one, so its
#: absence means a crafted request rather than a browser. The bracketed IPv6
#: literal "[::1]" is handled separately in _host_allowed, because it carries
#: its port inside the brackets and would not survive a naive split on ":".
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


ROOT = os.path.abspath(os.getcwd())
ALLOW_EXEC = False
ALLOW_GIT_WRITE = False
ALLOW_ANY_ORIGIN = False
ALLOW_FILE_ORIGIN = False
PORT = 8931
TOKEN = ''
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cogitator-token')


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

def origins_desc():
    if ALLOW_ANY_ORIGIN:
        return "ANY"
    if ALLOW_FILE_ORIGIN:
        return "localhost / null (file:// trusted)"
    return "localhost only (null refused)"

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

def _git_jail_flags(args):
    """Reject flags that take a path outside the repo, on ANY subcommand.

    Matched on `flag` or `flag=` prefix so both `--output=/tmp/x` and a bare
    `--output /tmp/x` are caught (CR-Nanites-harness-0001, and the `--no-index`
    read escape found by this phase's review).
    """
    for a in args:
        for flag in GIT_JAIL_FLAGS:
            if a == flag or a.startswith(flag + "="):
                return flag
    return None

def _git_flag_audit(args):
    """Return the safe read-only subcommand, or raise."""
    sub = args[0]
    rest = args[1:]
    # **Most specific refusal first.** `git archive --output=x` trips BOTH checks, and the
    # subcommand is the more specific of the two — evaluating the flag first made the
    # generic message shadow it, so the refusal stopped naming what it refused. Both
    # orderings are safe (each check is independently sufficient); this one is legible.
    if sub in GIT_REFUSED_WRITE:
        raise PermissionError("git %s writes files and is never permitted in the bridge" % sub)
    bad = _git_jail_flags(args)
    if bad:
        raise PermissionError("git flag %r reaches outside the project jail and is never "
                              "permitted (it would read or write a file the bridge does "
                              "not own)" % bad)
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
    for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        # A symlinked DIRECTORY is pruned EXPLICITLY rather than relied upon as an
        # `os.walk(followlinks=False)` implementation detail (CR-Nanites-harness-0002).
        # The default happens to be False today; a future edit that flipped the kwarg
        # would otherwise turn grep into a full-tree walk out of the jail, silently.
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                # **Per-file jail check.** The directory argument was jailed once at the
                # top, which does nothing for the files found inside it: a symlinked FILE
                # pointing outside ROOT was opened and its contents returned. `jail()`
                # realpaths, so this refuses the symlink exactly as `t_read_file` does.
                # Skipped rather than raised — one bad file must not fail the whole grep.
                if os.path.realpath(fp) != ROOT and not os.path.realpath(fp).startswith(ROOT + os.sep):
                    skipped += 1; continue
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
    def _host_allowed(self):
        """The Host header must name loopback, with or without a port."""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            # HTTP/1.1 requires a Host; its absence means a crafted request, not a browser.
            return False
        if host.startswith("["):
            # Bracketed IPv6 literal: [::1] or [::1]:PORT, and nothing else.
            if host == "[::1]":
                return True
            return host.startswith("[::1]:") and host[6:].isdigit()
        name, sep, port = host.partition(":")
        if name not in LOOPBACK_HOSTS:
            return False
        return not sep or port.isdigit()

    def _token_ok(self):
        """Constant-time compare of X-Cogitator-Token against the per-install token.

        Compare the ASCII-safe form, not the raw header. hmac.compare_digest
        raises TypeError on any str containing a byte >= 0x80, and http.server
        decodes header bytes as latin-1, so one such byte in the token crashed
        the gate: the request was not served, but the client got NO RESPONSE at
        all instead of a 403. A malformed token must be REFUSED, not fatal.
        """
        sent = self.headers.get("X-Cogitator-Token") or ""
        if not sent or not TOKEN:
            return False
        try:
            return hmac.compare_digest(sent.encode("utf-8"), TOKEN.encode("utf-8"))
        except (UnicodeError, TypeError):
            return False

    def _origin_allowed(self):
        if ALLOW_ANY_ORIGIN:
            return True
        origin = self.headers.get("Origin")
        if not origin:
            # Absent Origin on a PRIVILEGED route is refused (CR-Nanites-harness-0017):
            # only curl and native callers send none, and they must carry the token.
            # Read-only routes keep treating it as allowed, so the UI's status chip
            # works before the operator has paired.
            return not self._privileged()
        if origin == "null":
            # A browser sends "null" for a sandboxed iframe, a data:/blob:
            # document, or a file:// page — i.e. any hostile page can get an
            # opaque origin. Trust it only behind the explicit opt-in.
            return ALLOW_FILE_ORIGIN
        low = origin.lower()
        return low.startswith("http://localhost:") or low.startswith("http://127.0.0.1:") or low in ("http://localhost", "http://127.0.0.1")

    def _privileged(self):
        """POST /tools/execute is the only privileged route on the bridge worker."""
        return (self.command == "POST"
                and self.path.rstrip("/").endswith("tools/execute"))

    def _auth_gate(self):
        """Return an error string when the request must be refused, else None.

        Order is Host, then token, then Origin, and the three are ANDed on a
        privileged route: a correct token never buys a past a foreign Origin and
        a local Origin never buys a past a missing token.
        """
        if not self._host_allowed():
            return "host not permitted"
        if self._privileged() and not self._token_ok():
            return "X-Cogitator-Token required"
        if not self._origin_allowed():
            return "origin not permitted"
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
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed():
                self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Cogitator-Token")
    def _json(self, code, obj, cors=True):
        body = json.dumps(obj).encode()
        self.send_response(code); self._cors(cors)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def _read_body(self):
        """Read the JSON body as a BOUND, CONTAINED read (CR-Nanites-harness-0021).

        Returns (obj, err). `err` is None, or a (code, message) pair the caller
        answers with verbatim. ONE shape, shared with bridge_daemon.py.

        Why each refusal is what it is:

        * Content-Length absent, non-numeric or <= 0 => 400. `int()` used to be
          unwrapped, so "abc" raised ValueError and was only ever caught by the
          broad `except Exception` at the bottom of do_POST — a 400 whose body was
          the Python dump, i.e. a refusal for the wrong reason. And n <= 0 was
          accepted at all: rfile.read(-1) reads UNTIL EOF, an unbounded read that is
          precisely what MAX_JSON_BYTES exists to prevent (it also parsed and
          EXECUTED the body), while read(0) was coerced to {} and dispatched with an
          empty tool name.
        * Content-Length > MAX_JSON_BYTES => 413. Too large, and 413 outranks the 404
          so an oversized body can never be routed.
        * The READ itself is capped at MAX_JSON_BYTES bytes, and anything actually
          delivered beyond that is 413. Capping only the DECLARED number leaves the
          read itself trusting the client; this is the read-bound.
        * Not valid JSON => 400, and valid JSON that is not an object => 400. Both
          used to reach `.get()` and raise AttributeError behind a 400.

        The length check happens BEFORE the read, so an over-limit body is never
        buffered at all.
        """
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not str(raw_len).strip():
            return None, (400, "request body required: Content-Length missing")
        try:
            n = int(str(raw_len).strip())
        except (TypeError, ValueError):
            return None, (400, "invalid Content-Length header")
        if n <= 0:
            return None, (400, "invalid Content-Length header")
        if n > MAX_JSON_BYTES:
            return None, (413, "request exceeds %d byte limit" % MAX_JSON_BYTES)
        try:
            raw = self.rfile.read(min(n, MAX_JSON_BYTES + 1))
        except Exception:
            return None, (400, "could not read request body")
        if raw is None or len(raw) > MAX_JSON_BYTES:
            return None, (413, "request exceeds %d byte limit" % MAX_JSON_BYTES)
        if not raw.strip():
            return None, (400, "request body required: empty body")
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return None, (400, "invalid JSON body")
        if not isinstance(obj, dict):
            return None, (400, "request body must be a JSON object")
        return obj, None
    def do_OPTIONS(self):
        # A preflight cannot carry the token's VALUE, so the token check is not
        # performed here - the real request is where it is enforced. The Host and
        # Origin checks still gate the 204.
        if not self._host_allowed():
            self._json(403, {"ok": False, "error": "host not permitted"}, cors=False); return
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin not permitted"}, cors=False); return
        self.send_response(204); self._cors(True); self.end_headers()
    def do_GET(self):
        denied = self._auth_gate()
        if denied:
            self._json(403, {"ok": False, "error": denied}, cors=False); return
        if self.path.rstrip("/").endswith("health"):
            self._json(200, {"ok": True, "root": ROOT, "port": PORT,
                             "allow_exec": ALLOW_EXEC, "allow_git_write": ALLOW_GIT_WRITE,
                             "allow_any_origin": ALLOW_ANY_ORIGIN, "allow_file_origin": ALLOW_FILE_ORIGIN,
                             "origins": origins_desc(), "tools": sorted(TOOLS)})
        else:
            self._json(404, {"ok": False, "error": "unknown rite"})
    def do_POST(self):
        denied = self._auth_gate()
        if denied:
            self._json(403, {"ok": False, "error": denied}, cors=False); return
        if not self.path.rstrip("/").endswith("tools/execute"):
            self._json(404, {"ok": False, "error": "unknown rite"}); return
        try:
            req, err = self._read_body()
            if err is not None:
                self._json(err[0], {"ok": False, "error": err[1]}, cors=False); return
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
    global ROOT, ALLOW_EXEC, ALLOW_GIT_WRITE, ALLOW_ANY_ORIGIN, ALLOW_FILE_ORIGIN, PORT
    global TOKEN, TOKEN_PATH
    ap = argparse.ArgumentParser(description="Cogitator tool bridge")
    ap.add_argument("--root", default=os.getcwd(), help="project root jail (default: cwd)")
    ap.add_argument("--port", type=int, default=8931)
    ap.add_argument("--allow-exec", action="store_true", help="enable run_command tool")
    ap.add_argument("--allow-git-write", action="store_true", help="enable write git rites")
    ap.add_argument("--allow-any-origin", action="store_true", help="allow non-local web origins (not recommended)")
    ap.add_argument("--allow-file-origin", action="store_true", help="trust a null Origin (file:// page) — not recommended")
    ap.add_argument("--token-file", default=None,
                    help="path to the per-install token file (default: <this dir>/.cogitator-token)")
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root); ALLOW_EXEC = a.allow_exec
    ALLOW_GIT_WRITE = a.allow_git_write; ALLOW_ANY_ORIGIN = a.allow_any_origin
    ALLOW_FILE_ORIGIN = a.allow_file_origin; PORT = a.port
    TOKEN_PATH = os.path.abspath(a.token_file) if a.token_file else TOKEN_PATH
    TOKEN = load_or_create_token(TOKEN_PATH)
    print("=" * 56)
    print(" COGITATOR BRIDGE v2 — the machine extends into the physical")
    print("   root       : %s" % ROOT)
    print("   port       : %d" % PORT)
    print("   git write  : %s" % ("ENABLED" if ALLOW_GIT_WRITE else "disabled (read-only)"))
    print("   exec       : %s" % ("ENABLED" if ALLOW_EXEC else "disabled"))
    print("   origins    : %s" % origins_desc())
    print("   token file : %s" % TOKEN_PATH)
    print("   paste      : cat \"%s\"" % TOKEN_PATH)
    print("   health     : http://localhost:%d/health" % PORT)
    print("=" * 56)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
