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