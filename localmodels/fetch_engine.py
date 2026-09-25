#!/usr/bin/env python3
"""
LOCAL CORTEX - Needle engine fetch helper (used by setup.sh / setup.ps1).

The base weights (`needle3.cact`) download from the Cactus-Compute/needle3 repo with one
`hf_hub_download` call, but the ENGINE library (libneedle.so / .dylib / .dll) ships inside
a platform wheel under `python/` in the same repo - and the engine version the installed
`cactus-needle` package expects (e.g. 3.0.2) is not always published as a wheel (3.0.1 was
the highest for a while). This helper therefore:

  1. tries the expected wheel for the engine version + this platform tag
     (e.g. cactus_needle-3.0.2-py3-none-musllinux_1_2_aarch64.whl); and
  2. on 404 lists the repo's `python/` directory and picks the HIGHEST version whose
     wheel matches the runtime platform tag, then extracts `needle/libneedle3.so`
     (the member name carries the generation) into
     `<cache_dir(generation)>/<libneedle.so|dylib|dll>`.

Run it with the VENV python (the one that has cactus-needle installed):

    localmodels/.venv/bin/python localmodels/fetch_engine.py

Idempotent: an existing library is left alone. Exit codes: 0 ok/already cached,
1 weights missing, 2 no usable wheel found. Never hardcodes a path or a platform tag -
both come from the installed needle.agent.fetch.
"""
import os
import sys
import zipfile


def main():
    try:
        from needle.agent import fetch
    except ImportError:
        print('[fetch_engine] the `needle` package is not importable from this python -')
        print('[fetch_engine] run me with localmodels/.venv/bin/python (see setup.sh).')
        return 1

    generation = int(os.environ.get('NEEDLE_GENERATION', '3'))
    out_dir = fetch.cache_dir(generation)
    version = fetch.engine_version(generation)
    tag = fetch._platform_tag()
    lib_name = fetch._lib_name()
    target = os.path.join(out_dir, lib_name)

    if os.path.isfile(target):
        print('[fetch_engine] engine already cached: %s' % target)
        return 0

    # The loader needs the base weights next to the engine; fetch them first (~35 MB).
    weights = os.path.join(out_dir, fetch.base_weights(generation))
    if not os.path.isfile(weights):
        print('[fetch_engine] downloading base weights (%s, generation %d)...'
              % (fetch.base_weights(generation), generation))
        weights = fetch.fetch_weights(generation)
    print('[fetch_engine] base weights: %s' % weights)

    def extract(wheel_path):
        stem, suffix = os.path.splitext(lib_name)
        member = '%s%d%s' % (stem, generation, suffix) if generation >= 3 else lib_name
        with zipfile.ZipFile(wheel_path) as archive:
            data = archive.read('needle/' + member)
        os.makedirs(out_dir, exist_ok=True)
        with open(target, 'wb') as handle:
            handle.write(data)
        print('[fetch_engine] engine library: %s' % target)
        return 0

    try:
        from huggingface_hub import hf_hub_download, list_repo_files
        from huggingface_hub.errors import EntryNotFoundError, LocalEntryNotFoundError
    except ImportError:
        print('[fetch_engine] huggingface_hub unavailable inside this python.')
        return 1

    repo = fetch.engine_repo(generation)
    wheel = 'python/cactus_needle-%s-py3-none-%s.whl' % (version, tag)
    print('[fetch_engine] trying %s @ %s ...' % (wheel, repo))
    try:
        path = hf_hub_download(repo_id=repo, filename=wheel, repo_type='model')
        return extract(path)
    except (EntryNotFoundError, LocalEntryNotFoundError):
        pass
    except Exception as e:
        print('[fetch_engine] direct wheel fetch failed (%s); falling back to the version scan' % e)

    # Fallback: list python/ and pick the HIGHEST version with a wheel for THIS platform tag.
    prefix = 'cactus_needle-'
    suffix = '-py3-none-%s.whl' % tag
    versions = []
    for f in list_repo_files(repo, repo_type='model'):
        if f.startswith('python/' + prefix) and f.endswith(suffix):
            v = f[len('python/' + prefix):-len(suffix)]
            try:
                versions.append((tuple(int(x) for x in v.split('.')), f))
            except ValueError:
                continue
    if not versions:
        print('[fetch_engine] no engine wheel published for platform tag %r in %s' % (tag, repo))
        return 2
    versions.sort()
    chosen = versions[-1]
    print('[fetch_engine] expected wheel unpublished; using %s (highest for this platform)' % chosen[1])
    try:
        path = hf_hub_download(repo_id=repo, filename=chosen[1], repo_type='model')
        return extract(path)
    except Exception as e:
        print('[fetch_engine] fallback wheel fetch failed: %s: %s' % (type(e).__name__, e))
        return 2


if __name__ == '__main__':
    sys.exit(main())
