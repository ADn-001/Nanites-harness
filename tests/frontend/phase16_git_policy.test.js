/*
 * phase16_git_policy.test.js — the REAL isReadRite, and the single-source git policy.
 *
 * Why this file exists
 * --------------------
 * CR-Nanites-harness-0001 shipped because the frontend half of the git policy was never
 * tested: `phase14_dispatcher.test.js:121` injects a STUB
 * (`const isReadRite = function (name) { return IS_READ(name); }`), so the suite proved the
 * *dispatcher* consults its classifier and nothing at all about the classifier itself. A
 * stub that is wrong in the same direction as the code passes forever.
 *
 * So every assertion here drives `window.isReadRite` — the real function, loaded from the
 * real index.html by helpers.launchApp. No stub.
 *
 * The negative cases are the load-bearing half. `isReadRite` returning true for
 * `log --output=/tmp/x` is what makes a jail-escaping file write AUTO-APPROVED
 * (`DEF_SETTINGS.autoApproveRead` defaults true, and `approveToolCall` returns
 * Promise.resolve(true) for any read rite), so a false negative here is a false positive on
 * security. The positive cases are asserted too: a classifier that refuses everything would
 * satisfy every negative below, and "nothing auto-runs" is not the property we want either.
 */
'use strict';
const { launchApp, teardownApp, check, summary } = require('./helpers');

(async () => {
  const app = await launchApp();
  const win = app.window;
  const isReadRite = win.isReadRite;

  check(typeof isReadRite === 'function',
        'the REAL isReadRite is reachable on window (this suite must not stub it)');

  // ---- 1. file-writing flags on a read subcommand are NOT read rites (CR-0001) ----
  // Each of these writes a file outside the jail. A classifier that says "read" here hands
  // the model an auto-approved arbitrary-file-write.
  for (const args of [
    'log --output=/tmp/pwned.txt',
    'diff --output=/tmp/pwned.txt',
    'show --output=/tmp/pwned.txt',
    'blame --output=/tmp/pwned.txt',
    'log --output-indicator-new=x',
    'log --exec-path=/tmp',
    'format-patch -1',
    'archive --output=/tmp/pwned.tar',
    'log --output /tmp/pwned.txt'
  ]) {
    check(isReadRite('git', { args }) === false,
          'isReadRite says NOT-read for a file-writing git invocation: ' + args);
  }

  // ---- 1b. `git diff --no-index` is NOT a read rite (found by the phase-16 review) ----
  //
  // `--no-index` makes diff compare two ARBITRARY paths instead of repo contents, so
  // `diff --no-index /etc/passwd /dev/null` returns the contents of a file outside the
  // project root. It was classified a read in BOTH layers and therefore auto-approved with
  // no operator prompt — the same escape as the grep symlink leak, reached via the git
  // tool. The negative case is the load-bearing half: an allow-list keyed on the
  // subcommand alone says "diff is a read", which is exactly wrong here.
  for (const args of [
    'diff --no-index /etc/passwd /dev/null',
    'diff --no-index -- /etc/passwd /dev/null',
    'diff --no-index /etc/hostname /dev/null',
    'log --no-index'
  ]) {
    check(isReadRite('git', { args }) === false,
          'isReadRite says NOT-read for a --no-index out-of-jail read: ' + args);
  }
  // ...and ordinary diffs, including the similarly-spelled --no-color, are unaffected.
  for (const args of ['diff --stat', 'diff --no-color', 'diff HEAD~1 HEAD --stat']) {
    check(isReadRite('git', { args }) === true,
          'isReadRite still says read for an ordinary diff: ' + args);
  }
  // A QUOTED flag must classify the same as an unquoted one. The tokenizer keeps the
  // quotes, so `"--output=/tmp/x"` matches neither the exact nor the prefix test and was
  // reported a READ while the bridge (shlex.split) refused it. The bridge is the backstop
  // so there is no write today, but the two layers must AGREE on classification, not
  // merely on the final outcome — otherwise the operator is shown a read that is refused.
  check(isReadRite('git', { args: 'log "--output=/tmp/x"' }) === false,
        'a QUOTED output flag is refused like an unquoted one (the two layers agree)');
  check(isReadRite('git', { args: 'diff "--no-index" /etc/passwd /dev/null' }) === false,
        'a QUOTED --no-index is refused like an unquoted one (the two layers agree)');

  // ---- 2. The inverse: ordinary reads are still reads ----
  // A guard that refuses everything passes every case in section 1. These pin the other
  // direction, so the fix cannot degenerate into "deny all git".
  for (const args of [
    'status --short',
    'log --oneline -1',
    'diff --stat',
    'show --stat HEAD',
    'branch -r',
    'tag -l',
    'stash list',
    'blame appcore.js'
  ]) {
    check(isReadRite('git', { args }) === true,
          'isReadRite still says read for an ordinary read subcommand: ' + args);
  }

  // ---- 3. mutations are not reads (pre-existing contract, kept honest) ----
  for (const args of ['add .', 'commit -m x', 'push', 'reset --hard', 'checkout -- f']) {
    check(isReadRite('git', { args }) === false,
          'isReadRite says NOT-read for a mutation: ' + args);
  }

  // ---- 4. the policy is single-source (CR-0009) ----
  //
  // The structural cause of 0001 is that FRONT_GIT_READ and the bridge's GIT_READ are two
  // hand-maintained copies that already disagreed. This asserts the frontend's rendered
  // policy is derived from the one the bridge enforces, by checking the generated block in
  // index.html carries the bridge's subcommand set — a drift between the two now fails here
  // rather than silently in production.
  // A top-level `const` in a classic script is NOT a window property, so reaching it
  // through `win` reads undefined and every subTest below would be vacuously skipped —
  // which is exactly the "guard that silently finds nothing" failure. eval() in the
  // page's own global scope is the way to actually observe it, and the positive
  // assertions here are what prove the lookup worked rather than skipped.
  const frontRead = win.eval('typeof FRONT_GIT_READ !== "undefined" ? FRONT_GIT_READ : null');
  const frontRefused = win.eval('typeof FRONT_GIT_REFUSED_WRITE !== "undefined" ? FRONT_GIT_REFUSED_WRITE : null');
  const frontFlags = win.eval('typeof FRONT_GIT_JAIL_FLAGS !== "undefined" ? FRONT_GIT_JAIL_FLAGS : null');
  check(frontRead !== null && typeof frontRead.has === 'function',
        'FRONT_GIT_READ is reachable for the single-source assertion (not vacuously skipped)');
  check(frontRefused !== null && typeof frontRefused.has === 'function',
        'FRONT_GIT_REFUSED_WRITE is reachable (not vacuously skipped)');
  check(Array.isArray(frontFlags) && frontFlags.length > 0,
        'FRONT_GIT_JAIL_FLAGS is reachable and non-empty (not vacuously skipped)');
  if (frontRead && typeof frontRead.has === 'function') {
    // Every subcommand the frontend calls a read must also be refused-safe in the bridge.
    // The bridge's set is the source; these are its load-bearing members.
    for (const sub of ['status', 'diff', 'log', 'show', 'blame', 'branch', 'tag', 'stash']) {
      check(frontRead.has(sub), 'FRONT_GIT_READ still contains the bridge read subcommand: ' + sub);
    }
    // And the two that WRITE files must never appear, in either layer.
    for (const sub of ['format-patch', 'archive']) {
      check(!frontRead.has(sub),
            'FRONT_GIT_READ excludes the file-writing subcommand: ' + sub);
      check(frontRefused && frontRefused.has(sub),
            'FRONT_GIT_REFUSED_WRITE names the file-writing subcommand: ' + sub);
    }
  }
  // The output flags the frontend honours are the bridge's, not a second list.
  if (Array.isArray(frontFlags)) {
    for (const flag of ['--output', '--output-indicator-new', '--exec-path', '--no-index']) {
      check(frontFlags.includes(flag),
            'FRONT_GIT_JAIL_FLAGS carries the bridge flag: ' + flag);
    }
  }

  // ---- 6. the two layers AGREE, subcommand by subcommand (CR-0009, the real point) ----
  //
  // A boolean classifier hides *why* it said no. These assertions walk the policy as data
  // and require both directions, which is what makes the second guard observable: a
  // subcommand that is absent from FRONT_GIT_READ is refused regardless of
  // FRONT_GIT_REFUSED_WRITE, so a test asserting only "isReadRite says false" passes
  // whether or not the refusal table exists. Asserting the table's own contents — and that
  // it is non-empty — is what pins the layer rather than its shadow.
  const policy = win.eval('typeof frontGitPolicy === "function" ? frontGitPolicy() : null');
  check(policy !== null && Array.isArray(policy.read) && Array.isArray(policy.refusedWrite),
        'frontGitPolicy() exposes the policy as data, not just a boolean verdict');
  if (policy) {
    check(policy.refusedWrite.length > 0,
          'FRONT_GIT_REFUSED_WRITE is non-empty (not vacuously satisfied)');
    // Every refused-write subcommand must actually read as NOT a read rite...
    for (const sub of policy.refusedWrite) {
      check(isReadRite('git', { args: sub }) === false,
            'a refused-write subcommand is not a read rite: ' + sub);
    }
    // ...and no read subcommand may be one of them (the sets must not overlap).
    for (const sub of policy.read) {
      check(!policy.refusedWrite.includes(sub),
            'FRONT_GIT_READ and FRONT_GIT_REFUSED_WRITE do not overlap on: ' + sub);
    }
    // Each output flag, on each read subcommand, is refused. Looped over the family with
    // the subTest-style message so a subcommand that was never wired fails as itself.
    for (const sub of policy.read) {
      for (const flag of policy.outputFlags) {
        check(isReadRite('git', { args: sub + ' ' + flag + '=/tmp/x' }) === false,
              'output flag refused on read subcommand: ' + sub + ' ' + flag);
      }
    }
  }

  // ---- 5. non-git tools are unaffected ----
  check(isReadRite('read_file', { path: 'x' }) === true, 'read_file is still a read rite');
  check(isReadRite('grep', { pattern: 'x' }) === true, 'grep is still a read rite');
  check(isReadRite('write_file', { path: 'x' }) === false, 'write_file is still not a read rite');
  check(isReadRite('run_command', {}) === false, 'run_command is still not a read rite');

  teardownApp(app);
  process.exit(summary('PHASE 16 GIT POLICY'));
})();
