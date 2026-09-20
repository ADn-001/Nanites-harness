/*
 * run.js — run every *.test.js file in this directory and aggregate failures.
 * Usage: node tests/frontend/run.js [file...]   (defaults to all *.test.js)
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const dir = __dirname;
const requested = process.argv.slice(2);
let files = requested.map((f) =>
  f.endsWith('.js') ? f : path.join(dir, f)
);
if (!requested.length) {
  files = fs.readdirSync(dir).filter((f) => f.endsWith('.test.js')).sort()
    .map((f) => path.join(dir, f));
}
if (!files.length) {
  console.error('No test files found in ' + dir);
  process.exit(2);
}

let totalFails = 0;
for (const f of files) {
  console.log('\n=== ' + path.basename(f) + ' ===');
  let code;
  try {
    execFileSync('node', [f], { stdio: 'inherit', cwd: dir });
    code = 0;
  } catch (e) {
    code = e.status == null ? 1 : e.status;
  }
  totalFails += code;
}
console.log('\n==================================');
console.log('FRONTEND SUITE: ' + (totalFails ? totalFails + ' FAILING FILE(S)' : 'ALL GREEN'));
process.exit(totalFails ? 1 : 0);