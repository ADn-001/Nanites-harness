/*
 * appcore.js — COGITATOR shared pure logic.
 * UMD: exposed as `window.CogCore` in the browser (loaded by index.html BEFORE the
 * inline script) and as `module.exports` under Node so the frontend test suite can
 * unit-test the same code that ships. Keep everything in here DOM-free and side-effect
 * free (no globals, no localStorage touches) so it is deterministic to test.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.CogCore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
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
     * directory and teaches it how to drive the tool loop. `opts.workdir` is the
     * bound directory taken from the user's settings.
     */
    buildAgentSystemPrompt: function (opts) {
      opts = opts || {};
      var workdir = opts.workdir || '(the bound working directory)';
      return [
        'You are COGITATOR, an autonomous coding agent. You use a tool loop: emit tool calls, observe the results returned between turns, and keep going until the task is done.',
        'FILESYSTEM JAIL: your ONLY reachable filesystem root is WORKDIR below. Every path you place in a tool argument MUST be project-RELATIVE to WORKDIR (e.g. "src/app.py", "readme.md", or "." for the root itself). You will never be handed host-absolute paths such as /home/..., /etc/passwd, or C:\\...; do not invent them. If a step genuinely requires a path outside the workdir, refuse and ask the user to remount.',
        'WORKDIR: ' + workdir,
        'TOOLS available this session: list_dir, grep, read_file, write_file, shell_exec, and clipboard access. Tool results are returned verbatim between turns. Prefer tools over prose; keep prose concise.',
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

    /*
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