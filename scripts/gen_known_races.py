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
                rows.add((rid, sig))
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
