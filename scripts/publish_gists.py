#!/usr/bin/env python3
"""Publish each TSan finding as a PUBLIC gist and record the URL into its meta.json.

Per report it creates one gist containing every file in the report dir except meta.json
(report.md + repro*.py + tsan_report*.txt), each renamed to `<id>-<name>` so a downloaded
file is self-identifying. Description is `<id>: <title>`. On success it writes `gist_url`
into meta.json (adding the field if absent) -- it does NOT touch `status`, since TSan
findings carry meaningful statuses (confirmed/reported/...).

Idempotent: a report that already has a non-null `gist_url` is skipped unless `--force`.
Use `--dry-run` to see the plan without creating anything.

Gists are PUBLIC and indexable -- only run once the reports are reviewed and the maintainer
has approved. Requires `gh` authenticated.

  python3 scripts/publish_gists.py --dry-run
  python3 scripts/publish_gists.py TSAN-0001 TSAN-0035
  python3 scripts/publish_gists.py --ids-file /path/to/ids.txt
"""
import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def gist_files(report_dir):
    """report.md first, then repro*.py, then tsan_report*.txt -- everything but meta.json."""
    files = [p for p in sorted(report_dir.iterdir()) if p.is_file() and p.name != "meta.json"]

    def key(p):
        if p.name == "report.md":
            return (0, p.name)
        if p.name.startswith("repro"):
            return (1, p.name)
        return (2, p.name)

    return sorted(files, key=key)


def gh_gist_create(oid, files, desc, dry):
    names = ["%s-%s" % (oid, f.name) for f in files]
    if dry:
        print("    would publish:", ", ".join(names))
        print("      desc:", desc)
        return None
    with tempfile.TemporaryDirectory() as td:
        copies = []
        for f, name in zip(files, names):
            dst = pathlib.Path(td) / name
            shutil.copy(f, dst)
            copies.append(str(dst))
        out = subprocess.run(["gh", "gist", "create", "--public", "--desc", desc, *copies],
                             capture_output=True, text=True)
    if out.returncode != 0:
        print("    ERROR:", out.stderr.strip(), file=sys.stderr)
        return None
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    url = lines[-1] if lines else None
    if not url or not url.startswith("https://"):
        print("    ERROR: could not parse gist URL:", out.stdout, file=sys.stderr)
        return None
    return url


def record(meta_path, url):
    d = json.loads(meta_path.read_text())
    d["gist_url"] = url
    meta_path.write_text(json.dumps(d, indent=2) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="TSAN-#### ids (default: all)")
    ap.add_argument("--ids-file", help="file with one id per line")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ids = list(args.ids)
    if args.ids_file:
        ids += [ln.strip() for ln in open(args.ids_file) if ln.strip() and not ln.startswith("#")]
    ids = set(ids)

    published, skipped, failed = [], 0, 0
    for mp in sorted(REPORTS.glob("*/meta.json")):
        d = json.loads(mp.read_text())
        oid = d["id"]
        if ids and oid not in ids:
            continue
        if d.get("gist_url") and not args.force:
            print("%s: already gisted (%s) -- skip" % (oid, d["gist_url"]))
            skipped += 1
            continue
        files = gist_files(mp.parent)
        desc = "%s: %s" % (oid, d["title"])
        print("%s: %s" % (oid, ", ".join(f.name for f in files)))
        url = gh_gist_create(oid, files, desc, args.dry_run)
        if args.dry_run:
            continue
        if url:
            record(mp, url)
            print("    -> %s" % url)
            published.append((oid, url))
        else:
            failed += 1
    print("\npublished %d, skipped %d, failed %d" % (len(published), skipped, failed))
    for oid, url in published:
        print("  %s  %s" % (oid, url))


if __name__ == "__main__":
    main()
