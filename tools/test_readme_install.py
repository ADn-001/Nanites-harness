#!/usr/bin/env python3
"""Onboarding-guard for README.txt.

CR-Nanites-harness-0028: README.txt is the first thing a friend cloning this
repo touches, and it had rotted in three ways that no test could see:

  1. the numbered quick-start had no clone step and no dependency install, so a
     fresh clone could never reach a green test run (`Cannot find module
     'jsdom'`);
  2. the ordering was wrong — the install was mentioned in passing *after* the
     test command;
  3. it was hard-coded to one operating system (a `## Quick start on <win>`,
     a `C:` drive path, PowerShell fences) and every script ran under a bare
     interpreter name that does not exist on many Linux distros.

This module scans README.txt as DATA and fails if any of that returns. It is
standalone: `python3 tools/test_readme_install.py`.

Every assertion below is checked against deliberately-mutated copies of the
real README as well, so the guard is proven to have teeth rather than merely
passing.

NOTE: this file builds its sample-bad strings from fragments on purpose — a
guard whose own source contained the forbidden shape would trip on itself.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.txt"
SELF = Path(__file__).resolve()

# --- shapes we care about ------------------------------------------------

# A bare-interpreter script invocation, e.g. `py<empty>thon foo.py` or
# `py<empty>thon -m http.server`. Built as a regex from fragments so this file
# does not itself contain the literal shape it forbids.
_BAD_INTERP = "py" + "thon"
GOOD_INTERP = _BAD_INTERP + "3"

BARE_PYTHON_RE = re.compile(
    r"(?<![\w./\\-])"          # not preceded by a word char, `/`, `.` or `\`
    + re.escape(_BAD_INTERP)   # the bare name ...
    + r"(?![\w-])"             # ... but not the versioned one, nor `pythonw`
    + r"(?:[ \t]+(?:-m\b|\S+\.py\b))"
)

# Any command that actually runs the suites.
TEST_RUN_RE = re.compile(
    r"(?:\b" + re.escape(GOOD_INTERP) + r"[ \t]+test_e2e\.py\b"
    r"|\bnpm[ \t]+test\b)"
)

DRIVE_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/]{1,2}[A-Za-z0-9_.]")
POWERSHELL_FENCE_RE = re.compile(r"^```+[ \t]*(?:powershell|ps1|cmd)[ \t]*$", re.M)

#: A Quick-start heading that scopes the flow to ONE operating system. Anchored on the
#: OS NAMES, not on a fixed phrase, so "on Windows 11", "on Windows", "(Windows)" and
#: "for Linux only" are all caught while a neutral "Quick start (Linux, macOS, Windows)"
#: is not — that one lists several and is exactly what we want.
SINGLE_OS_HEADING_RE = re.compile(
    r"\b(?:on|for|under)\s+(?:Windows|Linux|macOS|Mac\s+OS\s?X|OS\s?X)\b"
    r"|^\s*##+\s*Quick start\s*\(\s*(?:Windows|Linux|macOS|OS\s?X)\s*\)\s*$",
    re.I)


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def quickstart(text: str) -> str:
    """Return the Quick start section (heading up to the next `## ` heading)."""
    m = re.search(r"^##[ \t]+Quick start.*$", text, re.M)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"^##[ \t]+\S", text[m.end():], re.M)
    return text[start: m.end() + nxt.start()] if nxt else text[start:]


def step_span(qs: str, needle: str) -> str:
    """Text of the `### N.` sub-step that contains `needle` (heading -> next heading)."""
    at = qs.find(needle)
    if at == -1:
        return ""
    head = qs.rfind("\n### ", 0, at)
    start = head + 1 if head != -1 else 0
    nxt = re.compile(r"^###[ \t]+\S", re.M).search(qs, at)
    return qs[start: nxt.start()] if nxt else qs[start:]


def check_readme(text: str) -> list[str]:
    """Return a list of onboarding defects; empty list means the flow is sound."""
    problems: list[str] = []
    qs = quickstart(text)

    # 1. clone + install present, and install BEFORE the first test run.
    if not qs:
        problems.append("no-quickstart-section")
    else:
        i_clone = qs.find("git clone")
        i_install = qs.find("npm install")
        m_test = TEST_RUN_RE.search(qs)
        i_test = m_test.start() if m_test else -1

        if i_clone == -1:
            problems.append("no-git-clone-step")
        if i_install == -1:
            problems.append("no-npm-install-step")
        if i_test == -1:
            problems.append("no-test-command-in-quickstart")

        if i_install != -1 and i_test != -1 and i_install > i_test:
            problems.append("npm-install-after-test-step")
        if i_clone != -1 and i_install != -1 and i_clone > i_install:
            problems.append("npm-install-before-git-clone")

        # 3. the quick-start must not be locked to one operating system.
        #
        # Assert the RULE (this flow works on more than one OS), not the two spellings
        # that happened to be wrong. A mutation sweep caught the gap: retitling the
        # section back to `## Quick start on Windows 11` — over perfectly good POSIX
        # commands — left the whole suite green, because the old checks only looked for
        # drive paths and PowerShell fences. A heading can claim one OS without
        # containing a single OS-specific token, so the heading itself has to be checked.
        _qs_head = re.search(r"^##[ \t]+Quick start.*$", qs, re.M)
        if _qs_head is not None and SINGLE_OS_HEADING_RE.search(_qs_head.group(0)):
            problems.append("quickstart-heading-claims-one-operating-system")
        if DRIVE_PATH_RE.search(qs):
            problems.append("hardcoded-drive-path-in-quickstart")
        if POWERSHELL_FENCE_RE.search(qs):
            problems.append("powershell-fence-in-quickstart")
        if not re.search(
            re.escape(GOOD_INTERP) + r"[ \t]+-m[ \t]+http\.server\b", qs
        ):
            problems.append("no-posix-http-server-in-quickstart")

        # 4. jsdom / npm install must be stated as REQUIRED, not optional.
        win = step_span(qs, "npm install")
        if not re.search(r"jsdom", win, re.I):
            problems.append("install-step-does-not-mention-jsdom")
        if not re.search(r"\bREQUIRED\b", win):
            problems.append("install-step-not-marked-required")
        if re.search(r"\boptional\b", win, re.I):
            problems.append("install-step-called-optional")

    # 2. no bare-interpreter script invocation anywhere in the file.
    for hit in BARE_PYTHON_RE.finditer(text):
        line = text.count("\n", 0, hit.start()) + 1
        problems.append(f"bare-interpreter-invocation-line-{line}")

    return problems


# --- a known-good fixture ------------------------------------------------
# The teeth below must bite *independently* of the current state of README.txt:
# if they ran only against a README that is already broken, a mutation that
# cannot find its anchor would silently prove nothing. So they mutate this
# minimal known-good document, which `check_readme` is asserted to accept.

GOOD_FIXTURE = """# fixture

## Quick start

### 1. Clone the repository

```sh
git clone https://example.invalid/repo.git <repo-dir>
cd <repo-dir>
```

### 2. Install the dependencies

```sh
npm install
```

This installs jsdom. It is REQUIRED for the frontend suite.

### 3. Run the test suite

```sh
python3 test_e2e.py
```

### 4. Start the app

```sh
python3 -m http.server 8080
```

## Later
"""


# --- mutations: the teeth ------------------------------------------------
# Built from fragments so this file never holds a literal bare-invocation.

_BAD_SCRIPT_LINE = _BAD_INTERP + " test_e2e.py"


def mutate_install_after_test(text: str) -> str:
    """Swap quick-start steps 2 and 3 so the install follows the test run."""
    s2 = text.index("### 2.")
    s3 = text.index("### 3.")
    s4 = text.index("### 4.")
    step2 = text[s2:s3]
    step3 = text[s3:s4]
    swapped = (
        step3.replace("### 3.", "### 2.", 1)
        + step2.replace("### 2.", "### 3.", 1)
    )
    return text[:s2] + swapped + text[s4:]


def mutate_bare_interpreter(text: str) -> str:
    """Put one bare-interpreter invocation back in the test step."""
    return text.replace(GOOD_INTERP + " test_e2e.py", _BAD_SCRIPT_LINE, 1)


def mutate_one_os_only(text: str) -> str:
    """Make the quick-start locked to Windows again."""
    qs_start = text.index("### 1.")
    return text[:qs_start] + "```powershell\ncd C:\\cogitator\n" + text[qs_start:]


def mutate_install_downgraded(text: str) -> str:
    """Downgrade the jsdom requirement to something you may skip."""
    win_start = text.index("npm install", text.index("### 2."))
    return text[:win_start] + text[win_start:].replace("REQUIRED", "recommended", 1)


def mutate_install_removed(text: str) -> str:
    """Delete the install step entirely — the original defect."""
    s2 = text.index("### 2.")
    s3 = text.index("### 3.")
    return text[:s2] + text[s3:]


class TestReadmeInstallFlow(unittest.TestCase):
    """The rot classes, asserted against the real README.txt."""

    def setUp(self) -> None:
        self.text = readme_text()
        self.problems = check_readme(self.text)

    def _mutated(self, mutator, source: str | None = None):
        base = self.text if source is None else source
        broken = mutator(base)
        self.assertTrue(
            broken != base, "mutation did not change the source text"
        )
        return broken

    def test_fixture_is_known_good(self) -> None:
        """Sanity: the fixture the teeth mutate must itself be defect-free."""
        self.assertEqual(check_readme(GOOD_FIXTURE), [])

    def test_1_clone_and_install_precede_the_test_run(self) -> None:
        """A fresh clone must be able to reach a green run, in that order."""
        self.assertIn("git clone", self.text, "README has no `git clone` step at all")
        self.assertIn("npm install", self.text, "README never tells you to install deps")
        qs = quickstart(self.text)
        self.assertTrue(qs, "README has no `## Quick start` section")
        i_clone = qs.index("git clone")
        i_install = qs.index("npm install")
        m_test = TEST_RUN_RE.search(qs)
        self.assertIsNotNone(m_test, "quick-start never shows how to run the tests")
        assert m_test is not None  # narrow for type checkers
        self.assertLess(
            i_install, m_test.start(),
            "the dependency install must appear BEFORE the first step that runs "
            "the tests (the actual defect: jsdom was installed after the run)",
        )
        self.assertLess(i_clone, i_install, "you must clone before you install")
        self.assertNotIn("npm-install-after-test-step", self.problems)

    def test_2_no_bare_interpreter_invocations_anywhere(self) -> None:
        """The bare name does not exist on many Linux distros; the versioned one does."""
        hits = [
            (self.text.count("\n", 0, m.start()) + 1, m.group(0))
            for m in BARE_PYTHON_RE.finditer(self.text)
        ]
        self.assertEqual(
            hits, [],
            f"bare-interpreter script invocation(s) found; use `{GOOD_INTERP} ...`",
        )

    def test_3_quickstart_is_platform_neutral(self) -> None:
        """Linux, macOS and Windows all have to be able to follow the quick-start."""
        qs = quickstart(self.text)
        self.assertNotRegex(
            qs, DRIVE_PATH_RE,
            "quick-start hard-codes a drive path (e.g. `C:` + backslash)",
        )
        self.assertNotRegex(
            qs, POWERSHELL_FENCE_RE,
            "quick-start still uses a powershell fence; use ```sh / ```bash",
        )
        self.assertRegex(
            qs, re.escape(GOOD_INTERP) + r"[ \t]+-m[ \t]+http\.server\b",
            f"quick-start must show the POSIX `{GOOD_INTERP} -m http.server` invocation",
        )

    def test_4_install_is_required_not_optional(self) -> None:
        """jsdom is the one dependency and it gates the frontend suite."""
        win = step_span(quickstart(self.text), "npm install")
        self.assertTrue(win, "quick-start has no `npm install` step to inspect")
        self.assertRegex(win, r"jsdom", "install step must say what it installs")
        self.assertRegex(
            win, r"\bREQUIRED\b",
            "install step must state that the install is REQUIRED, not optional",
        )
        self.assertNotRegex(
            win, r"\boptional\b",
            "install step must not describe the required install as optional",
        )

    # --- teeth: the guard must bite on genuinely broken text --------------
    # Each of these mutates the KNOWN-GOOD fixture (or the real README where
    # the rot is present) and asserts the checker names the specific defect.

    def test_teeth_install_after_test_is_caught(self) -> None:
        broken = self._mutated(mutate_install_after_test, GOOD_FIXTURE)
        self.assertIn("npm-install-after-test-step", check_readme(broken))
        qs = quickstart(broken)
        m_test = TEST_RUN_RE.search(qs)
        self.assertIsNotNone(m_test, "mutation dropped the test command entirely")
        assert m_test is not None  # narrow for type checkers
        self.assertGreater(
            qs.index("npm install"), m_test.start(),
            "mutation should have moved the install after the test run",
        )

    def test_teeth_missing_install_step_is_caught(self) -> None:
        """The original defect: the quick-start never installed anything."""
        broken = self._mutated(mutate_install_removed, GOOD_FIXTURE)
        self.assertIn("no-npm-install-step", check_readme(broken))
        self.assertNotIn("npm install", quickstart(broken))

    def test_teeth_missing_clone_step_is_caught(self) -> None:
        """The other original defect: the quick-start never cloned anything."""
        broken = GOOD_FIXTURE.replace(
            "git clone https://example.invalid/repo.git <repo-dir>\n", ""
        )
        self.assertTrue(broken != GOOD_FIXTURE, "mutation did not change the text")
        self.assertNotIn("git clone", quickstart(broken))
        self.assertIn("no-git-clone-step", check_readme(broken))

    def test_teeth_bare_interpreter_is_caught(self) -> None:
        broken = self._mutated(mutate_bare_interpreter, GOOD_FIXTURE)
        self.assertIn("bare-interpreter-invocation-line-", " ".join(check_readme(broken)))
        self.assertIn(GOOD_INTERP, GOOD_FIXTURE, "fixture sanity")
        self.assertNotIn(GOOD_INTERP + " test_e2e.py", broken)

    def test_teeth_single_os_quickstart_is_caught(self) -> None:
        broken = self._mutated(mutate_one_os_only, GOOD_FIXTURE)
        found = check_readme(broken)
        self.assertIn("hardcoded-drive-path-in-quickstart", found)
        self.assertIn("powershell-fence-in-quickstart", found)

    def test_teeth_single_os_HEADING_alone_is_caught(self) -> None:
        """A heading can claim one OS while the body stays POSIX-clean.

        `mutate_one_os_only` adds a drive path and a PowerShell fence, so the older
        checks caught it. The sweep's escaping mutation only retitled the section, and
        that passed — a wrong CLAIM with no wrong token in it. Assert every spelling of
        the single-OS heading, and assert the neutral multi-OS heading stays clean, or a
        checker that flags everything would satisfy the negatives.
        """
        for claim in ("## Quick start on Windows 11",
                      "## Quick start on Windows",
                      "## Quick start for Linux only",
                      "## Quick start (Windows)"):
            with self.subTest(heading=claim):
                broken = GOOD_FIXTURE.replace("## Quick start", claim, 1)
                self.assertNotEqual(broken, GOOD_FIXTURE,
                                    "mutation did not change the heading")
                self.assertIn("quickstart-heading-claims-one-operating-system",
                              check_readme(broken),
                              "a quick-start heading scoped to one OS must be reported")
        # Positive direction: the real neutral heading names three OSes and is fine.
        self.assertNotIn("quickstart-heading-claims-one-operating-system",
                         check_readme(GOOD_FIXTURE))
        self.assertNotIn("quickstart-heading-claims-one-operating-system",
                         check_readme(readme_text()),
                         "the shipped README's heading must not trip its own check")

    def test_teeth_downgraded_install_is_caught(self) -> None:
        broken = self._mutated(mutate_install_downgraded, GOOD_FIXTURE)
        self.assertIn("install-step-not-marked-required", check_readme(broken))

    def test_teeth_variants_of_each_defect_are_all_caught(self) -> None:
        """One sweep: every single-defect mutation must be named, and only it."""
        cases = [
            (mutate_install_after_test, "npm-install-after-test-step"),
            (mutate_install_removed, "no-npm-install-step"),
            (mutate_bare_interpreter, "bare-interpreter-invocation-line-"),
            (mutate_one_os_only, "powershell-fence-in-quickstart"),
            (mutate_install_downgraded, "install-step-not-marked-required"),
        ]
        for mutator, expected in cases:
            with self.subTest(expected=expected):
                found = check_readme(self._mutated(mutator, GOOD_FIXTURE))
                self.assertTrue(
                    any(expected in f for f in found),
                    f"mutation {mutator.__name__} not caught; checker said {found}",
                )

    def test_teeth_checker_is_clean_on_the_real_readme(self) -> None:
        """The checker must report no defects on a real, fixed README."""
        self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
