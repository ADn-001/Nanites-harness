# CODE REVIEW — COGITATOR

Severity key: **[SEC]** security, **[BUG]** broken behavior, **[LATENT]** only on a rarely-hit path,
**[UX]** awkward but works, **[SMELL]** maintainability / consistency, **[PITFALL]** process/deploy trap,
**[GAP]** missing test coverage.

| Review pass | Date | Scope | Head |
|---|---|---|---|
| 1 | 2026-09-20 | whole repo, read-only | `8b30be1` (items 1-9) |
| 2 | 2026-09-22 | Phase 8 close-out, read-only | post-Phase-7 (item 1 FIXED, items 10-13 added) |
| 3 | 2026-09-22 | post-Phase-8 gate, read-only | `b3158c9` (**items 14-26**, statuses of 1-13) |
| 4 | 2026-09-22 | Phase 9 close-out verification, read-only re-check | `phase9` commit on top of `ddeefcf` (**items 14-17 FIXED**, items 28-29 added) |

**Pass 4 baseline (re-verified before reviewing, per the standing directive):**
`npm test` → `FRONTEND SUITE: ALL GREEN` (phases 0,1,2,3,5,6,7,8 + new `phase9_tok_copy`) and
`python3 test_e2e.py` → `0 FAILURES` (66 cases). Phases 0-9 are DONE in `gatelog.md`. Pass 4
verified the Phase 9 fixes against the reproduction steps recorded in pass 3 (each was re-run: the
daemon now answers 403 to a foreign Origin on `/status`, `/health`, `/pick_directory`,
`/set_workdir`, `/start`, `/stop`, `/install_autostart`, `/remove_autostart` with no side effect;
a 20 000-char attachment prices at 5 000 tokens; COPY yields text) and recorded two new findings the
Phase 9 work surfaced in adjacent code.

**Pass 3 baseline (re-verified before reviewing, per the standing directive):**
`npm test` → `FRONTEND SUITE: ALL GREEN` (phases 0,1,2,3,5,6,7,8) and
`python3 test_e2e.py` → `0 FAILURES`. All phases 0-8 are DONE in `gatelog.md`; no half-finished
phase or stale gate entry was found. Product code was **not** modified by this pass — every new
claim below is either quoted from the source with its line, or reproduced by an executed probe.

---

# Pass 3 — new findings (post-Phase-8)

## 14. [SEC] `bridge_daemon.py` has **no Origin guard at all** — any web page can drive it

> **FIXED in Phase 9.** `bridge_daemon.py` now has `Handler._origin_allowed()` mirroring
> `bridge.py` (missing Origin = curl/native allowed; `localhost`/`127.0.0.1` allowed; everything
> else refused with `403 {"ok":false,"error":"origin not permitted"}`) enforced as the **first**
> statement of `do_GET` and `do_POST`, before any route body runs. It accepts the same
> `--allow-any-origin` / `--allow-file-origin` opt-ins. `test_e2e.py` pins the whole matrix,
> including the negative side effects: a refused `/set_workdir` writes no `bridge.py` and leaves
> the workdir unchanged, and a refused `/install_autostart` creates no autostart entry. The
> token-based hardening suggested below is still **not** implemented — see the residual note at the
> end of this item.

`bridge.py` restricts callers via `_origin_allowed()` (`bridge.py:214-221`) and the suite pins that
("foreign Origin refused"). The **daemon does not**: `bridge_daemon.py:242-304` implements
`_cors()`/`do_GET`/`do_POST` with **no origin check on any route** and answers
`Access-Control-Allow-Origin: *`. Every route is privileged: `/set_workdir` writes `bridge.py` into
an operator-supplied absolute path and can spawn a python worker there, `/start` / `/stop` control
that worker, `/install_autostart` registers a login autostart entry, `/pick_directory` opens a
native dialog.

**Reproduced** (isolated copies under `/tmp`, ports 8994/8995, foreign `Origin: https://evil.example`):

```
[3] DAEMON /status with a FOREIGN Origin      -> 200 {"ok": true, ...}
[4] DAEMON /set_workdir  FOREIGN Origin       -> 200 {"ok": true, "message": "workdir set to /tmp/cogrecon/jail2"}
    bridge.py planted by a foreign origin:    -rw-r--r-- 13204 bytes  /tmp/cogrecon/jail2/bridge.py
[5] DAEMON /install_autostart FOREIGN Origin  -> 200 {"ok": true, "result": "installed (/home/user/.config/autostart/CogitatorBridgeDaemon.desktop)"}
```

So *any* page the operator visits can re-point the bridge at any writable directory, plant a
`bridge.py` there, spawn a python process, and register login persistence — the bridge's whole
Origin security model is bypassed by simply talking to port 8930 instead of 8931. (The probe's
autostart entry was removed and both probe processes/pid files were cleaned up; nothing in the repo
was touched.)

**Fix:** give the daemon the same `_origin_allowed()` check as the bridge (localhost/null only), and
prefer a stronger control than Origin for the daemon: mint a random token at daemon start, require
it as a header on every non-`/health` route, and have the frontend obtain it out-of-band. At minimum
add the origin check — the asymmetry with `bridge.py` is not defensible.

## 15. [SEC] `Origin: null` is accepted by the bridge — a sandboxed iframe on any site is "local"

> **FIXED in Phase 9.** `bridge.py::_origin_allowed()` no longer returns `True` for `Origin: null`;
> a missing Origin is still allowed (curl/native callers) and `null` is now behind the explicit
> `--allow-file-origin` flag, which the other code paths of this repo do not pass. The startup
> banner, the module docstring and `/health` (`origins_desc()`, `allow_file_origin`) report the
> policy. `test_e2e.py` flips the old "null Origin (file:// page) allowed" case to
> `403 null Origin refused by default` and adds a second bridge instance started with
> `--allow-file-origin` asserting `null` → 200 while a foreign origin still gets 403.

`bridge.py:218-219` returns `True` for a **missing or `"null"`** Origin. Missing is required for
`curl`/native callers, but `"null"` is also what a browser sends for a `sandbox`ed iframe, a `data:`
or `blob:` document, and `file://` pages — i.e. content from *any* site can obtain an opaque origin
and be treated as trusted. Combined with `Access-Control-Allow-Origin: *` the response is readable,
so this is both write and exfiltration.

**Reproduced:**

```
[1] BRIDGE /tools/execute  Origin: https://evil.example -> 403 {"ok": false, "error": "origin not permitted"}   (guard works)
[2] BRIDGE /tools/execute  Origin: null                 -> 200 {"ok": true, "result": "WROTE pwned.txt (6 bytes, 1 lines)"}
    file written into jail by the null-origin caller:  NULLED
```

The suite pins `null Origin (file:// page) allowed` as intended, so this is a deliberate tradeoff —
but the tradeoff currently grants arbitrary read/write inside the jail to any hostile page that can
create an opaque-origin frame. **Fix:** stop accepting `null` by default. The app already tells the
operator to serve it over http (`python -m http.server`) rather than opening it as a file, so a
`file://` origin is not a required trust case; gate it behind an explicit `--allow-file-origin` (or
require a token) and extend `test_e2e.py`'s origin matrix accordingly.

## 16. [BUG] The live `tok()` is array-blind, so **attachments cost ~1 token** in the gauge and in the budget

> **FIXED in Phase 9.** `index.html:430` is now `const tok=s=>CogCore.tok(s);` — the live helper
> delegates to the array-aware implementation in `appcore.js`, so every call site (`ctxTokens`,
> `ctxUsage`, the `buildMessages()` budget walk, `maybeAutoCompact`, `compactChat`) prices
> multimodal content by characters and image parts. Side effect of the same change: `tok()` on an
> already-parsed object (a `toolCalls[].args` object) used to return `NaN` and now returns a finite
> number. New suite `tests/frontend/phase9_tok_copy.test.js` pins the unit values, the rendered
> `#ctx-pct`/`#ctx-bar`/`#ctx-fill` for a 20 000-char attachment (5000 tokens, `5.0k/131.1k`), the
> non-NaN property, and a plain-string regression (`100/131.1k`).

`appcore.js` ships an array-aware `CogCore.tok` (`appcore.js:37-48`, sums text parts + 85/img), and
gatelog Phase 3 claims the gauge/budget "stay sane" because of it. But `index.html:430` defines its
**own** `const tok=s=>Math.max(1,Math.ceil((s||'').length/4));`, and no shipped code path ever calls
`CogCore.tok` (repo-wide grep for `CogCore.tok`: zero references outside `appcore.js`). For a
multimodal message `s.length` is the **number of parts**, not characters.

**Reproduced** in the real app (jsdom, seeded chat with one user message carrying a 20 000-char
inline attachment):

```
live_tok_on_array      = 1
core_tok_on_array      = 5000
ctx_pct                = "1/131.1k"
ctx_bar_title          = "CONTEXT LOAD: ~1 TOKENS ACROSS 1 TRANSMISSIONS (SYSTEM PROMPT + SUMMARY INCLUDED)"
```

Consequences, all live: the context gauge and `#ctx-bar` tooltip under-report by orders of magnitude
whenever attachments are used; `buildMessages()`'s budget walk (`index.html:601-603`) treats a
20 000-char attachment as free, so a request can exceed `ctxLimit` and be truncated or rejected by
the server; `maybeAutoCompact()` (`index.html:666`) will not fire when the real cause of the blowup
is attachments; `compactChat`'s retention budget (`index.html:645-651`) is computed the same way.
Note the same `tok` also prices `m.thinking` and tool-call args, which are always strings, so the
defect is specific to array content — exactly the case Phase 3 added.

**Fix:** delete the local `tok` and use `CogCore.tok` (already implemented and, per item 26, never
tested), or make the local one delegate: `const tok=s=>CogCore.tok(s);`.

## 17. [BUG] COPY on an attachment message copies `[object Object],[object Object]`

> **FIXED in Phase 9.** `index.html` `msgAction('copy')` now writes
> `CogCore.contentText(c.messages[i].content)`. Pinned by the COPY case in
> `tests/frontend/phase9_tok_copy.test.js`, which stubs `navigator.clipboard` and clicks the real
> rendered COPY control (and also calls `msgAction('copy', 0)` directly), asserting the written
> string is the joined attachment text and contains no `[object Object]`.

`index.html:556` (`msgAction`) does `navigator.clipboard?.writeText(c.messages[i].content)`. For a
user message with attachments `content` is a multimodal **array**, and Clipboard `writeText` coerces
it. Reproduced: `String([{type:'text',text:'aaaa'},{type:'text',text:'bbbb'}])` →
`[object Object],[object Object]`. Fix: `CogCore.contentText(c.messages[i].content)` (already used by
`msgHTML`/`autoTitle` for exactly this reason).

## 18. [BUG] The 180 s stream timeout spans the **entire agent loop**, and its error is not recognised as a halt

`stream()` creates one signal (`index.html:1067`, `streamSignal(180000)` = `AbortSignal.any([abortCtl.signal, AbortSignal.timeout(180000)])`, `index.html:689-692`) and hands it to
`runAgentLoop`, which reuses it for **every** iteration — up to 12 model calls plus all tool calls
inside a single 180-second wall clock. A legitimate multi-step agent turn therefore dies mid-loop on
a slow endpoint. Worse, the failure is misclassified: `AbortSignal.timeout` rejects with a
`DOMException` whose `name` is **`TimeoutError`**, while `stream()`'s catch only special-cases
`'AbortError'` (`index.html:1077`). Reproduced: `timeout abort: name = TimeoutError` vs
`manual abort: name = AbortError`. So a timeout is rendered to the operator as
`[ RITE FAILED: ... ]` instead of a halt, and the operator-halt wording never appears.

**Fix:** create the timeout per model call (or budget it generously per iteration) and treat
`TimeoutError` like `AbortError` (or check `sig.reason?.name`).

## 19. [BUG/LATENT] `compactChat` discards transcript even when the summary came back empty

`index.html:643-654`: the compression call's result is used as
`c.summary=(c.summary?c.summary+'\n\n[DEEPER PAST] ':'')+res.trim()`, then the retained message
window is computed against `Math.ceil(ctxLimit*0.35)-tok(c.summary)` and the rest of `c.messages` is
**sliced away** — with no check that `res` is non-empty. A summary response that is empty (or
tool-call-only, or CORS-truncated) therefore silently deletes history that was never written into
the summary: worst case `c.summary=''` and everything before the retained window is gone. Reachable
in agent mode for a second reason: `compactChat` → `callModel(..., true)` → `callOpenAI`, which
appends `tools=TOOL_SCHEMAS; tool_choice='auto'` whenever `settings.agent` is on
(`index.html:765`), so the *compression* request advertises tools and invites a tool-call-only reply
with empty `content` — the exact shape that empties the summary. (The LM-Studio soul-load retry at
`index.html:1083` calls `callModel` the same way.)

**Fix:** `if(!res.trim()) throw new Error('empty summary — archive preserved');` before touching
`c.summary`/`c.messages`; optionally pass a tool-free flag through `callModel` for non-agent
requests (compaction, titles, retries).

## 20. [UX] SAVE AS PROFILE can only ever **overwrite** the active profile

`index.html:1233-1238` saves with `id: settings.activeProfile || undefined`. With any profile
active, pressing SAVE AS PROFILE updates that profile in place — there is no way to create a second
profile while one is active (the operator must DELETE the active profile first, which also clears
live endpoint/model/key). The label promises "save as". **Fix:** add an explicit NEW / SAVE-AS-NEW
button that passes `id: undefined`, and keep SAVE as update; or key on whether the name field
matches an existing profile.

## 21. [PITFALL] `sw.js` is cache-first and `CACHE` has never been bumped

`sw.js` serves `caches.match(...) || fetch(...)` with **no revalidation** for same-origin GETs, and
`const CACHE='cogitator-v10'` has changed only once in the repo's history (`git log -- sw.js` → the
initial commit), while `index.html`/`appcore.js` changed across all nine phases. Any operator with
the app installed as a PWA keeps receiving the stale shell until they clear site data — a fix merged
and tested here can simply never reach them, and it will look like "the change didn't work".
**Fix:** bump `CACHE` as part of any release touching `index.html`/`appcore.js` (make it a
documented release step, or switch the shell to stale-while-revalidate so a refresh self-heals).

## 22. [NIT] TEST CONNECTION mutates live settings without persisting them

`index.html:1296`: `settings.endpoint = $('set-endpoint').value.trim() || DEF_SETTINGS.endpoint;`
then `testConnection(false)`. From the open settings modal, testing a *proposed* endpoint silently
re-points the running app's `settings.endpoint` (used by the next send) while `cogitator.settings`
still holds the old value — dismissing the modal by clicking the backdrop keeps the mutated live
value and discards nothing, so behaviour diverges from what a reload will restore. Either persist
with `saveSet()` or probe a copy.

## 23. [SMELL] Profiles have a dual source of truth; export omits them

`settings.profiles` is declared in `DEF_SETTINGS` and loaded at boot (`index.html:419-422`), but the
authoritative list lives under a *separate* localStorage key (`cogitator.profiles`, via
`CogCore.profileStore`), and `saveSet()` keeps writing the stale `settings.profiles` (in practice
always `[]`) into the settings blob. `refreshProfileList()` carries an `|| settings.profiles`
fallback for the case where `CogCore` is missing, which is the only reason the dead field exists.
Separately, EXPORT ARCHIVE (`index.html:1321`) serialises `{exported, chats}` only — deliberately
excluding the api key, but endpoint/model profiles go with it, so a migration to a new browser
silently loses every profile. Worth a note in the UI ("profiles and keys are not exported").
**Fix suggestion:** drop `profiles` from the settings blob and stop writing it, or make the store the
only reader/writer.

## 24. [SMELL] The bridge URL is now duplicated four times (extends #5)

`'http://127.0.0.1:'+(settings.bridgePort||8931)+'/tools/execute'` appears at `index.html:575`
(`getWorkdirListing`), `948` (`executeTool`), `1032` and `1042` (`openWorkdirPicker`), while the
daemon base is a different constant (`DAEMON_URL`, `1109`). Correct today (bridge and daemon are
distinct services) but the port/URL plumbing should be a single `bridgeUrl()` helper. Note the
bridge calls correctly do **not** carry `authHeaders()` — only the model endpoint gets the key;
keep that invariant when refactoring.

## 25. [INFO] The API key sits in localStorage in plaintext

`settings.apiKey` and `profileStore`'s per-profile `apiKey` are persisted unencrypted under
`cogitator.settings` / `cogitator.profiles`. Inherent to a static frontend and it is *not* leaked
into chat payloads, `[WORKDIR CONTEXT]`, the bridge, or the export archive — recorded so it is a
known, accepted property rather than a surprise. Anything that ever adds third-party scripts to this
origin would read it.

## 26. [GAP] Test coverage holes that let 14-16 through

- **`tok` / gauge pricing is untested in the shipped path.** `grep -rn "tok" tests/` finds only
  `phase2_sysprompt.test.js`'s local variable `toks`; there is **no** assertion on `tok`,
  `CogCore.tok`, `ctxTokens`, or the `#ctx-pct` gauge. So Phase 3's gate passed while the live gauge
  disagreed with the function it claims to use (item 16).
- **`bridge_daemon.py`'s HTTP surface has no origin test.** `test_e2e.py` asserts origin refusal for
  `bridge.py` (5 origin cases) and drives the daemon for lifecycle only — which is exactly why
  item 14 could sit in the shipped code unnoticed. Any new daemon route should come with an origin
  case in `test_e2e.py`.
- The new findings 16/17 are cheap to pin as frontend e2e cases (seeded attachment message →
  assert `#ctx-pct` is in the right order of magnitude; assert COPY yields text, not
  `[object Object]`).

---

## 27. [SMELL] A runtime log file is tracked in git and is rewritten by every run

`bridge_daemon.log` is in the index (`git ls-files` → `bridge_daemon.log`), and the daemon appends
to it on every start/stop (`bridge_daemon.py:23-30`, `log()`), including from `test_e2e.py`'s daemon
lifecycle cases. The result is a permanently dirty working tree: any commit made after running the
suite either carries test noise or needs a manual revert, and `git status` stops being a useful
signal for the next agent. **Fix:** `git rm --cached bridge_daemon.log`, add it (and
`bridge_daemon.pid`) to `.gitignore` — the repo's `.gitignore` is currently a single line.

---

## 28. [BUG] The compaction prompt stringifies array content — attachment text is lost from the summary

`index.html` `compactChat` builds its compression prompt by joining
`'['+m.role.toUpperCase()+'] '+m.content` (~line 642) after pushing `{role, content: m.content}`
verbatim (~line 638). For an attachment message `content` is a multimodal **array**, so the prompt the
model receives contains `[USER] [object Object],[object Object]` instead of the attachment text —
the summary is written without the very content compaction is supposed to preserve, and then the
retained window is sliced against that summary (§19 shows the discard path). The two nearby
concatenations (assistant/tool content) are always strings, so this is array-specific.
**Fix:** `CogCore.contentText(m.content)` at the push sites or at the join. Found by the Phase 9
frontend workstream; deliberately left unfixed (out of that phase's two-fix mandate).

## 29. [LATENT] `refreshLast` renders last-message content without the array guard `msgHTML` has

`index.html` `refreshLast` (~line 562) does `el.innerHTML=renderMd(c.messages[i].content)`, while
`msgHTML` (~line 504) correctly branches: `isUser||isTool ? esc(CogCore.contentText(m.content)) :
renderMd(m.content)`. Only the last message reaches `refreshLast`, and in the streaming path that is
the assistant's, so no live array path was reproduced — but a regenerated/short-circuited turn whose
last message is a user attachment would render `[object Object]` into the transcript.
**Fix:** use the same `contentText` branch as `msgHTML`. Same provenance as #28.


---

# Status of the earlier findings (1-13)

| # | Sev | Status at `b3158c9` |
|---|---|---|
| 1 | BUG | **FIXED** (Phase 8, `acc.content+=o.content||''`, suite `phase8_streamagg`) |
| 2 | UX | open (silent attachment drop, `index.html:999`) |
| 3 | SMELL | open — `attachFiles(files,{folder:true})` still ignores `opts`; `f.webkitRelativePath` unused |
| 4 | SMELL | open — workdir picker still filters `e.tag==='f'` (`index.html:1035`) |
| 5 | SMELL | open — now 4 copies (see #24) |
| 6 | SMELL | open — `iter<12`, `ATTACH_CAP`, 85/0.25/0.35 still inline |
| 7 | INFO | open as designed — `--allow-exec` bypasses the jail; pair it with #14/#15 before exposing |
| 8 | NIT | open — `looksBinary`'s `&& /[\uFFFD]/.test(s)` still redundant |
| 9 | NIT | open — `profileStore.remove` still matches by id **or** name |
| 10 | LATENT | **open, unchanged** — `buildMessages` budget walk still `break`s mid tool-pair (`index.html:601-603`, pair serialised at `606-615`); `compactChat` mitigates it only at its own boundary (`653`) |
| 11 | SMELL | open — `thinking` still emitted as a second consecutive assistant message (`index.html:609-610`) |
| 12 | SMELL | open — validator's missing-`id` branch is still unreachable on the live path (`finalizeToolCalls` always mints one) |
| 13 | SMELL | **corrected: the flat claim is stale.** `settleApproval` DOES clear the resolver (`index.html:929` `approvalResolve=null`, present since `90e51f8`) and `halt()` uses it, so the leaked-promise path described no longer exists. What remains true: nothing rejects a *second* concurrent `approveToolCall` — it would clobber the first. Downgrade to a note, not a bug. |

---

# Prior findings (kept verbatim from pass 1/2)

## 2. [UX] Binary / non-matching attachments are silently dropped
`index.html:999` — `if(CogCore.looksBinary(text))return;` gives the operator no chip, no error, no
hint that a dropped file was ignored (e.g. `.psd`/`.tiff`, or any UTF-8-decoded binary). Suggestion:
push a non-content marker chip, or a one-line "SKIPPED <name> (binary / unreadable)" note.

## 3. [SMELL] `attachFiles(opts)` ignores its `folder` option; folder paths flattened
`index.html:991` accepts `opts` (called with `{folder:true}` at `1185`) but never reads it, and
folder inputs lose `f.webkitRelativePath` — nested workdir files arrive with no path context.
Suggestion: drop the dead argument, or honour it by using the relative path as the attachment name.

## 4. [SMELL] Workdir picker lists files only
`index.html:1035` filters to plain files, so a deep workdir cannot be browsed from the UI.

## 5. [SMELL] Endpoint/port plumbing duplicated and slightly inconsistent
Bridge host hardcoded with only the port parametrized (see #24); the daemon is a separate fixed
port (`8930`). A `bridgeUrl()` helper would keep them consistent.

## 6. [SMELL] Hard-coded magic numbers on the tool loop, no upper guard
`iter<12`, `ATTACH_CAP` 20 000, token costs 85/image, 0.25 reserve, 0.35 compaction — all inline
except `ATTACH_CAP`; worth named constants for auditability.

## 7. [INFO/PITFALL] `--allow-exec` fully bypasses the jail
`bridge.py:191-202` runs `shell=True` with `cwd=ROOT`, and `jail()` guards path arguments to file
tools only. A deliberate, documented escape hatch (off by default) — but keep it off anywhere the
endpoint is reachable beyond localhost, and note `--allow-exec` + `--allow-any-origin` together are
effectively arbitrary local code execution reachable from any web page. See also #14/#15: the
*daemon* is reachable from any page today, no flag required. Everything the security suite covers
(relative `..`, symlink escapes, git global-option bypasses, oversized POSTs) reads correct:
`jail()` realpaths and prefix-checks against `ROOT+sep`; `_git_flag_audit`/`_blocked_git_global_options`
block `-C/-c/--git-dir/--config*`; `_origin_allowed` restricts to localhost / null-origin.

## 8. [NIT] `CogCore.looksBinary` redundant condition
`appcore.js:59` — `s.indexOf('\uFFFD') !== -1 && /[\uFFFD]/.test(s)`: the RHS is equivalent to the
LHS just left of `&&`. Simplify to the single check.

## 9. [NIT] `profileStore.remove` matches by name with silent multi-delete risk
`appcore.js:386` — matching by `id` **or** `name` means two same-named profiles are both deleted;
prefer id-only (the UI already uses the id).

## 10. [LATENT] Context-budget walk can split an assistant `tool_calls` message from its `tool` result
`index.html:601-620` breaks as soon as the next message would exceed the budget, and an assistant
message carrying `tool_calls` is serialised separately from its `role:'tool'` reply. A boundary
between them yields a request that a strict OpenAI-compatible server rejects with a 400 —
reachable exactly when an agent is mid-task near the context limit. Suggestion: drop the whole
call/result pair together.

## 11. [SMELL] A reasoning turn is emitted as two consecutive `assistant` messages
`index.html:609-610` — `[COGITATION]: …` then the real assistant message violates strict
alternation on some proxies. Fold the cogitation text into one message.

## 12. [SMELL] The validator's "missing id" rejection is dead code on the live path
`appcore.js:287` vs `finalizeToolCalls` (`index.html:954-958`) which always mints an id — useful
defence in depth, but do not assume the live path exercises it.

## 13. [SMELL] Shared `approvalResolve` — see the corrected status in the table above.

---

# Summary & priority

Baseline is genuinely green, and the three phases since pass 2 (6, 7, 8) each closed a real defect
with a suite that went red first. Pass 3 found **two new live bugs** (#16 attachment token pricing,
#17 COPY on attachment messages), **one live security hole** (#14 unauthenticated daemon), two
latent-but-reachable defects (#18 agent-loop-wide timeout misclassified, #19 transcript loss on an
empty summary), and a set of UX/smell/pitfall items.

Suggested next phase (Phase 9) ordering:

1. **#14 + #15** — origin guard on `bridge_daemon.py`, and stop trusting `null` Origin by default;
   add the missing daemon origin cases to `test_e2e.py`. Security first: today the daemon is a
   local file-write + persistence primitive reachable from any web page.
2. **#16 + #17** — one-line-ish fixes (use `CogCore.tok`; `contentText` for COPY) with red-first
   frontend cases; these are operator-visible correctness bugs.
3. **#18 + #19** — agent-loop timeout semantics and the empty-summary guard before the next
   long-running agent session.
4. **#10** (still the oldest open latent defect), then #20/#21 as UX/release hygiene, and the
   remaining smells (#3,#5,#6,#11,#23,#24) as an opt-in cleanup pass.

**Phase 9 result: groups 1 and 2 are FIXED and gated green** (`npm test` exit 0; python 66 cases,
frontend 9 suites). Group 3 (#18/#19) and group 4 were not in Phase 9's scope and remain open, plus
the two new findings #28/#29 that the Phase 9 work surfaced. Suggested Phase 10 ordering:

1. **#18 + #19** — one session-long signal is shared by every agent iteration, so a slow endpoint
   kills a legitimate multi-step turn and reports it as `[ RITE FAILED ]` instead of a halt, and an
   empty/failed compaction response silently deletes transcript. Both are agent-path correctness;
   both are cheap to pin red-first (fault-inject the SSE route + a `compactChat` stub route).
2. **#28 + #29** — array-content `contentText` gaps in `compactChat`/`refreshLast`; same class of
   defect as #17, same style of test.
3. **#10** — the oldest open latent defect (context-budget walk splitting a tool call from its
   result); needs a request-shape assertion, not a UI assertion.
4. **Hygiene** — #27 (`git rm --cached bridge_daemon.log` + `.gitignore`), #21 (`sw.js` cache bump /
   stale-while-revalidate), #20 (SAVE AS NEW), #23 (single profile source of truth), #24
   (`bridgeUrl()` helper), #11/#12.
5. **Optional hardening** — the daemon/bridge still trust any *missing* Origin (curl/native), so the
   Origin guard is not authentication: a per-start random token required on every non-`/health`
   route would close that (codereview #14's preferred fix), with the frontend obtaining it
   out-of-band.
