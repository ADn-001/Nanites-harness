#!/usr/bin/env python3
"""Phase 16 mutation harness: does the jail actually have teeth?

A green suite proves the code is in the state the tests describe. It does not prove the
tests would NOTICE a regression. For a security gate, the second claim is the one that
matters, so each guard is broken on purpose and the suite re-run.

Every mutation asserts it APPLIED before trusting a red: a pattern that matches nothing
records a false pass, which is the most expensive way to spend a verification.

Refuses to score a run whose output lacks the real summary line — a harness that cannot
tell it did not run reports confident nonsense.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Derived from this file's location, never a hard-coded absolute path: this repo is
# shared, and a personal home path in a tracked file is both a leak and wrong on every
# other machine (the convention gen_git_policy.py already follows).
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = [sys.executable, "test_e2e.py"]
JS = ["node", "tests/frontend/phase16_git_policy.test.js"]


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run(cmd):
    p = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout + p.stderr


def scored(cmd, marker):
    """Run a suite and REFUSE to score unless the real summary line is present."""
    rc, out = run(cmd)
    if marker not in out:
        print("    HARNESS ERROR: no %r in output — the run did not happen, "
              "refusing to score it." % marker)
        print("    tail:", out[-400:].replace("\n", " | "))
        return None
    return rc, out


#: (label, file, pattern, replacement, suite, summary marker, expect_red)
MUTATIONS = [
    ("M1 git: drop the out-of-jail flag denial entirely",
     "bridge.py",
     r"    bad = _git_jail_flags\(args\)\n    if bad:\n        raise PermissionError\(\"git flag %r reaches outside the project jail and is never \"\n                              \"permitted \(it would read or write a file the bridge does \"\n                              \"not own\)\" % bad\)\n",
     "",
     PY, "FAILURES", True),

    ("M2 git: drop --no-index from the denied flags (the review's find)",
     "bridge.py",
     r'GIT_JAIL_FLAGS = \("--output", "--output-indicator-new", "--exec-path", "--no-index"\)',
     'GIT_JAIL_FLAGS = ("--output", "--output-indicator-new", "--exec-path")',
     PY, "FAILURES", True),

    ("M2b git: deny only --output, dropping the other three flags",
     "bridge.py",
     r'GIT_JAIL_FLAGS = \("--output", "--output-indicator-new", "--exec-path", "--no-index"\)',
     'GIT_JAIL_FLAGS = ("--output",)',
     PY, "FAILURES", True),

    ("M3 git: match only the exact flag, not the --flag=value form",
     "bridge.py",
     r'            if a == flag or a\.startswith\(flag \+ "="\):',
     '            if a == flag:',
     PY, "FAILURES", True),

    ("M4 git: stop refusing the file-writing subcommands",
     "bridge.py",
     r'    if sub in GIT_REFUSED_WRITE:\n        raise PermissionError\("git %s writes files and is never permitted in the bridge" % sub\)\n',
     "",
     PY, "FAILURES", True),

    ("M4b git: evaluate the generic flag check BEFORE the specific subcommand one",
     "bridge.py",
     r'    if sub in GIT_REFUSED_WRITE:\n        raise PermissionError\("git %s writes files and is never permitted in the bridge" % sub\)\n    bad = _git_jail_flags\(args\)\n    if bad:\n        raise PermissionError\("git flag %r reaches outside the project jail and is never "\n                              "permitted \(it would read or write a file the bridge does "\n                              "not own\)" % bad\)\n',
     '    bad = _git_jail_flags(args)\n    if bad:\n        raise PermissionError("git flag %r reaches outside the project jail and is never "\n                              "permitted (it would read or write a file the bridge does "\n                              "not own)" % bad)\n    if sub in GIT_REFUSED_WRITE:\n        raise PermissionError("git %s writes files and is never permitted in the bridge" % sub)\n',
     PY, "FAILURES", True),

    ("M5 grep: remove the per-file jail check (the CR-0002 leak)",
     "bridge.py",
     r'                if os\.path\.realpath\(fp\) != ROOT and not os\.path\.realpath\(fp\)\.startswith\(ROOT \+ os\.sep\):\n                    skipped \+= 1; continue\n',
     "",
     PY, "FAILURES", True),

    ("M6 grep: remove the explicit symlinked-dirname prune",
     "bridge.py",
     r'        dirnames\[:\] = \[d for d in dirnames\n                       if not os\.path\.islink\(os\.path\.join\(dirpath, d\)\)\]\n',
     "",
     PY, "FAILURES", None),

    ("M6b grep: drop followlinks=False so the walk follows symlinked dirs",
     "bridge.py",
     r"    for dirpath, dirnames, filenames in os\.walk\(p, followlinks=False\):",
     "    for dirpath, dirnames, filenames in os.walk(p, followlinks=True):",
     PY, "FAILURES", True),

    ("M7 frontend: drop the out-of-jail flag check from isReadRite",
     "index.html",
     r"  for\(const a of unquoted\)\{\n    for\(const flag of FRONT_GIT_JAIL_FLAGS\)\{\n      if\(a===flag\|\|a\.startsWith\(flag\+'='\)\)return false;\n    \}\n  \}\n",
     "",
     JS, "PHASE 16 GIT POLICY", True),

    ("M7b frontend: stop stripping quotes, so a quoted flag classifies as a read",
     "index.html",
     r"  const unquoted=parts\.map\(p=>p\.replace\(/\^\"\(\.\*\)\"\$/,'\$1'\)\);",
     "  const unquoted=parts;",
     JS, "PHASE 16 GIT POLICY", True),

    ("M8 frontend: only deny the exact flag, not --flag=value",
     "index.html",
     r"      if\(a===flag\|\|a\.startsWith\(flag\+'='\)\)return false;",
     "      if(a===flag)return false;",
     JS, "PHASE 16 GIT POLICY", True),

    # M9 is a DOCUMENTED EQUIVALENT, not a gap — proved by set disjointness, not assumed.
    # Every member of FRONT_GIT_REFUSED_WRITE is absent from FRONT_GIT_READ and is not
    # one of the special-cased subcommands, so `isReadRite` reaches the final
    # `return FRONT_GIT_READ.has(sub)` and gets false anyway. Removing the explicit check
    # changes no verdict for any reachable input, so no test could distinguish them.
    # The layer is still pinned two ways: M9b (emptying the table IS caught) and the
    # suite's non-overlap assertion, which fires the day the two tables do intersect.
    ("M9 frontend: drop the explicit refusal check (documented equivalent)",
     "index.html",
     r"  if\(FRONT_GIT_REFUSED_WRITE\.has\(sub\)\)return false;\n",
     "",
     JS, "PHASE 16 GIT POLICY", None),

    ("M9b frontend: empty the refusal table the generator fills",
     "index.html",
     r"const FRONT_GIT_REFUSED_WRITE=new Set\(\[[^\]]*\]\);",
     "const FRONT_GIT_REFUSED_WRITE=new Set([]);",
     JS, "PHASE 16 GIT POLICY", True),

    ("M10 policy: let the frontend call every git subcommand a read (0001 re-opened)",
     "index.html",
     r"  return FRONT_GIT_READ\.has\(sub\);",
     "  return true;",
     JS, "PHASE 16 GIT POLICY", True),

    ("M11 policy: let the generated block drift from bridge.py (0009 re-opened)",
     "index.html",
     r"const FRONT_GIT_READ=new Set\(\[[^\]]*\]\);",
     "const FRONT_GIT_READ=new Set(['status','log','cherry-pick']);",
     PY, "FAILURES", True),
]


def main():
    results = []
    for (label, rel, pattern, repl, suite, marker, expect_red) in MUTATIONS:
        path = os.path.join(BASE, rel)
        before_hash = digest(path)
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        mutated, n = re.subn(pattern, repl, original, count=1)
        if n == 0:
            # The anchor no longer exists: either the code is already correct (the fix
            # landed, so this is structural) or the pattern is stale. Never a pass.
            print("  %-62s SKIPPED (pattern matched nothing — stale harness)" % label)
            results.append((label, "stale", None))
            continue
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            applied = digest(path) != before_hash
            if not applied:
                print("  %-62s SKIPPED (patch did not change the file)" % label)
                results.append((label, "no-change", None))
                continue
            scored_result = scored(suite, marker)
            if scored_result is None:
                print("  %-62s HARNESS ERROR (run not scored)" % label)
                results.append((label, "harness-error", None))
                continue
            rc, out = scored_result
            red = rc != 0
            fails = re.findall(r"^FAIL- (.*)$", out, re.M)
            if expect_red is None:
                verdict = "ESCAPED" if not red else "caught"
            else:
                verdict = "caught" if red == expect_red else "UNEXPECTED"
            detail = ""
            if fails:
                detail = " | " + fails[0][:80]
            print("  %-62s %s%s" % (label, verdict, detail))
            results.append((label, verdict, fails))
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            restored = digest(path) == before_hash
            if not restored:
                print("  !! RESTORE FAILED for %s — the tree is dirty!" % rel)
                results.append((label, "restore-failed", None))
                return 2

    print("\n=== mutation summary ===")
    caught = sum(1 for _, v, _ in results if v == "caught")
    escaped = [l for l, v, _ in results if v == "ESCAPED"]
    other = [(l, v) for l, v, _ in results if v not in ("caught", "ESCAPED")]
    print("mutations=%d caught=%d escaped=%d other=%d"
          % (len(results), caught, len(escaped), len(other)))
    for l, v in other:
        print("  %s: %s" % (v, l))
    for l in escaped:
        print("  ESCAPED: %s" % l)
    return 0 if not escaped and not other else 1


if __name__ == "__main__":
    sys.exit(main())
