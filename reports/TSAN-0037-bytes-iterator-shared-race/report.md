# Data race: a shared `bytes` iterator advances `it_index` non-atomically and double-DECREFs `it_seq` on exhaustion (`striter_next`, `Objects/bytesobject.c`)

*The bytes iterator (`striter_next`, `Objects/bytesobject.c`) keeps its cursor in a plain `it->it_index` and its source in `it->it_seq`, with no synchronization. When one bytes iterator is shared across threads, `striter_next` reads `it->it_index` at the bounds check (`:3446`) and then reads-and-increments it in `seq->ob_sval[it->it_index++]` (`:3448`) — a non-atomic cursor race that can read `ob_sval` out of bounds — and on exhaustion two threads can both run `it->it_seq = NULL; Py_DECREF(seq)`, a double-DECREF / use-after-free. This is the `bytes` form of the str-iterator race [cpython#153928](https://github.com/python/cpython/issues/153928).*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`striter_next` mutates the iterator's private cursor with plain memory accesses:

```c
static PyObject *
striter_next(PyObject *op)
{
    striterobject *it = (striterobject *)op;
    PyBytesObject *seq = it->it_seq;
    if (seq == NULL) return NULL;
    if (it->it_index < PyBytes_GET_SIZE(seq)) {              /* :3446  READ it_index (bounds check) */
        return _PyLong_FromUnsignedChar(
            (unsigned char)seq->ob_sval[it->it_index++]);   /* :3448  READ+WRITE it_index (fetch + post-inc) */
    }
    it->it_seq = NULL;                                       /* :3451  exhaustion */
    Py_DECREF(seq);                                          /* :3452  DECREF it_seq */
    return NULL;
}
```

Two threads sharing one iterator race two ways: (1) the non-atomic `it_index` — a sibling can push it past the end between the `:3446` check and the `:3448` read, giving an out-of-bounds read of `ob_sval`; (2) both threads reaching the exhaustion branch DECREF the same `bytes` object twice → refcount underflow / UAF. TSan directly observes the `it_index` race (`striter_next:3446` read vs `:3448` write); the double-DECREF is by inspection, matching the confirmed str form in #153928.

## Reproducer

```python
import sys, threading
# Bytes-iterator analog of cpython#153928: many threads share ONE bytes iterator and
# advance its cursor concurrently. striter_next (Objects/bytesobject.c) reads it->it_index
# (bounds check) and writes it->it_index++ with no synchronization -> data race + OOB read;
# on exhaustion two threads both run `it->it_seq = NULL; Py_DECREF(seq)` -> double-DECREF UAF.
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NT = 8
LEN = 2000

def drain(it, b):
    b.wait()
    for _ in it:
        pass

for r in range(ROUNDS):
    it = iter(b"A" * LEN)
    b = threading.Barrier(NT)
    ts = [threading.Thread(target=drain, args=(it, b)) for _ in range(NT)]
    for t in ts: t.start()
    for t in ts: t.join()
print("done")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:exitcode=66:history_size=4" \
  setarch -R ./python repro.py
```

Deterministic, exit **66** within ~1 s. `SUMMARY` names `striter_next:3446` (read) vs `:3448` (write). Matches all 196 fleet-06 vehicles. (Full report in `tsan_report.txt`.)

## Root cause

`striterobject` (`it_index`, `it_seq`) is a builtin sequence iterator whose per-iteration state is mutated with no per-object critical section and no atomics. Under the GIL only one thread advances at a time; in the free-threaded build a shared iterator races on `it_index` (a data race + OOB read) and on the `it_seq` release (a double-DECREF). Structurally identical to the str iterator `unicode_ascii_iter_next` (#153928): same `it_index`-non-atomic + `it_seq`-double-DECREF pair, in `bytesobject.c` instead of `unicodeobject.c`.

## Impact / severity

**Moderate-to-high (memory-unsafe).** The `it_index` race is an out-of-bounds read of `ob_sval`; the `it_seq` double-DECREF is a refcount underflow / use-after-free of the shared `bytes` object. Sharing one iterator across threads is unusual, which lowers real-world priority — but per the maintainers' ruling on the shared-builtin class (Thomas Wouters, "yes, that's a bug"), a shared builtin must not data-race or read out of bounds. Free-threaded build only.

## Suggested fix

Same as the str iterator (#153928): make `it_index` an atomic load/store (or take a per-iterator critical section over the whole `striter_next` body), and release `it_seq` exactly once via an atomic exchange on exhaustion:

```c
PyObject *seq = _Py_atomic_exchange_ptr(&it->it_seq, NULL);
if (seq) Py_DECREF(seq);
```

The list/tuple/range/str iterators share this shape and want the same treatment.

## Notes

- **Why this one *is* fileable** (per CPython's iterator strategy). [gh-124397](https://github.com/python/cpython/issues/124397) ("Strategy for Iterators in Free Threading", Raymond Hettinger) sets the bar: C iterators get "only the minimal changes necessary to cause them to **not crash** … concurrent access is allowed to return duplicate values, skip values, or raise an exception." So the pure `it_index` *value* race would be acceptable — but the two consequences here are **memory-unsafe** and cross that bar: the `it_index` race is an out-of-bounds read of `ob_sval`, and the exhaustion path double-DECREFs `it_seq` (a UAF). Those are the reportable defects, not the value race.
- The **bytes** form of #153928 (str). Not separately filed — the appropriate outward-facing step (at the maintainer's discretion) is a note on the #153928 thread that "the same race exists in `bytesobject.c:striter_next`", since it is the same defect class already under review. `status: confirmed`.
- Same builtin-iterator shared-cursor family as TSAN-0038 (str / #153928), TSAN-0039 (struct / #154013), TSAN-0040 (set), and TSAN-0026 (dict).
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 06, 196 vehicles): the op-mix shared-iterator path shares one `iter(b'A'*N)` across workers.

---

*The bytes form of the builtin sequence-iterator shared-cursor race (cf. str #153928). Not separately filed.*
