# Data race (value-benign): `csv.reader` advances `self->line_num` without a lock vs a concurrent `reader.line_num` read (`Modules/_csv.c`)

*The `_csv` reader keeps its line counter in a plain `self->line_num` (a `T_ULONG` member exposed as `reader.line_num`). `Reader_iternext` advances/resets it while parsing rows, but a concurrent read of `reader.line_num` (via the member descriptor) is unsynchronized — a data race on the counter. The row parse itself is under the reader's critical section (`Reader_iternext_lock_held`); only the `line_num` member read is not.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`ReaderObj.line_num` counts input lines. `Reader_iternext_lock_held` (`Modules/_csv.c:974`) writes it while parsing, and `Reader_iternext` resets it to 0 (`:1103`). Reading `reader.line_num` goes through `PyMember_GetOne` on the `T_ULONG` member — a plain load, with no synchronization against the concurrent write. A shared reader used from multiple threads races on the counter.

## Reproducer

```python
import csv
import io
import threading

NT = 8
barrier = threading.Barrier(NT)


def worker(rdr, role):
    barrier.wait()
    for _ in range(5000):
        if role:
            try:
                next(rdr)        # Reader_iternext: writes self->line_num
            except StopIteration:
                pass
        else:
            _ = rdr.line_num     # PyMember_GetOne: reads self->line_num


for _round in range(300):
    rdr = csv.reader(io.StringIO("a,b\n" * 500))
    ts = [threading.Thread(target=worker, args=(rdr, i % 2)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
```

Under a free-threaded TSan build: exit **66**, deterministically. `SUMMARY` names `Reader_iternext_lock_held` (write `line_num`) vs the `T_ULONG` member read. (Full report in `tsan_report.txt`.)

## Impact / severity

**Low — value-benign.** A stale/torn `reader.line_num` read under concurrency; no memory unsafety. Sharing one `csv.reader` across threads is unusual, capping real-world priority.

## Suggested fix

Read/write `line_num` atomically (`FT_ATOMIC_LOAD/STORE`), or expose it under the reader's critical section. Low priority — value-benign and an unusual sharing pattern.

## Notes

- **Appears unfiled** (a `gh api` search for csv-reader / `line_num` free-threading found nothing), but **value-benign** — a stale counter, not a crash — so a low-priority candidate at most; not proposing a filing.
- Same shared-object member-counter shape as the value-benign iterator cursors (TSAN-0040 set, TSAN-0044 seq/deque). Found by `fusil --tsan` (fleet 12), surfaced via `--tsan-no-halt`.

---

*New but value-benign; recorded for the catalog. Not proposing a filing.*
