/*
 * appcore.js — COGITATOR shared pure logic.
 * UMD: exposed as `window.CogCore` in the browser (loaded by index.html BEFORE the
 * inline script) and as `module.exports` under Node so the frontend test suite can
 * unit-test the same code that ships. Keep everything in here DOM-free and side-effect
 * free (no globals, no localStorage touches) so it is deterministic to test.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory(root);
  } else {
    root.CogCore = factory(root);
  }
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  var CogCore = {
    VERSION: '0.1.0',

    /** Collision-tolerant id (ms base-36 + random tail). */
    uid: function () {
      return Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
    },

    /** HTML-escape a value for safe interpolation. */
    esc: function (s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    },

    /**
     * Rough token estimate used by the context gauge (4 chars ~ 1 token).
     * Accepts a plain string OR a multimodal content array (attachment messages):
     * for arrays it sums the visible text parts and counts images as a fixed cost.
     */
    tok: function (s) {
      if (Array.isArray(s)) {
        var total = 0, imgs = 0;
        for (var i = 0; i < s.length; i++) {
          var p = s[i] || {};
          if (p.type === 'image_url') imgs++;
          else total += String(p.text == null ? '' : p.text).length;
        }
        return Math.max(1, Math.ceil(total / 4) + imgs * 85);
      }
      return Math.max(1, Math.ceil((s == null ? '' : String(s)).length / 4));
    },

    /** True when the file name looks like a raster image (by extension). */
    isImageName: function (name) {
      return /\.(png|jpe?g|gif|webp|bmp|svg|avif|ico|heic)$/i.test(String(name || ''));
    },

    /** Cheap binary sniff: NUL bytes (or the Unicode replacement char) mean the
     *  decoded text is not a UTF-8 text file the model should be shown. */
    looksBinary: function (text) {
      var s = text == null ? '' : String(text);
      return s.indexOf('\x00') !== -1 || s.indexOf('\uFFFD') !== -1 && /[\uFFFD]/.test(s);
    },

    /** Truncate an inlined text attachment to `cap` chars, marking the cut. */
    capText: function (text, cap) {
      var s = text == null ? '' : String(text);
      var c = cap && cap > 0 ? cap : 20000;
      if (s.length <= c) return s;
      return s.slice(0, c) + '[\n… TRUNCATED — FILE TOO LARGE TO EMBED FULLY, REFER TO THE WORKDIR]';
    },

    /**
     * Build one labelled, fenced block for a text attachment:
     *   <ATTACHMENT [src/app.py]>
     *   ```
     *   <content>
     *   ```
     * Returns null when the attachment should be skipped (binary, or a kind we
     * don't inline) so the caller can drop it from the message.
     */
    attachmentBlock: function (att, cap) {
      att = att || {};
      if (att.kind === 'image') return null; // handled as image_url, not inlined text
      var content = att.data == null ? '' : String(att.data);
      // non-image but binary-by-content OR binary file name we refuse to inline
      if (CogCore.looksBinary('' + content)) return null;
      var body = CogCore.capText(content, cap);
      return '<ATTACHMENT [' + (att.name || 'file') + ']>\n```\n' + body + '\n```';
    },

    /**
     * Compose the `content` for a user message given a prompt and attachments.
     *   opts.text   the prompt text the operator typed
     *   opts.files  [{name, kind:'text'|'image', data}]  (data = text or data URL)
     *   opts.cap    max chars to inline per text attachment (default 20000)
     * Returns a plain string when there are no files (message unchanged), else a
     * multimodal array: [ {type:'text',text:prompt}, ...{type:'text',text:block}
     * (skipping binary), {type:'image_url',image_url:{url}} ].
     */
    buildAttachmentContent: function (opts) {
      opts = opts || {};
      var files = Array.isArray(opts.files) ? opts.files : [];
      var text = opts.text == null ? '' : String(opts.text);
      var cap = opts.cap;
      if (!files.length) return text;
      var out = [{ type: 'text', text: text }];
      for (var i = 0; i < files.length; i++) {
        var f = files[i] || {};
        if (f.kind === 'image') {
          out.push({ type: 'image_url', image_url: { url: f.data || '' } });
        } else {
          var block = CogCore.attachmentBlock(f, cap);
          if (block !== null) out.push({ type: 'text', text: block });
        }
      }
      return out;
    },

    /** Plain-text rendering of a message `content` (string or array) for the UI
     *  and auto-title. Images render as a short marker, text parts joined. */
    contentText: function (content) {
      if (!Array.isArray(content)) return content == null ? '' : String(content);
      return content.map(function (p) {
        p = p || {};
        if (p.type === 'image_url') return '[IMAGE]';
        return p.text == null ? '' : String(p.text);
      }).join('\n');
    },

    /**
         * Agent-mode orient prompt. Deliberately SHORT: it must stay effective for
         * both large- and small-context models. Jails the agent to one working
         * directory, teaches it how to drive the tool loop, and pins the tool roster
         * to the LIVE TOOL_SCHEMAS passed in as `opts.tools`. `opts.workdir` is the
         * bound directory taken from the user's settings.
         */
        _toolNames: function (tools) {
          var names = [];
          if (Array.isArray(tools)) {
            for (var i = 0; i < tools.length; i++) {
              var t = tools[i] || {};
              var inner = t.function || t;
              if (inner && typeof inner.name === 'string' && inner.name) {
                names.push(inner.name);
              }
            }
          }
          if (!names.length) {
            names = ['read_file', 'write_file', 'list_dir', 'grep', 'git', 'run_command'];
          }
          return names.filter(function (n, idx, arr) { return arr.indexOf(n) === idx; });
        },

        buildAgentSystemPrompt: function (opts) {
          opts = opts || {};
          var workdir = opts.workdir || '(the bound working directory)';
          var toolNames = CogCore._toolNames(opts.tools);
          var hasRunCmd = toolNames.indexOf('run_command') !== -1;
          var toolsLine = 'TOOLS available this session: ' + toolNames.join(', ') + '.';
          if (hasRunCmd) {
            toolsLine += ' run_command is DISABLED unless the bridge was started with --allow-exec; if the model calls it and it is refused, do not retry it — choose another tool or report the limitation.';
          }
          return [
            'You are COGITATOR, an autonomous coding agent. You use a tool loop: emit tool calls, observe the results returned between turns, and keep going until the task is done.',
            'FILESYSTEM JAIL: your ONLY reachable filesystem root is WORKDIR below. Every path you place in a tool argument MUST be project-RELATIVE to WORKDIR (e.g. "src/app.py", "readme.md", or "." for the root itself). You will never be handed host-absolute paths such as /home/..., /etc/passwd, or C:\\\\...; do not invent them. If a step genuinely requires a path outside the workdir, refuse and ask the user to remount.',
            'WORKDIR: ' + workdir,
            toolsLine,
            'TOOL-CALL CONTRACT: to call a tool, emit EXACTLY ONE function call as a JSON object {"type":"function","function":{"name":"<tool>","arguments":"{...}"}} where "arguments" is an inline, escaped JSON string (a plain object of the tool\'s parameters, not a narrative). Emit exactly one tool call per turn, then STOP and wait for the tool result. Observe the returned result, then either call the next tool or, once the task is complete, reply with your final answer in prose (no further tool call).',
            'STOPPING: ask for help only when you are blocked, lack a capability, or need a clarification you cannot resolve from the workdir listing.'
          ].join('\n');
        },

    /**
     * Build the system-message prefix for a model request.
     *   opts.system    base system string (agent orient prompt, or user canticle)
     *   opts.summary   compressed transcript summary -> [PRIOR COMMUNION] block
     *   opts.workdirCtx {path, listing} -> [WORKDIR CONTEXT] block (agent mode only)
     */
    buildSystemMessages: function (opts) {
      opts = opts || {};
      var msgs = [];
      if (opts.system) msgs.push({ role: 'system', content: opts.system });
      if (opts.summary) msgs.push({
        role: 'system',
        content: '[PRIOR COMMUNION — COMPRESSED RECORD. Treat as established fact, do not re-derive]: ' + opts.summary
      });
      if (opts.workdirCtx) {
        var l = opts.workdirCtx.listing || '';
        if (Array.isArray(l)) l = l.join('\n');
        msgs.push({
          role: 'system',
          content: '[WORKDIR CONTEXT] Working directory (your ONLY reachable root):\n' +
            opts.workdirCtx.path + '\n\nCurrent listing of "' + opts.workdirCtx.path + '":\n' +
            (l || '(no listing available)')
        });
      }
      return msgs;
    },

    /* ================= PHASE 7 — STRUCTURED OUTPUT VALIDATOR =================
     * A pure, deterministic gate between the model endpoint and the tool bridge.
     * Nothing here may touch the network, the filesystem, the DOM or localStorage:
     * it is the last checkpoint before an LLM-authored call is handed to the machine.
     *
     *   validateStructuredOutput(result, allowedTools) -> {ok, errors, sanitized}
     *
     *  - `result` may be: an array of calls, {toolCalls:[…]} (this app's finalized shape),
     *    or an OpenAI assistant message {tool_calls:[{id,type,function:{name,arguments}}]}.
     *  - `allowedTools` may be TOOL_SCHEMAS-shaped entries ({type:'function',function:{…}})
     *    or a plain list of names. Names AND per-tool required/typed parameters are taken
     *    from it, so the validator can never disagree with the prompt/tool payload.
     *  - Every call is rejected if it lacks an id or a name, names a tool outside the
     *    allow-list, or carries `arguments` that are not a JSON object; a call is also
     *    rejected when a schema-declared required field is absent or a declared type
     *    does not match. Rejections are collected in `errors`; `sanitized` carries ONLY
     *    the calls that may be dispatched, with `args` already parsed to a plain object.
     *  - `ok` is true only when nothing was rejected.
     */
    _toolSpec: function (allowedTools) {
      var spec = { names: [], required: {}, properties: {} };
      if (!Array.isArray(allowedTools)) return spec;
      for (var i = 0; i < allowedTools.length; i++) {
        var t = allowedTools[i];
        if (typeof t === 'string') {
          if (t && spec.names.indexOf(t) === -1) spec.names.push(t);
          continue;
        }
        var inner = (t || {}).function || t || {};
        var n = inner && typeof inner.name === 'string' ? inner.name : '';
        if (!n) continue;
        if (spec.names.indexOf(n) === -1) spec.names.push(n);
        var p = (inner && inner.parameters) || {};
        spec.required[n] = Array.isArray(p.required) ? p.required.slice() : [];
        spec.properties[n] = (p && p.properties) || {};
      }
      return spec;
    },

    /** Normalise the many tool-call shapes into [{id,name,args}] or null. */
    _normalizeToolCalls: function (result) {
      if (!result || typeof result !== 'object') return null;
      var raw = Array.isArray(result) ? result
        : (Array.isArray(result.toolCalls) ? result.toolCalls
          : (Array.isArray(result.tool_calls) ? result.tool_calls : null));
      if (!raw) return null;
      return raw.map(function (c) {
        c = c || {};
        var fn = c.function || c || {};
        var args = c.args !== undefined ? c.args
          : (c.arguments !== undefined ? c.arguments
            : (fn.arguments !== undefined ? fn.arguments : fn.args));
        return {
          id: c.id == null ? '' : String(c.id),
          name: (c.name || fn.name) == null ? '' : String(c.name || fn.name),
          args: args
        };
      });
    },

    /** Declared-type conformance for one already-parsed argument value. */
    _argTypeOk: function (v, type) {
      if (!type) return true;
      switch (type) {
        case 'string': return typeof v === 'string';
        case 'number': return typeof v === 'number' && isFinite(v);
        case 'integer': return typeof v === 'number' && v % 1 === 0;
        case 'boolean': return typeof v === 'boolean';
        case 'object': return v !== null && typeof v === 'object' && !Array.isArray(v);
        case 'array': return Array.isArray(v);
        default: return true;
      }
    },

    validateStructuredOutput: function (result, allowedTools) {
      var spec = CogCore._toolSpec(allowedTools);
      var calls = CogCore._normalizeToolCalls(result);
      var errors = [], sanitized = [];

      if (calls === null) {
        errors.push({ index: -1, id: '', name: '', error: 'tool_calls is not an array — nothing is dispatchable' });
        return { ok: false, errors: errors, sanitized: [] };
      }

      for (var i = 0; i < calls.length; i++) {
        var c = calls[i];
        var name = c.name, id = c.id;
        var bad = [];

        if (!id) bad.push('missing "id" (this app assigns one; an empty id cannot be correlated to a tool result)');
        if (!name) bad.push('missing tool "name"');
        else if (spec.names.length && spec.names.indexOf(name) === -1) {
          bad.push('unknown tool "' + name + '" — not in the allowed set (' + spec.names.join(', ') + ')');
        }

        var args = null, argsOk = true;
        if (typeof c.args === 'string') {
          var txt = c.args.trim();
          if (!txt) txt = '{}';
          try { args = JSON.parse(txt); }
          catch (e) { argsOk = false; bad.push('arguments is not valid JSON'); }
        } else if (c.args === undefined || c.args === null) {
          argsOk = false; bad.push('arguments is not valid JSON (missing)');
        } else {
          args = c.args;
        }

        if (argsOk) {
          if (args === null || typeof args !== 'object' || Array.isArray(args)) {
            argsOk = false; bad.push('arguments must be a JSON object, got ' + (Array.isArray(args) ? 'array' : typeof args));
          }
        }

        if (argsOk && args && spec.names.indexOf(name) !== -1) {
          var req = spec.required[name] || [];
          for (var r = 0; r < req.length; r++) {
            if (!(req[r] in args)) bad.push('missing required argument "' + req[r] + '"');
          }
          var props = spec.properties[name] || {};
          for (var k in props) {
            if (!Object.prototype.hasOwnProperty.call(props, k)) continue;
            if (!(k in args)) continue;
            if (!CogCore._argTypeOk(args[k], (props[k] || {}).type)) {
              bad.push('argument "' + k + '" must be of type ' + (props[k] || {}).type);
            }
          }
        }

        if (bad.length) errors.push({ index: i, id: id, name: name, error: bad.join('; ') });
        else sanitized.push({ id: id, name: name, args: args });
      }

      return { ok: errors.length === 0, errors: errors, sanitized: sanitized };
    },

    /* ================= PHASE 11 — DETERMINISTIC SALVAGE =================
     * `salvageToolCalls(reply, allowedTools)` — pure. No network, no filesystem,
     * no DOM, no localStorage, no clock. This is the cheap, ~0-cost repair stage
     * that runs BEFORE `validateStructuredOutput` in the agent turn.
     *
     * REPAIRS FORMAT, NEVER SEMANTICS. It may strip a markdown fence, drop a
     * trailing comma, re-parse a double-encoded `arguments` string, close a
     * truncated JSON object (only when the closed text actually parses), and
     * reconcile a tool NAME that is a near-miss of an allowed name. It may NEVER
     * invent an argument value, fill a required field with a guess, or choose
     * between two equally-close tool names — an ambiguous name is `unrepairable`,
     * never a guess. A name that reconciles to nothing is `unrepairable` too.
     *
     *   salvageToolCalls(reply, allowedTools) -> {calls, changed, unrepairable, source}
     *
     *  - `reply` may be: an array of calls, `{toolCalls:[…]}`, `{tool_calls:[…]}`,
     *    an OpenAI assistant message, an Ollama `{message:{tool_calls:[…]}}`, a
     *    buffered `{type:'chat.end', result:{output:[…]}}`, a single bare call
     *    object, a plain prose string, or `{content, tool_calls:[]}`.
     *  - Emitted calls use the harness's own shape `{id, name, args}` with `args`
     *    already parsed to a plain object (matching `validateStructuredOutput`'s
     *    `sanitized[].args`). `id` is passed through unchanged — the caller mints
     *    one when empty, exactly as `finalizeToolCalls` already does.
     *  - `calls` holds ONLY calls that are ready to be validated/dispatched.
     *    `unrepairable` holds `{id, name, args, reason}` for the calls this function
     *    could NOT make dispatchable — the caller must still hand those to the
     *    validator so they are rejected explicitly rather than silently dropped.
     *  - `changed` is a human-readable list of the repairs applied (empty for a
     *    clean turn — that is the false-repair guard: a legitimate call must come
     *    back with the same name and the same argument VALUES and no `changed` entry).
     *  - Never throws. Garbage in ⇒ {calls:[], changed:[], unrepairable:[…]}.
     */
    salvageToolCalls: function (reply, allowedTools) {
      var names = CogCore._toolSpec(allowedTools).names;
      var out = { calls: [], changed: [], unrepairable: [], source: 'deterministic' };
      var got;
      try { got = CogCore._cortexRawCalls(reply); } catch (e) { got = { raw: null, content: '' }; }
      var raw = got.raw, content = got.content, i, k;

      if (raw) {
        for (i = 0; i < raw.length; i++) {
          var c = raw[i] || {};
          var fn = (c.function && typeof c.function === 'object') ? c.function : c;
          var rawName = (c.name !== undefined && c.name !== null && c.name !== '') ? c.name : fn.name;
          var cid = (c.id === undefined || c.id === null) ? '' : String(c.id);
          var rawArgs = c.args !== undefined ? c.args
            : (c.arguments !== undefined ? c.arguments
              : (fn.arguments !== undefined ? fn.arguments : fn.args));

          var nr = CogCore._cortexReconcileName(rawName, names);
          var pa = CogCore._cortexParseArgs(rawArgs);

          if (nr.reason || !pa.ok) {
            out.unrepairable.push({
              id: cid, name: String(rawName == null ? '' : rawName),
              args: pa.ok ? pa.value : rawArgs,
              reason: [nr.reason, pa.ok ? null : pa.reason].filter(Boolean).join('; ')
            });
            continue;
          }
          if (nr.changed) out.changed.push(nr.changed);
          for (k = 0; k < pa.changed.length; k++) out.changed.push(pa.changed[k]);
          out.calls.push({ id: cid, name: nr.name, args: pa.value });
        }
      }

      /* Narration recovery: only when the reply carried NO call objects at all. */
      if (!raw || !raw.length) {
        var rc = null;
        try { rc = CogCore._cortexRecoverFromNarration(content, names); } catch (e) { rc = null; }
        if (rc && rc.ambiguous) {
          out.unrepairable.push({ id: '', name: '', args: null, reason: rc.reason });
        } else if (rc && rc.call) {
          out.calls.push({ id: '', name: rc.call.name, args: rc.call.args });
          for (k = 0; k < rc.changed.length; k++) out.changed.push(rc.changed[k]);
        }
      }
      return out;
    },

    /* ---- Phase 11 internals (all pure) ---- */

    /** Pull the raw call list + any prose content out of every supported reply shape. */
    _cortexRawCalls: function (reply) {
      var out = { raw: null, content: '' };
      if (reply === null || reply === undefined) return out;
      if (typeof reply === 'string') { out.content = reply; return out; }
      if (Array.isArray(reply)) { out.raw = reply.slice(); return out; }
      if (typeof reply !== 'object') return out;
      if (typeof reply.content === 'string') out.content = reply.content;
      var arr = function (v) { return Array.isArray(v) ? v : null; };
      var raw = arr(reply.toolCalls) || arr(reply.tool_calls)
        || (reply.message && (arr(reply.message.toolCalls) || arr(reply.message.tool_calls)))
        || null;
      if (!raw && reply.result && Array.isArray(reply.result.output)) {
        var gathered = [];
        for (var i = 0; i < reply.result.output.length; i++) {
          var o = reply.result.output[i] || {};
          if (Array.isArray(o.tool_calls)) gathered = gathered.concat(o.tool_calls);
          else if (Array.isArray(o.toolCalls)) gathered = gathered.concat(o.toolCalls);
          else if (o.type === 'function_call' || typeof o.name === 'string') gathered.push(o);
          if (typeof o.content === 'string' && o.content) out.content += (out.content ? '\n' : '') + o.content;
        }
        if (gathered.length) raw = gathered;
      }
      /* a single bare call object (not wrapped in an array) */
      if (!raw && typeof reply.name === 'string' &&
          (reply.arguments !== undefined || reply.args !== undefined || reply.function)) raw = [reply];
      out.raw = raw;
      return out;
    },

    /** Lowercase + strip every non-alphanumeric char (separator/space/case folding). */
    _cortexNormName: function (s) {
      return String(s === null || s === undefined ? '' : s).toLowerCase().replace(/[^a-z0-9]/g, '');
    },

    /** Similarity in [0,1] from the Levenshtein distance over two normalised names. */
    _cortexEditSim: function (a, b) {
      a = String(a === null || a === undefined ? '' : a);
      b = String(b === null || b === undefined ? '' : b);
      var la = a.length, lb = b.length;
      if (!la && !lb) return 1;
      if (!la || !lb) return 0;
      var prev = [], cur = [], i, j;
      for (j = 0; j <= lb; j++) prev[j] = j;
      for (i = 1; i <= la; i++) {
        cur[0] = i;
        for (j = 1; j <= lb; j++) {
          var cost = a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1;
          var del = prev[j] + 1, ins = cur[j - 1] + 1, sub = prev[j - 1] + cost;
          cur[j] = Math.min(del, ins, sub);
        }
        prev = cur.slice();
      }
      return 1 - prev[lb] / Math.max(la, lb);
    },

    /**
     * Reconcile a model-authored tool name against the allowed names:
     * exact -> case-insensitive -> separator-normalised -> nearest by similarity
     * with a UNIQUE winner at >= 0.86. Returns {name, changed?|reason?}.
     * NEVER guesses: an ambiguous near-miss returns a `reason`, not a winner.
     */
    _cortexReconcileName: function (name, names) {
      var NEAR = 0.86;
      var n = (name === null || name === undefined) ? '' : String(name);
      if (!n) return { name: '', reason: 'missing tool name' };
      if (!Array.isArray(names) || !names.length) return { name: n };
      if (names.indexOf(n) !== -1) return { name: n };
      var i, hits = [];
      for (i = 0; i < names.length; i++) if (names[i].toLowerCase() === n.toLowerCase()) hits.push(names[i]);
      if (hits.length === 1) return { name: hits[0], changed: 'tool name "' + n + '" -> "' + hits[0] + '" (case)' };
      if (hits.length > 1) return { name: n, reason: 'ambiguous tool name "' + n + '" — ' + hits.join(' / ') + ' differ only by case' };
      var norm = CogCore._cortexNormName(n);
      hits = [];
      for (i = 0; i < names.length; i++) if (CogCore._cortexNormName(names[i]) === norm) hits.push(names[i]);
      if (hits.length === 1) return { name: hits[0], changed: 'tool name "' + n + '" -> "' + hits[0] + '" (separator)' };
      if (hits.length > 1) return { name: n, reason: 'ambiguous tool name "' + n + '" — matches ' + hits.join(' / ') + ' after normalisation' };
      var best = -1, winners = [], sim;
      for (i = 0; i < names.length; i++) {
        sim = CogCore._cortexEditSim(norm, CogCore._cortexNormName(names[i]));
        if (sim > best + 1e-9) { best = sim; winners = [names[i]]; }
        else if (Math.abs(sim - best) <= 1e-9) winners.push(names[i]);
      }
      if (best >= NEAR) {
        if (winners.length === 1) {
          return { name: winners[0], changed: 'tool name "' + n + '" -> "' + winners[0] + '" (near-miss ' + best.toFixed(2) + ')' };
        }
        return { name: n, reason: 'ambiguous tool name "' + n + '" — ' + winners.join(' / ') + ' are equally close at ' + best.toFixed(2) };
      }
      return { name: n, reason: 'unknown tool name "' + n + '" (closest "' + winners[0] + '" at ' + best.toFixed(2) + ')' };
    },

    /** The balanced `{…}` span starting at `start`, or null. String-literal aware. */
    _cortexSpanObject: function (s, start) {
      s = String(s === null || s === undefined ? '' : s);
      if (!(start >= 0) || s.charAt(start) !== '{') return null;
      var depth = 0, inStr = false, esc = false;
      for (var i = start; i < s.length; i++) {
        var ch = s.charAt(i);
        if (inStr) {
          if (esc) { esc = false; continue; }
          if (ch === '\\') { esc = true; continue; }
          if (ch === '"') inStr = false;
          continue;
        }
        if (ch === '"') { inStr = true; continue; }
        if (ch === '{') depth++;
        else if (ch === '}') { depth--; if (depth === 0) return s.slice(start, i + 1); }
      }
      return null;
    },

    /** The first balanced `{…}` object inside a blob of text, or null. */
    _cortexFirstObject: function (s) {
      s = String(s === null || s === undefined ? '' : s);
      return CogCore._cortexSpanObject(s, s.indexOf('{'));
    },

    /** Every balanced `{…}` object inside a blob of text (bounded). */
    _cortexAllObjects: function (s) {
      s = String(s === null || s === undefined ? '' : s);
      var out = [], i = 0, span;
      for (var guard = 0; guard < 64; guard++) {
        var idx = s.indexOf('{', i);
        if (idx === -1) break;
        span = CogCore._cortexSpanObject(s, idx);
        if (!span) break;
        out.push(span);
        i = idx + span.length;
      }
      return out;
    },

    /**
     * Close an unbalanced JSON text: append the missing quote/closers, dropping a
     * dangling separator first. Returns null when the text is already balanced.
     * Only ever ADDS syntax — never a value.
     */
    _cortexCloseBalance: function (s) {
      s = String(s === null || s === undefined ? '' : s);
      var stack = [], inStr = false, esc = false, i, ch;
      for (i = 0; i < s.length; i++) {
        ch = s.charAt(i);
        if (inStr) {
          if (esc) { esc = false; continue; }
          if (ch === '\\') { esc = true; continue; }
          if (ch === '"') inStr = false;
          continue;
        }
        if (ch === '"') { inStr = true; continue; }
        if (ch === '{' || ch === '[') stack.push(ch);
        else if (ch === '}' || ch === ']') stack.pop();
      }
      if (!stack.length && !inStr) return null;
      var out = s;
      if (inStr) out += '"';
      out = out.replace(/,\s*$/, '');
      for (i = stack.length - 1; i >= 0; i--) out += (stack[i] === '{' ? '}' : ']');
      return out;
    },

    /**
     * Parse a text into a plain JSON object using FORMAT-ONLY repairs, in order:
     * as-is -> trailing commas dropped -> first balanced object -> close-balance
     * -> a JSON string that itself contains the object (double-encoded). Returns
     * the object or null. It never fabricates a member or a value.
     */
    _cortexTryParseObject: function (text, depth) {
      var t = String(text === null || text === undefined ? '' : text).trim();
      if (!t) return null;
      if ((depth || 0) > 2) return null;
      var variants = [t, t.replace(/,\s*([}\]])/g, '$1')];
      var span = CogCore._cortexFirstObject(t);
      if (span) { variants.push(span); variants.push(span.replace(/,\s*([}\]])/g, '$1')); }
      var closed = CogCore._cortexCloseBalance(t);
      if (closed) variants.push(closed);
      if (span) { var closedSpan = CogCore._cortexCloseBalance(span); if (closedSpan) variants.push(closedSpan); }
      /* A markdown fence — closed OR left unclosed by a truncated reply. */
      var unfenced = t.replace(/^\s*```[A-Za-z0-9_-]*[ \t]*\r?\n?/, '').replace(/\r?\n?\s*```\s*$/, '');
      if (unfenced !== t) {
        variants.push(unfenced);
        variants.push(unfenced.replace(/,\s*([}\]])/g, '$1'));
        var uSpan = CogCore._cortexFirstObject(unfenced);
        if (uSpan) {
          variants.push(uSpan);
          variants.push(uSpan.replace(/,\s*([}\]])/g, '$1'));
          var uClosed = CogCore._cortexCloseBalance(uSpan);
          if (uClosed) variants.push(uClosed);
        }
        var uAll = CogCore._cortexCloseBalance(unfenced);
        if (uAll) variants.push(uAll);
      }
      for (var i = 0; i < variants.length; i++) {
        var parsed;
        try { parsed = JSON.parse(variants[i]); } catch (e) { continue; }
        if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
        if (typeof parsed === 'string' && parsed.trim().charAt(0) === '{') {
          var inner = CogCore._cortexTryParseObject(parsed, (depth || 0) + 1);
          if (inner) return inner;
        }
      }
      return null;
    },

    /** Repair one call's `arguments` into a plain object. {ok, value, changed, reason}. */
    _cortexParseArgs: function (raw) {
      if (raw !== null && raw !== undefined && typeof raw === 'object') {
        if (Array.isArray(raw)) return { ok: false, value: raw, changed: [], reason: 'arguments is an array, not an object' };
        return { ok: true, value: raw, changed: [], reason: null };
      }
      if (raw === null || raw === undefined) return { ok: false, value: raw, changed: [], reason: 'arguments missing' };
      var s = String(raw).trim();
      if (!s) return { ok: true, value: {}, changed: ['empty arguments -> {}'], reason: null };

      var changed = [], body = s;
      var fence = body.match(/^```[A-Za-z0-9_-]*\s*\n?([\s\S]*?)\n?\s*```$/);
      if (fence) { body = fence[1].trim(); changed.push('stripped a markdown fence from arguments'); }

      /* Clean pass first: a legitimate JSON object string must stay repair-free. */
      var direct = null;
      try { direct = JSON.parse(body); } catch (e) { direct = null; }
      if (direct !== null && typeof direct === 'object' && !Array.isArray(direct)) {
        return { ok: true, value: direct, changed: changed, reason: null };
      }
      if (typeof direct === 'string' && direct.trim().charAt(0) === '{') {
        var once = CogCore._cortexTryParseObject(direct);
        if (once) return { ok: true, value: once, changed: changed.concat(['decoded double-encoded arguments']), reason: null };
      }
      var loose = CogCore._cortexTryParseObject(body);
      if (loose) {
        if (/,\s*[\]}]/.test(body)) changed.push('removed a trailing comma');
        if (CogCore._cortexCloseBalance(body) || CogCore._cortexCloseBalance(CogCore._cortexFirstObject(body) || '')) {
          changed.push('closed an unbalanced JSON object');
        }
        changed.push('extracted a parseable JSON object from the arguments');
        return { ok: true, value: loose, changed: changed, reason: null };
      }
      return { ok: false, value: raw, changed: changed, reason: 'arguments is not a parseable JSON object' };
    },

    /** A `{name, arguments}`-shaped plain object -> a candidate call, or null. */
    _cortexCallFromObject: function (obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
      var fn = (obj.function && typeof obj.function === 'object') ? obj.function : obj;
      var name = fn.name !== undefined ? fn.name : (obj.tool !== undefined ? obj.tool : obj.tool_name);
      if (typeof name !== 'string' || !name) return null;
      var args = fn.arguments !== undefined ? fn.arguments
        : (fn.args !== undefined ? fn.args
          : (obj.arguments !== undefined ? obj.arguments
            : (obj.args !== undefined ? obj.args : obj.parameters)));
      if (args === undefined) return null;
      if (typeof args === 'string') {
        var pa = CogCore._cortexParseArgs(args);
        if (!pa.ok) return null;
        args = pa.value;
      }
      if (args === null || typeof args !== 'object' || Array.isArray(args)) return null;
      return { name: name, args: args };
    },

    /**
     * Recover ONE call from a prose reply: either a `name({…})`-shaped span, or a
     * JSON object carrying a name + arguments (fenced or bare). Returns
     * {call, changed} | {ambiguous:true, reason} | null. More than one DISTINCT
     * candidate is ambiguous and is refused rather than guessed.
     */
    _cortexRecoverFromNarration: function (content, names) {
      var text = String(content === null || content === undefined ? '' : content);
      if (!text.trim()) return null;
      var cands = [], i;
      var re = /([A-Za-z_][A-Za-z0-9_.-]*)\s*\(\s*\{/g, m;
      while ((m = re.exec(text)) !== null) {
        var open = m.index + m[0].length - 1;
        var span = CogCore._cortexSpanObject(text, open);
        var argsText = span;
        if (!argsText) {
          /* truncated narration: close-balance the tail and keep it only if it parses */
          var closedTail = CogCore._cortexCloseBalance(text.slice(open));
          if (closedTail) {
            var tailObj = CogCore._cortexTryParseObject(closedTail);
            if (tailObj) argsText = JSON.stringify(tailObj);
          }
        }
        if (argsText) cands.push({ name: m[1], argsText: argsText, how: 'narration "name({…})"' });
      }
      var objs = CogCore._cortexAllObjects(text);
      for (i = 0; i < objs.length; i++) {
        var parsed = CogCore._cortexTryParseObject(objs[i]);
        var cand = CogCore._cortexCallFromObject(parsed);
        if (cand) cands.push({ name: cand.name, argsValue: cand.args, how: 'narrated JSON object' });
      }

      var kept = [], seen = {};
      for (i = 0; i < cands.length; i++) {
        var cd = cands[i];
        var nr = CogCore._cortexReconcileName(cd.name, names);
        if (nr.reason) continue;                        /* not an allowed tool: prose, not a call */
        var args = cd.argsValue;
        if (args === undefined) {
          var pa = CogCore._cortexParseArgs(cd.argsText);
          if (!pa.ok) continue;
          args = pa.value;
        }
        if (!args || typeof args !== 'object' || Array.isArray(args)) continue;
        var sig = nr.name + '|' + JSON.stringify(args);
        if (seen[sig]) continue;
        seen[sig] = 1;
        kept.push({ name: nr.name, args: args, changed: nr.changed, how: cd.how });
      }
      if (!kept.length) return null;
      if (kept.length > 1) {
        return { ambiguous: true, reason: 'ambiguous narration — ' + kept.length + ' distinct candidate calls (' +
          kept.map(function (k) { return k.name; }).join(', ') + ')' };
      }
      var win = kept[0];
      var changed = ['recovered a tool call from narration (' + win.how + ')'];
      if (win.changed) changed.push(win.changed);
      return { call: { name: win.name, args: win.args }, changed: changed };
    },

    /* ================= PHASE 10 — LOCAL CORTEX CLIENT =================
     * The ONLY door between the harness and the local-models sidecar (127.0.0.1:8932).
     * Pure and DOM-free: the transport is INJECTED so jsdom/Node tests need no server,
     * and the whole object may never throw or reject — the sidecar is opt-in, so a
     * dead/missing/lying sidecar must degrade, never break the conversation.
     *
     *   CogCore.localModels.client(base, fetchImpl, clock) ->
     *     { health(), repair(payload,opts), decide(payload,opts),
     *       selectTool(payload,opts), outcome(payload,opts) }
     *
     *  - `base` is e.g. 'http://127.0.0.1:8932' (trailing slashes are tolerated).
     *  - `fetchImpl` defaults to `root.fetch`; when neither exists every call resolves
     *    {ok:false, degraded:true}.
     *  - `clock` is an injected time source: an object with now() OR a function returning
     *    ms. It is used ONLY for the elapsed-ms field; default Date.now.
     *  - Every failure (network reject, abort/timeout, non-2xx, unparseable JSON, no fetch)
     *    resolves {ok:false, degraded:true, error:'<short reason>'} — never a throw.
     *  - No `Authorization` header is ever sent: the sidecar is local and must never
     *    receive an API key.
     */
    localModels: {
      /* The settings block (plan §4). Kept byte-comparable with DEF_SETTINGS.localModels. */
      DEFAULTS: {
        enabled: false,
        needle: { enabled: false, minConfidence: 0.75, confirmBand: [0.5, 0.75], timeoutMs: 800 },
        laya: { enabled: false, minConfidence: 0.70, timeoutMs: 500, preflight: true, anomaly: true },
        sanitizer: { enabled: true, mode: 'auto', deterministicPass: true },
        dispatcher: { enabled: false, autoReadOnly: true },
        port: 8932,
        ledger: 'var/local-models.jsonl'
      },

      /* Per-call bounded timeouts (ms). Overridable with opts.timeoutMs. */
      TIMEOUTS: { health: 1500, repair: 800, decide: 500, select: 800, outcome: 1500 },

      client: function (base, fetchImpl, clock) {
        var b = '';
        try { b = String(base == null ? '' : base).replace(/\/+$/, ''); } catch (e) { b = ''; }

        var doFetch = (typeof fetchImpl === 'function') ? fetchImpl
          : ((root && typeof root.fetch === 'function') ? function () { return root.fetch.apply(root, arguments); } : null);

        var nowMs = function () {
          try {
            if (typeof clock === 'function') return clock();
            if (clock && typeof clock.now === 'function') return clock.now();
          } catch (e) { /* fall through to the wall clock */ }
          try { return Date.now(); } catch (e) { return 0; }
        };

        var degraded = function (reason) {
          return { ok: false, degraded: true, error: String(reason == null ? 'unavailable' : reason) };
        };

        var timeoutFor = function (opts, dflt) {
          var v = opts && opts.timeoutMs;
          return (typeof v === 'number' && isFinite(v) && v > 0) ? v : dflt;
        };

        /* One bounded request. ALWAYS resolves an object; never throws, never rejects. */
        var request = function (method, path, payload, timeoutMs) {
          if (!doFetch) return Promise.resolve(degraded('no fetch implementation available'));
          var url = b + path;
          var init = { method: method, headers: { 'Content-Type': 'application/json' } };
          if (method !== 'GET') {
            var bodyText = '{}';
            try { bodyText = JSON.stringify(payload || {}); } catch (e) { bodyText = '{}'; }
            init.body = bodyText;
          }
          try {
            /* jsdom has no AbortSignal.timeout — build the signal DEFENSIVELY. */
            init.signal = (root && root.AbortSignal && root.AbortSignal.timeout)
              ? root.AbortSignal.timeout(timeoutMs) : undefined;
          } catch (e) { init.signal = undefined; }

          var started = nowMs();
          return new Promise(function (resolve) {
            var settled = false;
            var done = function (v) { if (settled) return; settled = true; try { resolve(v); } catch (e) { /* nothing left to do */ } };

            var p;
            try { p = doFetch(url, init); }
            catch (e) { return done(degraded('fetch threw: ' + ((e && e.message) || e))); }
            if (!p || typeof p.then !== 'function') return done(degraded('fetch returned no promise'));

            p.then(function (res) {
              try {
                if (!res) return done(degraded('empty response'));
                if (res.ok === false) return done(degraded('http ' + (res.status || 'error')));
                if (typeof res.json !== 'function') return done(degraded('response has no json()'));
                return Promise.resolve(res.json()).then(function (json) {
                  var out = Object.assign({}, (json && typeof json === 'object') ? json : {}, {
                    /* keep the server's own ok:false; otherwise the body parsed => ok */
                    ok: (json && json.ok === false) ? false : true
                  });
                  var elapsed = nowMs() - started;
                  out.latency_ms = (typeof elapsed === 'number' && isFinite(elapsed) && elapsed >= 0) ? Math.round(elapsed) : 0;
                  done(out);
                }, function (e) { done(degraded('unparseable json: ' + ((e && e.message) || e))); });
              } catch (e) { done(degraded('bad response: ' + ((e && e.message) || e))); }
            }, function (e) { done(degraded('network: ' + ((e && e.message) || e))); });
          });
        };

        return {
          /* GET /health — is the sidecar alive, are the models loaded, is the ledger writable? */
          health: function (opts) { return request('GET', '/health', null, timeoutFor(opts, 1500)); },
          /* POST /repair — Needle: salvage a malformed tool call. */
          repair: function (payload, opts) { return request('POST', '/repair', payload, timeoutFor(opts, 800)); },
          /* POST /decide — Laya: gate a mutating rite / flag a reply anomaly. */
          decide: function (payload, opts) { return request('POST', '/decide', payload, timeoutFor(opts, 500)); },
          /* POST /select — cheap local pre-router candidate selection. */
          selectTool: function (payload, opts) { return request('POST', '/select', payload, timeoutFor(opts, 800)); },
          /* POST /ledger — record the operator/outcome verdict for a trace (payload as-is). */
          outcome: function (payload, opts) { return request('POST', '/ledger', payload, timeoutFor(opts, 1500)); }
        };
      }
    },

    /* ================= PHASE 12 — NEEDLE REPAIR PASS (F1) =================
     * `sanitizeReply(reply, allowedTools, deps)` — the single door the agent turn uses to turn a
     * model reply into dispatchable calls. Stage 1 is the Phase 11 deterministic salvage
     * (`salvageToolCalls`) and is COMPLETELY unchanged: if plain code can produce dispatchable
     * calls with nothing left over, this function does NO I/O.
     *
     * Stage 2 (opt-in, OFF by default) offers the calls plain code could not resolve — and, in
     * `mode:'on'`, a prose-only narration — to the local Needle sidecar, bounded by
     * `needle.timeoutMs`. Local inference is NEVER in the critical path:
     *   - sidecar down / disabled / unconfigured / slow / empty ⇒ the ORIGINAL reply and its
     *     Phase 11 unrepairable suspects pass through byte-identically;
     *   - a repair fixes FORMAT ONLY and is still gated by `validateStructuredOutput` before it
     *     can be dispatched — the local model never widens the allow-list, and a name it invents
     *     is rejected by the deterministic gate, not trusted;
     *   - an empty `calls:[]` never manufactures a call (the turn ends as prose);
     *   - it NEVER throws; any failure degrades to `source:'original'`.
     *
     * Return: {calls, source:'deterministic'|'needle'|'original', confidence, accepted,
     *          trace_id, degraded, changed, unrepairable, reason}
     * `calls` is ALWAYS what the caller should dispatch. Phase 11 deterministic calls are never
     * dropped: a mixed turn (repairable + suspects) keeps its dispatchable call even when the
     * probe is skipped, times out or is rejected. `unrepairable` holds the still-unresolved calls
     * so the caller can re-attach them to the Phase 7 gate.
     *
     * `deps` is INJECTED so jsdom/Node tests need no server and the function stays pure:
     *   {client, settings, traceId, timeout, now}
     * The `/repair` and `/ledger` requests carry NO Authorization header and never the provider
     * API key: only `{suspect, candidates, schema, trace_id}` / `{trace_id, action}` are sent.
     *
     * Returns a plain object on every non-probing path, and a Promise when it actually probes, so
     * `await sanitizeReply(...)` is always correct and a no-deps caller stays synchronous.
     */
    sanitizeReply: function (reply, allowedTools, deps) {
      var out = {
        calls: [], source: 'original', confidence: null, accepted: false,
        trace_id: '', degraded: false, changed: [], unrepairable: [], reason: ''
      };
      try {
        out.trace_id = CogCore._cortexTraceId(deps);
        return CogCore._cortexSanitize(reply, allowedTools, deps, out);
      } catch (e) {
        out.calls = []; out.source = 'original'; out.accepted = false;
        out.degraded = true; out.reason = 'error';
        return out;
      }
    },

    /* ---- Phase 12 internals (pure except for the injected client) ---- */

    _cortexTraceId: function (deps) {
      try {
        if (deps && typeof deps.traceId === 'function') {
          var t = deps.traceId();
          if (t !== undefined && t !== null && String(t)) return String(t);
        }
        if (deps && deps.traceId !== undefined && deps.traceId !== null && String(deps.traceId)) {
          return String(deps.traceId);
        }
      } catch (e) { /* fall through to a fresh id */ }
      try { return CogCore.uid(); } catch (e2) { return 'tr_' + Math.random().toString(36).slice(2); }
    },

    /* Merge an injected settings block over the §4 defaults, so a partial/legacy blob can never
       leave a sub-object undefined. Never mutates the caller's object. */
    _cortexLmSettings: function (settings) {
      var d = (CogCore.localModels && CogCore.localModels.DEFAULTS) || {};
      var src = (settings && typeof settings === 'object' && !Array.isArray(settings)) ? settings : {};
      var out = {}, k;
      for (k in d) if (Object.prototype.hasOwnProperty.call(d, k) && k !== 'needle' && k !== 'laya' && k !== 'sanitizer' && k !== 'dispatcher') out[k] = d[k];
      for (k in src) if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
      var subs = ['needle', 'laya', 'sanitizer', 'dispatcher'];
      for (var s = 0; s < subs.length; s++) {
        var name = subs[s], base = d[name] || {}, got = src[name], sub = {}, b, g;
        for (b in base) if (Object.prototype.hasOwnProperty.call(base, b)) sub[b] = base[b];
        if (got && typeof got === 'object' && !Array.isArray(got)) {
          for (g in got) if (Object.prototype.hasOwnProperty.call(got, g)) sub[g] = got[g];
        }
        out[name] = sub;
      }
      return out;
    },

    _cortexSanitize: function (reply, allowedTools, deps, out) {
      deps = (deps && typeof deps === 'object') ? deps : {};
      var lm = CogCore._cortexLmSettings(deps.settings);

      /* ---- STAGE 1: deterministic (Phase 11, unchanged) ---- */
      var salv;
      try { salv = CogCore.salvageToolCalls(reply, allowedTools); }
      catch (e) { salv = { calls: [], changed: [], unrepairable: [] }; }
      var detCalls = Array.isArray(salv.calls) ? salv.calls.slice() : [];
      out.changed = Array.isArray(salv.changed) ? salv.changed.slice() : [];
      out.unrepairable = Array.isArray(salv.unrepairable) ? salv.unrepairable.slice() : [];
      out.calls = detCalls.slice();

      if (detCalls.length && !out.unrepairable.length) {
        out.source = 'deterministic'; out.accepted = true;
        return out;   /* nothing suspect: Phase 11 behaviour, no I/O at all */
      }

      /* ---- decide whether to probe Needle (plan §4 phase 12, step 2) ---- */
      var san = lm.sanitizer || {}, nd = lm.needle || {};
      var mode = (san.mode === undefined || san.mode === null) ? 'auto' : String(san.mode);
      var client = (deps.client && typeof deps.client.repair === 'function') ? deps.client : null;
      var suspects = out.unrepairable.length > 0;
      var proseOnly = detCalls.length === 0 && out.unrepairable.length === 0;
      var wantProbe = false;
      if (lm.enabled !== false && san.enabled !== false && nd.enabled !== false &&
          san.deterministicPass !== false && client && mode !== 'off') {
        if (mode === 'on') wantProbe = suspects || proseOnly;
        else wantProbe = suspects;   /* 'auto' probes only on suspects; unknown modes behave as auto */
      }

      if (!wantProbe) {
        if (detCalls.length) { out.source = 'deterministic'; out.accepted = true; }
        else { out.source = 'original'; out.accepted = false; }
        return out;
      }

      /* ---- STAGE 2: exactly one bounded, gated Needle probe ---- */
      var minConf = (typeof nd.minConfidence === 'number' && isFinite(nd.minConfidence)) ? nd.minConfidence : 0.75;
      var ms = (typeof nd.timeoutMs === 'number' && isFinite(nd.timeoutMs) && nd.timeoutMs > 0) ? nd.timeoutMs : 800;
      var payload = CogCore._cortexRepairPayload(reply, allowedTools, out.unrepairable, out.trace_id);

      var probe;
      try { probe = client.repair(payload); }
      catch (e) { probe = Promise.reject(e); }
      if (!probe || typeof probe.then !== 'function') probe = Promise.resolve(probe);

      var raced = CogCore._cortexRace(probe, ms, deps.timeout);
      return raced.then(function (r) {
        try {
          if (r && r.expired) {
            /* Bounded timeout: the transport may never honour an AbortSignal, so we race our own
               timer. Pass the ORIGINAL reply through and flag the turn. Never hangs. */
            out.calls = detCalls.slice();
            out.source = detCalls.length ? 'deterministic' : 'original';
            out.accepted = detCalls.length > 0;
            out.degraded = true;
            out.reason = 'timeout';
            CogCore._cortexPostOutcome(deps, client, out.trace_id, 'timeout');
            return out;
          }
          return CogCore._cortexApplyRepair(r ? r.value : null, out, detCalls, allowedTools, minConf, deps, client);
        } catch (e) {
          out.calls = detCalls.slice(); out.source = 'original'; out.accepted = false;
          out.degraded = true; out.reason = 'error';
          return out;
        }
      });
    },

    /*
     * Race an awaited local call against OUR OWN timer — never the transport's AbortSignal (the
     * sidecar/legacy fetch impl may ignore one entirely). Resolves {value, expired}; `expired`
     * true means the timer won. `timeoutDep` may be a scheduler fn(fn, ms, fallbackValue) or an
     * object with setTimeout; otherwise the real setTimeout is used.
     */
    _cortexRace: function (promise, ms, timeoutDep) {
      return new Promise(function (resolve) {
        var settled = false;
        var done = function (value, expired) {
          if (settled) return;
          settled = true;
          resolve({ value: value, expired: !!expired });
        };
        var fire = function () { done(null, true); };
        var scheduled = false;
        try {
          if (typeof timeoutDep === 'function') { timeoutDep(fire, ms, null); scheduled = true; }
          else if (timeoutDep && typeof timeoutDep.setTimeout === 'function') { timeoutDep.setTimeout(fire, ms); scheduled = true; }
        } catch (e) { scheduled = false; }
        if (!scheduled) { try { setTimeout(fire, ms); } catch (e2) { /* resolve on the promise then */ } }
        Promise.resolve(promise).then(
          function (v) { done(v, false); },
          function (e) { done({ ok: false, degraded: true, error: String((e && e.message) || e) }, false); }
        );
      });
    },

    /* Build the /repair payload. Suspects for a real repair; the reply's prose for a prose probe.
       Contains NO settings, NO API key: just the suspect, the candidate schemas and a trace id. */
    _cortexRepairPayload: function (reply, allowedTools, unrepairable, traceId) {
      var candidates = [];
      try {
        var list = Array.isArray(allowedTools) ? allowedTools.slice(0, 10) : [];
        for (var i = 0; i < list.length; i++) candidates.push(CogCore._cortexCompactTool(list[i]));
      } catch (e) { candidates = []; }
      var suspect;
      if (unrepairable && unrepairable.length) {
        suspect = [];
        for (var u = 0; u < unrepairable.length; u++) {
          var su = unrepairable[u] || {};
          suspect.push({
            name: String(su.name === undefined || su.name === null ? '' : su.name),
            arguments: su.args,
            reason: String(su.reason || '')
          });
        }
      } else {
        var content = '';
        try { content = CogCore._cortexRawCalls(reply).content || ''; } catch (e2) { content = ''; }
        suspect = String(content);
      }
      return { suspect: suspect, candidates: candidates, schema: { tools: candidates }, trace_id: traceId };
    },

    _cortexCompactTool: function (t) {
      var inner = (t && t.function && typeof t.function === 'object') ? t.function : (t || {});
      return {
        name: String(inner.name === undefined || inner.name === null ? '' : inner.name),
        parameters: inner.parameters || { type: 'object', properties: {} }
      };
    },

    /* Normalise a Needle `{calls:[{name,arguments}]}` reply into the harness's `{id,name,args}`
       shape and mint an id (the Phase 7 validator requires one to correlate a tool result). */
    _cortexNeedleCalls: function (calls, traceId) {
      var out = [];
      var tag = String(traceId === undefined || traceId === null ? 'tr' : traceId).replace(/[^a-z0-9]/gi, '').slice(0, 10) || 'tr';
      for (var i = 0; i < (calls || []).length; i++) {
        var c = calls[i] || {};
        var fn = (c.function && typeof c.function === 'object') ? c.function : c;
        var name = (c.name !== undefined && c.name !== null && c.name !== '') ? c.name : fn.name;
        var args = c.args !== undefined ? c.args
          : (c.arguments !== undefined ? c.arguments
            : (fn.arguments !== undefined ? fn.arguments : fn.args));
        var id = (c.id !== undefined && c.id !== null && String(c.id)) ? String(c.id) : ('call_needle_' + tag + '_' + i);
        out.push({ id: id, name: String(name === undefined || name === null ? '' : name), args: args });
      }
      return out;
    },

    _cortexMergeCalls: function (a, b) {
      var out = (a || []).slice();
      for (var i = 0; i < (b || []).length; i++) {
        var dup = false;
        for (var j = 0; j < out.length; j++) {
          if (out[j].name === b[i].name && CogCore._cortexSameArgs(out[j].args, b[i].args)) { dup = true; break; }
        }
        if (!dup) out.push(b[i]);
      }
      return out;
    },

    _cortexSameArgs: function (x, y) {
      try { return JSON.stringify(x) === JSON.stringify(y); } catch (e) { return false; }
    },

    /* Gate a Needle reply and fold the verdict into `out`. The Phase 7 validator is the gate:
       ok + at least one sanitized call + a numeric confidence >= minConfidence, or nothing. */
    _cortexApplyRepair: function (res, out, detCalls, allowedTools, minConf, deps, client) {
      var resCalls = (res && Array.isArray(res.calls)) ? res.calls : [];
      var conf = (res && typeof res.confidence === 'number') ? res.confidence : null;  /* NaN stays below */
      var harness = CogCore._cortexNeedleCalls(resCalls, out.trace_id);
      var gate = CogCore.validateStructuredOutput(harness, allowedTools);
      var degradedRes = !!(res && (res.ok === false || res.degraded));
      var confOk = (typeof conf === 'number' && conf >= minConf);

      if (!degradedRes && gate.ok && gate.sanitized.length > 0 && confOk) {
        out.source = 'needle';
        out.accepted = true;
        out.confidence = conf;
        out.calls = CogCore._cortexMergeCalls(detCalls, gate.sanitized);
        out.unrepairable = [];          /* the successful repair RESOLVES the suspects */
        out.degraded = false;
        out.reason = '';
        out.changed = out.changed.concat(['needle repaired ' + gate.sanitized.length + ' rite(s)']);
        CogCore._cortexPostOutcome(deps, client, out.trace_id, 'accepted');
        return out;
      }

      out.calls = detCalls.slice();
      out.source = detCalls.length ? 'deterministic' : 'original';
      out.accepted = detCalls.length > 0;
      out.confidence = conf;
      out.degraded = degradedRes;
      out.reason = degradedRes ? 'degraded'
        : (resCalls.length === 0 ? 'empty' : (!gate.ok ? 'invalid' : 'below_threshold'));
      CogCore._cortexPostOutcome(deps, client, out.trace_id, 'passed_through');
      return out;
    },

    /* Fire-and-forget ledger outcome. The client already never rejects; do not rely on that —
       and NEVER await it (the ledger must not delay or hang the turn). */
    _cortexPostOutcome: function (deps, client, traceId, action) {
      try {
        var c = (client && typeof client.outcome === 'function') ? client
          : ((deps && deps.client && typeof deps.client.outcome === 'function') ? deps.client : null);
        if (!c) return;
        var p = c.outcome({ trace_id: traceId, action: action });
        if (p && typeof p.then === 'function') p.then(function () {}, function () {});
      } catch (e) { /* the ledger is a nicety; it must never break the turn */ }
    },

    /**
     * providerProfileStore — persisted list of provider endpoint profiles.
     * DOM-free. Operates over an injected storage adapter that satisfies
     * {getItem,setItem,removeItem} (localStorage in the browser, fake in tests).
     * Profile shape: {id, name, backend, endpoint, model, apiKey}.
     * A profile is a saved snapshot of {backend, endpoint, model, apiKey}; "applying" it
     * returns that slice so the caller can rewrite the live settings.
     */
    profileStore: {
      key: 'cogitator.profiles',

      _read: function (storage) {
        try {
          var v = storage.getItem(this.key);
          var a = v ? JSON.parse(v) : [];
          return Array.isArray(a) ? a : [];
        } catch (e) { return []; }
      },
      _write: function (storage, arr) {
        try { storage.setItem(this.key, JSON.stringify(arr)); return true; }
        catch (e) { return false; }
      },

      /** Sorted copy of all profiles (newest first). */
      list: function (storage) {
        return this._read(storage).slice().reverse();
      },

      /** Look up a profile by id. */
      get: function (storage, id) {
        return this._read(storage).find(function (p) { return p.id === id; }) || null;
      },

      /** Insert (no id) or update in place (with id). Returns the saved profile. */
      save: function (storage, profile) {
        var a = this._read(storage);
        var existing = (profile && profile.id) ? this.get(storage, profile.id) : null;
        var rec = {
          id: profile && profile.id ? profile.id : CogCore.uid(),
          name: String((profile && profile.name) || '').trim() || 'UNNAMED PROFILE',
          backend: (profile && profile.backend) || 'auto',
          endpoint: (profile && profile.endpoint) || '',
          model: (profile && profile.model) || '',
          apiKey: profile && profile.apiKey !== undefined ? String(profile.apiKey) : (existing ? String(existing.apiKey || '') : '')
        };
        var i = a.findIndex(function (p) { return p.id === rec.id; });
        if (i >= 0) a[i] = rec; else a.push(rec);
        this._write(storage, a);
        return this.get(storage, rec.id);
      },

      /** Delete a profile by id (or by name as a convenience). Returns the remaining list. */
      remove: function (storage, id) {
        var a = this._read(storage).filter(function (p) { return p.id !== id && p.name !== id; });
        this._write(storage, a);
        return a;
      },

      /** The slice of settings a profile applies. */
      apply: function (storage, id) {
        return this.get(storage, id);
      }
    }
  };

  return CogCore;
});