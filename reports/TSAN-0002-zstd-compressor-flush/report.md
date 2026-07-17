# Data race: `_zstd.ZstdCompressor.last_mode` is written plain under the lock but read lock-free via its member descriptor (`compressor.c:679`)

*`ZstdCompressor.flush()`/`.compress()` store the `int last_mode` field with a plain C write while holding `self->lock` (`Modules/_zstd/compressor.c:679`). The same field is exposed as a read-only `Py_T_INT` member, so `getattr(c, "last_mode")` reads it through `PyMember_GetOne`, which does a **relaxed-atomic** load (`Python/structmember.c:64`) and does **not** take `self->lock`. On a shared compressor, the unlocked plain write races the unlocked atomic read of `last_mode`. `getattr` and `flush()` both look thread-safe to callers, so a shared `ZstdCompressor` is not actually safe to use this way.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Summary

`Modules/_zstd/compressor.c` keeps a per-compressor `int last_mode` (the last compression end-directive used). It is mutated by `compress()` and `flush()` as a plain store, but only ever *written* under `self->lock`:

```c
/* _zstd_ZstdCompressor_flush_impl */
PyMutex_Lock(&self->lock);
ret = compress_lock_held(self, NULL, mode);
if (ret) {
    self->last_mode = mode;            /* :679  plain write, lock held */
}
else {
    self->last_mode = ZSTD_e_end;      /* :682  plain write, lock held */
    ZSTD_CCtx_reset(self->cctx, ZSTD_reset_session_only);
}
PyMutex_Unlock(&self->lock);
```

The field is *also* published as a read-only member:

```c
static PyMemberDef ZstdCompressor_members[] = {
    {"last_mode", Py_T_INT, offsetof(ZstdCompressor, last_mode),
     Py_READONLY, ZstdCompressor_last_mode_doc},   /* compressor.c:759 */
    {NULL}
};
```

Reading that attribute goes through `PyMember_GetOne`, which for `Py_T_INT` uses a relaxed atomic load and takes **no object lock**:

```c
case Py_T_INT:
    v = PyLong_FromLong(FT_ATOMIC_LOAD_INT_RELAXED(*(int*)addr));  /* structmember.c:64 */
    break;
```

So on a shared `ZstdCompressor`, thread A doing `c.flush()` (plain, non-atomic write of `last_mode`, under the lock) races thread B doing `getattr(c, "last_mode")` (relaxed-atomic read, no lock). Because the writer's store is **not** atomic while the reader's load **is**, ThreadSanitizer reports a data race. It is value-benign (a single aligned 4-byte word holding one of a few enum constants), but it is a genuine reported race on operations callers treat as thread-safe.

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

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, TSan build `debug-ft-nojit-tsan`)

```
WARNING: ThreadSanitizer: data race (pid=1963046)
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
    #29 thread_run Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/_zstd/compressor.c:679:25 in _zstd_ZstdCompressor_flush_impl
```

Reproduces reliably (exit 66) across repeated runs and does not crash. The two racing functions match the seeded signature (`_Py_atomic_load_int_relaxed` in `PyMember_GetOne` vs `_zstd_ZstdCompressor_flush_impl`); TSan names whichever side it saw second in the one-line SUMMARY, so it may head with either function. (Note: `compress()` writes the same field at `compressor.c:631/634` and produces the same race face.)

## Root cause

`last_mode` is *mutable internal state* that CPython also publishes as a plain `Py_T_INT` member. In the free-threaded build, the generic member-read path (`PyMember_GetOne`) was hardened to use relaxed atomics (`FT_ATOMIC_LOAD_INT_RELAXED`, `structmember.c:64`) precisely so lock-free attribute reads are well-defined. The `_zstd` module, however, still writes `last_mode` with a **plain** C store (`compressor.c:631`, `:634`, `:679`, `:682`). `self->lock` serializes the writers against each other and against the internal read in `set_pledged_input_size` (`:725`, taken under the lock), but the *member-descriptor read path never acquires `self->lock`*, and a plain write paired with an atomic read is, by definition, a data race.

In other words, the object's own mutating methods and its published attribute disagree on the access discipline for `last_mode`: the read side is relaxed-atomic and lock-free; the write side is plain and lock-guarded. Those two disciplines are not compatible, and TSan flags the write/atomic-read pair.

## Impact / severity

**Low.** `last_mode` is a naturally aligned 4-byte `int` holding one of three small enum constants (`ZSTD_e_continue`/`ZSTD_e_flush`/`ZSTD_e_end`). On the platforms CPython targets the store/load are single-word and not torn, so the observed value is always one of the valid constants — no crash, no use-after-free, no memory corruption. But it is a real, TSan-reported data race on APIs (`getattr` of a documented read-only attribute, and `flush()`/`compress()`) that callers reasonably assume are individually thread-safe, and it will show up as noise in any TSan run that touches a shared compressor. `ZstdCompressor` even documents itself as "Thread-safe at method level," which reading `last_mode` concurrently technically violates.

## Suggested fix

Make the module's writes to `last_mode` use the matching relaxed atomic store so they pair cleanly with the relaxed-atomic member read (the value is idempotent enum state, so relaxed ordering is sufficient — this is exactly what the reader already uses):

```c
/* replace plain `self->last_mode = X;` at compressor.c:631, 634, 679, 682 */
FT_ATOMIC_STORE_INT_RELAXED(self->last_mode, mode);        /* success paths */
FT_ATOMIC_STORE_INT_RELAXED(self->last_mode, ZSTD_e_end);  /* reset paths   */
```

(`FT_ATOMIC_STORE_INT_RELAXED` is the write counterpart of the `FT_ATOMIC_LOAD_INT_RELAXED` already used in `structmember.c`, defined in `Include/internal/pycore_pyatomic_ft_wrappers.h`.) The in-lock read at `set_pledged_input_size` (`:725`) can stay as-is (it is lock-protected against writers) or be switched to the atomic load for uniformity. No new lock is needed on the read path; the fix is purely to make the store atomic so it is well-defined against the existing lock-free member read.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, module `_zstd`. This is the general "int/flag field mutated by a plain store but published as a `Py_T_INT`/`Py_T_*` member and read lock-free via `PyMember_GetOne`" class — the same shape can exist in any C type that both exposes a numeric member and mutates it at runtime. `_zstd` is new in 3.14; the member-read atomics landed as part of free-threading hardening, and the module-side stores were simply not converted to match. Worth a small upstream PR (make the four `last_mode` stores atomic); low priority given the benign value.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
