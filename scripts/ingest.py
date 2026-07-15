#!/usr/bin/env python3
"""Batch-dedupe a pile of fuzzer --tsan crash dirs against catalog/known_races.tsv, and surface
ONLY genuinely-new race signatures. Reuses fusil's tsan_dedup parser (the ONE source of truth for
the signature) loaded BY FILE PATH -- so no fusil package import (and no python-ptrace) is needed,
and the snapshot can never drift from the in-loop deduper.

Usage:
  [FUSIL_TSAN_DEDUP=/path/to/fusil/fusil/python/tsan_dedup.py] ingest.py [globs...]
  default glob: ~/crashers/tsan-*/*   (dirs containing a `stdout` with a TSan report)
"""
import collections
import glob
import importlib.util
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_tsan_dedup():
    candidates = [
        os.environ.get("FUSIL_TSAN_DEDUP"),
        str(ROOT.parent / "fusil" / "fusil" / "python" / "tsan_dedup.py"),
        os.path.expanduser("~/projects/fusil/fusil/python/tsan_dedup.py"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            spec = importlib.util.spec_from_file_location("tsan_dedup", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.exit(
        "could not find fusil's tsan_dedup.py; set FUSIL_TSAN_DEDUP=/path/to/tsan_dedup.py"
    )


def main():
    td = _load_tsan_dedup()
    globs = [a for a in sys.argv[1:] if not a.startswith("-")] or [
        os.path.expanduser("~/crashers/tsan-*/*")
    ]
    snap_path = ROOT / "catalog" / "known_races.tsv"
    snap = td.load_catalog_file(snap_path) if snap_path.exists() else {}

    dirs = []
    for g in globs:
        for p in glob.glob(g):
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "stdout")):
                dirs.append(p)
    dirs = sorted(set(dirs))

    known = collections.Counter()
    new = collections.defaultdict(list)
    other = collections.Counter()
    for d in dirs:
        try:
            text = td.read_crash_stdout(os.path.join(d, "stdout"))
        except OSError:
            other["unreadable"] += 1
            continue
        report = td.parse_report(text)
        if report is None:
            other["noparse"] += 1
            continue
        if report["framework"]:
            other["framework"] += 1
            continue
        rid = snap.get(report["signature"])
        if rid:
            known[rid] += 1
        else:
            new[report["signature"]].append(os.path.basename(d))

    print("# ingested %d crash dirs vs %s\n" % (len(dirs), snap_path.name))
    print("## NEW race signatures (need a report) " + "-" * 24)
    if not new:
        print("  (none -- every parsed race matched the catalog)")
    for sig, dd in sorted(new.items(), key=lambda kv: -len(kv[1])):
        print("  [%3d] %s\n        e.g. %s" % (len(dd), sig, ", ".join(dd[:3])))
    print("\n## known races (dedupe tally) " + "-" * 24)
    for rid, n in known.most_common():
        print("  %s: %d" % (rid, n))
    print(
        "\n## other: %s  |  new-signature groups: %d" % (dict(other), len(new))
    )


if __name__ == "__main__":
    main()
