#!/usr/bin/env python3
"""Generate the Local Cortex plan doc's status + file-inventory blocks from REALITY.

CR-Nanites-harness-0027: `docs/plans/needle-laya-middleware-plan.md` opened with
`**Status:** planned (not started). Phases 10–15 of gatelog.md` — and 440 lines
lower the SAME file said `**Status: DONE** (2026-09-26)`, while `gatelog.md`
recorded phases 10-15 all DONE. Phases 10-14 carried no status line at all.

That file names its own reader: *"a fresh session/cron call with NO conversational
context"*. A context-free reader stops at line 3, redoes six finished phases, or
declares a shipped project unfinished. The §1 size table rotted the same way: it
claimed `index.html 1356 / appcore.js 398 / test_e2e.py 169` against a real
2287 / 2083 / 4908.

Hand-maintaining that across 16 phases would rot again, so it is GENERATED here:
one source (`gatelog.md` for status, the files themselves for line counts), three
generated blocks, and a `--check` mode that `tools/test_gen_plan_status.py` drives
— so a stale status header FAILS the cheap guard instead of misleading the next
context-free session.

Same shape and the same contract as `tools/gen_sw_cache.py` and
`tools/gen_git_policy.py`: bare invocation prints the blocks, `--check` exits 1
with a human-readable reason naming what drifted, `--write` rewrites in place.

Two deliberate limits:

* **The per-file Language and Role prose is NOT generated.** Only the line counts
  are, and they are measured by counting the real files. The prose is human-written
  and still true; a generator that rewrote it would be inventing documentation.
* **The `Last synced:` date is never invented.** It is the date of the last commit
  that touched `gatelog.md`; outside a git checkout it reads `unknown` rather than
  making something up.

Usage:
    python3 tools/gen_plan_status.py            # print the blocks
    python3 tools/gen_plan_status.py --check    # exit 1 if the doc is out of date
    python3 tools/gen_plan_status.py --write    # rewrite the blocks in the plan doc

Env overrides (used by tools/test_gen_plan_status.py's negative controls; they
never touch the real tree):
    GEN_PLAN_STATUS_ROOT     repo root the inventory files are measured in
    GEN_PLAN_STATUS_PLAN    plan doc to read/write
    GEN_PLAN_STATUS_GATELOG gatelog to parse
"""
import os
import re
import subprocess
import sys

BASE = os.environ.get("GEN_PLAN_STATUS_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
PLAN = os.environ.get("GEN_PLAN_STATUS_PLAN") or os.path.join(
    BASE, "docs", "plans", "needle-laya-middleware-plan.md")
GATELOG = os.environ.get("GEN_PLAN_STATUS_GATELOG") or os.path.join(BASE, "gatelog.md")

# --- markers -------------------------------------------------------------
# Both halves of the contract quote these (the generator and its guard), so a
# rename breaks loudly instead of leaving a stale hand-written copy in place.
HEADER_BEGIN = "<!-- BEGIN GENERATED status header (tools/gen_plan_status.py) -->"
HEADER_END = "<!-- END GENERATED status header -->"
INVENTORY_BEGIN = "<!-- BEGIN GENERATED file inventory (tools/gen_plan_status.py) -->"
INVENTORY_END = "<!-- END GENERATED file inventory -->"


def phase_begin(n):
    return "<!-- BEGIN GENERATED phase status: %d (tools/gen_plan_status.py) -->" % n


def phase_end(n):
    return "<!-- END GENERATED phase status: %d -->" % n


# --- gatelog parsing -----------------------------------------------------

#: `## Phase 12 — Title`. The em dash is U+2014, not a hyphen.
GATELOG_PHASE_RE = re.compile(r"^## Phase (\d+) — ")
#: The `Status:` line immediately under a phase heading.
STATUS_RE = re.compile(r"^Status:\s*(.*)$")
#: A parenthesised ISO date inside that Status line, if the author wrote one.
#: Phase 16 writes `**DONE** (after two review rounds: R1 and R2 — see Findings)`,
#: which correctly yields nothing; phase 15's gatelog line has no date at all.
ISO_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
#: `### Phase 12 — Title  (size L)` in the plan doc.
PLAN_PHASE_RE = re.compile(r"^### Phase (\d+) — ", re.M)


def parse_gatelog(text):
    """`{phase: {'status': str, 'done': bool, 'date': str|None}}`.

    A phase counts as DONE only when its `Status:` value contains a
    case-sensitive `DONE`, so `not started` is not DONE. The parenthesised date is
    picked out with a regex, never by phase number.
    """
    out = {}
    current = None
    for line in text.splitlines():
        m = GATELOG_PHASE_RE.match(line)
        if m:
            current = int(m.group(1))
            out.setdefault(current, {"status": "", "done": False, "date": None})
            continue
        if line.startswith("#"):
            current = None  # a new section closes the phase's Status slot
        if current is None or current not in out:
            continue
        m = STATUS_RE.match(line)
        if m:
            out[current].setdefault("_lines", []).append(m.group(1))
    # Fail-closed across multiple Status lines: a phase section that ever says
    # "not done" is NOT done, even if an earlier line said DONE. Publishing a
    # phase as finished that a later line corrects is the rot this generator
    # exists to prevent, so a negation anywhere wins.
    for n, rec in out.items():
        lines = rec.pop("_lines", [])
        if not lines:
            continue
        rec["status"] = lines[0]
        rec["done"] = all(status_is_done(s) for s in lines)
        d = ISO_DATE_RE.search(lines[0])
        rec["date"] = d.group(1) if d else None
    return out


NEGATED_DONE_RE = re.compile(
    r"\bNOT\s+DONE\b|\bUN-?DONE\b|\bRE-?DONE\b|\bNOT\s+COMPLETE\b", re.I)

DONE_POSITIVE_RE = re.compile(r"^\**\s*DONE\b", re.I)


def status_is_done(status):
    """Is this gatelog `Status:` line a claim that the phase is DONE?

    NOT a substring test. `"DONE" in status` reads `**NOT DONE**` as DONE, and
    that inverts the entire deliverable: a phase the gatelog says is NOT done
    gets published as `**Status: DONE**`, the header says COMPLETE, and
    `--check` plus the whole guard suite stay green. That is precisely the rot
    this generator was written to kill (CR-Nanites-harness-0027), reproduced by
    the tool meant to prevent it — see gatelog.md Phase 24 findings.

    Fail-closed: only an explicit leading `DONE` token counts, and any explicit
    negation anywhere in the line wins. An unrecognised wording is NOT done, so
    a future status string nobody anticipated shows up as drift to be resolved
    rather than silently published as finished.
    """
    if NEGATED_DONE_RE.search(status):
        return False
    return bool(DONE_POSITIVE_RE.match(status.strip()))


def read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def phases_in_plan(text):
    """The phases the plan doc actually covers — parsed, not hardcoded.

    Deriving the set from the document is what lets a future phase-16 section be
    picked up by adding a heading instead of editing a list in this generator.
    """
    return sorted(int(m.group(1)) for m in PLAN_PHASE_RE.finditer(text))


# --- the date ------------------------------------------------------------

def last_synced():
    """The date of the last commit that touched gatelog.md, or `unknown`.

    Never a fabricated date: outside a git checkout (or with an untracked
    gatelog, which is what the negative controls produce) this says `unknown`.
    """
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ad", "--date=short", "--",
             os.path.relpath(GATELOG, BASE)],
            cwd=BASE, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else "unknown"


# --- the blocks ----------------------------------------------------------

def render_header(gate, phases):
    """The document's opening status. Truth first, provenance second."""
    missing = [n for n in phases if not gate.get(n, {}).get("done")]
    lines = [HEADER_BEGIN]
    if missing:
        # Never hardcoded "COMPLETE": if the gatelog stops a phase, say which.
        # The verb must agree with the SUBJECT, which is the whole phase list
        # (`phases 10, 11, ...`), not with how many are missing. Keying it off
        # `len(missing)` rendered "phases 10, 11, 12, 13, 14, 15 ... is not" for
        # a single missing phase — in the one block whose entire job is to be
        # unambiguous to a context-free reader.
        lines.append(
            "**Status: INCOMPLETE** — phases %s of `gatelog.md` %s recorded DONE."
            % (", ".join(str(n) for n in phases),
               "is not" if len(phases) == 1 else "are not"))
        for n in missing:
            st = gate.get(n, {}).get("status", "") or "no Status line"
            lines.append("  - Phase %d: `%s`" % (n, st))
        lines.append("")
        lines.append("Do not treat the phases above as finished work until the gatelog says so.")
    else:
        lines.append(
            "**Status: COMPLETE** — phases %s of `gatelog.md` are all recorded DONE."
            % ", ".join(str(n) for n in phases))
        lines.append("")
        lines.append(
            "Each phase's evidence, exit criteria and blocked legs live in `gatelog.md`; "
            "see that file's `### Findings` for the phase before trusting a summary here.")
    lines += [
        "",
        "**Source of truth:** `gatelog.md`. This block is GENERATED by "
        "`tools/gen_plan_status.py` from that file — do not hand-edit it; edit the",
        "gatelog and run `python3 tools/gen_plan_status.py --write`. "
        "`tools/test_gen_plan_status.py` fails if it drifts.",
        "**Last synced:** %s (date of the last commit touching `gatelog.md`; "
        "`unknown` outside a git checkout)." % last_synced(),
        HEADER_END,
    ]
    return "\n".join(lines)


def render_phase(n, info):
    """One phase's status line. DONE-ness comes from the gatelog, never a literal."""
    lines = [phase_begin(n)]
    if info.get("done"):
        when = " (%s)" % info["date"] if info.get("date") else ""
        lines.append(
            "**Status: DONE**%s — recorded in `gatelog.md` (authoritative); see that "
            "file's Phase %d" % (when, n))
        lines.append(
            "findings for the evidence. Status line generated by "
            "`tools/gen_plan_status.py`.")
    else:
        st = info.get("status") or "no Status line"
        lines.append("**Status: NOT DONE** — see `gatelog.md` Phase %d (recorded: `%s`)."
                     % (n, st))
        lines.append(
            "Status line generated by `tools/gen_plan_status.py`; the verdict is the "
            "gatelog's, not this file's.")
    lines.append(phase_end(n))
    return "\n".join(lines)


# --- the inventory table -------------------------------------------------

#: Files counted by default. Every one the §1 table listed individually.
INVENTORY_FILES = ["index.html", "appcore.js", "bridge.py", "bridge_daemon.py",
                   "test_e2e.py"]

#: A table row's trailing cell: the count, or an em dash for a row with no single
#: file. Kept for documentation of the shape; row splitting is cell-wise below.
COUNT_CELL_RE = re.compile(r"\|\s*([\d]+\s+lines|—)\s*\|\s*$")


def count_lines(root, rel):
    """Lines in the real file, counted the way `wc -l` counts them.

    A missing file is an error, never a zero — a zero would look like "this file is
    empty", which is the false agreement this generator exists to prevent.

    Counted as NEWLINES, not as iterated lines, on purpose. `index.html` and
    `appcore.js` have no trailing newline, so Python's `sum(1 for _ in fh)` calls
    their last line 2288 and 2084 while every other tool — and every human who
    checks this table — says 2287 and 2083. A generated number a reader cannot
    reproduce with `wc -l` is exactly the rot this generator was written to kill,
    so the doc's numbers match `wc -l`. `errors='replace'` keeps a stray byte from
    turning a docs refresh into a crash.
    """
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        raise SystemExit("FAIL: inventory file does not exist: %s" % rel)
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().count("\n")


def parse_inventory_rows(text):
    """The existing table's rows as `(unit, language, role, size_cell)` tuples.

    Read from the doc so the human-written Language/Role prose is PRESERVED and
    only the counts get regenerated. Works whether the table is the original
    hand-typed one or an already-generated block, so `--write` is idempotent.

    Split on whole cells rather than matching a leading `` `file` `` token, because
    the last row's first cell is a LIST — `` `sw.js`, `manifest.webmanifest`, icons,
    GIFs `` — and a token-shaped regex drops that row silently. Losing a row on the
    first `--write` would be the same class of bug this generator exists to fix.
    """
    rows = []
    seen_header = False
    for line in text.splitlines():
        if re.match(r"^\|\s*(?:File|Unit)\s*\|", line):
            seen_header = True
            continue
        if not seen_header:
            continue
        if re.match(r"^\|[\s-]+\|", line):  # the |---|---| separator row
            continue
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def _single_path(root, unit):
    """The repo-relative path a table row names, or None when it names several.

    A row is counted only if its first cell is a single backticked path that exists.
    That leaves glob rows (`tests/frontend/*.test.js`) and asset groups on the em
    dash they already carried, instead of inventing a count for a set.
    """
    m = re.fullmatch(r"`([^`]+)`", unit)
    if not m:
        return None
    return m.group(1) if os.path.exists(os.path.join(root, m.group(1))) else None


def render_inventory(text, root):
    """The inventory table with FRESH line counts and the existing prose."""
    rows = parse_inventory_rows(text)
    if not rows:
        raise SystemExit("FAIL: could not find the §1 file-inventory table in %s" % PLAN)
    lines = [INVENTORY_BEGIN,
             "| File | Language | Role | Lines |",
             "|---|---|---|---|"]
    for unit, language, role, _size in rows:
        target = _single_path(root, unit)
        cell = ("%d lines" % count_lines(root, target)) if target else "—"
        lines.append("| %s | %s | %s | %s |" % (unit, language, role, cell))
    lines.append("")
    lines.append("Counts are measured from the real files by `tools/gen_plan_status.py`;")
    lines.append("run `--write` after touching any of them.")
    lines.append(INVENTORY_END)
    return "\n".join(lines), [r[0] for r in rows]


# --- block placement -----------------------------------------------------

def find_block(text, begin, end):
    return text.find(begin), text.find(end)


def _legacy_header_span(text):
    """The hand-written `**Status:** …` line the generated header supersedes."""
    m = re.search(r"^\*\*Status:\*\*[^\n]*\n", text, re.M)
    return (m.start(), m.end()) if m else None


def _legacy_phase_span(text, n):
    """Where a phase's generated status block goes: where the hand-written one is.

    Two cases, and both are real on this doc:
      * phase 15 has a hand-written status paragraph (four wrapped lines) — the
        block REPLACES it, so the file never carries two DONE claims;
      * phases 10-14 have none — the block is INSERTED at a zero-width point just
        after the heading.

    Only consumed when the line IMMEDIATELY after the heading starts with
    `**Status`. That guard matters: every phase heading is followed (with no blank
    line) by its `**Goal:**`, and a block that swallowed that would delete the
    phase's purpose. The status is taken as a whole paragraph so a wrapped status
    does not leave orphaned prose.
    """
    h = re.search(r"^### Phase %d — [^\n]*\n" % n, text, re.M)
    if not h:
        return None
    rest = text[h.end():]
    if not rest.startswith("**Status"):
        return (h.end(), h.end())  # nothing hand-written to take over
    end = h.end()
    for line in rest.splitlines(keepends=True):
        if not line.strip():
            break
        end += len(line)
    return (h.end(), end)


def _legacy_inventory_span(text):
    """The hand-typed §1 table, header row through its last row."""
    m = re.search(r"^\|\s*(?:File|Unit)\s*\|.*(?:\n\|[^\n]*)*\n?", text, re.M)
    return (m.start(), m.end()) if m else None


def replace_or_insert(text, begin, end, block, legacy_span_fn):
    """Replace the block between markers, or take over the hand-written copy.

    Once the markers exist, only the text between them is ever rewritten — the
    surrounding prose is never touched, so `--write` is idempotent and a human's
    edit next to a block survives.

    On the FIRST run the doc has no markers, so `legacy_span_fn` selects the
    hand-written text this block supersedes and it is REPLACED, not shadowed.
    Inserting above a stale `**Status:** planned (not started)` line or a stale
    `| … | 1356 lines |` table would leave two contradictory answers in one file,
    which is the bug this generator was written to remove.
    """
    i, j = find_block(text, begin, end)
    if i != -1 and j != -1 and j >= i:
        return text[:i] + block + text[j + len(end):], False

    span = legacy_span_fn(text)
    if span is None:
        raise SystemExit("FAIL: no anchor found in %s — cannot place the block:\n%s"
                         % (PLAN, begin))
    # The block must not fuse with whatever follows it. A span that consumed the
    # legacy line's own newline (the header line, the whole table) would otherwise
    # leave `<!-- END … -->**Requirements source:**` on one line — valid markdown
    # that renders wrong, and impossible to notice in a 226 KB document.
    out = text[:span[0]] + block + text[span[1]:]
    if not out.startswith("\n", span[0] + len(block)):
        out = out[:span[0] + len(block)] + "\n" + out[span[0] + len(block):]
    return out, True


def apply_all(text, header, phase_blocks, inventory):
    """All three block kinds, in one pass, each independently anchored."""
    inserted = []
    out, was = replace_or_insert(text, HEADER_BEGIN, HEADER_END, header,
                                 _legacy_header_span)
    inserted.append(("header", was))
    for n, block in phase_blocks:
        out, was = replace_or_insert(
            out, phase_begin(n), phase_end(n), block,
            lambda t, n=n: _legacy_phase_span(t, n))
        inserted.append(("phase %d" % n, was))
    out, was = replace_or_insert(out, INVENTORY_BEGIN, INVENTORY_END, inventory,
                                 _legacy_inventory_span)
    inserted.append(("inventory", was))
    return out, inserted


# --- check ---------------------------------------------------------------

def check(text):
    """Every drift as a human-readable line. Empty list means the doc is current."""
    problems = []
    gate = parse_gatelog(read(GATELOG))
    phases = phases_in_plan(text)
    if not phases:
        problems.append("no `### Phase N —` headings found in %s" % PLAN)
        return problems

    for n in phases:
        i, j = find_block(text, phase_begin(n), phase_end(n))
        info = gate.get(n)
        if info is None:
            problems.append("phase %d: no `## Phase %d` entry in %s — cannot generate a "
                            "verdict" % (n, n, os.path.basename(GATELOG)))
            continue
        if i == -1 or j == -1 or j < i:
            problems.append("phase %d: generated status block is MISSING from %s"
                            % (n, os.path.basename(PLAN)))
            continue
        want = render_phase(n, info)
        if text[i:j + len(phase_end(n))] != want:
            problems.append(
                "phase %d: status block is STALE — gatelog says `%s`, the doc's block "
                "disagrees" % (n, info["status"] or "no Status line"))

    i, j = find_block(text, HEADER_BEGIN, HEADER_END)
    if i == -1 or j == -1:
        problems.append("status header: generated block is MISSING from %s "
                        "(the hand-written line is not a source of truth)"
                        % os.path.basename(PLAN))
    elif text[i:j + len(HEADER_END)] != render_header(gate, phases):
        problems.append("status header: generated block is STALE w.r.t. %s"
                        % os.path.basename(GATELOG))

    i, j = find_block(text, INVENTORY_BEGIN, INVENTORY_END)
    if i == -1 or j == -1:
        problems.append("file inventory: generated block is MISSING from %s"
                        % os.path.basename(PLAN))
    else:
        block_text = text[i:j]
        counted = dict((m.group(1), m.group(2)) for m in
                       re.finditer(r"^\|\s*`([^`]+)`\s*\|[^|]*\|[^|]*\|\s*(\d+)\s+lines\s*\|",
                                   block_text, re.M))
        for rel in INVENTORY_FILES:
            if rel not in counted:
                problems.append("file inventory: row for `%s` is MISSING" % rel)
        # Every row the table actually counts, not only the five above — a row
        # added to the table later is checked the moment it carries a number.
        for rel, said in sorted(counted.items()):
            try:
                real = count_lines(BASE, rel)
            except SystemExit as exc:
                problems.append(str(exc))
                continue
            if int(said) != real:
                problems.append("file inventory: `%s` says %s lines, the real file has %d"
                                % (rel, said, real))
    return problems


# --- entry points --------------------------------------------------------

def build():
    text = read(PLAN)
    gate = parse_gatelog(read(GATELOG))
    phases = phases_in_plan(text)
    phase_blocks = [(n, render_phase(n, gate.get(n, {"status": "", "done": False})))
                    for n in phases]
    inventory, _rows = render_inventory(text, BASE)
    return text, render_header(gate, phases), phase_blocks, inventory


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    unknown = [a for a in sys.argv[1:] if a.startswith("--")
               and a not in ("--check", "--write")]
    if unknown:
        print("FAIL: unknown argument(s): %s" % " ".join(unknown), file=sys.stderr)
        print("usage: gen_plan_status.py [--check | --write]", file=sys.stderr)
        return 2
    if args:
        print("FAIL: unexpected argument(s): %s" % " ".join(args), file=sys.stderr)
        return 2

    text, header, phase_blocks, inventory = build()

    if "--check" in sys.argv:
        problems = check(text)
        if problems:
            print("FAIL: %s has %d generated block(s) out of date w.r.t. %s and the "
                  "real files:" % (os.path.basename(PLAN), len(problems),
                                   os.path.basename(GATELOG)))
            for p in problems:
                print("      - %s" % p)
            print("      run: python3 tools/gen_plan_status.py --write")
            return 1
        print("ok: %s's status header, phase statuses and file inventory match %s "
              "and the real files" % (os.path.basename(PLAN), os.path.basename(GATELOG)))
        return 0

    if "--write" in sys.argv:
        out, inserted = apply_all(text, header, phase_blocks, inventory)
        with open(PLAN, "w", encoding="utf-8") as fh:
            fh.write(out)
        fresh = [name for name, was in inserted if was]
        print("wrote the generated status + inventory blocks into %s%s"
              % (PLAN, " (inserted: %s)" % ", ".join(fresh) if fresh else " (all markers "
                                 "already present — content refreshed only)"))
        return 0

    print(header)
    print()
    for _n, block in phase_blocks:
        print(block)
        print()
    print(inventory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
