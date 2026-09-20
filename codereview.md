# CODE REVIEW — COGITATOR (post v1 feature work)

Reviewer run: 2026-09-20 (cron, read-only — no product code changed)
Scope: whole repo on `main` @ 8b30be1. All suites green at review time:
`phase0/1/2/3` frontend jsdom suites + `python3 test_e2e.py` (0 FAILURES).
Files reviewed: `appcore.js`, `index.html` (inline script), `bridge.py`, test rig.

Severity key: **[BUG]** broken behavior, **[LATENT]** only on a rarely-hit path,
**[UX]** awkward but works, **[SMELL]** maintainability / consistency.

---

## 1. [LATENT BUG] Duplicated response content on the `chat.end` stream shape

`index.html:707` (in `processStreamObject`, the aggregated `j.type==='chat.end'`
branch):

```js
if(o.type==='message')acc.content+=o.content||acc.content;
```

When a message object carries **empty** content (a reasoning-only assistant
message, or a tool-call-only message whose content is emitted separately), the
term `o.content || acc.content` evaluates to `acc.content`, so the accumulator
appends its own already-collected content to itself — duplicating the entire
response so far. Correct form is `o.content || ''`.

Why "latent" not "live": the common OpenAI SSE path (and the Analogue/Ollama
paths) stream per-chunk `choices[].delta` objects and go through line 714-716,
which is correct. Only providers that already buffered the turn and emit one
`{type:'chat.end', result:{output:[...]}}` aggregate (llama.cpp-class servers
and some LM-Studio modes) reach this branch, and only when one of their output
objects has falsy `content`.

**Fix (when touched):** `acc.content += (o.content || '');` — one character-class
change, add a unit case to the phase2/3 harness with a content-less `message`
object asserting no self-append.

---

## 2. [UX] Binary / non-matching attachments are silently dropped

`index.html:986` (`attachFiles`):

```js
if(CogCore.looksBinary(text))return; // binary text: skip silently
```

A user who drops a `.png` (which `isImageName` misses — e.g. `.psd`, `.tiff`,
or an image with an unusual extension) gets **no chip, no error, no hint** that
the file was ignored. Same for any UTF-8-decoded binary. The operator never
learns the attachment wasn't forwarded.

**Suggestion:** on a skip, push a non-content marker chip (or a one-line inline
note "SKIPPED <name> (binary / unreadable)") rather than returning empty-handed;
optionally widen `isImageName` for common raster types.

---

## 3. [SMELL] `attachFiles(opts)` ignores its `folder` option; folder paths flattened

`index.html:978` — `attachFiles(files, opts)` accepts `opts`, and the folder
input (line 1172) calls it with `{folder:true}`, but `opts` is never read. Also,
for folder inputs (`webkitdirectory`), each file's `f.webkitRelativePath` carries
the sub-folder path yet the chip/renderer uses only `f.name` — files collected
from nested workdir folders lose their relative path context in the payload.

**Suggestion:** either drop the never-read `{folder:true}` argument, or honor it
by using `f.webkitRelativePath` as the attachment `name` so path context survives.

---

## 4. [SMELL] Workdir picker lists files only

`index.html:1022` — `parseWorkingList(...).filter(e=>e.tag==='f')` shows only
plain files in the "pick from workdir" dialog; directories are filtered out, so
a deep workdir can't be browsed recursively from the UI. Works, but limited; a
folder drill-down would let "from workdir" match the folder-attach feature.

---

## 5. [SMELL] Endpoint/port plumbing is duplicated and slightly inconsistent

- Bridge tool calls hardcode the host and only parametrize the port:
  `http://127.0.0.1:'+(settings.bridgePort||8931)` appears in `executeTool`
  (943), `getWorkdirListing` (570), `openWorkdirPicker` (1019, 1029) — a 4th copy
  of the same URL string.
- The daemon is a *different* fixed port, `http://127.0.0.1:8930` (`DAEMON_URL`,
  1096). Not wrong (bridge vs daemon are separate services), but there are now
  two magic ports + one default in different places; a `bridgeUrl()` helper that
  honors `settings.bridgePort` would keep them consistent if ever made host-aware.

---

## 6. [SMELL] Hard-coded magic numbers on the tool loop, no upper guard

The agent loop hard-caps at `iter<12` invocations (`runAgentLoop`, 955) with a
hard-coded "12" and MAX cap 20 000 (`ATTACH_CAP`) / token costs 85/`image`,
0.25 reserve, 0.35 compaction — all inline. Fine for this app's scale; would be
worth hoisting to named constants (as `ATTACH_CAP` already is) for auditability.

---

## 7. [INFO/PITFALL] `--allow-exec` fully bypasses the jail

`bridge.py:191-202` — `t_run_command` runs `shell=True` with `cwd=ROOT`; the jail
`jail()` guards *path arguments to file tools only*. A command like
`cd / && cat /etc/shadow` is executed after the bridge is started with
`--allow-exec`. This is a deliberate, documented escape hatch (off by default,
rejected with a clear message when off), *not* a defect — but keep it off in any
exposure where the endpoint is reachable beyond localhost, and note that
`--allow-exec` and `--allow-any-origin` together are effectively arbitrary-local
code execution reachable from any web page.

Everything the security e2e suite covers (relative `..`, symlink escapes, git
global-option bypasses, foreign-Origin refusal, oversized POSTs) is confirmed
correct on read: `jail()` realpaths and prefix-checks against `ROOT+sep`;
`_git_flag_audit`/`_blocked_git_global_options` block `-C/-c/--git-dir/--config*
/etc`; `_origin_allowed` restricts to localhost / null-origin.

---

## 8. [NIT] `CogCore.looksBinary` redundant condition

`appcore.js:59` — `s.indexOf('\uFFFD') !== -1 && /[\uFFFD]/.test(s)`: the RHS
`/[\uFFFD]/.test(s)` is exactly equivalent to the LHS test just left of `&&`, so
the expression `A || (B && C)` has a redundant `&& C`. Correct behavior, but
simplify to `s.indexOf('\uFFFD') !== -1` (or a single `\uFFFD|\u0000` check) to
remove the appearance of two different checks.

---

## 9. [NIT] profileStore `remove` matches by name with silent multi-delete risk

`appcore.js:224` — `remove` matches by `id` **or** `name`; two profiles sharing a
name would both be removed by a name-keyed delete. Documented as accepted for
app scale in the gate log; leaving as-is but flagging it — prefer id-only delete
from the dropdown (the UI already uses the profile `id`, so the name fallback is
only a unit-test convenience).

---

## Summary

No new **live** bugs found — the shipped e2e suites (which cover the security
jaw of `bridge.py` thoroughly and the three new frontend features end-to-end)
are genuinely green and the implementation is solid for its scale. The one real
latent defect is **#1** (content duplication on the `chat.end` aggregate stream
shape); it will not fire on the default OpenAI SSE path but should be fixed (one
line) the next time the streaming parser is touched, with a companion test case.
Items 2-4 are UX/SMELL candidates worth a follow-up pass but need no urgent fix.