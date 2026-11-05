#!/usr/bin/env python3
"""Mutation harness for Phase 21 (CR-Nanites-harness-0018 / 0007).

A green suite is the INPUT to this step, never the output. This applies each mutation to
sw.js, re-runs the phase-21 suite, and records which assertions noticed.

HOW THE TREE IS PROTECTED — read this before "simplifying" the loop below:

The suite reads sw.js by PATH, so the mutant has to be in the real file for the suite to
see it. This harness therefore writes the mutant to the REAL sw.js, runs the suite, and
restores from an in-memory backup in a `finally`. That is a real risk, not a simulated
one: a SIGKILL or a hard timeout between the write and the restore leaves the repository
holding a mutant, and the trailing hash assertion never runs. Mitigations, in order of
how much they buy:

  * the mutant is written from an in-memory backup, so the restore needs no disk state
    and cannot itself fail halfway;
  * the restore hash is asserted at the end, so a SILENT leak is caught by this tool on
    any run that completes;
  * a run REFUSES TO START if sw.js already contains a mutation marker, so a mutant left
    by a killed run cannot be mutated again and scored as a clean sweep.

An earlier version of this docstring claimed the mutants were written "to a scratch copy
of sw.js ... so the real tree is never left mutated". That was false — the tmpdir copy
was written and never read, while the real file was mutated in place. A later session
would have believed the tool could not dirty the tree. The safety here is real but
partial, and it is described as such.

Every mutation's output is captured to a FILE, never a pipe, and scoring is refused
unless the real summary line is present: a backgrounded or empty run is a missing run,
not a clean sweep.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE = os.path.join(BASE, 'tests', 'frontend', 'phase21_service_worker.test.js')
SW = os.path.join(BASE, 'sw.js')
SUMMARY = 'PHASE 21 SERVICE WORKER:'

# (name, pattern, replacement) -- each must make sw.js WORSE in a specific way.
MUTANTS = [
    ('M1 navigation-fallback-restored (CR-0018 headline: any GET gets the HTML)',
     r'if \(!isNavigation\(e\.request\)\) throw err;',
     '/* mutant: fallback unscoped */'),
    ('M2 isNavigation always true (every request falls back to HTML)',
     r"return request\.mode === 'navigate' \|\| request\.destination === 'document';",
     'return true; /* mutant */'),
    ('M3 isNavigation always false (offline navigation broken too)',
     r"return request\.mode === 'navigate' \|\| request\.destination === 'document';",
     'return false; /* mutant */'),
    ('M4 drop appcore.js from SHELL (CR-0018 part 1)',
     r"'\./index\.html','\./appcore\.js',", r"'./index.html',"),
    ('M5 error responses cached (a 404 body replayed from cache)',
     r'if \(res && res\.ok\) \{', 'if (res) { /* mutant */'),
    ('M6 CACHE back to a hand-maintained literal (CR-0007)',
     r"const CACHE = deriveCacheName\(SHELL, ASSET_DIGESTS\);",
     "const CACHE = 'cogitator-v10'; /* mutant */"),
    ('M7 derive ignores asset contents (name constant unless the SET changes)',
     r"parts\.push\(key \+ ':' \+ String\(\(digests \|\| \{\}\)\[entry\] \|\| digests\?\.\[key\] \|\| ''\)\);",
     "parts.push(key); /* mutant */"),
    # M8 keeps every cache that is NOT the current one and deletes only the current:
    # the inverse of the real bug. (A first form used `filter(() => true)`, which deletes
    # EVERYTHING including the live cache -- and the suite passed it, because the seeded
    # fixture had no current cache to preserve. That escape produced the seeding fix in
    # the suite; these are the mutations the suite can now actually distinguish.)
    ('M8 activate deletes the CURRENT cache and keeps stale ones',
     r'ks\.filter\(k => k !== CACHE\)', 'ks.filter(k => k === CACHE) /* mutant */'),
    ('M8b activate deletes nothing at all (unbounded growth)',
     r'ks\.filter\(k => k !== CACHE\)', 'ks.filter(() => false) /* mutant */'),
    ('M9 intercept cross-origin too (cache poisoning)',
     r'if \(url\.origin !== location\.origin\) return;', '/* mutant */'),
    ('M10 intercept non-GET too',
     r"if \(e\.request\.method !== 'GET'\) return;", '/* mutant */'),
]

# Acceptance control: a handler that rejects EVERYTHING would satisfy every negative in
# the suite if the positive (offline navigation still works) were not asserted. Proving
# it goes red here is what makes the positives load-bearing rather than decorative.
CONTROLS = [
    ('C1 respondWith never called (app offline entirely)',
     r'e\.respondWith\(', 'if (false) e.respondWith('),
]


def run_suite():
    """Run the phase-21 suite, capturing to a temp FILE. Refuse to score a run that did
    not actually happen."""
    tmp = tempfile.mkdtemp(prefix='p21_out_')
    try:
        out = os.path.join(tmp, '_mut_out.txt')
        p = subprocess.run(['node', SUITE], capture_output=True,
                           text=True, cwd=os.path.join(BASE, 'tests', 'frontend'),
                           timeout=300)
        with open(out, 'w') as fh:
            fh.write(p.stdout + '\n' + p.stderr)
        with open(out, encoding='utf-8') as fh:
            captured = fh.read()
        if SUMMARY not in p.stdout:
            return None, captured[-800:]
        m = re.search(re.escape(SUMMARY) + r'\s*(\d+) FAILURES', p.stdout)
        if m is None:
            return None, captured[-800:]
        return int(m.group(1)), p.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mutate_src(src, pat, rep):
    new, n = re.subn(pat, rep, src, count=1)
    return new, n


def main():
    original = open(SW, encoding='utf-8').read()
    orig_hash = hashlib.sha256(original.encode()).hexdigest()
    results = []

    for label, pat, rep in MUTANTS + CONTROLS:
        mutated, n = mutate_src(original, pat, rep)
        if n != 1:
            print('SKIP  %-62s (pattern matched %d times -- mutation never applied)' % (label, n))
            results.append((label, 'NOT-APPLIED', None))
            continue
        if mutated == original:
            print('SKIP  %-62s (equivalent: file unchanged)' % label)
            results.append((label, 'EQUIVALENT', None))
            continue
        # Refuse to run at all if sw.js still carries a LEAKED MUTANT from a previous run
        # that was killed before its restore. Mutating a mutant would score nonsense
        # while looking like a clean sweep. Deliberately NOT a "differs from HEAD"
        # check: this harness legitimately runs against an uncommitted sw.js (the phase
        # under test is itself uncommitted), so that comparison would block every run.
        if 'mutant' in original:
            print('REFUSING TO RUN: sw.js still contains a mutation marker.')
            print('  A previous mutation run was killed before its restore.')
            print('  Inspect it, then: git checkout -- sw.js  (or re-apply your work)')
            return 2

        # The suite reads sw.js by path, so the mutant must be in the real file. Write it,
        # run, and restore from the in-memory `original` in a finally.
        try:
            with open(SW, 'w', encoding='utf-8') as fh:
                fh.write(mutated)
            fails, raw = run_suite()
        finally:
            with open(SW, 'w', encoding='utf-8') as fh:
                fh.write(original)
        if fails is None:
            print('SKIP  %-62s (harness refused to score: no summary line)' % label)
            results.append((label, 'NO-RUN', None))
            continue
        verdict = 'CAUGHT' if fails > 0 else 'ESCAPED'
        print('%-6s %-62s (%d failures)' % (verdict, label, fails))
        results.append((label, verdict, fails))

    # The restore must be EXACT. A failed restore would mean every later result
    # describes a file nobody intended.
    now = open(SW, encoding='utf-8').read()
    assert hashlib.sha256(now.encode()).hexdigest() == orig_hash, \
        'RESTORE FAILED: sw.js is not back to its original bytes'
    print('\nrestore verified: sw.js sha256 unchanged (%s)' % orig_hash[:16])

    caught = [r for r in results if r[1] == 'CAUGHT']
    escaped = [r for r in results if r[1] == 'ESCAPED']
    other = [r for r in results if r[1] not in ('CAUGHT', 'ESCAPED')]
    print('\n%d mutations, %d caught, %d escaped, %d not-applied/equivalent'
          % (len(results), len(caught), len(escaped), len(other)))
    for lbl, v, f in escaped + other:
        print('  %s: %s' % (v, lbl))
    return 1 if escaped else 0


if __name__ == '__main__':
    sys.exit(main())