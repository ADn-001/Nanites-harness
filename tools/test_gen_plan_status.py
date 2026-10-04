#!/usr/bin/env python3
"""Cheap CI guard: the plan doc's status/inventory blocks must match reality.

CR-Nanites-harness-0027: `docs/plans/needle-laya-middleware-plan.md` said
`**Status:** planned (not started)` in its header while the SAME file said
`**Status: DONE** (2026-09-26)` 440 lines lower and `gatelog.md` recorded phases
10-15 all DONE. Its audience is a context-free session, so the header won and a
finished project looked unstarted. The file-size table in §1 was stale the same
way (claimed `index.html 1356` against a real 2287).

`tools/gen_plan_status.py` now GENERATES the header status block, one status
block per phase 10-15, and the inventory table's line counts from `gatelog.md`
and the real files, so the doc cannot claim a status the gatelog does not
record. This module is the guard that makes the generator's `--check` cheap to
run on every change.

The negative controls are the point: they prove the guard BITES. A test that
only ever sees a green tree proves nothing, so each control drives the REAL
generator against a throwaway copy of the doc + gatelog, breaks exactly one
thing, and asserts `--check` exits non-zero AND names the offending phase/file in
its output. The real working tree is never written to; it is hashed before and
after and asserted byte-identical.

Run:
    python3 tools/test_gen_plan_status.py
    python3 tools/test_gen_plan_status.py -v

Env overrides (used by the negative controls and for testing against a copy —
they never change the real tree):
    GEN_PLAN_STATUS_ROOT     repo root that inventory files are measured in
    GEN_PLAN_STATUS_PLAN    plan doc path
    GEN_PLAN_STATUS_GATELOG gatelog path
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("GEN_PLAN_STATUS_ROOT") or os.path.dirname(HERE)
PLAN = os.environ.get("GEN_PLAN_STATUS_PLAN") or os.path.join(
    BASE, "docs", "plans", "needle-laya-middleware-plan.md")
GATELOG = os.environ.get("GEN_PLAN_STATUS_GATELOG") or os.path.join(BASE, "gatelog.md")
GEN = os.path.join(HERE, "gen_plan_status.py")

#: The Local Cortex phases the plan doc covers. Matches the generator's own set,
#: which derives it from the `### Phase N —` headings actually present in the doc.
PHASES = [10, 11, 12, 13, 14, 15]

#: Never edited in a tracked file.
STALE_WORDING = "planned (not started)"

HEADING_RE = re.compile(r"^### Phase (\d+) — ")
GATELOG_PHASE_RE = re.compile(r"^## Phase (\d+) — ")
STATUS_RE = re.compile(r"^Status:\s*(.*)$")
ISO_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")

#: Every file whose existence must be proven before/after the negative controls.
WATCHED = [PLAN, GATELOG]


def read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


class Case(unittest.TestCase):
    """assertTrue-based membership assertions.

    `assertIn(needle, haystack)` prints the WHOLE haystack when it fails, and the
    plan doc is 226 KB — a failing guard would bury the one line that matters in
    200 KB of markdown. These report the needle, never the document.
    """

    def assertContains(self, haystack, needle, msg):
        self.assertTrue(needle in haystack, "%s (missing %r)" % (msg, needle))

    def assertNotContains(self, haystack, needle, msg):
        self.assertTrue(needle not in haystack, "%s (found %r)" % (msg, needle))


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


_NEG = re.compile(r"\bNOT\s+DONE\b|\bUN-?DONE\b|\bRE-?DONE\b|\bNOT\s+COMPLETE\b", re.I)
_POS = re.compile(r"^\**\s*DONE\b", re.I)


def _is_done(status):
    """Deliberately a SECOND implementation of the generator's rule, not an import.

    The point of re-parsing the gatelog here is that this guard fails when the
    gATELOG disagrees with the doc. If it called `gen_plan_status.status_is_done`
    it would inherit the generator's opinion and could never catch an error the
    generator also has — which is exactly how `**NOT DONE**` was published as
    `**Status: DONE**` with the full suite green.

    So the rule is restated from the SPEC (a leading explicit DONE token, with
    any explicit negation winning) rather than shared. test_11 then asserts the
    two implementations agree across a table of adversarial status lines, so
    independence is preserved where it counts and divergence is caught.
    """
    if _NEG.search(status):
        return False
    return bool(_POS.match(status.strip()))


def parse_gatelog(text):
    """`{phase: (status_text, is_done)}` parsed from gatelog.md itself.

    Re-parsed here rather than imported so this guard fails when the GATELOG
    says a phase is not started — it must not inherit the generator's opinion.
    """
    out = {}
    current = None
    for line in text.splitlines():
        m = GATELOG_PHASE_RE.match(line)
        if m:
            current = int(m.group(1))
            out.setdefault(current, [])
            continue
        if line.startswith("## "):
            current = None
        if current is None:
            continue
        m = STATUS_RE.match(line)
        if m:
            out[current].append(m.group(1))
    # Fail-closed, mirroring the generator: a negation in ANY Status line wins.
    # Chosen deliberately over first-wins/last-wins — with a corrective line
    # ("NOT DONE") after a stale DONE, the safe reading is not-done, because
    # the cost of a false DONE is publishing unfinished work as shipped.
    return {k: ((v[0], all(_is_done(s) for s in v)) if v else ("", False))
            for k, v in out.items()}


def block(text, begin, end, label):
    """The text between two markers, or None when either marker is absent."""
    i = text.find(begin)
    j = text.find(end)
    if i == -1 or j == -1 or j < i:
        return None
    return text[i + len(begin):j].strip("\n")


def inventory_rows(text):
    """The `{file: count}` rows in the generated inventory block."""
    b = block(text, INV_BEGIN, INV_END, "inventory")
    if b is None:
        return {}
    return dict(re.findall(r"^\|\s*`([^`]+)`\s*\|[^|]*\|[^|]*\|\s*(\d+)\s+lines\s*\|",
                           b, re.M))


def mismatching_counts(rows, root=BASE):
    """Files whose tabled line count disagrees with `wc -l` on the real file.

    THE comparison, in one place, because a check that exists in two copies is a check
    that can be broken in one of them: a sweep mutation of test_05's inline comparison
    left the suite green while test_10 sat there asserting the same property against its
    OWN copy of the logic. Tests that must agree on a rule have to CALL the rule.
    """
    bad = []
    for rel, n in sorted(rows.items()):
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            bad.append(rel)
            continue
        wc = subprocess.run(["wc", "-l", path], capture_output=True, text=True).stdout.split()[0]
        if int(n) != int(wc):
            bad.append(rel)
    return bad


def gen_status_is_done(status):
    """The GENERATOR's own rule, loaded from its source file.

    Used ONLY by the divergence test. Every other guard in this file uses the
    independent `_is_done` above on purpose; this one exists precisely to
    compare the two implementations, which it can only do by calling the real
    one rather than restating it a third time.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_gen_plan_status_under_test", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.status_is_done(status)


def run_check(env_extra=None, root=BASE, plan=PLAN, gatelog=GATELOG):
    env = dict(os.environ)
    env["GEN_PLAN_STATUS_ROOT"] = root
    env["GEN_PLAN_STATUS_PLAN"] = plan
    env["GEN_PLAN_STATUS_GATELOG"] = gatelog
    env.update(env_extra or {})
    return subprocess.run([sys.executable, GEN, "--check"], env=env,
                          capture_output=True, text=True)


def run_write(env_extra=None, root=BASE, plan=PLAN, gatelog=GATELOG):
    env = dict(os.environ)
    env["GEN_PLAN_STATUS_ROOT"] = root
    env["GEN_PLAN_STATUS_PLAN"] = plan
    env["GEN_PLAN_STATUS_GATELOG"] = gatelog
    env.update(env_extra or {})
    return subprocess.run([sys.executable, GEN, "--write"], env=env,
                          capture_output=True, text=True)


def green_baseline(plan_copy, log_copy, tmp):
    """Make the COPY green, then assert it — a control needs a known-good starting point.

    A negative control that asserts "this starts green" is only meaningful if the start
    really is green, and a temp copy is NOT: `Last synced:` is derived from
    `git log -1 -- gatelog.md`, which has no answer outside a git checkout, so the copied
    header renders `unknown` while the real one says `2026-10-04`. Asserting green on an
    untouched copy therefore fails on the copy's own environment and proves nothing about
    the mutation under test.

    So write the copy's baseline FIRST, then assert green. The control still measures what
    it claims to: one flip, from a verified-green tree, to a red one that names the phase.
    """
    w = run_write(root=tmp, plan=plan_copy, gatelog=log_copy)
    if w.returncode != 0:
        return w, run_check(root=tmp, plan=plan_copy, gatelog=log_copy)
    return w, run_check(root=tmp, plan=plan_copy, gatelog=log_copy)


def make_copy_tree(tmp):
    """A temp root holding a copy of the doc + gatelog (nothing else)."""
    os.makedirs(os.path.join(tmp, "docs", "plans"))
    plan_copy = os.path.join(tmp, "docs", "plans", os.path.basename(PLAN))
    log_copy = os.path.join(tmp, "gatelog.md")
    shutil.copyfile(PLAN, plan_copy)
    shutil.copyfile(GATELOG, log_copy)
    return plan_copy, log_copy


def make_instrumented_copy(tmp):
    """A temp copy with a real filesystem, so inventory counts can be measured.

    The counts are measured in GEN_PLAN_STATUS_ROOT, so the root must hold the
    same files at the same sizes. Symlinks (never copies of the source bytes, and
    never opened for writing) keep it cheap; a symlink is not possible across
    filesystems or on Windows without privileges, so fall back to copying.
    """
    os.makedirs(os.path.join(tmp, "docs", "plans"))
    plan_copy = os.path.join(tmp, "docs", "plans", os.path.basename(PLAN))
    log_copy = os.path.join(tmp, "gatelog.md")
    shutil.copyfile(PLAN, plan_copy)
    shutil.copyfile(GATELOG, log_copy)
    for rel in INVENTORY_FILES:
        src = os.path.join(BASE, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.symlink(os.path.abspath(src), dst)
        except (OSError, NotImplementedError):
            shutil.copyfile(src, dst)
    return plan_copy, log_copy


#: Repo-relative inventory files the generator counts. Mirrors its own list.
INVENTORY_FILES = ["index.html", "appcore.js", "bridge.py", "bridge_daemon.py", "test_e2e.py"]

PHASE_BEGIN = "<!-- BEGIN GENERATED phase status: %d (tools/gen_plan_status.py) -->"
PHASE_END = "<!-- END GENERATED phase status: %d -->"
HEADER_BEGIN = "<!-- BEGIN GENERATED status header (tools/gen_plan_status.py) -->"
HEADER_END = "<!-- END GENERATED status header -->"
INV_BEGIN = "<!-- BEGIN GENERATED file inventory (tools/gen_plan_status.py) -->"
INV_END = "<!-- END GENERATED file inventory -->"


class PlanStatusMatchesGatelog(Case):
    """The doc's generated blocks must agree with gatelog.md and the filesystem."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(PLAN)
        cls.gate = parse_gatelog(read(GATELOG))

    # -- (a) header ---------------------------------------------------------

    def test_01_header_block_is_generated_and_agrees_with_gatelog(self):
        b = block(self.text, HEADER_BEGIN, HEADER_END, "header")
        self.assertIsNotNone(b, "no generated status header block in %s" % PLAN)

        missing = [p for p in PHASES if not self.gate.get(p, ("", False))[1]]
        if missing:
            # A gatelog that stopped a phase must show up in the header by name.
            self.assertNotContains(
                b, "COMPLETE",
                "header still claims COMPLETE while gatelog has phase(s) %s not DONE"
                % missing)
            for p in missing:
                self.assertContains(b, str(p), "header does not name not-DONE phase %d" % p)
        else:
            self.assertContains(b, "COMPLETE",
                                "header does not state the phases are COMPLETE")

        self.assertContains(b, "gatelog.md",
                            "header must name gatelog.md as the source of truth")
        self.assertContains(b, "gen_plan_status.py",
                            "header must say it is generated, so nobody hand-edits it")
        m = re.search(r"^\*\*Last synced:\*\*\s*(.+)$", b, re.M)
        self.assertIsNotNone(m, "header has no generated `Last synced:` date")
        self.assertTrue(m.group(1).strip(), "Last synced must never be blank")

    def test_02_header_does_not_say_planned_not_started(self):
        b = block(self.text, HEADER_BEGIN, HEADER_END, "header")
        self.assertIsNotNone(b, "no generated status header block in %s" % PLAN)
        self.assertNotContains(
            b, STALE_WORDING,
            "the pre-phase-24 wording `%s` is back in the header" % STALE_WORDING)

    # -- (b) per-phase ------------------------------------------------------

    def test_03_each_phase_heading_is_immediately_followed_by_its_status_block(self):
        for n in PHASES:
            with self.subTest(phase=n):
                b, e = PHASE_BEGIN % n, PHASE_END % n
                self.assertContains(self.text, b, "phase %d has no generated status block" % n)
                self.assertContains(self.text, e,
                                    "phase %d status block has no END marker" % n)

                heads = [m.start() for m in
                         re.finditer(r"^### Phase %d — .*$" % n, self.text, re.M)]
                self.assertEqual(len(heads), 1,
                                 "phase %s must have exactly one `### Phase` heading" % n)
                after = self.text[heads[0]:].splitlines()[1].strip()
                self.assertEqual(
                    after, b,
                    "phase %s: line after its heading is %r, not its BEGIN marker" % (n, after))

                body = block(self.text, b, e, "phase %d" % n)
                self.assertIsNotNone(body, "phase %s block is malformed" % n)
                want_done = self.gate.get(n, ("", False))[1]
                got_done = "**Status: DONE**" in body
                self.assertEqual(
                    got_done, want_done,
                    "phase %s block says DONE=%s but gatelog says DONE=%s (gatelog: %r)"
                    % (n, got_done, want_done, self.gate.get(n, ("", None))[0]))

    # -- (c) the generator's own verdict ------------------------------------

    def test_09_header_reports_incomplete_when_the_gatelog_stops_a_phase(self):
        """The INCOMPLETE branch, exercised — not merely asserted about.

        `test_01` only reaches the `missing` branch when the REAL gatelog has a
        not-done phase, and it never has one, so that branch was dead to the suite:
        hardcoding `missing = []` (the generator always claiming COMPLETE) left every
        test green. A mutation sweep caught exactly that.

        So drive the generator against a gatelog copy with one phase flipped, and assert
        the header it renders says INCOMPLETE and NAMES the phase. This is the property
        a context-free reader depends on most: a plan document that says COMPLETE when
        the gatelog disagrees is the precise rot this phase exists to kill.

        The `assertNotEqual` below is DEFENCE IN DEPTH, not the load-bearing check, and a
        mutation sweep confirmed it: deleting it leaves the suite green. That is
        equivalence, not a gap — if the replace() ever stopped matching, the flip would
        be a no-op, the header would still say COMPLETE, and the INCOMPLETE assertion
        right below would fail on its own. It earns its place by naming the cause (the
        gatelog's wording moved) instead of reporting a confusing verdict about a header.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plan_copy, log_copy = make_instrumented_copy(tmp)
            log = read(log_copy)
            flipped = log.replace(
                "## Phase 12 — Needle repair pass (F1 ML stage)\nStatus: **DONE**",
                "## Phase 12 — Needle repair pass (F1 ML stage)\nStatus: **not started**")
            self.assertNotEqual(flipped, log,
                                "could not flip phase 12 in the gatelog copy — the fixture "
                                "no longer matches the gatelog's text")
            with open(log_copy, "w", encoding="utf-8") as fh:
                fh.write(flipped)

            w = run_write(root=tmp, plan=plan_copy, gatelog=log_copy)
            self.assertEqual(w.returncode, 0, "--write failed on the copy: %s" % w.stdout)

            hdr = block(read(plan_copy), HEADER_BEGIN, HEADER_END, "header")
            self.assertIsNotNone(hdr, "no header block in the regenerated copy")
            self.assertContains(hdr, "INCOMPLETE",
                                "with phase 12 not DONE the header must not claim COMPLETE")
            self.assertContains(hdr, "12",
                                "the INCOMPLETE header must NAME the phase that is not done, "
                                "not just say something is wrong")

    def test_11_this_guard_has_a_caller_in_the_regression_suite(self):
        """The guard must be INVOKED, not merely present.

        A sweep mutant that emptied the tuple in `test_e2e.py`'s phase-24 loop —
        `for _p24_mod in ():` — left the ENTIRE suite green: both guard modules still
        pass when run by hand, and nothing in `test_e2e.py` noticed they had stopped
        being called. That is the dormant-guard failure in its purest form, and it is
        the one this phase cannot afford: the finding was filed as "N/A — documentation,
        untested by nature, which is exactly why it drifted", so the whole deliverable is
        the edge from the suite to these checks. Lose the edge and the deliverable is
        inert while every test still passes.

        Assert the edge by a stable token (the module names, as the loop spells them),
        not by the surrounding prose — a reworded comment is not a broken edge, and
        neither is a reworded check.
        """
        e2e = read(os.path.join(BASE, "test_e2e.py"))
        for mod in ("test_gen_plan_status", "test_readme_install"):
            with self.subTest(guard=mod):
                self.assertContains(
                    e2e, mod,
                    "test_e2e.py never names %s.py, so this guard has no caller and the "
                    "phase-24 docs rot can return unnoticed" % mod)
        # And the loop must not be emptied out from under those names.
        m = re.search(r"for _p24_mod in \(([^)]*)\):", e2e)
        self.assertIsNotNone(m, "the phase-24 guard loop in test_e2e.py is gone")
        named = re.findall(r"'([^']+)'", m.group(1))
        self.assertEqual(sorted(named), ["test_gen_plan_status", "test_readme_install"],
                         "the phase-24 guard loop must invoke BOTH guards; it invokes %r"
                         % (named,))

    def test_04_generator_check_is_green_on_this_tree(self):
        r = run_check()
        self.assertEqual(
            r.returncode, 0,
            "--check failed on this tree:\n%s\n%s" % (r.stdout, r.stderr))

    # -- (d) inventory counts ----------------------------------------------

    def test_05_inventory_counts_equal_a_fresh_wc_l(self):
        b = block(self.text, INV_BEGIN, INV_END, "inventory")
        self.assertIsNotNone(b, "no generated file inventory block in %s" % PLAN)
        rows = inventory_rows(self.text)
        self.assertTrue(rows, "inventory block has no `file | … | N lines` rows")
        # The comparison itself lives in mismatching_counts() so test_10 can drive the
        # SAME function on a doctored table. Do not inline it here: a check duplicated
        # across two tests is a check that can be broken in one of them.
        self.assertEqual(mismatching_counts(rows), [],
                         "inventory counts disagree with the real files")
        for required in INVENTORY_FILES:
            self.assertIn(required, rows, "inventory omits required file %s" % required)

    def test_10_the_inventory_comparison_rejects_a_wrong_count(self):
        """Prove `mismatching_counts()` is not a tautology, by running it on a WRONG table.

        A sweep mutation rewrote test_05's inline `assertEqual(int(n), int(wc))` into
        `assertEqual(int(n), int(n))` and the suite stayed GREEN. Two things were wrong:
        the comparison was inline, so nothing else exercised it, and it only ever ran
        against the real table, which is CORRECT — so on this tree the two forms cannot
        be distinguished at all.

        The fix is the distinguishing input: a table that DISAGREES with the file. Bump
        one file's count by one and assert the shared helper reports exactly that file. A
        comparison that returns [] here is comparing the table to itself, and test_05's
        green is then worth nothing.
        """
        real_rows = inventory_rows(self.text)
        self.assertTrue(real_rows, "no inventory rows to work from")
        self.assertEqual(mismatching_counts(real_rows), [],
                         "the real inventory table must agree with the real files")

        doctored = dict(real_rows)
        target = sorted(doctored)[0]
        doctored[target] = str(int(doctored[target]) + 1)
        self.assertEqual(mismatching_counts(doctored), [target],
                         "a table whose %s count is off by one must be reported, and only "
                         "that file — a helper returning [] here is comparing the table to "
                         "itself, which makes test_05 vacuous" % target)

        # And the positive direction, or a helper that reports everything passes just as
        # happily as one that reports nothing.
        self.assertEqual(mismatching_counts({}), [],
                         "an empty table must report no mismatches")


class NegativeControls(Case):
    """Teeth: break one thing in a COPY and prove `--check` notices and explains."""

    def setUp(self):
        self.before = {p: sha(p) for p in WATCHED}

    def tearDown(self):
        # The real tree must be untouched no matter what the control did.
        self.assertEqual(self.before, {p: sha(p) for p in WATCHED},
                         "a negative control mutated the real working tree")

    def test_06_flipping_a_phase_in_the_gatelog_fails_check_and_names_the_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_copy, log_copy = make_instrumented_copy(tmp)
            _w, green = green_baseline(plan_copy, log_copy, tmp)
            self.assertEqual(green.returncode, 0,
                             "the copied tree must start green, else this control proves "
                             "nothing:\n%s" % green.stdout)

            log = read(log_copy)
            log = log.replace("## Phase 12 — Needle repair pass (F1 ML stage)\n"
                              "Status: **DONE**",
                              "## Phase 12 — Needle repair pass (F1 ML stage)\n"
                              "Status: **not started**")
            self.assertNotEqual(read(log_copy), log, "the control did not change anything")
            with open(log_copy, "w", encoding="utf-8") as fh:
                fh.write(log)

            red = run_check(root=tmp, plan=plan_copy, gatelog=log_copy)
            self.assertNotEqual(red.returncode, 0,
                                "--check exited 0 after phase 12 was flipped to not started")
            self.assertIn("12", red.stdout + red.stderr,
                          "--check failed but never named phase 12:\n%s" % red.stdout)

    def test_07_a_corrupted_inventory_count_fails_check_and_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_copy, log_copy = make_instrumented_copy(tmp)
            _w, green = green_baseline(plan_copy, log_copy, tmp)
            self.assertEqual(green.returncode, 0,
                             "the copied tree must start green:\n%s" % green.stdout)

            doc = read(plan_copy)
            i = doc.find(INV_BEGIN)
            j = doc.find(INV_END)
            self.assertNotEqual((i, j), (-1, -1), "no inventory block in the real doc to corrupt")
            body = doc[i:j]
            m = re.search(r"^(\|\s*`bridge\.py`\s*\|[^|]*\|[^|]*\|\s*)(\d+)(\s+lines\s*\|)$",
                          body, re.M)
            self.assertIsNotNone(m, "could not find the bridge.py inventory row")
            doctored = body[:m.start()] + m.group(1) + "1" + m.group(3) + body[m.end():]
            with open(plan_copy, "w", encoding="utf-8") as fh:
                fh.write(doc[:i] + doctored + doc[j:])

            red = run_check(root=tmp, plan=plan_copy, gatelog=log_copy)
            self.assertNotEqual(red.returncode, 0,
                                "--check exited 0 with a corrupted bridge.py line count")
            self.assertIn("bridge.py", red.stdout + red.stderr,
                          "--check failed but never named bridge.py:\n%s" % red.stdout)

    def test_08_a_missing_phase_block_fails_check(self):
        """The other rot direction: deleting a block must not read as agreement."""
        with tempfile.TemporaryDirectory() as tmp:
            plan_copy, log_copy = make_instrumented_copy(tmp)
            _w, green = green_baseline(plan_copy, log_copy, tmp)
            self.assertEqual(green.returncode, 0,
                             "the copied tree must start green before the block is "
                             "deleted, else this red proves nothing:\n%s" % green.stdout)
            doc = read(plan_copy)
            i = doc.find(PHASE_BEGIN % 13)
            j = doc.find(PHASE_END % 13) + len(PHASE_END % 13)
            self.assertNotEqual(i, -1, "phase 13 has no generated block to remove")
            with open(plan_copy, "w", encoding="utf-8") as fh:
                fh.write(doc[:i] + doc[j:])
            red = run_check(root=tmp, plan=plan_copy, gatelog=log_copy)
            self.assertNotEqual(red.returncode, 0,
                                "--check exited 0 with phase 13's status block deleted")
            self.assertIn("13", red.stdout + red.stderr,
                          "--check did not name the phase whose block vanished:\n%s" % red.stdout)

    # -- the inversion that made this generator publish lies -----------------

    def test_09_a_NOT_DONE_phase_is_never_published_as_DONE(self):
        """Kills the `"DONE" in status` inversion (Phase 24, review finding L1).

        A substring test reads `Status: **NOT DONE**` as DONE, so the doc
        published `**Status: DONE**` and a COMPLETE header for a phase the
        gatelog says is unfinished — with `--check` rc=0 and all 11 tests green.
        That is the exact rot CR-Nanites-harness-0027 filed, reproduced by the
        tool written to prevent it.

        Phase 14 is used, NOT phase 12: tests 06/09(above) only ever rewrite
        phase 12's literal fixture string, so a failure there would be fixture
        brittleness rather than the rule being pinned. This input must reach
        the comparison.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plan_copy, log_copy = make_instrumented_copy(tmp)
            _w, green = green_baseline(plan_copy, log_copy, tmp)
            self.assertEqual(green.returncode, 0,
                             "the copied tree must start green before the gatelog "
                             "is flipped, else this red proves nothing:\n%s" % green.stdout)

            log = read(log_copy)
            head = "## Phase 14 — F3 cheap local dispatcher (Needle as pre-router)"
            i = log.index(head)
            j = log.index("\n", i)
            k = log.index("\n", j + 1)
            self.assertIn("DONE", log[j:k + 1],
                          "phase 14's Status line no longer mentions DONE:\n%s" % log[j:k])
            with open(log_copy, "w", encoding="utf-8") as fh:
                fh.write(log[:j + 1] + "Status: **NOT DONE** — acceptance regressed\n"
                         + log[k + 1:])

            # The gatelog now disagrees with the doc, so --check must say so.
            red = run_check(root=tmp, plan=plan_copy, gatelog=log_copy)
            self.assertNotEqual(red.returncode, 0,
                                "--check exited 0 after the gatelog recorded phase 14 "
                                "as NOT DONE — the generator is publishing a phase as "
                                "finished that the gatelog says is not")

            # And regenerating must NOT invent a DONE claim for it.
            w = run_write(root=tmp, plan=plan_copy, gatelog=log_copy)
            self.assertEqual(w.returncode, 0, "--write failed:\n%s" % w.stderr)
            doc = read(plan_copy)
            b = block(doc, PHASE_BEGIN % 14, PHASE_END % 14, "phase 14")
            self.assertIsNotNone(b, "phase 14 block vanished from the regenerated doc")
            self.assertNotIn("**Status: DONE**", b,
                             "a gatelog status of NOT DONE was published as DONE:\n%s" % b)
            hb = block(doc, HEADER_BEGIN, HEADER_END, "header")
            self.assertNotIn("are all recorded DONE", hb,
                             "the header still claims COMPLETE with a NOT DONE phase:\n%s" % hb)
            self.assertIn("14", hb,
                          "the header must NAME the phase that is not done:\n%s" % hb)

    def test_10_both_parses_agree_on_adversarial_status_lines(self):
        """The two parsers are independent in code but must agree in meaning.

        Independence is the point (the guard must not inherit the generator's
        opinion), so they are NOT shared. This pins that they cannot drift apart
        the way they did: one reset phase scope on `###`, the other only on
        `##`; one kept the first Status line, the other the last. Either
        divergence lets `--check` and the suite certify opposite verdicts.
        """
        table = [
            ("**DONE**", True),
            ("**not started**", False),
            ("**NOT DONE**", False),
            ("**NOT DONE** — acceptance regressed", False),
            ("**DONE, NOT DONE for F3**", False),
            ("**UN-DONE**", False),
            ("**UNDONE**", False),
            ("**REDONE**", False),
            ("**DONE (2026-09-26)**", True),
            ("**in progress**", False),
        ]
        for status, want in table:
            with self.subTest(status=status):
                doc = ("## Phase 14 — F3 cheap local dispatcher (Needle as "
                       "pre-router)\nStatus: " + status + "\n")
                self.assertEqual(parse_gatelog(doc)[14][1], want,
                                 "the guard's own parser misread %r" % status)
                # The generator's rule takes the VALUE, not the `Status:` line:
                # STATUS_RE captures group(1) after the colon. Passing the whole
                # line here would test a shape the generator never receives.
                self.assertEqual(
                    gen_status_is_done(status), want,
                    "the generator's rule misread %r — if this fails the guard "
                    "and the generator have DIVERGED, which is the defect the "
                    "independence is supposed to survive" % status)

    def test_11_scope_and_duplicate_status_lines_do_not_diverge(self):
        """A `###` sub-heading and a second `Status:` line must parse alike.

        Phase 14 again, never phase 12: these assert the RULE, and a rule proven
        only against one fixture string is a rule nobody has pinned.
        """
        shapes = {
            # All-DONE across every Status line -> done.
            "status after a ### sub-heading":
                ("## Phase 14 — T\n### Findings\nStatus: **DONE**\n", True),
            # A later corrective negation wins over an earlier DONE. Asserted
            # explicitly, in BOTH orders, because first-wins and last-wins were
            # the two readings that diverged between the parsers — this pins the
            # one both now implement.
            "DONE then corrective NOT DONE":
                ("## Phase 14 — T\nStatus: **DONE**\nStatus: **NOT DONE**\n", False),
            "not started then DONE":
                ("## Phase 14 — T\nStatus: **not started**\nStatus: **DONE**\n", False),
        }
        for label, (doc, want) in shapes.items():
            with self.subTest(shape=label):
                self.assertEqual(
                    parse_gatelog(doc)[14][1], want,
                    "the guard read %r as done=%s; fail-closed means a negation "
                    "anywhere wins:\n%s" % (label, want, doc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
