#!/usr/bin/env python3
"""Generate the Phase 11 golden corpus (cases.json) with exact, valid escaping.

Written as a script so every nested JSON string is produced by json.dumps rather
than hand-escaped (hand-escaping produced an invalid file on the first attempt).
Re-runnable: `python3 tools/gen_toolcall_corpus.py`.
"""
import json
import os

TOOLS = ["read_file", "write_file", "list_dir", "grep", "git", "run_command"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "tests", "fixtures", "toolcall-corpus", "cases.json")


def J(o):
    """The JSON text of an object, as a model would emit it in `arguments`."""
    return json.dumps(o)


cases = []


def c(cid, category, fmt, reply, expect, tools=None):
    case = {"id": cid, "category": category, "format": fmt, "reply": reply, "expect": expect}
    if tools:
        case["tools"] = tools
    cases.append(case)


# ---------- A. legitimate, clean calls: byte-identical semantics, no repair ----------
c("clean-openai-array", "legitimate-call", "openai",
  [{"id": "call_a", "name": "read_file", "args": J({"path": "src/main.py"})}],
  {"name": "read_file", "args": {"path": "src/main.py"}, "unchanged": True})
c("clean-finalized-shape", "legitimate-call", "openai",
  {"toolCalls": [{"id": "call_a", "name": "list_dir", "args": J({"path": "."})}]},
  {"name": "list_dir", "args": {"path": "."}, "unchanged": True})
c("clean-openai-message", "legitimate-call", "openai",
  {"tool_calls": [{"id": "call_a", "type": "function",
                   "function": {"name": "grep", "arguments": J({"path": ".", "pattern": "TODO"})}}]},
  {"name": "grep", "args": {"path": ".", "pattern": "TODO"}, "unchanged": True})
c("clean-ollama", "legitimate-call", "ollama",
  {"message": {"tool_calls": [{"id": "call_a",
                               "function": {"name": "git", "arguments": J({"args": "status --short"})}}]}},
  {"name": "git", "args": {"args": "status --short"}, "unchanged": True})
c("clean-chatend", "legitimate-call", "chat.end",
  {"type": "chat.end", "result": {"output": [{"type": "message", "tool_calls": [
      {"index": 0, "id": "call_a", "function": {"name": "read_file", "arguments": J({"path": "README.txt"})}}]}]}},
  {"name": "read_file", "args": {"path": "README.txt"}, "unchanged": True})
c("clean-args-already-object", "legitimate-call", "openai",
  [{"id": "call_a", "name": "write_file", "args": {"path": "a.txt", "content": "hi"}}],
  {"name": "write_file", "args": {"path": "a.txt", "content": "hi"}, "unchanged": True})
c("clean-single-bare-call-object", "legitimate-call", "openai",
  {"id": "call_a", "name": "read_file", "arguments": J({"path": "x.py"})},
  {"name": "read_file", "args": {"path": "x.py"}, "unchanged": True})
c("clean-two-calls", "legitimate-call", "openai",
  [{"id": "call_a", "name": "list_dir", "args": J({"path": "."})},
   {"id": "call_b", "name": "read_file", "args": J({"path": "a.py"})}],
  {"name": "list_dir", "args": {"path": "."}, "unchanged": True, "callCount": 2})
c("clean-args-with-newlines", "legitimate-call", "openai",
  [{"id": "call_a", "name": "write_file",
    "args": '{\n  "path": "a.txt",\n  "content": "line1\\nline2"\n}'}],
  {"name": "write_file", "args": {"path": "a.txt", "content": "line1\nline2"}, "unchanged": True})
c("clean-run-command", "legitimate-call", "ollama",
  {"message": {"tool_calls": [{"function": {"name": "run_command", "arguments": J({"command": "ls -la"})}}]}},
  {"name": "run_command", "args": {"command": "ls -la"}, "unchanged": True})

# ---------- B. string-args needing a format repair ----------
c("string-args-fenced", "string-args", "openai",
  [{"id": "call_a", "name": "read_file", "args": "```json\n" + J({"path": "a.py"}) + "\n```"}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("string-args-fence-no-lang", "string-args", "openai",
  [{"id": "call_a", "name": "read_file", "args": "```\n" + J({"path": "a.py"}) + "\n```"}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("string-args-trailing-comma", "string-args", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py",}'}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("string-args-trailing-comma-nested", "string-args", "openai",
  [{"id": "call_a", "name": "grep", "args": '{"path":".","pattern":"x",}'}],
  {"name": "grep", "args": {"path": ".", "pattern": "x"}})
c("string-args-prose-prefix", "string-args", "openai",
  [{"id": "call_a", "name": "read_file", "args": 'Here are the arguments: {"path":"a.py"}'}],
  {"name": "read_file", "args": {"path": "a.py"}})

# ---------- C. trailing garbage ----------
c("string-args-trailing-garbage-brace", "trailing-garbage", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py"}}'}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("string-args-trailing-garbage-comment", "trailing-garbage", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py"} // done'}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("string-args-trailing-semicolon", "trailing-garbage", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py"};'}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("string-args-trailing-comma-after-close", "trailing-garbage", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py"},'}],
  {"name": "read_file", "args": {"path": "a.py"}})

# ---------- D. truncated JSON ----------
c("truncated-args-unclosed-object", "truncated-json", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py"'}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("truncated-args-unclosed-string", "truncated-json", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py'}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("truncated-args-two-fields", "truncated-json", "openai",
  [{"id": "call_a", "name": "grep", "args": '{"path":".","pattern":"x"'}],
  {"name": "grep", "args": {"path": ".", "pattern": "x"}})
c("truncated-args-dangling-comma", "truncated-json", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py",'}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("truncated-content-fenced", "truncated-json", "content",
  {"content": "```json\n" + J({"name": "read_file", "arguments": {"path": "a.py"}}) + "\n```"},
  {"name": "read_file", "args": {"path": "a.py"}})
c("truncated-content-narration", "truncated-json", "content",
  {"content": 'Invoking read_file({"path": "a.py"'},
  {"name": "read_file", "args": {"path": "a.py"}})
c("truncated-args-mid-value-unrepairable", "truncated-json", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":"a.py","line":'}],
  {"unrepairable": True})
c("truncated-args-no-value-unrepairable", "truncated-json", "openai",
  [{"id": "call_a", "name": "read_file", "args": '{"path":'}],
  {"unrepairable": True})

# ---------- E. double-encoded arguments ----------
c("double-encoded-args", "double-encoded", "openai",
  [{"id": "call_a", "name": "read_file", "args": json.dumps(J({"path": "a.py"}))}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("double-encoded-args-fenced", "double-encoded", "openai",
  [{"id": "call_a", "name": "read_file", "args": "```json\n" + json.dumps(J({"path": "b.py"})) + "\n```"}],
  {"name": "read_file", "args": {"path": "b.py"}})
c("double-encoded-args-ollama", "double-encoded", "ollama",
  {"message": {"tool_calls": [{"function": {"name": "list_dir", "arguments": json.dumps(J({"path": "."}))}}]}},
  {"name": "list_dir", "args": {"path": "."}})
c("double-encoded-args-spaced", "double-encoded", "openai",
  [{"id": "call_a", "name": "read_file", "args": json.dumps('{ "path": "c.py" }')}],
  {"name": "read_file", "args": {"path": "c.py"}})

# ---------- F. near-miss tool names ----------
c("near-case-upper", "near-miss-name", "openai",
  [{"id": "call_a", "name": "READ_FILE", "args": J({"path": "a.py"})}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("near-case-mixed", "near-miss-name", "openai",
  [{"id": "call_a", "name": "List_Dir", "args": J({"path": "."})}],
  {"name": "list_dir", "args": {"path": "."}})
c("near-separator-dash", "near-miss-name", "openai",
  [{"id": "call_a", "name": "read-file", "args": J({"path": "a.py"})}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("near-separator-space", "near-miss-name", "openai",
  [{"id": "call_a", "name": "read file", "args": J({"path": "a.py"})}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("near-separator-camel", "near-miss-name", "openai",
  [{"id": "call_a", "name": "ReadFile", "args": J({"path": "a.py"})}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("near-separator-dot", "near-miss-name", "openai",
  [{"id": "call_a", "name": "list.dir", "args": J({"path": "."})}],
  {"name": "list_dir", "args": {"path": "."}})
c("near-typo-missing-char", "near-miss-name", "openai",
  [{"id": "call_a", "name": "read_fil", "args": J({"path": "a.py"})}],
  {"name": "read_file", "args": {"path": "a.py"}})
c("near-typo-extra-char", "near-miss-name", "openai",
  [{"id": "call_a", "name": "list_dirr", "args": J({"path": "."})}],
  {"name": "list_dir", "args": {"path": "."}})
c("near-typo-substitution", "near-miss-name", "openai",
  [{"id": "call_a", "name": "writ_file", "args": J({"path": "a.txt", "content": "x"})}],
  {"name": "write_file", "args": {"path": "a.txt", "content": "x"}})
c("near-miss-in-ollama", "near-miss-name", "ollama",
  {"message": {"tool_calls": [{"function": {"name": "read-file", "arguments": J({"path": "a.py"})}}]}},
  {"name": "read_file", "args": {"path": "a.py"}})
c("near-miss-in-chatend", "near-miss-name", "chat.end",
  {"type": "chat.end", "result": {"output": [{"type": "message", "tool_calls": [
      {"index": 0, "function": {"name": "List-Dir", "arguments": J({"path": "."})}}]}]}},
  {"name": "list_dir", "args": {"path": "."}})
c("near-miss-with-broken-args", "near-miss-name", "openai",
  [{"id": "call_a", "name": "read-file", "args": "```json\n" + J({"path": "a.py"}) + "\n```"}],
  {"name": "read_file", "args": {"path": "a.py"}})

# ---------- G. ambiguous / below-threshold names: never guessed ----------
c("ambiguous-equidistant", "ambiguous-name", "openai",
  [{"id": "call_a", "name": "read_fil", "args": J({"path": "a.py"})}],
  {"unrepairable": True, "ambiguous": True, "never": ["read_file", "read_filx"]},
  tools=["read_file", "read_filx"])
c("ambiguous-case-duplicates", "ambiguous-name", "openai",
  [{"id": "call_a", "name": "READ_FILE", "args": J({"path": "a.py"})}],
  {"unrepairable": True, "ambiguous": True, "never": ["Read_File", "read_file"]},
  tools=["Read_File", "read_file"])
c("ambiguous-two-narrated-calls", "ambiguous-name", "content",
  {"content": 'I will read_file({"path": "a.py"}) and then list_dir({"path": "."}).'},
  {"unrepairable": True, "ambiguous": True, "never": ["read_file", "list_dir"]})
c("below-threshold-unknown", "unknown-name", "openai",
  [{"id": "call_a", "name": "gitz", "args": J({"args": "status"})}],
  {"unrepairable": True, "never": ["git", "gits"]},
  tools=["git", "gits"])

# ---------- H. unknown names ----------
c("unknown-tool", "unknown-name", "openai",
  [{"id": "call_a", "name": "definitely_not_a_tool", "args": "{}"}],
  {"unrepairable": True, "never": ["read_file", "list_dir", "grep", "git"]})
c("unknown-phantom-shell-exec", "unknown-name", "openai",
  [{"id": "call_a", "name": "shell_exec", "args": J({"command": "ls"})}],
  {"unrepairable": True, "never": ["run_command"]})
c("unknown-python-call-in-prose", "unknown-name", "content",
  {"content": 'I would call python({"code": "print(1)"}) if such a rite existed.'},
  {"callCount": 0})

# ---------- I. schema violations: salvage must NOT fix these ----------
c("schema-missing-required", "schema-violation", "openai",
  [{"id": "call_a", "name": "read_file", "args": J({"line": 3})}],
  {"name": "read_file", "args": {"line": 3}, "validatorOk": False})
c("schema-wrong-type", "schema-violation", "openai",
  [{"id": "call_a", "name": "read_file", "args": J({"path": 42})}],
  {"name": "read_file", "args": {"path": 42}, "validatorOk": False})
c("schema-write-file-no-content", "schema-violation", "openai",
  [{"id": "call_a", "name": "write_file", "args": J({"path": "a.txt"})}],
  {"name": "write_file", "args": {"path": "a.txt"}, "validatorOk": False})
c("schema-grep-no-pattern", "schema-violation", "openai",
  [{"id": "call_a", "name": "grep", "args": J({"path": "."})}],
  {"name": "grep", "args": {"path": "."}, "validatorOk": False})
c("schema-extra-member-passes", "schema-violation", "openai",
  [{"id": "call_a", "name": "list_dir", "args": J({"path": ".", "depth": 2})}],
  {"name": "list_dir", "args": {"path": ".", "depth": 2}, "validatorOk": True})
c("schema-empty-object", "schema-violation", "openai",
  [{"id": "call_a", "name": "read_file", "args": "{}"}],
  {"name": "read_file", "args": {}, "validatorOk": False})
c("args-missing-entirely", "schema-violation", "openai",
  [{"id": "call_a", "name": "read_file"}],
  {"unrepairable": True})
c("args-non-object-string", "schema-violation", "openai",
  [{"id": "call_a", "name": "read_file", "args": '"just a string"'}],
  {"unrepairable": True})
c("args-array", "schema-violation", "openai",
  [{"id": "call_a", "name": "read_file", "args": [{"path": "a.py"}]}],
  {"unrepairable": True})
c("args-non-json-garbage", "schema-violation", "openai",
  [{"id": "call_a", "name": "read_file", "args": "{oops"}],
  {"unrepairable": True})

# ---------- J. false-repair guards: legitimate prose / code, no call shape ----------
c("prose-plain-answer", "false-repair-guard", "content",
  {"content": "I have read the file and updated the plan. No further rites are required, operator."},
  {"unchanged": True, "callCount": 0})
c("prose-mentions-tool-names", "false-repair-guard", "content",
  {"content": "The read_file and write_file rites are available to me, but prose is enough here."},
  {"unchanged": True, "callCount": 0})
c("prose-json-schema-blob", "false-repair-guard", "content",
  {"content": 'Here is the shape the harness expects: {"path": "string", "pattern": "string"}.'},
  {"unchanged": True, "callCount": 0})
c("prose-code-block-helper", "false-repair-guard", "content",
  {"content": "Use a helper:\n```js\nfunction readFile(path) { return fs.read(path); }\n```\nThat is all."},
  {"unchanged": True, "callCount": 0})
c("prose-hypothetical-call", "false-repair-guard", "content",
  {"content": 'If I were to call read_file, I would pass {"path": "a.py"} to it.'},
  {"unchanged": True, "callCount": 0})
c("prose-unknown-tool-call-span", "false-repair-guard", "content",
  {"content": 'I would invoke shell_exec({"command":"ls"}) but that rite does not exist.'},
  {"unchanged": True, "callCount": 0})
c("prose-empty-content", "false-repair-guard", "content",
  {"content": ""},
  {"unchanged": True, "callCount": 0})
c("prose-array-blob", "false-repair-guard", "content",
  {"content": 'The candidate list is ["read_file", "grep"] and nothing else matters.'},
  {"unchanged": True, "callCount": 0})
c("prose-markdown-table", "false-repair-guard", "content",
  {"content": "| rite | purpose |\n|---|---|\n| read_file | inspect |\n| grep | search |\nNo call is needed."},
  {"unchanged": True, "callCount": 0})
c("prose-fenced-config-not-a-call", "false-repair-guard", "content",
  {"content": 'The config block looks like:\n```json\n{"temperature": 0.2, "top_p": 0.9}\n```\nNothing to invoke.'},
  {"unchanged": True, "callCount": 0})
c("prose-json-with-name-no-args", "false-repair-guard", "content",
  {"content": 'The call object has the form {"name": "<tool>", "arguments": {...}}.'},
  {"unchanged": True, "callCount": 0})

# ---------- K. narration recovery ----------
c("narration-call-span", "narration-recovery", "content",
  {"content": 'Let me inspect it: read_file({"path": "src/main.py"})'},
  {"name": "read_file", "args": {"path": "src/main.py"}})
c("narration-call-span-near-miss", "narration-recovery", "content",
  {"content": 'Invoking list-dir({"path": "."}) now.'},
  {"name": "list_dir", "args": {"path": "."}})
c("narration-call-span-trailing-prose", "narration-recovery", "content",
  {"content": 'Done: grep({"path":".","pattern":"TODO"}) — that is all for now.'},
  {"name": "grep", "args": {"path": ".", "pattern": "TODO"}})
c("narration-fenced-json-object", "narration-recovery", "content",
  {"content": "Sure, invoking:\n```json\n" + J({"name": "read_file", "arguments": {"path": "a.py"}}) + "\n```"},
  {"name": "read_file", "args": {"path": "a.py"}})
c("narration-fenced-json-no-lang", "narration-recovery", "content",
  {"content": "```\n" + J({"name": "list_dir", "arguments": {"path": "."}}) + "\n```"},
  {"name": "list_dir", "args": {"path": "."}})
c("narration-bare-json-object", "narration-recovery", "content",
  {"content": J({"name": "list_dir", "arguments": {"path": "."}})},
  {"name": "list_dir", "args": {"path": "."}})
c("narration-fenced-json-args-as-string", "narration-recovery", "content",
  {"content": "```json\n" + J({"name": "grep", "arguments": J({"path": ".", "pattern": "TODO"})}) + "\n```"},
  {"name": "grep", "args": {"path": ".", "pattern": "TODO"}})
c("narration-fenced-with-empty-tool-calls", "narration-recovery", "content",
  {"content": "```json\n" + J({"name": "git", "arguments": {"args": "log --oneline"}}) + "\n```",
   "tool_calls": []},
  {"name": "git", "args": {"args": "log --oneline"}})
c("narration-openai-style-function", "narration-recovery", "content",
  {"content": "```json\n" + J({"type": "function", "function": {"name": "list_dir", "arguments": {"path": "src"}}}) + "\n```"},
  {"name": "list_dir", "args": {"path": "src"}})
c("narration-tool-and-args-keys", "narration-recovery", "content",
  {"content": "```json\n" + J({"tool": "read_file", "args": {"path": "b.py"}}) + "\n```"},
  {"name": "read_file", "args": {"path": "b.py"}})

# ---------- L. mixed turns ----------
c("mixed-good-and-unknown", "mixed-turn", "openai",
  [{"id": "call_a", "name": "read_file", "args": J({"path": "a.py"})},
   {"id": "call_b", "name": "evil_tool", "args": "{}"}],
  {"name": "read_file", "args": {"path": "a.py"}, "unrepairable": True, "callCount": 1})
c("mixed-good-and-broken-args", "mixed-turn", "openai",
  [{"id": "call_a", "name": "read_file", "args": "{oops"},
   {"id": "call_b", "name": "list_dir", "args": J({"path": "."})}],
  {"name": "list_dir", "args": {"path": "."}, "unrepairable": True, "callCount": 1})
c("mixed-repaired-name-and-broken-args", "mixed-turn", "ollama",
  {"message": {"tool_calls": [
      {"id": "call_a", "function": {"name": "read-file", "arguments": '{"path":"a.py",}'}},
      {"id": "call_b", "function": {"name": "grep", "arguments": "{nope"}}]}},
  {"name": "read_file", "args": {"path": "a.py"}, "unrepairable": True, "callCount": 1})

# ---------- M. garbage input: never throws ----------
c("garbage-null", "garbage", "content", None, {"unchanged": True, "callCount": 0})
c("garbage-number", "garbage", "content", 42, {"unchanged": True, "callCount": 0})
c("garbage-plain-string", "garbage", "content", "not a call at all",
  {"unchanged": True, "callCount": 0})
c("garbage-tool-calls-not-array", "garbage", "content", {"tool_calls": "nope"},
  {"unchanged": True, "callCount": 0})
c("garbage-empty-array", "garbage", "openai", [], {"unchanged": True, "callCount": 0})

corpus = {
    "version": 1,
    "note": (
        "Golden corpus for the Phase 11 deterministic salvage pass. Every `reply` is a "
        "realistic model output in one of the three wire formats this harness actually speaks, "
        "or a legitimate prose answer. See README.md for the schema and for the two rules a new "
        "case must follow (fold guard replies must not contain an allowed-tool name({...}) span; "
        "`unchanged: true` means salvage must change nothing at all)."
    ),
    "tools": TOOLS,
    "cases": cases,
}

os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(corpus, fh, indent=2, ensure_ascii=False)
    fh.write("\n")

cats = {}
for case in cases:
    cats[case["category"]] = cats.get(case["category"], 0) + 1
print("wrote", os.path.abspath(OUT))
print("cases:", len(cases))
for k in sorted(cats):
    print("  %-22s %d" % (k, cats[k]))
