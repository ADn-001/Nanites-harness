#!/usr/bin/env python3
"""Phase 18 mutation harness: do the sidecar concurrency guards have teeth?

A green suite proves the code is in the state the tests describe. It does not prove the
tests would NOTICE a regression. Each guard is broken on purpose and the suite re-run.

Every mutation asserts it APPLIED before trusting a red: a pattern that matches nothing
records a false pass, which is the most expensive way to spend a verification.

Refuses to score a run whose output lacks the real summary line - a harness that cannot
tell it did not run reports confident nonsense.

SCORING IS PHASE-18-ONLY, deliberately. `test_e2e.py` has a KNOWN PRE-EXISTING FLAKE at
line 210 (the bridge 413 case: the server answers 413 without draining the body, so the
client can get a RST instead of a response). Measured on an unmodified checkout of HEAD,
it aborts roughly half of all runs BEFORE the phase-18 block is ever reached. Scoring the
whole suite would therefore report that flake as a "caught" mutation, which would be a
lie. So a run counts as red only when a `localmodels (phase18)` case actually FAILS, and
a run that never reached the block is retried rather than scored.

Run it after touching _LOCK, _BoundedModelPool, submit_model_call, the timeout paths, or
the child generation: `python3 tools/mutate_phase18.py`
"""
import hashlib
import os
import re
import subprocess
import sys

# Derived from this file's location, never a hard-coded absolute path: this repo is
# shared, and a personal home path in a tracked file is both a leak and wrong on every
# other machine (the convention gen_git_policy.py already follows).
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON = "localmodels/local_models_daemon.py"
BACKEND = "localmodels/needle_backend.py"
SUITE = [sys.executable, "test_e2e.py"]
PHASE = "localmodels (phase18)"
SUMMARY = "FAILURES"
RUN_TIMEOUT = 900
# The line-210 flake aborts a run before phase 18 is reached. Retry rather than score.
ATTEMPTS = 6


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run(cmd):
    p = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    return p.returncode, p.stdout + p.stderr


def scored_phase18(prefix=PHASE):
    """Run the suite; return (reached_block, failing messages for `prefix`) or None.

    Two distinct "cannot score" conditions, and they must not be confused:
      - the run produced no summary line AND no traceback -> the suite genuinely did
        not run (wrong cwd, interpreter error). Refuse.
      - the run raised part-way (the line-210 flake) -> it aborted BEFORE reaching the
        phase block. Retry; scoring it as "green" would be reporting coverage that
        does not exist, and scoring it as red would blame a pre-existing bug on a
        mutation.
    """
    for attempt in range(ATTEMPTS):
        rc, out = run(SUITE)
        crashed = 'Traceback (most recent call last)' in out
        if SUMMARY not in out:
            if crashed or prefix not in out:
                continue                       # aborted early (known flake) -> retry
            print("    HARNESS ERROR: no %r line and no traceback - the run did not "
                  "happen, refusing to score it." % SUMMARY)
            print("    tail:", out[-400:].replace("\n", " | "))
            return None
        fails = re.findall(r"^FAIL- (.*)$", out, re.M)
        mine = [f for f in fails if f.startswith(prefix)]
        if mine:
            return True, mine
        if prefix in out:
            return True, []          # reached the block, nothing red
        print("    NOTE: run reached the summary but printed no %r case at all "
              "(attempt %d) - treating as NOT SCORED." % (prefix, attempt + 1))
        continue
    print("    HARNESS ERROR: never got a scored run in %d attempts (known flake at "
          "test_e2e.py:210); refusing to score." % ATTEMPTS)
    return None


#: (label, file, pattern, replacement, expect_red)
#: expect_red None => documented equivalent (proved unobservable, NOT a gap).
#:
#: SCORED_PREFIX says which cases may mark a mutation red. It defaults to the phase-18
#: cases; a mutation whose only pin lives in an EARLIER phase's suite must name that
#: suite, or it gets scored against a file that never asserted it and is reported as a
#: hole that does not exist (the phase-17 harness bug).
SCORED_PREFIX = {
    "M12 _write: never re-stamp the request onto the child that received it "
    "(phase-13 child_gone regresses)": "localmodels (phase13)",
}
MUTATIONS = [
    # ---- CR-0010: the model lock must be reentrant ----
    ("M1 _LOCK: plain Lock again (the 0010 deadlock returns)",
     BACKEND, r"_LOCK = threading\.RLock\(\)", "_LOCK = threading.Lock()", True),

    # DOCUMENTED EQUIVALENT, proved by reading both call sites rather than assumed:
    # `repair()` and `load()` BOTH hold _LOCK across their call to _build(), so once the
    # lock is reentrant the inner `with _LOCK:` in _build is a no-op - the same thread
    # already holds it. Dropping it changes no observable behaviour. It is defence in
    # depth against a future caller that reaches _build WITHOUT the lock, so keep it.
    ("M2 _build: drop the inner tuned-weights acquire (documented equivalent - "
     "repair/load already hold the now-reentrant lock)",
     BACKEND,
     r"            # Tuned weights spawn a FineTuneWorker; build it under the call lock so no\n"
     r"            # in-flight repair can use a half-built agent\.\n"
     r"            with _LOCK:\n"
     r"                build\(\)",
     "            build()", None),

    # ---- CR-0019: the pool must be bounded and must cancel on timeout ----
    ("M3 pool: admission bound never trips (unbounded queue again)",
     DAEMON,
     r"            if self\._admitted >= self\._max_queue:",
     "            if False:", True),

    ("M4 _repair_call: stop cancelling the work item on timeout",
     DAEMON,
     r"        # behind it\. cancel\(\) is False when the call is already RUNNING; that is\n"
     r"        # deliberate and is why the queue is bounded\.\n"
     r"        future\.cancel\(\)\n",
     "", True),

    ("M5 _select_call: stop cancelling the work item on timeout",
     DAEMON,
     r"    except TimeoutError:\n        future\.cancel\(\)\n"
     r"        if getattr\(BACKEND, '_tools_key', None\) is None:",
     "    except TimeoutError:\n        if getattr(BACKEND, '_tools_key', None) is None:", True),

    ("M6 _repair_call: bypass the bounded admission (straight to the executor)",
     DAEMON,
     r"        future = submit_model_call\(BACKEND\.repair, text, candidates\)\n"
     r"    except ModelBusy:",
     "        future = MODEL_POOL._pool.submit(BACKEND.repair, text, candidates)\n"
     "    except ModelBusy:", True),

    # ---- CR-0020: a dying child must not fail the live child's work ----
    ("M7 _register: stamp every request with generation 0 (the fence can never match a "
     "real child)",
     DAEMON, r"            self\._futures\[rid\] = \(self\._generation, fut\)",
     "            self._futures[rid] = (0, fut)", True),

    ("M8 _spawn_locked: generation never advances (every child looks current)",
     DAEMON, r"        self\._generation \+= 1", "        self._generation = 1", True),

    ("M9 _spawn_locked: stdout reader not told which child it belongs to",
     DAEMON,
     r"        threading\.Thread\(target=self\._read_stdout, args=\(proc, gen\), daemon=True,",
     "        threading.Thread(target=self._read_stdout, args=(proc, 0), daemon=True,", True),

    ("M10 _fail_pending: ignore the generation and fail EVERY pending request "
     "(the CR-0020 corruption)",
     DAEMON,
     r"            doomed = \[fut for \(gen, fut\) in self\._futures\.values\(\) "
     r"if gen == generation\]",
     "            doomed = [fut for (gen, fut) in self._futures.values()]", True),

    ("M11 _fail_pending: generation argument optional again (unscoped teardown returns)",
     DAEMON, r"    def _fail_pending\(self, exc, generation\):",
     "    def _fail_pending(self, exc, generation=None):", True),

    # M12 is the regression this phase nearly shipped: without the re-stamp, a request
    # is stamped with the generation current at REGISTRATION time, which is the previous
    # child whenever _write has to spawn. The dying child's teardown then misses the very
    # request it was serving and the caller waits out its full timeout. The phase-13
    # `child_gone` case is what catches it - a phase-18-only suite would NOT have.
    #
    # The anchor must name the ARGUMENT the fixed code passes (`gen`), not the local the
    # pre-fix version had. A pattern that matches nothing is scored `no-change`, which
    # reads like a pass and is not one - see assert_clean_tree() below.
    ("M12 _write: never re-stamp the request onto the child that received it "
     "(phase-13 child_gone regresses)",
     DAEMON,
     r"        # The bytes are with THAT child now - bind the request to it\.\n"
     r"        self\._stamp\(rid, gen\)",
     "        pass", True),

    # ---- the four defects an independent review of this diff found ----
    # None of these were reachable by M1-M12: every one of them needs a case that either
    # leaves NO request in flight, or reorders a respawn against a stamp. The harness
    # scoring 11/12 green while all four were live is the reason they are listed here.

    ("M13 _fail_pending: only clear `loaded` when the dying child HAD requests (a child "
     "that dies between requests stays loaded=True forever)",
     DAEMON,
     r"            is_current = \(generation == self\._generation\)\n"
     r"            if is_current:\n"
     r"                self\.loaded = False\n",
     "            pass\n", True),

    ("M14 _fail_pending: clear `loaded` unconditionally (a DYING child clears the LIVE "
     "child's flag - CR-0020 half-open)",
     DAEMON,
     r"            is_current = \(generation == self\._generation\)\n"
     r"            if is_current:\n"
     r"                self\.loaded = False\n",
     "            self.loaded = False\n", True),

    ("M15 _stamp: read self._generation at stamp time instead of using the receiving "
     "child's identity (a respawn between the write and the stamp re-opens the leak)",
     DAEMON,
     r"                self\._futures\[rid\] = \(generation, entry\[1\]\)",
     "                self._futures[rid] = (self._generation, entry[1])", True),

    ("M16 _take_future: pop `_futures` without the lock, so the stdout reader can "
     "interleave with the drain's iteration",
     DAEMON,
     r"        with self\._fut_lock:\n"
     r"            entry = self\._futures\.pop\(rid, None\)\n"
     r"        return entry\[1\] if entry is not None else None",
     "        entry = self._futures.pop(rid, None)\n"
     "        return entry[1] if entry is not None else None", True),
]


def assert_clean_tree():
    """Refuse to score if the files this harness edits are not in their committed state.

    Learned the hard way on this phase. A previous invocation of this harness was
    killed by an outer tool timeout while M15's mutation was LIVE on disk. The next
    invocation inherited that dirty file, so its own M15 pattern matched nothing, scored
    `no-change` - which reads like a pass - and every result it reported after that was
    measured against a tree that was not the real one. It also made a documented
    equivalent (M2) go red for reasons that had nothing to do with M2.

    So the harness now refuses to start unless the two files it mutates match git HEAD.
    The state it needs is exactly "what is committed", so HEAD is the right oracle - and
    the check is cheap. Run `git stash`/`git checkout` to resolve, then re-run.
    """
    dirty = []
    for rel in (DAEMON, BACKEND):
        p = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel], cwd=BASE)
        if p.returncode != 0:
            dirty.append(rel)
    if dirty:
        print("REFUSING TO SCORE: %s differ from git HEAD." % ", ".join(dirty))
        print("This harness mutates those files in place; scoring a dirty tree measures")
        print("someone else's edit (or a leaked mutation from a killed run), not the")
        print("guards. Commit or revert them first:")
        print("    git stash push -- %s" % " ".join(dirty))
        return False
    return True


def main():
    print("phase 18 mutation harness (scored on phase-18 cases only)")
    if not assert_clean_tree():
        return 2
    results = []
    for label, rel, pattern, repl, expect_red in MUTATIONS:
        path = os.path.join(BASE, rel)
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        before_hash = digest(path)
        mutated, n = re.subn(pattern, repl, original)
        if n == 0 or mutated == original:
            print("  %-70s SKIPPED (patch did not change the file)" % label)
            results.append((label, "no-change", None))
            continue
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            # Prove the mutation is live rather than trusting the write.
            if digest(path) == before_hash:
                print("  %-70s SKIPPED (mutation did not change the file on disk)" % label)
                results.append((label, "no-change", None))
                continue
            scored = scored_phase18(SCORED_PREFIX.get(label, PHASE))
            if scored is None:
                print("  %-70s HARNESS ERROR (run not scored)" % label)
                results.append((label, "harness-error", None))
                continue
            _reached, fails = scored
            red = bool(fails)
            if expect_red is None:
                verdict = "equivalent" if not red else "UNEXPECTED-RED"
            else:
                verdict = "caught" if red == expect_red else "UNEXPECTED"
            detail = (" | " + fails[0][:70]) if fails else ""
            print("  %-70s %s%s" % (label, verdict, detail))
            results.append((label, verdict, fails))
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            if digest(path) != before_hash:
                print("  !! RESTORE FAILED for %s - the tree is dirty!" % rel)
                results.append((label, "restore-failed", None))
                return 2

    print("\n=== mutation summary ===")
    caught = sum(1 for _, v, _ in results if v == "caught")
    escaped = [l for l, v, _ in results if v == "UNEXPECTED"]
    equivalents = [l for l, v, _ in results if v == "equivalent"]
    other = [(l, v) for l, v, _ in results
             if v not in ("caught", "UNEXPECTED", "equivalent")]
    print("mutations=%d caught=%d escaped=%d equivalent=%d other=%d"
          % (len(results), caught, len(escaped), len(equivalents), len(other)))
    for l, v in other:
        print("  %s: %s" % (v, l))
    for l in escaped:
        print("  ESCAPED: %s" % l)
    if equivalents:
        print("  documented equivalents (unobservable, NOT gaps): %d" % len(equivalents))
        for l in equivalents:
            print("    - %s" % l)
    return 0 if not escaped and not other else 1


if __name__ == "__main__":
    sys.exit(main())