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

Multi-race per session (fusil #221/#222): under ``--tsan-no-halt`` (TSan halt_on_error=0) a single
session's stdout carries MANY races, and a kept multi-race dir gets a ``tsan_races.tsv`` sidecar.
This tallies EVERY distinct race in a dir (via ``parse_all_reports``), not just the first, so a new
race hiding behind a known first race is still surfaced -- and reports which dirs held multiple
races (and which new signatures were reachable ONLY past the first race). A dir with a single
report (the default halt_on_error=1 fleet -- no sidecar) reduces to exactly one race, so its
known/new/other tally is byte-for-byte unchanged; the sidecar is read only as a fallback when a
dir's ``stdout`` is unreadable. If ``FUSIL_TSAN_DEDUP`` points at a pre-#221 fusil checkout without
``parse_all_reports``, this transparently falls back to the single-race ``parse_report``.
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


def read_sidecar(path):
    """Parse a fusil ``tsan_races.tsv`` sidecar into a list of race dicts (or [] if absent /
    unreadable / malformed). Columns are ``order  label  kind  after_fault  signature`` with the
    signature LAST (tab-free), so a naive tab-split recovers every column; ``#`` lines are skipped.
    Used only as a fallback when a dir's ``stdout`` is gone -- ``parse_all_reports`` on the stdout
    is the primary, richer source (it yields full sites for per-site suppression)."""
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                out.append(
                    {
                        "order": parts[0],
                        "label": parts[1],
                        "kind": parts[2],
                        "after_fault": parts[3] == "1",
                        "signature": "\t".join(
                            parts[4:]
                        ),  # signatures are tab-free; be defensive
                    }
                )
    except OSError:
        return []
    return out


def dir_reports(td, text, source_roots):
    """Every DISTINCT TSan report in one dir's stdout, as full report dicts for exact
    classification. ``parse_all_reports`` (halt_on_error=0 may hold many) when the loaded
    tsan_dedup provides it, else the single-race ``parse_report`` (pre-#221 fusil). If the
    multi-race parse finds nothing, fall back to ``parse_report`` too, so a lone report whose
    header the splitter treats differently is still caught -- keeping a single-race
    (halt_on_error=1) fleet byte-for-byte identical to the old first-race-only ingest.

    ``source_roots`` is passed only when set, so with no roots the parser is called the old
    one-arg way (compatible with a fusil checkout that predates Slice D)."""

    def _parse_report(t):
        return (
            td.parse_report(t, source_roots=source_roots)
            if source_roots
            else td.parse_report(t)
        )

    if hasattr(td, "parse_all_reports"):
        reports = (
            td.parse_all_reports(text, source_roots=source_roots)
            if source_roots
            else td.parse_all_reports(text)
        )
        if reports:
            return reports
    r = _parse_report(text)
    return [r] if r else []


def _sidecar_reports(side):
    """Minimal report dicts from sidecar rows (no sites -> signature-regex suppression only;
    framework recovered from the fuzz-time ``tsanFRAME`` label, which is catalog-independent)."""
    return [
        {
            "signature": r["signature"],
            "sites": [],
            "kind": r["kind"],
            "framework": r["label"] == "tsanFRAME",
        }
        for r in side
    ]


def _classify(reports, snap, supp, known, new, other, dirname):
    """Classify each race in a dir's report list into known/new/other (per RACE, not per dir),
    returning the ordered per-race summary [(bucket, signature, after_fault)] for the multi-race
    section. ``bucket`` is a catalog id, ``NEW``, ``framework``, or ``suppressed``."""
    fault_seen = False
    summary = []
    for report in reports:
        after = fault_seen
        if report.get("kind") == "segv":
            fault_seen = (
                True  # a race reported AFTER a fault may be a corruption artifact
            )
        if supp.suppresses(report):
            other["suppressed"] += 1
            summary.append(("suppressed", report["signature"], after))
        elif report.get("framework"):
            other["framework"] += 1
            summary.append(("framework", report["signature"], after))
        else:
            rid = snap.get(report["signature"])
            if rid:
                known[rid] += 1
                summary.append((rid, report["signature"], after))
            else:
                new[report["signature"]].append(dirname)
                summary.append(("NEW", report["signature"], after))
    return summary


def _headline(summary):
    """The multi-race dir's headline for display: first NEW race, else first known id, else the
    first race's bucket (mirrors the fuzzer's dir-name headline)."""
    for bucket, sig, _ in summary:
        if bucket == "NEW":
            return "NEW " + sig
    for bucket, sig, _ in summary:
        if bucket not in ("framework", "suppressed"):
            return bucket  # a catalog id
    return summary[0][0] if summary else "?"


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
    multi = []  # (dirname, summary) for dirs with >=2 distinct races
    seen_at_first = (
        set()
    )  # signatures ever seen as a dir's FIRST race (visible to old ingest)
    for d in dirs:
        dirname = os.path.basename(d)
        try:
            text = td.read_crash_stdout(os.path.join(d, "stdout"))
        except OSError:
            # stdout gone: fall back to the sidecar's recorded race set if one was written.
            reports = _sidecar_reports(read_sidecar(os.path.join(d, "tsan_races.tsv")))
            if not reports:
                other["unreadable"] += 1
                continue
        else:
            reports = dir_reports(td, text, source_roots)
        if not reports:
            other["noparse"] += 1
            continue
        summary = _classify(reports, snap, supp, known, new, other, dirname)
        if summary:
            seen_at_first.add(
                summary[0][1]
            )  # summary[0] = (bucket, signature, after_fault)
        if len(reports) >= 2:
            multi.append((dirname, summary))

    roots_note = (
        (" | source-roots: %s" % ", ".join(source_roots)) if source_roots else ""
    )
    print(
        "# ingested %d crash dirs vs %s%s\n" % (len(dirs), snap_path.name, roots_note)
    )
    print("## NEW race signatures (need a report) " + "-" * 24)
    if not new:
        print("  (none -- every parsed race matched the catalog)")
    for sig, dd in sorted(new.items(), key=lambda kv: -len(kv[1])):
        # A new race NEVER seen as any dir's first report was invisible to the old first-race-only
        # ingest -- flag it, since it is exactly what multi-race capture buys us.
        buried = " [only via multi-race]" if sig not in seen_at_first else ""
        print(
            "  [%3d] %s%s\n        e.g. %s" % (len(dd), sig, buried, ", ".join(dd[:3]))
        )
    print("\n## known races (dedupe tally) " + "-" * 24)
    for rid, n in known.most_common():
        print("  %s: %d" % (rid, n))
    print("\n## other: %s  |  new-signature groups: %d" % (dict(other), len(new)))
    if multi:
        # Only appears for --tsan-no-halt fleets; a single-race (halt_on_error=1) fleet leaves this
        # empty, so its output is byte-for-byte the old ingest's.
        print("\n## multi-race dirs (--tsan-no-halt: >=2 races/session) " + "-" * 12)
        print("  %d dir(s) held multiple distinct races" % len(multi))
        for dirname, summary in multi:
            print(
                "  %s  [%d races] headline=%s"
                % (dirname, len(summary), _headline(summary))
            )
            for bucket, sig, after in summary:
                flag = "  (after-fault: possible artifact)" if after else ""
                print("      - %-10s %s%s" % (bucket, sig, flag))


if __name__ == "__main__":
    main()
