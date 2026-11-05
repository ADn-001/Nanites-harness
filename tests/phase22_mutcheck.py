#!/usr/bin/env python3
"""Mutation check: does the phase-22 byte-cap assertion actually bite?

Builds a THROWAWAY copy of the two servers in a temp tree (the real repo is never
touched), replaces the bounded read with an unbounded one that ignores the client's
declared Content-Length, and re-runs the phase-22 suite against it. A suite that
still passes has a decorative assertion.
"""
import os
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOUNDED = "raw = self.rfile.read(min(n, MAX_JSON_BYTES + 1))"
UNBOUNDED = "raw = self.rfile.read(MAX_JSON_BYTES + 1)  # MUTANT: ignores the declared length"

T = tempfile.mkdtemp(prefix='p22-mut-')
os.makedirs(os.path.join(T, 'tests'))
for f in ('bridge.py', 'bridge_daemon.py'):
    shutil.copy2(os.path.join(SRC, f), os.path.join(T, f))
shutil.copy2(os.path.join(SRC, 'tests', 'phase22_bridge_body_read.py'),
             os.path.join(T, 'tests', 'phase22_bridge_body_read.py'))
for f in ('bridge.py', 'bridge_daemon.py'):
    p = os.path.join(T, f)
    s = open(p, encoding='utf-8').read()
    assert BOUNDED in s, 'bounded read not found in ' + f
    open(p, 'w', encoding='utf-8').write(s.replace(BOUNDED, UNBOUNDED))

env = dict(os.environ)
env['PYTHONDONTWRITEBYTECODE'] = '1'
try:
    r = subprocess.run([sys.executable, 'tests/phase22_bridge_body_read.py'],
                       cwd=T, env=env, capture_output=True, text=True, timeout=300)
    print('--- mutant suite (read ignores the declared length) ---')
    print(r.stdout[-3000:])
    if r.stderr:
        print('STDERR:', r.stderr[-1000:])
    print('exit', r.returncode)
finally:
    shutil.rmtree(T, ignore_errors=True)
