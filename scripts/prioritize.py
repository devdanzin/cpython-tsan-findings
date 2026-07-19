#!/usr/bin/env python3
"""Rank a fleet's TSan findings by "rare + crash-shaped" so the next memory-unsafe bug gets the
"replay without a sanitizer -> does it segfault?" check first. That single move turned TSAN-0045
(the types.GenericAlias iterator double-DECREF) from a TSan data-race warning into a filed,
reproducible crash (cpython#154043); this automates the triage step that found it.

For each distinct race signature in the fleet it computes:
  - vehicles: how many crash dirs show it (rare = more interesting for a targeted hunt);
  - a crash-shape score from the raw TSan report:
      CRASH   (3) the report itself is a SEGV / heap-use-after-free / deadlock (kind == segv);
      UAF-RISK(2) a data race whose frames touch refcount/lifetime machinery (dealloc, finalize,
                  Py_SETREF, _Py_Dealloc, tp_clear, free, it_seq, long_cnt, ...): a plausible UAF
                  that may crash without a sanitizer;
      RACE    (1) a plain data race (usually value-benign per the iterator strategy gh-124397).
  - known: the catalog id (via known_races.tsv) if any.

Output ranks candidates by (score desc, vehicles asc) so rare + crash-shaped rises to the top, and
prints a representative vehicle dir + a ready-to-run replay command for each. By default it shows
only NEW (uncatalogued) signatures; pass --all to include known ones (e.g. to re-verify whether a
value-benign-labelled race is actually crashing).

Reuses fusil's tsan_dedup parser by file path (same contract as ingest.py); no fusil package import.

Usage:
  [FUSIL_TSAN_DEDUP=…/tsan_dedup.py] prioritize.py [globs...] [--all] [--source-root DIR]
  default glob: ~/crashers/tsan-*/*
  PLAIN_FT_PYTHON=… overrides the plain (non-sanitizer) free-threaded interpreter in replay hints.
"""
import glob
import importlib.util
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Frames that mark a data race as a plausible use-after-free / refcount-lifetime hazard (i.e. it may
# crash on a plain build, not just trip TSan). Matched against the raw report block.
_DANGER = re.compile(
    r"\b(_Py_Dealloc|subtype_dealloc|tp_dealloc|[a-z_]+_dealloc|_Py_MergeZeroLocalRefcount"
    r"|PyObject_CallFinalizer|[a-z_]+_finalize|Py_SETREF|Py_CLEAR|Py_DECREF|_PyMem_Free"
    r"|PyMem_Free|tp_clear|[a-z_]+_clear|it_seq|long_cnt|BaseException_clear|freefunc)\b"
)
# An iterator ADVANCE function. A SELF-race on one of these (both racing sites the same next()) is
# the double-DECREF-prone shape -- a shared one-shot/exhausting iterator can free its state twice
# (the bytes/str it_seq and the GenericAlias gi->obj / TSAN-0045 case). The danger lives INSIDE the
# function (a Py_SETREF / Py_DECREF the TSan frame names don't reveal), so score it from the
# signature shape, not the frames. The value-benign cursor faces (len | next) are left as RACE.
_ITER_NEXT = re.compile(r"(?:iternext|iter_next|_next(?:_lock_held)?)$")


def _iter_self_race(sig):
    parts = sig.split(" | ")
    if len(parts) == 2 and parts[0] == parts[1]:
        func = parts[0].rsplit(":", 1)[-1]
        return bool(_ITER_NEXT.search(func))
    return False
_PLAIN_FT = os.environ.get(
    "PLAIN_FT_PYTHON",
    os.path.expanduser("~/projects/python_build_matrix/builds/debug-ft-nojit/python"),
)
# One TSan report block: from a WARNING:/==PID==ERROR: header up to (but not including) the next one.
_REPORT_START = re.compile(r"^(?:={2,}\d+={2,})?\s*(?:WARNING|ERROR): ThreadSanitizer: ", re.M)


def _load_tsan_dedup():
    for c in (
        os.environ.get("FUSIL_TSAN_DEDUP"),
        str(ROOT.parent / "fusil" / "fusil" / "python" / "tsan_dedup.py"),
        os.path.expanduser("~/projects/fusil/fusil/python/tsan_dedup.py"),
    ):
        if c and os.path.exists(c):
            spec = importlib.util.spec_from_file_location("tsan_dedup", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.exit("could not find fusil's tsan_dedup.py; set FUSIL_TSAN_DEDUP=…")


def _read_full_or_bounded(td, path):
    """Full stdout if it fits, else the bounded head+tail read. Re-parsing the FULL file catches
    races the bounded read would elide in the middle of a very long stdout (a real risk for a
    runaway-vehicle session); small stdouts are read whole either way."""
    size = os.path.getsize(path)
    if size <= 4 * 1024 * 1024:
        with open(path, errors="replace") as fh:
            return fh.read()
    return td.read_crash_stdout(path)


def _blocks(text):
    starts = [m.start() for m in _REPORT_START.finditer(text)]
    for i, s in enumerate(starts):
        yield text[s : starts[i + 1] if i + 1 < len(starts) else len(text)]


def _parse_roots(argv):
    roots, rest, show_all = [], [], False
    env = os.environ.get("FUSIL_TSAN_SOURCE_ROOTS")
    if env:
        roots += [p for p in env.split(":") if p]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--all":
            show_all = True
        elif a in ("-r", "--source-root") and i + 1 < len(argv):
            roots.append(argv[i + 1])
            i += 1
        elif a.startswith("--source-root="):
            roots.append(a.split("=", 1)[1])
        elif not a.startswith("-"):
            rest.append(a)
        i += 1
    return rest, roots, show_all


def main():
    td = _load_tsan_dedup()
    globs, roots, show_all = _parse_roots(sys.argv[1:])
    globs = globs or [os.path.expanduser("~/crashers/tsan-*/*")]
    snap_path = ROOT / "catalog" / "known_races.tsv"
    snap = td.load_catalog_file(snap_path) if snap_path.exists() else {}
    supp = td.Suppressor.from_file(str(ROOT / "catalog" / "suppressions.txt"))

    dirs = sorted(
        {
            p
            for g in globs
            for p in glob.glob(g)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "stdout"))
        }
    )

    info = {}  # signature -> dict(vehicles, kind, score, sample_dir, sample_block)
    for d in dirs:
        try:
            text = _read_full_or_bounded(td, os.path.join(d, "stdout"))
        except OSError:
            continue
        for block in _blocks(text):
            rep = td.parse_report(block, roots) if roots else td.parse_report(block)
            if not rep or not rep.get("signature") or supp.suppresses(rep) or rep.get("framework"):
                continue
            sig = rep["signature"]
            e = info.setdefault(sig, {"vehicles": 0, "kind": rep["kind"], "score": 1, "sample": d})
            e["vehicles"] += 1
            if rep["kind"] == "segv":
                e["score"] = max(e["score"], 3)
            elif _DANGER.search(block) or _iter_self_race(sig):
                e["score"] = max(e["score"], 2)

    rows = []
    for sig, e in info.items():
        known = snap.get(sig)
        if known and not show_all:
            continue
        rows.append((e["score"], -e["vehicles"], sig, e, known))
    # rank: crash-shape desc, then rarest first
    rows.sort(key=lambda r: (-r[0], r[1]))

    label = {3: "CRASH   ", 2: "UAF-RISK", 1: "RACE    "}
    print("# prioritized TSan findings (%d dirs, %d distinct sig%s)"
          % (len(dirs), len(info), "" if show_all else ", NEW only"))
    print("# rank by crash-shape then rarity; CRASH/UAF-RISK are the ones to replay on a plain FT build\n")
    for score, negveh, sig, e, known in rows:
        veh = -negveh
        tag = ("[%s]" % known) if known else "[NEW]"
        print("%s veh=%-3d %-7s %s" % (label[score], veh, tag, sig))
        if score >= 2:
            src = os.path.join(e["sample"], "source.py")
            print("    vehicle: %s" % e["sample"])
            print("    replay (does it crash without a sanitizer?):")
            print("      PYTHON_GIL=0 setarch -R %s -u %s   # run a few times; watch for SIGSEGV"
                  % (_PLAIN_FT, src if os.path.exists(src) else "<source.py>"))
    n_crash = sum(1 for r in rows if r[0] == 3)
    n_uaf = sum(1 for r in rows if r[0] == 2)
    print("\n# %d CRASH-shaped + %d UAF-RISK candidate(s) worth the plain-build replay." % (n_crash, n_uaf))


if __name__ == "__main__":
    main()
