#!/usr/bin/env python3
"""Generate catalog/known_races.tsv: a flat, read-only dedupe snapshot derived from the
canonical catalog (reports/*/meta.json). Fuzzer instances (fusil --tsan-dedup-catalog) and
ingest.py load this instead of parsing every meta.json.

Row format (tab-separated):  <race_id>\t<signature>
where <signature> is fusil tsan_dedup's sorted "file:func | file:func" site pair. A race may
carry several signatures (variant site pairs seen across runs); each becomes a row.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
OUT = ROOT / "catalog" / "known_races.tsv"


def _normalize(sig):
    """Return the signature with its two `file:func` sites sorted, matching what
    fusil tsan_dedup.parse_report emits (an *unordered* pair). A meta.json that stored
    the pair in the other order would otherwise never match a live report -- see the
    TSAN-0013/-0029 mis-ordering caught 2026-07-16. SEGV/other single-token signatures
    (no ` | `, or not exactly two parts) pass through untouched."""
    parts = sig.split(" | ")
    if len(parts) == 2:
        return " | ".join(sorted(parts))
    return sig


def main():
    rows = set()
    ids = set()
    for meta in sorted(REPORTS.glob("*/meta.json")):
        d = json.loads(meta.read_text())
        if d.get("status") == "folded":
            continue  # retired id, merged into another race that carries the signature
        rid = d["id"]
        ids.add(rid)
        for sig in d.get("signatures", []):
            sig = sig.strip()
            if sig:
                rows.add((rid, _normalize(sig)))
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w") as fh:
        fh.write("# race_id\tsignature\n")
        for rid, sig in sorted(rows):
            fh.write("%s\t%s\n" % (rid, sig))
    print(
        "wrote %s: %d signatures for %d races"
        % (OUT.relative_to(ROOT), len(rows), len(ids))
    )


if __name__ == "__main__":
    main()
