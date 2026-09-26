# Spec: Needle + Laya Tool-Call Middleware for the Agent Harness

**Status:** Draft v2 — for handoff to the harness dev session
**Scope:** A middleware layer that sits **between the harness and the LLM models**, using
two small local models (Needle, Laya) to validate, format, repair, and catch tool calls
and model replies. This is harness-internal only: no external services, no router
components, no remote backends. Everything here runs on the same machine as the harness.

---

## 1. The two models

### 1.1 Needle — tiny function-calling / extraction model

- **What:** A tiny foundation model (single ~8–29 MB `.cact` binary, ~2.1 bits/weight)
  specialized for **tool calling, structured extraction, and embeddings**. Runs on CPU.
- **Where:**
  - GitHub: https://github.com/cactus-compute/needle
  - Docs/site: https://needle.build/
  - Weights/engine: Hugging Face `cactus-compute` (fetched on first install/run)
  - Install: `pip install cactus-needle` (Python package — see §4 for the implication)
- **API shape:** You register functions (name, description, typed params); you get back
  one JSON object per call:
  ```json
  {
    "function_calls": [ { "name": "...", "arguments": { } } ],
    "reasoning": "short text",
    "confidence": 0.0
  }
  ```
  Two properties matter enormously for us:
  1. **It returns an empty list instead of hallucinating** when no tool matches the input.
  2. **Schema-constrained extraction**: declare an output shape; the decode grammar
     guarantees parseable output (use this for repair, not free-form JSON surgery).
- **Hard limits (design around these):**
  - Accuracy degrades past roughly **~50 tools** → always present a candidate subset.
  - It *extracts*, it does not *infer*: arguments that must be reasoned out rather than
    found in the input will be weak. It is a repair/reformat engine, not a mind-reader.
  - **Confidence is calibrated** → act above a threshold, confirm in a middle band,
    refuse/pass-through below.
  - Not a chat model: no free-form generation, no streaming prose.

### 1.2 Laya — System 1 decision model

- **What:** A ~421M-parameter **decision model** (no text generation). You hand it a
  state (JSON) plus typed questions; it answers all of them in **one forward pass** with
  calibrated probabilities. Apache-2.0 weights.
- **Where:**
  - GitHub: https://github.com/receptron/laya
  - npm: `@receptron/laya` (Node.js/TypeScript, ONNX Runtime — **no Python needed**)
  - Paper: arXiv:2607.22041
- **Question types:**
  - `choice` — pick one option (+ probability distribution over options)
  - `score` — expected level on a rubric
  - `noul` — calibrated P(true) for a yes/no statement
- **Operational profile:** ~1.7 GB fp32 weights (downloaded on first use; `float16`
  preset ≈ halves that), ~140 ms per batched call warm on CPU. Limits: ~192 tokens per
  question's options, state truncated to ~512 tokens.
- **Hard limits:** It needs a **confidence threshold in front of it** — below threshold,
  defer to deterministic behavior. It decides; it never writes content.

### 1.3 Division of labor (memorize this)

| Job | Model | Why |
|---|---|---|
| Repair malformed tool calls, re-extract args to schema, tool-name reconciliation, narration→call recovery | **Needle** | Only one that emits structured tool calls |
| Triage "is this a real call or a false positive?", validate before/after repair, anomaly catch on replies | **Laya** | Calibrated P(true)/choice — decisions, not content |
| Deterministic fixes (JSON parse tolerance, fences, exact schema check) | **Neither — plain code** | ~0 cost, fixes the bulk |

---

## 2. Where the middleware sits

```
harness agent loop
      │
      ▼
┌─────────────────────────────────────────────┐
│  outbound: pre-flight request validation     │  (F2a)
│  ┌───────────────────────────────────────┐  │
│  │            LLM model call             │  │
│  └───────────────────────────────────────┘  │
│  inbound: reply inspection + tool-call       │
│    sanitizer pipeline (deterministic →       │  (F1)
│    Needle repair → Laya verdict)             │
│  inbound: reply anomaly catch                │  (F2b)
└─────────────────────────────────────────────┘
      │
      ▼
tool execution / next turn
```

It is transparent middleware: the models and the agent loop do not know it exists, and
it never changes the shape of a successful turn.

## 3. Feature set

### F1 — Tool-Call Sanitizer pipeline (the headline feature)

Applied to model replies **when the request carried tools** and the reply is suspect.
Runs on non-streaming replies, and incrementally on accumulated tool-call deltas during
streaming (never buffer the whole stream to decide).

Failure modes it must handle:

- `tool_calls` arguments arrive as a **string** instead of an object, or with trailing
  garbage / truncated JSON (common with reasoning models and small endpoints).
- Model **narrates** the call instead of making it ("I'll now run `ls -la`" in `content`,
  empty `tool_calls`).
- **Near-miss tool names** (`run_bash` vs `bash`, `Bash`, `execute_command`) — fatal for
  harnesses that match tool names exactly.
- Arguments are valid JSON but **schema-invalid**: wrong types, missing required fields,
  enums out of range.

Stages, in order:

1. **Deterministic pass (always, ~0 cost).** Strip markdown fences / trailing commas;
   parse arguments; fuzzy-match tool name against the registry (exact → case-insensitive
   → prefix/similarity); validate against the tool's input schema. If clean → done.
2. **Needle repair pass (on failure only).** Present the top ~10 candidate tools (never
   the full surface — the ~50-tool limit) + the suspect call as context; use
   schema-constrained extraction to re-emit arguments correctly typed; fuzzy-select the
   canonical tool name. Empty `function_calls` result = do **not** force a repair.
3. **Laya verdict gate.** One batched call: `noul`("is this a genuine tool call the model
   intended?") + `choice` over {accept_repair, pass_through_unrepaired} + `score`
   (repaired-call coherence vs the model's stated reasoning). Accept only above
   configured thresholds.
4. **Pass-through fallback.** If repair is rejected, times out, or local models are
   unloaded/crashed: forward the original reply untouched, flag it in logs. **The
   sanitizer must never block or drop a reply.**

Repairs fix **format, never semantics** — this sentence belongs in the code docs. A
hallucinated-but-well-formed call will get cleanly reformatted, not corrected; the Laya
gate and the no-hallucination property are the mitigations.

### F2 — Request/reply validation

- **Pre-flight (outbound):** Laya `noul` on "do these arguments plausibly satisfy this
  tool's schema description?" — batched with any other per-turn decisions to amortize
  the ~140 ms pass. Fail → cheap deterministic error back to the model loop (so it can
  self-correct) rather than a harness crash.
- **Post-reply anomaly catch:** one `noul` per reply: "does this look like an error
  page, refusal, or loop rather than a real answer?" Feeds the harness's retry/fallback
  logic — closes the "successful 200 with garbage" hole.

### F3 — Cheap local dispatcher (Needle as intent router)

Expose the harness's tool registry to Needle as its toolset (batched into candidate
subsets if large) and use it as a fast local pre-router: "which tool does this user
utterance want, with what args?" — before spending a big-model call. Confidence-gated;
empty result → fall through to the normal model path.

### F4 — Natural-language admin / command extraction

Needle maps free-form user commands onto harness admin operations (add/configure
providers, keys, models — whatever the harness exposes): it selects the operation and
fills typed arguments. **All mutating operations pass through the guardrail gate (F5)
before execution.** This gives the harness an agentic-feeling CLI without building an
agent loop.

### F5 — Guardrail gate for destructive operations

Before executing mutating/destructive admin ops: Laya `noul` over the operation
description + Needle's confidence band → **act / confirm / refuse**. Log the
probabilities. (Needle also ships an `act_or_reject` head that routes to
act/confirm/refuse with a confidence value — use whichever path is cleaner after recon.)

---

## 4. Internal interface (sketch — adapt to harness stack during recon)

```ts
// One seam for everything in §3.
interface LocalModels {
  // Needle
  selectTool(input: string, candidates: ToolDef[]): Promise<{
    calls: { name: string; args: unknown }[]; confidence: number; reasoning: string;
  }>;
  repairCall(suspect: unknown, candidates: ToolDef[], schema: JSONSchema): Promise<{
    calls: { name: string; args: unknown }[]; confidence: number;
  }>; // schema-constrained extraction; empty calls = no repair

  // Laya — one batched forward pass per decision point
  decide(state: unknown, questions: LayaQuestion[]): Promise<LayaAnswer[]>;
  // choice -> { option, probabilities } ; score -> { value } ; noul -> { p }
}

type LayaQuestion =
  | { kind: "choice"; text: string; options: string[] }
  | { kind: "score";  text: string; rubric: string }
  | { kind: "noul";   text: string };

type LayaAnswer =
  | { kind: "choice"; option: string | null; probabilities: Record<string, number> }
  | { kind: "score";  value: number }
  | { kind: "noul";   p: number };
```

Runtime split (decide from recon): if the harness is TypeScript/Node, Laya runs
in-process via `@receptron/laya` and Needle runs as a small Python sidecar (stdio
subprocess or localhost service) owned by the harness, health-checked, exposing exactly
the two Needle methods above. If the harness is Python, the reverse (Needle in-process;
Laya via its ONNX graph or sidecar).

## 5. Architecture rules (non-negotiable)

1. **Local inference is never in the critical path of the agent loop.** The harness must
   be fully functional with both models unloaded. All features degrade to deterministic
   behavior / pass-through when the models are unavailable.
2. **Tight timeouts on any awaited local inference** (suggested: 500 ms–1 s for repair;
   the F1 exception: awaiting a triggered repair is allowed, since the alternative is a
   hard harness error — but it must still be bounded and pass through on timeout).
3. **Confidence thresholds everywhere**, configurable, with act/confirm/refuse bands.
4. **Append-only JSONL ledger** of every local-model decision: inputs (redacted), model,
   question/call, output, confidence, action taken, latency. This ledger is also the
   future fine-tuning corpus (Needle LoRA on your own repair traces).
5. **Opt-in, lazy-loaded.** Laya ≈ 2 GB RAM + ~1.7 GB download; Needle needs a Python
   runtime. Neither may be a surprise at startup. Per-feature flags (§6).
6. **Streaming-aware.** Never buffer an entire stream to run detection; scan accumulated
   deltas incrementally.
7. **Prompt-injection awareness:** user-supplied text passed to Needle/Laya can influence
   tool selection. The F5 confirm gate is the mitigation for mutating ops — verify it
   cannot be bypassed by model output alone.

## 6. Configuration sketch

```yaml
local_models:
  enabled: true
  needle:
    enabled: true
    mode: sidecar           # sidecar | in-process (per harness language)
    min_confidence: 0.75    # act threshold for auto-repair / auto-exec
    confirm_band: [0.5, 0.75]
    timeout_ms: 800
  laya:
    enabled: true
    precision: float16      # halves the ~1.7GB footprint
    min_confidence: 0.70
    timeout_ms: 500
  sanitizer:
    enabled: true
    mode: auto              # auto (repair on suspect only) | on | off
    deterministic_pass: true   # always on in practice; flag for A/B
  ledger:
    path: ./var/local-models.jsonl
    redact: true
```

## 7. Acceptance criteria (test strategy)

- Golden-corpus test set of ~50–100 malformed tool-call cases (string args, fenced JSON,
  truncated, near-miss names, narration-instead-of-call, schema violations) → measure
  deterministic-pass fix rate, repair acceptance rate, and **false-repair rate** (the
  number that must stay near zero: innocent prose converted into phantom calls).
- Kill-the-model test: with Needle/Laya down, harness behavior is identical to
  pre-integration behavior (plus ledger entries noting degraded mode).
- Timeout test: injected 2 s latency in repair pass → pass-through within budget.
- Ledger completeness test: every local-model decision produces a JSONL record with
  confidence and action taken.
- F5 gate test: mutating op with low-confidence extraction → confirm/refuse, never silent
  execution.

## 8. Risks / open questions for the recon phase

1. Harness stack (language, model client layer, where tool calls are parsed) — decides
   sidecar vs in-process split. **Recon first, then finalize.**
2. Streaming tool-call implementation in the harness: does it accumulate deltas? Where
   would incremental sanitizer detection hook in?
3. Tool registry size: if > ~50 tools, candidate-subset selection (fuzzy pre-match)
   becomes a required stage, not an optimization.
4. Does the harness already have a retry/self-correction path the F2 pre-flight failure
   should feed into?
5. Deployment target: is a Python runtime acceptable wherever the harness runs? If not,
   Needle-dependent features stay off until a sidecar-free option exists.
6. Which LLM API format(s) the harness speaks — the malformed-call patterns differ
   between OpenAI-style `tool_calls` and Anthropic-style `tool_use` blocks, and the
   golden corpus must cover the ones actually in play.

## 9. Non-goals (v1)

- Fine-tuning / LoRA (future work — the ledger is the corpus).
- Replacing the primary model's tool calling — this validates, formats, and repairs;
  it does not become the caller of record.
- Running Needle's engine natively in a non-Python runtime.
- Any external service, endpoint, or router integration — this layer is local-only.
- Any feature that requires local inference to be up for the harness to function.
