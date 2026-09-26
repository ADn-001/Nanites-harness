#!/usr/bin/env python3
"""
LOCAL CORTEX - Needle repair backend (Phase 12).

Importable with NO `needle` package present: every `needle` import is lazy, inside a
function, so the daemon's system-python code path (package absent) behaves exactly as
"# device disabled": probes answer False, `load()` returns a degraded dict, `repair()`
never reaches this module's model code.

`cactus-needle` keeps ONE process-global active instance per engine generation
(`needle.__init__._active`), so every model call is serialised through the module-level
`_LOCK`. Concurrent /repair requests queue here with a bounded acquire; holder threads
never see each other's state (the backend constructs with `stateless=True`).

Repair fixes FORMAT, never semantics. The prompt asks the model to re-issue the call the
assistant intended with the ARGUMENT VALUES the assistant already supplied, never to
invent a value. An empty `function_calls` list from the model is reported as `calls: []`
("no repair") - this module never manufactures a call.

`build_select_prompt` (Phase 14) is the pre-router half of the same rule: it shows the model
the operator's UTTERANCE verbatim plus the candidate tools and asks for the one call the
operator clearly asked for, grounded in that utterance or in the schema - and for nothing at
all when the utterance is not a clear tool request. The engine call is identical to a repair
(the tools are baked in at construction); only the prompt differs.

`confidence: None` (untuned weights without a confidence head) is passed through as None;
the frontend treats it as below-threshold. It is NOT coerced to 0.0 or 1.0.

The anonymous-usage counter in the needle package is silenced by setting
NEEDLE_TELEMETRY=0 before the (lazy) import; the daemon sets it in its own environment
at boot for the same reason. No repair data ever leaves the machine.
"""
import json
import os
import threading
import time

# Telemetry is off before needle is ever imported - nothing about a repair leaves the
# machine, including anonymous usage counters. A real user setting always wins.
os.environ.setdefault('NEEDLE_TELEMETRY', '0')

GENERATION = 3

# One process-global active instance per generation (needle.__init__._active) =>
# one process-global lock serialising every model call.
_LOCK = threading.Lock()


class NeedleBackend:
    """Lazy, serialised wrapper around one Needle agent per candidate set.

    Threads: every public model call must be inside `_LOCK` (by its caller or by
    `acquire_call` below) because the engine keeps a single active instance per
    generation process-wide.
    """

    def __init__(self, generation=GENERATION, weights=None):
        self.generation = int(generation)
        self._weights_override = weights if weights is not None else os.environ.get('NEEDLE_WEIGHTS') or None
        self._agent = None        # the constructed Needle (tools baked in)
        self._tools_key = None    # canonical JSON of the candidate set the agent was built with
        self.loaded = False
        self.lib = None

    # ---- construction (lazy: needle imported HERE, never at module import) ----
    def _build(self, tools):
        if self._weights_override and not os.path.isfile(self._weights_override):
            return {'ok': False, 'reason': 'weights_missing'}
        if self._weights_override is None and not weights_present(self.generation):
            return {'ok': False, 'reason': 'weights_missing'}
        try:
            import needle  # noqa: F401  (lazy by design - the daemon must import this
                            # module fine with no needle package at all)
        except Exception:
            return {'ok': False, 'reason': 'tool_unavailable'}

        def build():
            self._agent = needle.Needle(tools=tools, weights=self._weights_override,
                                        generation=self.generation, stateless=True)
            self.lib = _resolve_lib_path(needle, self.generation)
            self.loaded = True
            self._tools_key = _canon(tools)

        if self._weights_override is not None:
            # Tuned weights spawn a FineTuneWorker; build it under the call lock so no
            # in-flight repair can use a half-built agent.
            with _LOCK:
                build()
        else:
            # Base weights: construction binds the process-global instance - do it with
            # the lock already held by the caller.
            build()
        return {'ok': True}

    def load(self, tools=None):
        """Preload the engine (e.g. the daemon's --preload-needle boot path)."""
        if self._weights_override is None and not weights_present(self.generation):
            return {'ok': False, 'reason': 'weights_missing'}
        try:
            import needle  # noqa: F401 - probe: is the package even importable?
        except Exception:
            return {'ok': False, 'reason': 'tool_unavailable'}
        with _LOCK:
            if not self.loaded:
                try:
                    return self._build(list(tools or []))
                except Exception:
                    return {'ok': False, 'reason': 'tool_unavailable'}
        return {'ok': True}

    # ---- the repair call ----
    def repair(self, text, candidates):
        """Repair the malformed tool call described by `text` against `candidates`.

        Returns a normalised dict; NEVER raises, NEVER returns an invented call:

          {ok: True, calls: [{name, arguments}], confidence: float|None,
           reasoning: str, raw: <envelope minus bulky perf keys>}
          {ok: False, reason: 'tool_unavailable'|'weights_missing'}

        The agent is (re)built when the candidate set changes (the engine bakes tools at
        construction time). `arguments` arriving as a JSON string are parsed back into a
        plain object - the shape the harness speaks.
        """
        tools = _flatten_tools(candidates)
        if not self.loaded and not self._weights_override and not weights_present(self.generation):
            return {'ok': False, 'reason': 'weights_missing'}
        with _LOCK:
            key = _canon(tools)
            if not self.loaded or key != self._tools_key:
                built = self._build(tools)
                if not built['ok']:
                    return built
            envelope = self._agent.complete(text, max_new_tokens=384)
        return _normalise(envelope)


def _canon(tools):
    return json.dumps(tools, sort_keys=True, separators=(',', ':'))


def _flatten_tools(candidates):
    """Needle's tools are the FLAT shape {name, description, parameters}; tolerate an
    OpenAI-shaped entry ({type:'function', function:{...}}) by unwrapping it."""
    out = []
    for entry in candidates or []:
        if not isinstance(entry, dict):
            continue
        fn = entry.get('function') if isinstance(entry.get('function'), dict) else None
        if fn and 'name' not in entry:
            out.append({'name': fn.get('name'), 'description': fn.get('description') or '',
                        'parameters': fn.get('parameters') or {}})
        else:
            out.append({'name': entry.get('name'), 'description': entry.get('description') or '',
                        'parameters': entry.get('parameters') or {}})
    return [t for t in out if t.get('name')]


def _normalise(envelope):
    """Defensive envelope -> response mapping. Never assume a key exists."""
    if not isinstance(envelope, dict):
        return {'ok': False, 'reason': 'tool_unavailable'}
    calls = []
    for c in envelope.get('function_calls') or []:
        if not isinstance(c, dict):
            continue
        fn = c.get('function') if isinstance(c.get('function'), dict) else c
        name = fn.get('name')
        args = fn.get('arguments')
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if isinstance(args, dict):
            calls.append({'name': name, 'arguments': args})
        else:
            call = {'name': name, 'arguments': args if args is not None else {}}
            calls.append(call)
    confidence = envelope.get('confidence')
    if confidence is not None and not isinstance(confidence, (int, float)):
        confidence = None
    raw = {k: v for k, v in envelope.items() if k in ('type', 'success', 'validation')}
    return {'ok': True, 'calls': calls, 'confidence': confidence,
            'reasoning': str(envelope.get('reasoning') or ''), 'raw': raw}


def weights_present(generation=GENERATION):
    """REAL probe (replaces Phase 10's env-var-only check): NEEDLE_WEIGHTS first
    (a file path), else the package's own cache path. Returns False (never raises)
    when the needle package is missing."""
    p = os.environ.get('NEEDLE_WEIGHTS')
    if p:
        return os.path.isfile(p)
    try:
        from needle.agent import fetch
        return os.path.isfile(os.path.join(fetch.cache_dir(generation),
                                           fetch.base_weights(generation)))
    except Exception:
        return False


def lib_present(generation=GENERATION):
    """The engine library path if resolvable, else None (never raises, never loads)."""
    env = os.environ.get('NEEDLE%d_LIB_PATH' % int(generation)) or os.environ.get('NEEDLE_LIB_PATH')
    if env and os.path.isfile(env):
        return env
    try:
        from needle.agent import fetch
        here = os.path.join(os.path.dirname(fetch.__file__), '..', fetch._lib_name())
        here = os.path.normpath(here)
        if os.path.isfile(here):
            return here
        version = fetch.engine_version(generation)
        cached = os.path.join(fetch.cache_dir(generation), fetch._lib_name())
        return cached if os.path.isfile(cached) else None
    except Exception:
        return None


def _resolve_lib_path(needle_mod, generation):
    """Where the loaded engine's lib actually lives (for /health)."""
    try:
        from needle.agent import fetch
        version = fetch.engine_version(generation)
        cached = os.path.join(fetch.cache_dir(generation), fetch._lib_name())
        if os.path.isfile(cached):
            return cached
    except Exception:
        pass
    return lib_present(generation)


def build_repair_prompt(suspect):
    """FORMAT-only repair instruction. The model re-issues the intended call with the
    argument VALUES the assistant already supplied; it never invents a value.

    `suspect` accepts every shape the frontend actually sends (the plan §4 shows the
    single-dict form, but `_cortexRepairPayload` in appcore.js sends a LIST when a turn
    carries several unrepairable calls, and the raw reply text for a prose-only probe in
    `mode:'on'`). Rendering only the dict form silently dropped the suspect and asked the
    model to repair nothing -> a context-free guess. All three shapes are rendered.
    """
    lines = ['The assistant intended one tool call, but the call it emitted was malformed.',
             'Re-issue that call using the values it already supplied. Never invent a value.']

    def _one(s):
        """One suspect -> its prompt lines. Accepts {name, arguments|args}, nested
        `function.arguments` (OpenAI shape) and anything else by stringifying it."""
        if not isinstance(s, dict):
            if s is None or s == '':
                return
            lines.append('The assistant\'s reply was: ' + str(s)[:2000])
            return
        fn = s.get('function') if isinstance(s.get('function'), dict) else None
        name = s.get('name') or (fn or {}).get('name') or ''
        args = s.get('arguments', s.get('args'))
        if args is None and fn is not None:
            args = fn.get('arguments', fn.get('args'))
        raw = args if isinstance(args, str) else json.dumps(args)
        if raw and raw != 'null' and raw != '{}':
            lines.append('Previous tool call (malformed): ' + str(raw))
        if name:
            lines.append('Previous tool name: ' + str(name))
        reason = s.get('reason')
        if reason:
            lines.append('Why it was rejected: ' + str(reason))

    if isinstance(suspect, (list, tuple)):
        for entry in suspect:
            _one(entry)
    else:
        _one(suspect)

    lines.append('Emit the corrected call.')
    return '\n'.join(lines)


def build_select_prompt(input_text, candidates):
    """Propose the ONE tool call the operator clearly asked for - or emit nothing.

    The select prompt is deliberately the same shape as the repair prompt: the utterance
    is rendered VERBATIM (truncated at 2000 chars with an ellipsis marker so a long paste
    cannot blow the context) and the tools are listed by name + description. Every tool
    shape the harness actually sends is accepted - flat {name, description, parameters} and
    OpenAI-nested {type:'function', function:{...}} - via `_flatten_tools`, the same
    unwrapper the engine uses, so a tool the model can be called by is a tool it is SHOWN.

    Two hard rules, in the "repairs format, never semantics" spirit of build_repair_prompt:
      1. arguments must be GROUNDED - taken from the utterance or from the schema. Never
         invent a value the operator did not give and the schema does not pin.
      2. an utterance that is not a clear tool request gets NO call. `calls: []` is a
         complete, correct answer; a manufactured call is not.
    """
    if input_text is None:
        utterance = ''
    elif isinstance(input_text, str):
        utterance = input_text
    else:
        utterance = str(input_text)
    if len(utterance) > 2000:
        utterance = utterance[:2000] + '…[truncated %d]' % (len(utterance) - 2000)

    lines = ['The operator sent one message. Propose the single tool call it clearly asks for.',
             'Use only argument values that are grounded in the operator\'s message or in the '
             'tool schema below. Never invent a value.',
             'If the message is not a clear request for one of these tools, propose nothing.']

    tools = _flatten_tools(candidates)
    if tools:
        lines.append('Available tools:')
        for t in tools:
            lines.append('- %s: %s' % (t.get('name'), t.get('description') or '(no description)'))
    else:
        lines.append('Available tools: none.')

    lines.append('Operator message: ' + utterance)
    lines.append('Emit the tool call, or nothing.')
    return '\n'.join(lines)


def now_ms():
    return int(round(time.time() * 1000))
