# Handoff Prompt — Needle + Laya Tool-Call Middleware, Phased Plan

You are the dev session responsible for the agent harness in this repository. Your task
in this session is **recon + planning only. Do not write implementation code.** Produce
one document: a phased implementation plan for a tool-call middleware layer that sits
between this harness and its LLM models, using two small local models — Needle and
Laya — to validate, format, and repair tool calls and catch bad replies.

## Context

The product owner wants a local middleware layer inside the harness (no external
services) that:

- sanitizes malformed model tool calls (string/truncated/fenced JSON arguments,
  near-miss tool names, schema violations) and recovers tool calls the model narrated
  in prose instead of emitting,
- validates outbound tool-call requests and inbound replies (catching "200 OK but
  garbage" responses),
- optionally uses Needle as a cheap local tool dispatcher before spending big-model
  calls, and for natural-language admin commands with a confidence-gated
  act/confirm/refuse guardrail on mutating operations.

Read the full spec first. It is at:
`<SPEC_PATH>` (file: `needle-laya-harness-integration-spec.md` — if it lives elsewhere,
find it; if you cannot find it, stop and ask.)

The spec defines: what Needle and Laya are (with links and install commands), where the
middleware sits, the five features F1–F5, the architecture rules (especially: local
inference is never in the critical path; confidence gating; JSONL ledger; opt-in lazy
loading; local-only, no external services), an internal interface sketch, a config
sketch, risks, and acceptance criteria. Treat the spec as the requirements source. Where
it conflicts with what you find in the repo, note the conflict explicitly in the plan
rather than silently deviating.

## Step 1 — Recon (do this before any planning)

Survey the repository and report:

1. **Stack:** language(s), runtime, package manager, build/test commands, how the project
   is structured (entry points, main modules).
2. **Model client layer:** where outbound LLM calls are made, which API format(s) are
   spoken (OpenAI-compatible, Anthropic, other), streaming vs non-streaming handling.
3. **Tool-calling path:** how tools are registered (the tool registry), how tool calls
   are parsed from model replies, where malformed calls would currently fail, how
   tool results are fed back. Note the registry size (the ~50-tool Needle limit matters).
4. **Agent loop:** retry/self-correction behavior, error taxonomy for model failures.
5. **Extension points:** existing middleware/hooks/interceptors where a sanitizer stage
   and validation gates could attach; existing config system (file format, loading);
   existing logging/ledger conventions.
6. **Tests:** framework, fixtures, how golden-corpus-style tests would fit.
7. **Deployment:** how/where the harness runs — is a Python runtime or a sidecar process
   acceptable there?

Keep the recon factual, with file paths. This section becomes the plan's appendix.

## Step 2 — Write the phased plan

Write `docs/plans/needle-laya-middleware-plan.md` (create the directory if needed)
containing:

1. **Recon summary** (from Step 1) + a short "spec vs. repo" delta list: anything in the
   spec that doesn't fit this codebase as found, and your proposed adaptation.
2. **Resolved decisions:** answers to the spec's §8 open questions, each with a one-line
   rationale tied to something you found in recon. If a question genuinely can't be
   resolved from the repo, mark it `NEEDS-OWNER` with your recommendation.
3. **Phases.** Each phase must be independently shippable, leave the harness fully
   functional, and end with the acceptance tests from spec §7 that now pass. Suggested
   shape (adjust to what recon justifies):
   - Phase 0: scaffolding — config flags, JSONL ledger, internal `LocalModels` interface
     with a no-op backend, model health checks.
   - Phase 1: deterministic sanitizer pass (no ML) + golden-corpus test harness.
   - Phase 2: Laya (in-process or sidecar, per recon) — validation gates F2, F5.
   - Phase 3: Needle (sidecar or in-process, per recon) — repair pass F1, dispatcher F3,
     NL admin F4.
   - Phase 4: streaming-aware incremental detection, threshold tuning from ledger data.
   For every phase: goal, scope (features from the spec), touched subsystems (real file
   paths from recon), estimated size (S/M/L), risks, and exit criteria.
4. **Test plan:** how the §7 acceptance criteria map onto the repo's existing test
   framework; where the golden corpus of malformed tool calls will live; which of the
   harness's actual API format(s) it must cover.
5. **Explicit non-goals** for the plan's horizon (mirror and extend spec §9).
6. **Open questions for the owner** — consolidated `NEEDS-OWNER` list.

## Rules

- Planning only. No implementation code, no new dependencies installed, no config changes.
- Local-only: nothing in the plan may introduce an external service or network
  dependency at runtime (model weight downloads at install/first-run are fine).
- Prefer the spec's architecture rules verbatim where possible; deviations must be
  justified from recon findings.
- The plan is written for an executor (human or AI session) who has NOT seen this
  conversation: it must be self-contained, link the spec, and define "done" per phase.
- Estimate effort honestly. If recon shows the harness's tool-call path is a mess, say so
  in the delta list rather than planning around it silently.

## Definition of done for THIS session

- Recon section complete with file paths.
- `docs/plans/needle-laya-middleware-plan.md` written and self-contained.
- Every spec feature F1–F5 is either assigned to a phase with exit criteria, or listed
  as a non-goal with a reason.
- `NEEDS-OWNER` list is short and specific.

Begin with Step 1.
