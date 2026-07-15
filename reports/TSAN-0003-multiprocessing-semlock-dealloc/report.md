# Data race: concurrent `_multiprocessing.SemLock` create/destroy races on glibc's `__sem_mappings` tree — **TSan false positive** (`semaphore.c:516` / `object.c:3319`)

*Creating a `SemLock` calls glibc `sem_open()` → `tsearch()` (insert into the process‑global `__sem_mappings` red‑black tree); destroying one calls `sem_close()` → `tdelete()` (remove from it). Doing both concurrently from several threads makes TSan flag a data race on the shared tree. But glibc already serializes every access to that tree with its internal `__sem_mappings_lock` (an `lll`/futex low‑level lock). TSan does not intercept `lll_lock`, so it cannot see the happens‑before edge and reports a race that isn't real. **This is not a CPython free‑threading bug and not a use‑after‑free of the `SemLock` object.***

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

The auto‑seeded signature framed this as "a `SemLock` method call races with its deallocation (possible use‑after‑free)". Reading the two racing stacks shows that is **not** what happens:

- The racing memory (`0x720800…`) is **not** the `SemLock` PyObject — it is a node of glibc's process‑global named‑semaphore mapping tree (`__sem_mappings`), allocated/freed by libc's `tsearch`/`tdelete`.
- **Thread A (creator):** `_multiprocessing.SemLock(...)` → `_multiprocessing_SemLock_impl` (`semaphore.c:516`, `SEM_CREATE` = `sem_open`) → glibc `tsearch` → `malloc` a tree node.
- **Thread B (destroyer):** last `Py_DECREF` → `_Py_Dealloc` (`object.c:3319`) → `semlock_dealloc` (`SEM_CLOSE` = `sem_close`) → glibc `tdelete` → `free` the tree node.

CPython's own refcounting is correctly synchronized here (`_Py_MergeZeroLocalRefcount`/`_Py_Dealloc` are not flagged). The only accesses TSan complains about are the libc `malloc`/`free` inside `tsearch`/`tdelete`. And glibc protects that tree with an internal lock — so the "race" is an artifact of TSan not instrumenting glibc's low‑level lock.

## Reproducer

Uses `_multiprocessing.SemLock` directly (with `unlink=True`, so no `resource_tracker` process is involved) to hammer `sem_open`/`sem_close` from 8 threads:

```python
import sys, os, threading
import _multiprocessing
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

SEMAPHORE = 1                 # kind: RECURSIVE_MUTEX=0, SEMAPHORE=1
NT = 8
ROUNDS = 4000
enter = threading.Barrier(NT)

def worker(tid):
    enter.wait()
    for i in range(ROUNDS):
        name = "/fu-%d-%d-%d" % (os.getpid(), tid, i)
        try:
            sl = _multiprocessing.SemLock(SEMAPHORE, 1, 1, name, True)  # sem_open -> tsearch
        except (FileExistsError, OSError):
            continue
        del sl                # dealloc -> sem_close -> tdelete

ts = [threading.Thread(target=worker, args=(t,)) for t in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
```

Run (free‑threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, glibc 2.43)

Reproduces deterministically (exit **66**, no crash):

```
WARNING: ThreadSanitizer: data race
  Write of size 8 at 0x720800002000 by thread T5:
    #0 free
    #1 __tdelete misc/tsearch.c:675:3            (libc.so.6)
    #2 _Py_Dealloc Objects/object.c:3319:5       (semlock_dealloc -> sem_close -> tdelete)
    #3 _Py_MergeZeroLocalRefcount Objects/object.c:444
    ...  (Py_DECREF from the owning thread's frame)

  Previous write of size 8 at 0x720800002000 by thread T2:
    #0 malloc
    #1 __GI___tsearch misc/tsearch.c:337:25      (libc.so.6)
    #2 tsearch misc/tsearch.c:290:1
    #3 _multiprocessing_SemLock_impl Modules/_multiprocessing/semaphore.c:516:14   (sem_open -> tsearch)
    #4 _multiprocessing_SemLock  clinic/semaphore.c.h:308
    #5 type_call Objects/typeobject.c:2472
    ...

SUMMARY: ThreadSanitizer: data race (python+0xfe6ae) in free
```

Same two functions as the seeded signature: `_multiprocessing_SemLock_impl` (`semaphore.c:516`) and `_Py_Dealloc` (`object.c:3319`).

## Root cause

`semaphore.c` uses named POSIX semaphores on Unix:

```c
// Modules/_multiprocessing/semaphore.c
#define SEM_CREATE(name, val, max) sem_open(name, O_CREAT | O_EXCL, 0600, val)   // :226
#define SEM_CLOSE(sem) sem_close(sem)                                            // :227

static PyObject *
_multiprocessing_SemLock_impl(...)
{
    ...
    handle = SEM_CREATE(name, value, maxvalue);   // :516  -> sem_open() -> tsearch()
    ...
}

static void
semlock_dealloc(PyObject *op)
{
    ...
    if (self->handle != SEM_FAILED)
        SEM_CLOSE(self->handle);                  // :588  -> sem_close() -> tdelete()
    PyMem_Free(self->name);
    tp->tp_free(self);
    ...
}
```

`sem_open`/`sem_close` maintain a **process‑global** red‑black tree, `__sem_mappings`, so that a semaphore mapped twice returns the same `sem_t*` and reference counts are tracked. Insertion uses `tsearch` (malloc a node) and removal uses `tdelete` (free a node). Concurrent create/destroy therefore mutate the same shared tree — which is exactly what TSan flags.

**But glibc serializes every tree access with `__sem_mappings_lock`.** Disassembling glibc 2.43 (`/usr/lib/x86_64-linux-gnu/libc.so.6`), the static helpers that call `tsearch`/`tdelete` bracket them with acquire/release on one lock word (address `0x2187d8`; objdump mislabels it as near `__pthread_keys` because it is a hidden static symbol):

```
  lock cmpxchg %edx, 0x…(%rip)   # 2187d8   <- acquire __sem_mappings_lock (lll fast path)
  call  __lll_lock_wait_private            <- contended acquire
  ...
  call  __tsearch                          <- add-mapping, under the lock
  ...
  call  __tdelete                          <- remove-mapping, under the lock
  xchg  %eax, 0x…(%rip)          # 2187d8   <- release __sem_mappings_lock
  call  __lll_lock_wake_private            <- wake waiters
```

`__sem_mappings_lock` is a glibc `__libc_lock_t` — a raw `int` driven by `lll_lock`/`lll_unlock` (futex), **not** a `pthread_mutex_t`. ThreadSanitizer's runtime interposes `pthread_mutex_lock`/`pthread_rwlock_*` etc., but it does **not** interpose glibc's internal `lll_lock`/`__lll_lock_wait_private`. glibc itself is not built with TSan instrumentation. So TSan never records the acquire/release on `__sem_mappings_lock`, never sees the happens‑before edge between the `tsearch` and the `tdelete`, and reports the tree mutation as a data race.

This is the well‑known "uninstrumented‑libc low‑level lock" class of TSan false positive (same shape as races TSan reports inside `dlopen`, `tzset`, `__gconv`, etc.).

## Impact / severity

**None — this is a TSan false positive, not a real data race.**

- No CPython invariant is violated: the `SemLock` PyObject is refcounted correctly, and there is no use‑after‑free of it. Under a normal (non‑TSan) build the program runs cleanly.
- No glibc bug either: POSIX requires `sem_open`/`sem_close` to be thread‑safe, and glibc satisfies that with `__sem_mappings_lock`. Millions of concurrent create/destroy operations do not corrupt the tree.
- The report is pure instrumentation noise caused by TSan's blind spot for glibc‑internal `lll` locks.

The seeded "possible use‑after‑free of a shared `SemLock`" reading is incorrect: the racing address is a libc tree node, and the two Python‑level `SemLock` objects involved are distinct (created by one thread, destroyed by another) — no cross‑thread reuse of freed CPython memory occurs.

## Suggested fix

Nothing to fix in CPython. Options for silencing the noise:

1. **TSan suppression (recommended).** Add a `race:` / `called_from_lib` suppression for the glibc named‑semaphore mapping path, e.g. in a `TSAN_OPTIONS=suppressions=...` file:
   ```
   # glibc __sem_mappings tree is protected by __sem_mappings_lock (an lll/futex
   # lock TSan cannot see); sem_open/sem_close are thread-safe.
   race:tsearch
   race:tdelete
   race:sem_open
   race:sem_close
   ```
   (or a broader `called_from_lib:libc.so`).
2. **Use a TSan‑instrumented libc** so the `lll_lock` is visible — impractical for most setups.
3. A CPython‑side lock around `sem_open`/`sem_close` would suppress the warning but only papers over harmless TSan noise while adding real contention; **not worth doing.**

For the fuzzing catalog, the right action is to add this to the TSan dedup/suppression list so future fleets don't re‑flag it.

## Notes

- Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, vehicle `inst-03/python/multiprocessing_context-…-tsanNEW` (the fuzzer shared `multiprocessing.context`‑created locks across threads and let some be dropped).
- The same class will appear for **any** concurrent named‑POSIX‑semaphore churn: `multiprocessing.Lock()/RLock()/Semaphore()/BoundedSemaphore()/Condition()/Event()` all build on `_multiprocessing.SemLock` and thus on `sem_open`/`sem_close`. Suppressing on the `tsearch`/`tdelete`/`sem_*` frames covers all of them.
- Confirmed build: `python_build_matrix/builds/debug-ft-nojit-tsan` (CPython 3.16.0a0, `--disable-gil --with-thread-sanitizer`), glibc 2.43, ASLR off (`setarch -R`).

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed. This entry is classified a **TSan false positive (glibc-internal lock)** and is a candidate for the suppression list rather than an upstream report.*
