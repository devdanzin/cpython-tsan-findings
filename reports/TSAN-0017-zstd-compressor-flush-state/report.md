# Data race: shared `ZstdCompressor.flush()` racing `getattr(c, "last_mode")` — the same `last_mode` write/member-read race as TSAN-0002 (`compressor.c:679`)

*A `ZstdCompressor.flush()` writes `self->last_mode` with a plain C store under `self->lock` (`Modules/_zstd/compressor.c:679`). The same field is published as a read-only `Py_T_INT` member, so `getattr(c, "last_mode")` reads it lock-free with a relaxed-atomic load in `PyMember_GetOne` (`Python/structmember.c:64`). On a shared compressor those two accesses race. This is not a distinct "two concurrent `flush()` corrupt the zstd stream" bug: `flush()` holds `self->lock` around the whole compression context and every `last_mode` write, so writer-vs-writer is fully serialized. TSAN-0017 is the **same field, same race face as TSAN-0002**, reached through a different call shape — a duplicate/variant, not a new bug.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

TSAN-0017 was auto-seeded from a `_zstd` fleet vehicle whose stress worker both **calls** `c.flush()` and **reads** `getattr(c, name)` over every non-underscore name in `dir(c)` — and `dir(ZstdCompressor)` includes the read-only member `last_mode`. The captured race (`tsan_report.txt`) is therefore:

- **write side**: `_zstd_ZstdCompressor_flush_impl` storing `self->last_mode = mode` at `compressor.c:679` (plain C store, under `self->lock`);
- **read side**: `PyMember_GetOne` loading that same `int` with `FT_ATOMIC_LOAD_INT_RELAXED` at `structmember.c:64` (no lock).

The fleet dedup labelled the vehicle `flush | flush` (the write side headlines the one-line SUMMARY as `_zstd_ZstdCompressor_flush_impl`, and the worker's dominant operation is `flush()`), which raised the question of whether two concurrent `flush()` calls corrupt the shared compression context (`self->cctx`) or output-buffer state. **They do not** — see the "Distinct-from vs duplicate-of TSAN-0002" section. The only unsynchronized access to any compressor field is the member-descriptor read of `last_mode`, which is exactly TSAN-0002.

## Reproducer

```python
import sys, threading
from compression import zstd
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 8            # threads per shared compressor (even = flush/write, odd = read)
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        c = pool[0]
        if wid % 2 == 0:
            c.flush()                      # _zstd_ZstdCompressor_flush_impl: self->last_mode = mode
        else:
            for _ in range(16):
                getattr(c, "last_mode")    # PyMember_GetOne: relaxed-atomic read of last_mode
        leave.wait()

ts = [threading.Thread(target=worker, args=(w,)) for w in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = zstd.ZstdCompressor()        # fresh, unlocked compressor each round
    enter.wait()
    leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, build `debug-ft-nojit-tsan`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2149120)
  Write of size 4 at 0x7fffb6672500 by thread T1:
    #0 _zstd_ZstdCompressor_flush_impl Modules/_zstd/compressor.c:679:25   (self->last_mode = mode)
    #1 _zstd_ZstdCompressor_flush     Modules/_zstd/clinic/compressor.c.h:250:20
    #2 method_vectorcall_FASTCALL_KEYWORDS Objects/descrobject.c:421:24
    ...
    #29 thread_run Modules/_threadmodule.c:388:21

  Previous atomic read of size 4 at 0x7fffb6672500 by thread T8:
    #0 _Py_atomic_load_int_relaxed Include/cpython/pyatomic_gcc.h:307:10
    #1 PyMember_GetOne             Python/structmember.c:64:29   (FT_ATOMIC_LOAD_INT_RELAXED(*(int*)addr))
    #2 member_get                 Objects/descrobject.c:180:12
    #3 _PyObject_GenericGetAttrWithDict Objects/object.c:1926:19
    #4 PyObject_GenericGetAttr     Objects/object.c:2012:12
    #5 PyObject_GetAttr            Objects/object.c:1322:18
    #6 builtin_getattr             Python/bltinmodule.c:1331:18
    ...
    #31 thread_run Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/_zstd/compressor.c:679:25 in _zstd_ZstdCompressor_flush_impl
```

Reproduces reliably: exit 66 on every run (3/3), same SUMMARY. The two racing accesses (address, size 4, the two functions) are byte-for-byte the seeded `tsan_report.txt` and identical to TSAN-0002.

## Root cause

The compressor struct keeps a mutable `int last_mode` and a `PyMutex lock` (`compressor.c:24-44`):

```c
typedef struct {
    PyObject_HEAD
    ZSTD_CCtx *cctx;      /* compression context */
    PyObject *dict;
    int last_mode;        /* last end-directive */
    int use_multithread;
    int compression_level;
    PyMutex lock;         /* protects the compression context */
} ZstdCompressor;
```

`flush()` takes the lock around the *entire* operation, including both `last_mode` stores (`compressor.c:673-687`):

```c
PyMutex_Lock(&self->lock);
ret = compress_lock_held(self, NULL, mode);   /* all cctx work, under the lock */
if (ret) {
    self->last_mode = mode;                    /* :679  plain write, lock held */
}
else {
    self->last_mode = ZSTD_e_end;              /* :682  plain write, lock held */
    ZSTD_CCtx_reset(self->cctx, ZSTD_reset_session_only);
}
PyMutex_Unlock(&self->lock);
```

`last_mode` is *also* published as a read-only member (`compressor.c:758-762`), read lock-free through `PyMember_GetOne` with a relaxed atomic (`structmember.c:64`):

```c
static PyMemberDef ZstdCompressor_members[] = {
    {"last_mode", Py_T_INT, offsetof(ZstdCompressor, last_mode),
     Py_READONLY, ZstdCompressor_last_mode_doc},   /* :759 */
    {NULL}
};
```
```c
case Py_T_INT:
    v = PyLong_FromLong(FT_ATOMIC_LOAD_INT_RELAXED(*(int*)addr));  /* structmember.c:64 */
```

So the write side is a **plain** store (lock-guarded against other writers) and the read side is a **lock-free relaxed-atomic** load. A plain write paired with an atomic read on the same word, with no happens-before edge between them, is a data race — which is exactly what TSan reports. The `self->lock` serializes the writers and the in-lock read at `set_pledged_input_size` (`:725`), but it never covers the member-descriptor read path.

## Distinct-from vs duplicate-of TSAN-0002 — the key determination

The task was to decide whether this `flush`-labelled race is (a) the same `last_mode` field as **TSAN-0002** → duplicate/variant, or (b) a *different* field (the `ZSTD_CCtx` compression context `self->cctx`, or unprotected output-buffer/stream state) that two concurrent `flush()` calls could corrupt → a distinct, more serious bug.

**Answer: (a) — it is the same `last_mode` field, a duplicate/variant of TSAN-0002.** Evidence:

1. **The captured race is `last_mode`, not `cctx`.** The write is `self->last_mode = mode` at `compressor.c:679`; the paired access is `PyMember_GetOne` reading the `last_mode` `Py_T_INT` member. Same address, same 4-byte `int`, same two functions as TSAN-0002's `tsan_report.txt`. No `cctx`/buffer address is involved.

2. **`flush()` fully serializes the compression context.** `flush()` acquires `self->lock` (`:674`) and holds it across `compress_lock_held()` — which is where all `self->cctx` mutation and the `_BlocksOutputBuffer` work happen — and across both `last_mode` stores, releasing only at `:687`. The `Py_BEGIN_ALLOW_THREADS` region inside `compress_lock_held()` (`:480-482`) detaches the thread state but does **not** release `self->lock`, so a second `flush()`/`compress()` blocks on `PyMutex_Lock`. Writer-vs-writer on `cctx`, the output buffer, and `last_mode` is therefore impossible.

3. **Control experiment: pure `flush | flush` produces no race.** Running 8 threads that only call `c.compress(...)` + `c.flush(...)` on a shared compressor (fresh each round, **no** `getattr`) completes cleanly — `done, no race (control)`, **exit 0**, zero TSan warnings. If a distinct `cctx`/buffer/stream race existed, this is precisely the workload that would surface it. (Control script kept in scratch; not shipped in the catalog dir.)

The `flush | flush` seed label is a fleet-dedup artifact: the vehicle worker's dominant call is `flush()` (which headlines the SUMMARY as `_zstd_ZstdCompressor_flush_impl`), and the same worker separately does `getattr(c, "last_mode")` via its `dir(c)` sweep — so both racing accesses trace back to the same `_zstd` vehicle, and the signature collapsed onto the flush frame. The underlying bug is TSAN-0002.

## Impact / severity

**Low — and no additional severity beyond TSAN-0002.** `last_mode` is a naturally aligned 4-byte `int` holding one of three small enum constants (`ZSTD_e_continue`/`ZSTD_e_flush`/`ZSTD_e_end`); the store/load are single-word and untorn on the platforms CPython targets, so the observed value is always valid — no crash, no use-after-free, no stream corruption. It is a genuine TSan-reported data race on a documented read-only attribute (`getattr(c, "last_mode")`) plus `flush()`/`compress()`, which `ZstdCompressor` documents as "Thread-safe at method level." Crucially, the more serious hypothesis — concurrent `flush()` corrupting the shared `ZSTD_CCtx` stream state — is **ruled out** by the lock discipline and the control experiment.

## Suggested fix

Same as TSAN-0002: convert the four `last_mode` stores to the matching relaxed-atomic store so they pair cleanly with the relaxed-atomic member read already used by `PyMember_GetOne` (the value is idempotent enum state, so relaxed ordering suffices):

```c
/* replace plain `self->last_mode = X;` at compressor.c:631, 634, 679, 682 */
FT_ATOMIC_STORE_INT_RELAXED(self->last_mode, mode);        /* success paths */
FT_ATOMIC_STORE_INT_RELAXED(self->last_mode, ZSTD_e_end);  /* reset paths   */
```

(`FT_ATOMIC_STORE_INT_RELAXED` is the write counterpart of `FT_ATOMIC_LOAD_INT_RELAXED`, in `Include/internal/pycore_pyatomic_ft_wrappers.h`.) The in-lock read at `set_pledged_input_size` (`:725`) may stay as-is or switch to the atomic load for uniformity. No new lock is needed on the read path. One PR fixing the four stores resolves both TSAN-0002 and TSAN-0017.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 02, module `_zstd`, vehicle `inst-01/python/_zstd-warning_threadsanitizer_data_race-tsanNEW`. This is the same "numeric field mutated by a plain store but published as a `Py_T_*` member and read lock-free via `PyMember_GetOne`" class as TSAN-0002; TSAN-0017 is a **duplicate reached via a different call shape** (fleet 02's richer op-mix does `flush()` + a `dir()`-driven `getattr` sweep, so the member read and the flush write collide). `Modules/_zstd/` is new in 3.14 and is **not** on the gh-116738 audit list; the `last_mode` stores are still plain on current main (`heads/main:bcf98ddbc40`). No separate upstream filing is warranted — fold into the TSAN-0002 fix.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
