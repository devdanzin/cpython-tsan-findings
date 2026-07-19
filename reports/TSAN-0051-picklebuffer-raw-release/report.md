# Data race: `pickle.PickleBuffer.raw()` vs `.release()` on a shared buffer (`Objects/picklebufobject.c`)

*`pickle.PickleBuffer` wraps a `Py_buffer` view. On a free-threaded build, a shared `PickleBuffer` used concurrently races: `.raw()` (`picklebuf_raw`, `picklebufobject.c:155`) reads `self->view` while `.release()` (`picklebuf_release:196` → `PyBuffer_Release`) writes/invalidates it — a data race on the view, and `.raw()` may read a just-released buffer.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`PicklebufObject.view` is a `Py_buffer` with no per-object synchronization. `.raw()` builds a `memoryview` over `self->view`; `.release()` calls `PyBuffer_Release(&self->view)`, tearing it down. Concurrent `.raw()`/`.release()` on one shared `PickleBuffer` race the view fields, and a `.raw()` interleaved with a `.release()` can read a buffer being released.

## Reproducer

```python
import pickle
import threading

NT = 8
barrier = threading.Barrier(NT)


def worker(pb, role):
    barrier.wait()
    for _ in range(4000):
        try:
            if role:
                pb.raw()
            else:
                pb.release()
        except (BufferError, ValueError):
            pass


for _round in range(400):
    buf = pickle.PickleBuffer(bytearray(64))
    ts = [threading.Thread(target=worker, args=(buf, i % 2)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
```

Under a free-threaded TSan build: exit **66**, deterministically (`SUMMARY: … PyBuffer_Release vs picklebuf_raw`). Reproduced on our `debug-ft-nojit-tsan`; signature matches the magalu fleet exactly. (Full report in `tsan_report.txt`.)

## Impact / severity

**Low–moderate.** A `.raw()` racing `.release()` can read a buffer being torn down (use-after-release of the view fields). Sharing one `PickleBuffer` and releasing it while another thread reads it is unusual, which caps priority. Free-threaded build only.

## Suggested fix

Take the `PickleBuffer`'s per-object critical section around `.raw()`, `.release()`, and the buffer-export path — or document that a `PickleBuffer` must not be released concurrently with other use.

## Notes

- **Appears unfiled** (a `gh api` search found no `PickleBuffer` FT issue). New but **low priority** (unusual sharing pattern). Cataloged for dedup; not proposing a filing without maintainer interest.
- Found in a downloaded remote fleet (`magalu`, `3.16_ft_debug_tsan`), reproduced independently here.

---

*New but low-priority shared-buffer race; recorded for the catalog. Not proposing a filing.*
