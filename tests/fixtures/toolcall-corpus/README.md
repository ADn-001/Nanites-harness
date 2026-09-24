# Tool-call corpus (`cases.json`)

The measured golden corpus for the **Phase 11 deterministic salvage pass**
(`CogCore.salvageToolCalls`). It grades the cheap, ~0-cost repair stage, and — as the local
models come online in Phases 12-14 — it is the same corpus the ledger will later mirror for
fine-tuning. Regenerate it with:

```bash
python3 tools/gen_toolcall_corpus.py
```

(Generate, don't hand-edit: nested JSON escaping is exactly the kind of thing hand-editing gets
wrong — the first attempt at this file was invalid JSON for that reason.)

## Case schema

```jsonc
{
  "id": "string-args-fenced",          // unique, kebab-case
  "category": "string-args",           // see the category list below
  "format": "openai",                  // openai | ollama | chat.end | content
  "tools": ["read_file", "…"],         // OPTIONAL allow-list override; defaults to the
                                       // top-level "tools" (the 6 real TOOL_SCHEMAS names)
  "reply": { … },                      // the raw model reply, in one of the wire shapes
  "expect": {
    "name": "read_file",               // salvage must yield a call with THIS canonical name
    "args": { "path": "a.py" },        // …and exactly these argument VALUES (deep-equal)
    "callCount": 1,                    // OPTIONAL exact number of dispatchable calls
    "unchanged": true,                 // salvage must change NOTHING (see the rule below)
    "unrepairable": true,              // salvage must not yield a dispatchable call
    "ambiguous": true,                 // …and must report it as ambiguous, not just unknown
    "never": ["read_file"],            // names salvage may NEVER guess (ambiguity guard)
    "validatorOk": false               // what the Phase 7 validator must then say about the
                                       // emitted calls (schema faults are NOT salvage's job)
  }
}
```

### Rules a new case must follow

1. **`unchanged: true` means salvage changes *nothing*.** If the reply carries no call object,
   `calls` must be empty and `changed` must be empty. If it carries a call object, the emitted
   call must have the identical name and identical argument *values*, and `changed` must be empty.
   Every case with `unchanged: true` is a false-repair guard: the suite asserts the false-repair
   rate is **exactly 0**.
2. **A false-repair guard reply must not contain an allowed-tool `name({…})` span or a
   `{"name": …, "arguments": …}` object.** Those two shapes *are* the narration-recovery contract
   (a model that narrates a call instead of emitting `tool_calls` has still asked for the call),
   so a guard case containing one is not a guard — it is a repairable narration case. Use
   non-call prose/code (`function readFile(path) {…}`, a schema sketch such as
   `{"path": "string"}`, an unknown tool name, a config block with no `name` member).
3. **Schema faults are not salvage's job.** A missing required field, a wrong declared type or an
   empty object is left exactly as the model wrote it — salvage repairs *format*, never semantics,
   and never invents an argument value. Those cases set `validatorOk: false` and assert the
   Phase 7 validator still rejects them.
4. **Ambiguity is never guessed.** When two allowed names are equally close, or an allow-list
   override makes a name genuinely ambiguous, the case sets `ambiguous: true` and lists the
   names in `never`; the suite asserts none of them is emitted.

### Categories

`legitimate-call` · `string-args` · `trailing-garbage` · `truncated-json` · `double-encoded` ·
`near-miss-name` · `ambiguous-name` · `unknown-name` · `schema-violation` ·
`false-repair-guard` · `narration-recovery` · `mixed-turn` · `garbage`

### Wire formats

Only the three shapes this harness actually speaks (see `index.html`
`mergeToolDelta` / `processStreamObject`): OpenAI-compatible `tool_calls` (including arguments
split across chunks), Ollama `message.tool_calls`, and the buffered
`{"type":"chat.end","result":{"output":[…]}}` aggregate. Anthropic `tool_use` is a deliberate
non-goal — the harness does not speak it.
