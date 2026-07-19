# Data race: a shared `struct.iter_unpack` iterator advances its index non-atomically (`unpackiter_iternext` vs `unpackiter_len`, `Modules/_struct.c`) — this is cpython#154013

*The struct unpack-iterator (`unpackiterobject`, from `Struct.iter_unpack`) keeps its byte offset / index in a plain field with no synchronization. When one unpack iterator is shared across threads, `unpackiter_iternext` advances that index (`:2278`) with no atomicity while `unpackiter_len` (the `__length_hint__` slot, `:2249`) plainly reads it — a data race on the cursor. This is the `struct` form of the builtin-iterator shared-cursor defect (str [cpython#153928](https://github.com/python/cpython/issues/153928) / bytes TSAN-0037), filed as [cpython#154013](https://github.com/python/cpython/issues/154013).*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`unpackiterobject` (`Modules/_struct.c`) holds a reference to its source `Struct` + buffer and a running index/offset. `unpackiter_iternext` advances that index and `unpackiter_len` reads it, both without synchronization:

```c
/* unpackiter_iternext */
    ...
    self->index += self->so->s_size;                        /* :2278  WRITE the index/offset */
    ...
/* unpackiter_len (__length_hint__) */
    len = (self->buf.len - self->index) / self->so->s_size; /* :2249  READ the index/offset */
```

A single `Struct('i').iter_unpack(...)` shared across threads — some advancing via `next()`, some reading its cursor via `operator.length_hint` — races on the index word.

## Reproducer

```python
import operator
import struct
import sys
import threading

# struct.Struct.iter_unpack form of the builtin-iterator shared-cursor race (cpython#154013): many
# threads share ONE unpack iterator; half advance it (unpackiter_iternext writes the index,
# Modules/_struct.c:2278) and half read its cursor via __length_hint__ (unpackiter_len reads the
# index, :2249) -> non-atomic index data race. Run under the debug-ft-nojit-tsan build with
# PYTHON_GIL=0; exit 66 = TSan race.
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NT = 8
N = 4096
S = struct.Struct("i")


def advance(it, bar):
    bar.wait()
    for _ in it:
        pass


def measure(it, bar):
    bar.wait()
    for _ in range(N):
        operator.length_hint(it, 0)


for _r in range(ROUNDS):
    shared = S.iter_unpack(bytes(4 * N))
    bar = threading.Barrier(NT)
    ts = [
        threading.Thread(target=(advance if i % 2 else measure), args=(shared, bar))
        for i in range(NT)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:exitcode=66:history_size=4" \
  setarch -R ./python repro.py
```

Deterministic, exit **66**. `SUMMARY` names `unpackiter_iternext:2278` (write) vs `unpackiter_len:2249` (read). (Full report in `tsan_report.txt`.)

## Root cause

The unpack iterator's index/offset is per-iterator state mutated with no per-object critical section and no atomics — safe under the GIL, a data race when the iterator is shared in the free-threaded build. Same class as the str (#153928), bytes (TSAN-0037), and dict (TSAN-0026) iterators.

## Impact / severity

**Moderate.** A data race on the cursor → mis-stepped / duplicated / out-of-bounds unpack reads under concurrency. Sharing one iterator across threads is unusual (lower real-world priority), but `_struct` declares no per-object locking. Already filed upstream (#154013). Free-threaded build only.

## Suggested fix

Same as the str/bytes iterators: make the unpack-iterator's index an atomic load/store, or take a per-iterator critical section over `unpackiter_iternext` / `unpackiter_len`. The whole builtin/stdlib sequence-iterator family shares this shape.

## Notes

- **Governing strategy.** [gh-124397](https://github.com/python/cpython/issues/124397) ("Strategy for Iterators in Free Threading", Raymond Hettinger): C iterators get "only the minimal changes necessary to cause them to **not crash** … concurrent access is allowed to return duplicate values, skip values, or raise an exception." The fileable concern for the unpack iterator is that a mis-stepped index can drive an out-of-bounds unpack read (a crash), not the value race per se.
- This **is** cpython#154013 (`struct.iter_unpack` iterator). `status: reported`.
- Same builtin-iterator shared-cursor family as TSAN-0037 (bytes), TSAN-0038 (str / #153928), TSAN-0040 (set), TSAN-0026 (dict).
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 08, 88 vehicles): the op-mix shared-iterator path shares one `Struct('i').iter_unpack(bytes(4*4096))` across workers, some advancing via `next()` and some reading the cursor via `operator.length_hint`.

---

*This is cpython#154013 (struct unpack iterator). Recorded for the catalog; not a separate filing.*
