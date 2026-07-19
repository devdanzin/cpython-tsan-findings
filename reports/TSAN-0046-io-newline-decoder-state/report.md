# Data race: `io.IncrementalNewlineDecoder.reset()` writes `self->seennl` without a lock vs `.newlines` reading it (`Modules/_io/textio.c`) — cpython#144777

*The `_io.IncrementalNewlineDecoder` keeps its seen-newline bitmask in a plain `self->seennl`. `_io_IncrementalNewlineDecoder_reset_impl` (`textio.c:630`) writes `self->seennl = 0` with no critical section, while the `.newlines` getter (`incrementalnewlinedecoder_newlines_get`, `:644`) and `decode()` read/update it. A shared decoder driven by `.reset()`/`.decode()` from one thread and `.newlines` from another races on `seennl`. This **is** [cpython#144777](https://github.com/python/cpython/issues/144777) (CLOSED).*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`nldecoder_object.seennl` is a plain `int` bitmask of which newline kinds have been seen. `reset()` clears it, `decode()` OR-accumulates into it (`textio.c:511`), and the `.newlines` property reads it — none under synchronization. A shared decoder used from multiple threads races on the field.

## Reproducer

```python
import codecs
import io
import threading

NT = 8
barrier = threading.Barrier(NT)


def worker(dec, role):
    barrier.wait()
    for _ in range(5000):
        if role:
            dec.reset()          # _io_IncrementalNewlineDecoder_reset_impl: self->seennl = 0
        else:
            _ = dec.newlines     # incrementalnewlinedecoder_newlines_get: reads self->seennl


for _round in range(200):
    d = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), True)
    d.decode(b"a\r\nb\n")
    ts = [threading.Thread(target=worker, args=(d, i % 2)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
```

Under a free-threaded TSan build (`PYTHON_GIL=0`, `TSAN_OPTIONS=…exitcode=66…`): exit **66**, deterministically. `SUMMARY` names `_io_IncrementalNewlineDecoder_reset_impl` (write) vs `incrementalnewlinedecoder_newlines_get` (read). (Full report in `tsan_report.txt`.)

## Impact / severity

**Low — value-benign.** A stale/mixed `seennl` bitmask yields a wrong `.newlines` report or a mis-split `\r\n` boundary under concurrency; no memory unsafety. Free-threaded build only.

## Suggested fix

Take the decoder's per-object critical section (`Py_BEGIN_CRITICAL_SECTION(self)`) around the `seennl`/`pendingcr` read-modify-writes in `reset`/`decode` and the `.newlines` getter, or make `seennl` atomic. (See cpython#144777.)

## Notes

- **This is cpython#144777** ("Possible data race in `_io_IncrementalNewlineDecoder_reset_impl` in `textio.c`", **CLOSED**). Our build (`main@a1d580430c8`) still reproduces it — so either it was closed as won't-fix, or the build predates a fix. Cataloged for dedup; not a new filing.
- Same incremental-decoder-state class as TSAN-0001 (cjkcodecs `MultibyteIncrementalDecoder` / `MultibyteStreamReader`). Found by `fusil --tsan` (fleet 12), surfaced via `--tsan-no-halt`.

---

*This is cpython#144777 (closed). Recorded for the catalog; not a separate filing.*
