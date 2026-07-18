#!/usr/bin/env python3
"""Batch-dedupe a pile of fuzzer --tsan crash dirs against catalog/known_races.tsv, and surface
ONLY genuinely-new race signatures. Reuses fusil's tsan_dedup parser (the ONE source of truth for
the signature) loaded BY FILE PATH -- so no fusil package import (and no python-ptrace) is needed,
and the snapshot can never drift from the in-loop deduper.

Usage:
  [FUSIL_TSAN_DEDUP=/path/to/fusil/fusil/python/tsan_dedup.py] ingest.py [globs...]
  default glob: ~/crashers/tsan-*/*   (dirs containing a `stdout` with a TSan report)

Extension source roots (fusil Slice D): to dedupe an out-of-tree C extension's OWN races (rather
than dropping them to noparse), pass its source-tree root(s) so a frame under that root is keyed
by its path relative to it. Either repeat ``--source-root DIR`` (``-r DIR``) or set
``FUSIL_TSAN_SOURCE_ROOTS`` to a colon-separated list. With no roots the CPython-only signatures
are byte-for-byte unchanged (and the parser is called the old one-arg way, so this stays
compatible with a fusil checkout that predates Slice D).
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


def _parse_source_roots(argv):
    """Collect --source-root/-r DIR args + FUSIL_TSAN_SOURCE_ROOTS (colon-sep). Returns the
    remaining non-flag args (globs) and the roots list."""
    roots = []
    env = os.environ.get("FUSIL_TSAN_SOURCE_ROOTS")
    if env:
        roots.extend(p for p in env.split(":") if p)
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-r", "--source-root"):
            if i + 1 < len(argv):
                roots.append(argv[i + 1])
                i += 2
                continue
        elif a.startswith("--source-root="):
            roots.append(a.split("=", 1)[1])
        elif not a.startswith("-"):
            rest.append(a)
        i += 1
    return rest, roots


def main():
    td = _load_tsan_dedup()
    globs, source_roots = _parse_source_roots(sys.argv[1:])
    globs = globs or [os.path.expanduser("~/crashers/tsan-*/*")]
    snap_path = ROOT / "catalog" / "known_races.tsv"
    snap = td.load_catalog_file(snap_path) if snap_path.exists() else {}
    supp = td.Suppressor.from_file(str(ROOT / "catalog" / "suppressions.txt"))

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
        # Pass source_roots ONLY when set, so with no roots we call the old one-arg parse_report
        # -- keeping this compatible with a fusil checkout that predates Slice D.
        report = (
            td.parse_report(text, source_roots=source_roots)
            if source_roots
            else td.parse_report(text)
        )
        if report is None:
            other["noparse"] += 1
            continue
        if supp.suppresses(report):
            other["suppressed"] += 1
            continue
        if report["framework"]:
            other["framework"] += 1
            continue
        rid = snap.get(report["signature"])
        if rid:
            known[rid] += 1
        else:
            new[report["signature"]].append(os.path.basename(d))

    roots_note = (" | source-roots: %s" % ", ".join(source_roots)) if source_roots else ""
    print("# ingested %d crash dirs vs %s%s\n" % (len(dirs), snap_path.name, roots_note))
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
