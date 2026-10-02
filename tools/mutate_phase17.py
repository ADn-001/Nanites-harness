#!/usr/bin/env python3
"""Phase 17 mutation harness: do the agent-turn integrity guards have teeth?

A green suite proves the code is in the state the tests describe. It does not prove the
tests would NOTICE a regression. Each guard is broken on purpose and the suite re-run.

Every mutation asserts it APPLIED before trusting a red: a pattern that matches nothing
records a false pass, which is the most expensive way to spend a verification.

Refuses to score a run whose output lacks the real summary line — a harness that cannot
tell it did not run reports confident nonsense.

Run it after touching mergeToolDelta, refreshLast/streamTarget, stream(), halt(), or the
dispatcher's declareRites: `python3 tools/mutate_phase17.py`
"""
import os
import re
import subprocess
import sys

# Derived from this file's location, never a hard-coded absolute path: this repo is
# shared, and a personal home path in a tracked file is both a leak and wrong on every
# other machine (the convention gen_git_policy.py already follows).
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = ["node", "tests/frontend/phase17_agent_turn.test.js"]
MARKER = "PHASE 17: agent-turn stream integrity"

# The R1 review pass added its own suite file. A mutation whose only pin lives THERE must be
# scored against it, or the harness reports a hole that does not exist (or, worse, misses a
# hole that does). Which file pins which mutation was determined by RUNNING BOTH against each
# mutation, never by reading which file "looks like" it owns the fix.
JS_R1 = ["node", "tests/frontend/phase17_r1_review_fixes.test.js"]
MARKER_R1 = "PHASE 17 R1: review-found defects"

# The R2 suite pins the consequences of the R1 fixes (see the R2 header comment).
JS_R2 = ["node", "tests/frontend/phase17_r2_consequences.test.js"]
MARKER_R2 = "PHASE 17 R2: consequences of the R1 fixes"


def digest(path):
    import hashlib
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
    # ---- 0015: mergeToolDelta must not compact per delta ----
    ("M1 mergeToolDelta: compact on every delta again (the 0015 corruption)",
     "index.html",
     r"(  if\(f\.args!==undefined\)slot\.args\+=\(typeof f\.args==='string'\?f\.args:JSON\.stringify\(f\.args\)\);\n)\}",
     r"\1  acc.toolCalls=acc.toolCalls.filter(Boolean);\n}",
     JS, MARKER, True),

    ("M2 mergeToolDelta: ignore index entirely (order-dependence returns)",
     "index.html",
     r"  let idx;\n  if\(Number\.isInteger\(t\.index\)\)\{idx=t\.index;\}\n  else\{",
     "  let idx;\n  if(false){idx=t.index;}\n  else{",
     JS, MARKER, True),

    ("M2b mergeToolDelta: mint a fresh slot per index-less fragment",
     "index.html",
     r"    if\(acc\._fbIdx===undefined\|\|acc\._fbIdx>=\(acc\._next\|\|0\)\)acc\._fbIdx=\(acc\._next\|\|0\);\n    idx=acc\._fbIdx;",
     "    acc._fbIdx=(acc._next||0);\n    idx=acc._fbIdx;",
     JS, MARKER, True),

    # M3 and M3b are DOCUMENTED EQUIVALENTS, not gaps — proved by reachability, not assumed.
    # Removing the hand-off `compactToolCalls(acc)` leaves holes in `acc.toolCalls`; removing
    # `finalizeToolCalls`'s own filter does the same. Neither is observable, because the OTHER
    # layer still removes holes before anything consumes them: compaction runs at hand-off on the
    # SSE path, and `finalizeToolCalls` filters again. Each is defence in depth against the other.
    # No input reaches a hole, so no test could distinguish them. Keep BOTH — they are load-bearing
    # the moment either layer's preconditions change (a new consumer of `acc.toolCalls`, or a
    # stream path that forgets to compact).
    ("M3 hand-off: never compact (documented equivalent — finalizeToolCalls filters)",
     "index.html",
     r"  compactToolCalls\(acc\);\n  return acc;\n}\nfunction processOllamaObject",
     "  return acc;\n}\nfunction processOllamaObject",
     JS, MARKER, None),

    ("M3b finalizeToolCalls: stop filtering holes (documented equivalent — hand-off compacts)",
     "index.html",
     r"  return \(acc\.toolCalls\|\|\[\]\)\.filter\(Boolean\)\.map\(\(t,ix\)=>\(\{\n    id:t\.id\|\|\('call_'\+msgIndex\+'_'\+ix\+'_'\+Date\.now\(\)\.toString\(36\)\),",
     "  return (acc.toolCalls||[]).map((t,ix)=>({\n    id:t.id||('call_'+msgIndex+'_'+ix+'_'+Date.now().toString(36)),",
     JS, MARKER, None),

    # ---- 0011: the pump must bind the message the turn created ----
    ("M4 pump: write deltas into messages[length-1] again (the 0011 corruption)",
     "index.html",
     r"  return await pumpSSE\(r\.body,acc,\(\)=>\{\n    /\* PHASE 17 \(CR-0011\): write into the message THIS turn created, never messages\[length-1\]\. \*/\n    const t=streamTarget;const m=\(t&&t\.msg\)\|\|null;",
     "  return await pumpSSE(r.body,acc,()=>{\n    const c=active();const m=c?.messages[c.messages.length-1];",
     JS, MARKER, True),

    ("M4b pump: ignore the bound target and always take the last message",
     "index.html",
     r"  const t=streamTarget;\n  /\* A bound target wins; without one \(a non-streaming refresh\) fall back to the last message\. \*/\n  if\(t&&t\.chat&&t\.msg&&t\.chat\.messages\.indexOf\(t\.msg\)!==-1\)return refreshLastFor\(t\.chat,t\.msg\);\n  const c=active\(\);if\(!c\)return;\n  return refreshLastFor\(c,c\.messages\[c\.messages\.length-1\]\);",
     "  const c=active();if(!c)return;\n  return refreshLastFor(c,c.messages[c.messages.length-1]);",
     JS, MARKER, True),

    ("M5 refreshLastFor: resolve the message by last-index instead of identity",
     "index.html",
     r"  const i=c\.messages\.indexOf\(m\);\n  if\(i===-1\)return;",
     "  const i=c.messages.length-1;\n  if(i===-1)return;",
     JS, MARKER, True),

    # ---- 0012: every tool message needs a declaring assistant ----
    ("M6 dispatcher: drop the synthesised declaring assistant (0012 re-opened)",
         "index.html",
         r"    declareRites\(gate\.sanitized,'\[ AUTO-ROUTED READ RITE \]'\);\n", "",
         JS, MARKER, True),

        ("M6b dispatcher: drop the declaring assistant on the ACCEPTED path too",
         "index.html",
         r"    declareRites\(gate\.sanitized,'\[ OPERATOR-ACCEPTED RITE \]'\);\n", "",
         JS, MARKER, True),

    ("M7 declareRites: emit an assistant with NO tool_calls (declares nothing)",
         "index.html",
         r"    c\.messages\.push\(\{role:'assistant',content:origin\|\|'',thinking:'',toolCalls:[\s\S]*?ts:Date\.now\(\),cortexSynthesized:true\}\);",
         "    c.messages.push({role:'assistant',content:origin||'',thinking:'',toolCalls:[],ts:Date.now(),cortexSynthesized:true});",
         JS, MARKER, True),

        # M7b (added in the R1 pass): the tool_call_id axis was made legal, but the ARGUMENT ENCODING
        # was not — declareRites copied the validator's already-PARSED args object straight through,
        # so `function.arguments` reached the wire as an object where the OpenAI format requires a
        # JSON-encoded string. The suite was green over it because nothing asserted the argument
        # SHAPE, only that the id was declared. This anchor was re-pointed after the declareRites
        # body changed; the harness flagged the old one as stale rather than scoring a false pass,
        # which is the behaviour it is supposed to have.
        # M7b (added in the R1 pass): the tool_call_id axis was made legal, but the ARGUMENT
        # ENCODING WAS NOT - declareRites copied the validator's already-PARSED args object
        # straight through, so `function.arguments` reached the wire as an object where the
        # OpenAI format requires a JSON-encoded string. The suite was green over it because
        # nothing asserted the argument SHAPE, only that the id was declared.
        #
        # Scored against the R1 suite, NOT the phase-17 one, and that is a MEASURED fact rather
        # than a preference: this mutation goes GREEN on phase17_agent_turn and RED only on
        # phase17_r1_review_fixes (probed with both suites run, not assumed). Running only the
        # phase-17 file reports it ESCAPED - manufacturing a hole that does not exist - because
        # the file that pins it was never executed. A harness pointed at the wrong suite is worse
        # than no harness: it invents findings.
        ("M7b declareRites: emit raw parsed args (object instead of JSON string)",
         "index.html",
         r"        args:\(typeof tc\.args==='string'\)\?tc\.args:JSON\.stringify\(tc\.args\|\|\{\}\)\}\)\),",
         "        args:tc.args||'{}'})),",
         JS_R1, MARKER_R1, True),

    # ---- 0013: STOP settles every resolver; destructive actions refuse mid-turn ----
    ("M8 halt: settle only the approval resolver (0013 re-opened)",
     "index.html",
     r"  if\(proposalResolve\)\{try\{settleCortexProposal\(false\);\}catch\(e\)\{proposalResolve=null;\}\}\n", "",
     JS, MARKER, True),

    ("M8b halt: settle the proposal resolver but leave the modal open",
     "index.html",
     r"  if\(proposalResolve\)\{try\{settleCortexProposal\(false\);\}catch\(e\)\{proposalResolve=null;\}\}",
     "  if(proposalResolve){const r=proposalResolve;proposalResolve=null;if(r)r(false);}",
     JS, MARKER, True),

    # M8c was FOUND BY AUDIT, not by a failing test: the gate says "STOP from every modal
    # state", halt() owns two resolvers, and only the proposal one had any assertion — so
    # deleting the approval settle left the entire suite GREEN. The phase-17 suite now drives
    # the real #agent-modal and arms a real approvalResolve (see the 0013 block in
    # phase17_agent_turn.test.js). This mutation is the regression guard for that addition:
    # if anyone drops the approval settle again, M8c is what says so.
    ("M8c halt: drop the approvalResolve settle (the second modal state)",
     "index.html",
     r"  if\(approvalResolve\)\{try\{settleApproval\(false\);\}catch\(e\)\{approvalResolve=null;\}\}\n",
     "",
     JS, MARKER, True),

    ("M9 clearActive: allow the wipe mid-stream",
     "index.html",
     r"  if\(generating\)\{flashCompact\('RITE IN PROGRESS — COMMUNION LOCKED UNTIL COMPLETION'\);return;\}\n  if\(!confirm\('WIPE MESSAGES IN THIS LOG\?'\)\)return;",
     "  if(!confirm('WIPE MESSAGES IN THIS LOG?'))return;",
     JS, MARKER, True),

    ("M9b deleteChat: allow the purge mid-stream",
     "index.html",
     r"  if\(generating\)\{flashCompact\('RITE IN PROGRESS — COMMUNION LOCKED UNTIL COMPLETION'\);return;\}\n  if\(!confirm\('PURGE THIS DATA-LOOM\?'\)\)return;",
     "  if(!confirm('PURGE THIS DATA-LOOM?'))return;",
     JS, MARKER, True),

    # ---- 0014: teardown on every exit ----
    # M10 is a DOCUMENTED EQUIVALENT, and the reason is a fix this phase made. Originally the
    # teardown sat AFTER the try/catch, so a throw from inside the catch skipped it. The fix has
    # TWO layers: the `finally`, AND a guard around the catch body so the reporter cannot throw
    # at all. With both in place, no reachable input distinguishes "teardown in a finally" from
    # "teardown on the normal path", because the catch body can no longer escape.
    #
    # M10b and M11 below are the mutations that DO pin the two layers respectively: M10b removes
    # the catch-body guard (the reporter can throw again) and M11 moves the flag reset off the
    # success path. Together they cover what M10 structurally cannot. Keep all three.
    # A bare block after the try/catch is exactly "teardown on the normal path only" and is
    # syntactically valid — the earlier attempt to delete `finally{` outright produced a parse
    # error and the harness (correctly) refused to score it. A mutation that does not run is not
    # evidence of anything.
    ("M10 stream: teardown back on the normal path only (documented equivalent — catch is guarded)",
     "index.html",
     r"  \}finally\{\n    /\* PHASE 17 \(CR-0014\): teardown on EVERY exit\.",
     "  }\n  {\n    /* PHASE 17 (CR-0014): teardown on EVERY exit.",
     JS, MARKER, None),

    # M10c (added in the R1 pass): M10 was the ONLY thing pinning "teardown happens on the
    # fault path", and it did not cover the PRE-try window — `generating` was set, then four
    # statements ran before the try opened, and a throw there skipped the finally completely.
    # The app was left permanently busy, which is verbatim what CR-0014 exists to prevent. This
    # mutation puts the preamble back OUTSIDE the try, so only a real regression can catch it.
    #
    # Scored against the R1 suite because that is where the pre-try pin lives, MEASURED by
    # running both files against this mutation: phase17_agent_turn stays GREEN (its 0014 case
    # arms a fault INSIDE the try, which M10c does not touch) and phase17_r1_review_fixes goes
    # RED on R1-B. Scoring this against the phase-17 file would have reported a manufactured
    # escape. The original M10c was never executed at all — it shipped with a multi-line plain
    # string literal that made the whole harness a SyntaxError, so the phase had ZERO mutation
    # evidence while looking fully instrumented.
    #
    # RE-ANCHORED in R2: R2-E moved `generating=true; abortCtl=...; sig=...` INTO the try, so
    # this anchor (which still expected it outside) matched nothing and the harness reported
    # `stale` rather than scoring a false pass — which is the behaviour it exists for. The
    # mutation is still meaningful: it moves the whole preamble back out, which is the R1-era
    # defect R2-E closed.
    ("M10c stream: move the preamble back outside the try (the pre-try window)",
     "index.html",
     r"  let target=null,m=null,sig=null;\n  try\{\n  try\{generating=true;abortCtl=new AbortController\(\);sig=streamSignal\(\);\}catch\(e\)\{\}\n(  try\{[^\n]*\}\n)+",
     "  let target=null,m=null;\n  generating=true;abortCtl=new AbortController();const sig=streamSignal();\n  try{\n\\1",
     JS_R1, MARKER_R1, True),

    ("M10b stream: catch body reports but never re-raises its own fault",
     "index.html",
     r"    \}catch\(reportFault\)\{",
     "    }catch(reportFault){if(1)throw reportFault;",
     JS, MARKER, True),

    ("M11 stream: set generating=false only on the success path",
     "index.html",
     r"    generating=false;abortCtl=null;\n    try\{c\.updated=Date\.now\(\);save\(\);renderList\(\);\}catch\(e\)\{\}",
     "    try{c.updated=Date.now();save();renderList();}catch(e){}",
     JS, MARKER, True),

    # ================= R2: one mutation per R2 fix =================
    # Each of the five R2 defects was found by REVIEW, not by a failing test, so before
    # trusting the R2 suite as a gate, these mutations check that it would actually notice each
    # one being reintroduced. Scored against the R2 suite because that is where each pin lives
    # (measured per mutation, not assumed).

    ("M12 runAgentLoop: stop rebinding the stream target each iteration (the R2-A defect)",
     "index.html",
     r"    if\(streamTarget&&streamTarget\.chat===c\)streamTarget\.msg=m;\n",
     "",
     JS_R2, MARKER_R2, True),

    ("M13 buildMessages: break on budget with no dangling-tool guard (the R2-B defect)",
     "index.html",
     r"      while\(msgs\.length&&msgs\[0\]\.role==='tool'\)msgs\.shift\(\);\n      break;",
     "      break;",
     JS_R2, MARKER_R2, True),

    ("M14 compactToolCalls: rebind instead of compacting in place (the R2-C defect)",
     "index.html",
     r"  const s=acc\.toolCalls\|\|\[\];\n  const keep=s\.filter\(Boolean\);\n  s\.length=0;\n  for\(let i=0;i<keep\.length;i\+\+\)s\[i\]=keep\[i\];\n  acc\.toolCalls=s;\n  return s;",
     "  acc.toolCalls=(acc.toolCalls||[]).filter(Boolean);return acc.toolCalls;",
     JS_R2, MARKER_R2, True),

    ("M15 msgAction: drop the generating guard on the per-message purge (the R2-D defect)",
     "index.html",
     r"  if\(a==='del'&&generating\)return;\n",
     "",
     JS_R2, MARKER_R2, True),

    ("M16 stream: move generating/abortCtl/sig back above the try (the R2-E defect)",
     "index.html",
     r"  let target=null,m=null,sig=null;\n  try\{\n  try\{generating=true;abortCtl=new AbortController\(\);sig=streamSignal\(\);\}catch\(e\)\{\}\n",
     "  let target=null,m=null;\n  generating=true;abortCtl=new AbortController();const sig=streamSignal();\n  try{\n",
     JS_R2, MARKER_R2, True),
]


def main():
    results = []
    for tup in MUTATIONS:
        # M6 carries an extra trailing comment field, so the table is not uniformly 7 wide.
        # Unpacking strictly made the harness die with "too many values to unpack" the first
        # time a mutation was added with a note — a harness that crashes cannot report anything.
        label, rel, pattern, repl, suite, marker, expect_red = tup[:7]
        path = os.path.join(BASE, rel)
        before_hash = digest(path)
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        mutated, n = re.subn(pattern, repl, original, count=1)
        if n == 0:
            print("  %-64s SKIPPED (pattern matched nothing — stale harness)" % label)
            results.append((label, "stale", None))
            continue
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            applied = digest(path) != before_hash
            if not applied:
                print("  %-64s SKIPPED (patch did not change the file)" % label)
                results.append((label, "no-change", None))
                continue
            scored_result = scored(suite, marker)
            if scored_result is None:
                print("  %-64s HARNESS ERROR (run not scored)" % label)
                results.append((label, "harness-error", None))
                continue
            rc, out = scored_result
            red = rc != 0
            fails = re.findall(r"^FAIL- (.*)$", out, re.M)
            if expect_red is None:
                # A mutation with no expectation is a DOCUMENTED EQUIVALENT: proven unobservable
                # by reachability, not a hole. Scoring it "ESCAPED" made the harness impossible
                # to pass — three equivalents are permanent, so the exit code was always 1 and a
                # real regression was indistinguishable from the baseline. They are reported in
                # their own bucket and excluded from the exit code.
                verdict = "equivalent" if not red else "UNEXPECTED-RED"
            else:
                verdict = "caught" if red == expect_red else "UNEXPECTED"
            detail = ""
            if fails:
                detail = " | " + fails[0][:78]
            print("  %-64s %s%s" % (label, verdict, detail))
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
    equivalents = [l for l, v, _ in results if v == "equivalent"]
    other = [(l, v) for l, v, _ in results
             if v not in ("caught", "ESCAPED", "equivalent")]
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