# CODE REVIEW — COGITATOR (post v1 feature work)

Reviewer run: 2026-09-20 (cron, read-only — no product code changed)
Scope: whole repo on `main` @ 8b30be1. All suites green at review time:
`phase0/1/2/3` frontend jsdom suites + `python3 test_e2e.py` (0 FAILURES).
Files reviewed: `appcore.js`, `index.html` (inline script), `bridge.py`, test rig.

Re-review run: 2026-09-22 (Phase 8 close-out, read-only). Scope: `main` @ post-Phase-7.
All suites green (`npm test` → frontend ALL GREEN incl. `phase8_streamagg`; `python3
test_e2e.py` → 0 FAILURES). Item **#1 is now FIXED** (Phase 8); a fresh pass over the
streaming/budget/agent-loop seams produced items **10-13** below. None are blocking.

Severity key: **[BUG]** broken behavior, **[LATENT]** only on a rarely-hit path,
**[UX]** awkward but works, **[SMELL]** maintainability / consistency.

---

## 1. [LATENT BUG] Duplicated response content on the `chat.end` stream shape
**STATUS: FIXED (Phase 8, 2026-09-22).** `index.html` now reads `acc.content+=o.content||''`
in the aggregate branch (was `o.content||acc.content`). Covered by
`tests/frontend/phase8_streamagg.test.js` (5 cases; at RED it printed
`stored content is exactly "Hello world" (got "Hello worldHello world")`). The parity test
asserts the per-delta SSE path still concatenates normally. Kept below as the original finding.

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

## 10. [LATENT] Context-budget walk can split an assistant `tool_calls` message from its `tool` result

`index.html:601-620` (`buildMessages`) walks the transcript newest→oldest and `break`s as soon
as the next message would exceed the budget:

```js
for(let i=end-1;i>=0;i--){const m=c.messages[i];
  const cost=…;
  if(used+cost>budget&&msgs.length)break;   // <-- cuts anywhere, including mid tool-pair
```

A `role:'tool'` message and the `assistant` message carrying its `tool_calls` are serialized as
**separate** entries (lines 606-615). If the budget boundary lands between them, the request can
carry an assistant message whose `tool_calls` have no matching `tool` reply, or (the other way
round) a `tool` reply whose assistant message was dropped. Strict OpenAI-compatible servers
reject that with a 400 (`tool_calls` must be followed by a tool message for every id) — the turn
then dies with `[ RITE FAILED: HTTP 400 …]`. Reachable only on a long agent transcript near the
context limit, i.e. exactly when an agent is mid-task.

**Suggestion:** when a cut would orphan a call/result pair, drop the whole pair — extend the walk
so a `tool` message is skipped together with its preceding assistant-with-`tool_calls`, and never
let a request end on a dangling pair.

---

## 11. [SMELL] A reasoning turn is emitted as two consecutive `assistant` messages

`index.html:608-615`: when a stored assistant message has `thinking`, the loop pushes
`{role:'assistant', content:'[COGITATION]: '+m.thinking}` **and then** the real
`{role:'assistant', content:m.content, tool_calls?}`. Two consecutive assistant messages violate
the strict alternation some OpenAI-compatible proxies (and Anthropic-shaped backends) enforce.
OpenAI itself tolerates it, so this is a smell, not a live bug.

**Suggestion:** fold the cogitation text into the same message (prefixed) rather than emitting a
sibling, or merge into one assistant message per stored turn.

---

## 12. [SMELL] The validator's "missing id" rejection is dead code on the live path

`appcore.js:287` rejects a call with no `id`, and the Phase 7 unit tests exercise that branch —
but the only live producer is `finalizeToolCalls` (`index.html:954-958`), which **always** mints
one: `id:t.id||('call_'+msgIndex+'_'+ix+'_'+Date.now().toString(36))`. So on the real agent path
the gate can never observe an id-less call; the check only guards direct/other callers. Useful as
defence-in-depth, worth knowing so a future reader doesn't assume the live path exercises it.

---

## 13. [SMELL] A single shared `approvalResolve` is never cleared

`index.html:930-942` (`approveToolCall`) stores the pending resolver in one module-level
`approvalResolve` and never nulls it on settle. Tool dispatch is sequential (`for(const tc of
gate.sanitized){ … await executeTool(tc) }`), so today only one approval can be pending at a time
and the pattern is safe — but any future parallel dispatch (or a second modal source) would
silently clobber the first promise, leaving that `executeTool` awaiting forever. Clearing
`approvalResolve=null` on settle, and rejecting when one is already pending, would make the
invariant explicit.

---

## Summary

No new **live** bugs found — the shipped e2e suites (which cover the security
jaw of `bridge.py` thoroughly and the frontend features end-to-end)
are genuinely green and the implementation is solid for its scale.

The one real latent defect (**#1**, content duplication on the `chat.end` aggregate stream
shape) was **FIXED in Phase 8** with a dedicated jsdom suite driving the real send path, now at
0 failures. The Phase 8 re-review produced items **#10-13**: #10 is the one worth scheduling next
(it can break a long agent turn against a strict OpenAI-compatible endpoint); #11-13 are
consistency/robustness smells with no urgent fix needed. Items #2-4 (attachment UX), #5-6
(duplication / magic numbers) and #8-9 (nits) remain open from the first pass as opt-in cleanups.