# Data race: `_lsprof` profiler teardown races concurrent monitoring callbacks (`profiler_dealloc`/`flush_unmatched` vs `ptrace_leave_call`)

*`cProfile`/`_lsprof` registers a **global** `sys.monitoring` tool, so while one `Profiler` is enabled every thread running Python drives that single object's shared call-stack state (`self->currentProfilerContext`, `self->profilerEntries`). The per-call callbacks and `enable`/`disable`/`clear` are `@critical_section`-guarded, but `profiler_dealloc` — which calls `flush_unmatched()` and `clearEntries()` — is **not**. Tearing a profiler down therefore races (and use-after-frees) against callbacks still in flight on other threads. Confirmed under TSan (exit 66), including a reproducible **use-after-free SEGV**.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Modules/_lsprof.c` stores all of a profiler's call-tracking state inside the `ProfilerObject` itself:

```c
typedef struct {
    PyObject_HEAD
    rotating_node_t *profilerEntries;          /* tree of ProfilerEntry     */
    ProfilerContext *currentProfilerContext;   /* the live call stack       */
    ProfilerContext *freelistProfilerContext;
    int flags;                                 /* POF_ENABLED, ...           */
    ...
} ProfilerObject;
```

Since CPython 3.12, `cProfile`/`profiling.tracing` drives this through **`sys.monitoring`** using a single, process-global tool id (`PY_MONITORING_PROFILER_ID == 2`). `enable()` calls `sys.monitoring.set_events(...)`, which turns the events on for the **whole interpreter** — so once *any* `Profiler` is enabled, *every* thread that executes Python fires that one profiler's callbacks (`_pystart_callback`/`_pyreturn_callback`/...) into its single shared `currentProfilerContext` stack and `profilerEntries` tree.

The per-call callbacks and `enable`/`disable`/`clear` are all decorated `@critical_section`, so they serialize against each other on the profiler object. But **`profiler_dealloc()` holds no critical section**, and it runs `flush_unmatched(self)` (walks/frees the `currentProfilerContext` chain) and `clearEntries(self)` (frees the `profilerEntries` tree) directly:

```c
static void
profiler_dealloc(PyObject *op)
{
    ProfilerObject *self = ProfilerObject_CAST(op);
    PyObject_GC_UnTrack(self);
    if (self->flags & POF_ENABLED) {                 /* still enabled at teardown */
        PyThreadState *tstate = _PyThreadState_GET();
        _PyEval_SetProfile(tstate, NULL, NULL);      /* only unhooks THIS thread   */
    }
    flush_unmatched(self);                           /* :984  reads/frees ctx stack */
    clearEntries(self);                              /* :985  frees entries tree     */
    ...
}
```

If the object is torn down while another thread is mid-callback, the teardown reads/frees the exact state the callback is reading/writing. TSan reports the race on `self->currentProfilerContext` between `flush_unmatched`'s loop guard (read) and a callback's write.

## Reproducer

`repro.py` (stdlib only): N threads run deep non-tail recursion (a long `PY_START` burst down, then a long `PY_RETURN` cascade up) to keep the shared profiler busy, while a few threads rapidly create/enable/disable/drop a fresh `Profile`; each drop deallocs it (`flush_unmatched` at `:984`) while a gen thread is mid-callback.

```python
import sys, threading, time
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"
from profiling.tracing import Profile      # == cProfile.Profile == _lsprof.Profiler

STOP = False
def rec(n):
    if n > 0:
        rec(n - 1)
    return n
def busy():
    while not STOP:
        rec(60)
def churn():
    while not STOP:
        p = Profile()
        try:
            p.enable()
        except ValueError:          # another churn thread holds the global tool id
            continue
        p.disable()
        del p                       # -> profiler_dealloc -> flush_unmatched

ts  = [threading.Thread(target=busy)  for _ in range(8)]
ts += [threading.Thread(target=churn) for _ in range(3)]
for t in ts: t.start()
time.sleep(20); STOP = True
for t in ts: t.join()
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report

### Seeded face — `flush_unmatched` vs `ptrace_leave_call` (from the fleet vehicle)

The original fleet crash caught the profiler being deallocated **while still enabled** (via an exception/traceback teardown on T5), racing a live `PY_RETURN` callback on T6 that was mid-import:

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x... by thread T5:
    #0 flush_unmatched      Modules/_lsprof.c:866:18   (while (pObj->currentProfilerContext))
    #1 profiler_dealloc     Modules/_lsprof.c:984:5
    #2 subtype_dealloc      Objects/typeobject.c:2876
    ...  (BaseException_dealloc -> tb_dealloc -> frame_dealloc -> Py_DECREF)

  Previous write of size 8 at 0x... by thread T6:
    #0 ptrace_leave_call    Modules/_lsprof.c:416:38   (pObj->currentProfilerContext = pContext->previous)
    #1 _lsprof_Profiler__pyreturn_callback_impl  Modules/_lsprof.c:677:5
    #2 _lsprof_Profiler__pyreturn_callback        Modules/clinic/_lsprof.c.h:159
    ...  call_one_instrument -> _Py_call_instrumentation_arg  (PY_RETURN during import_find_and_load)
SUMMARY: ThreadSanitizer: data race Modules/_lsprof.c:866:18 in flush_unmatched
```

### Confirmed face — `flush_unmatched` vs `ptrace_enter_call`/`initContext` (this repro, exit 66)

The minimal repro deterministically reproduces the **same read site on the same field** (`flush_unmatched` at `:866`, `self->currentProfilerContext`), paired with the **enter-side** writer of that field (`initContext:314`, the `pObj->currentProfilerContext = self` in a `PY_START` callback) rather than the seeded leave-side writer. Same object field, same race, sibling writer (see *Notes* for why the leave-side writer is a rarer manifestation):

```
WARNING: ThreadSanitizer: data race (pid=1982023)
  Write of size 8 at 0x7fffd8150208 by thread T10:
    #0 initContext          Modules/_lsprof.c:314:34   (pObj->currentProfilerContext = self)
    #1 ptrace_enter_call    Modules/_lsprof.c:394:5
    #2 _lsprof_Profiler__pystart_callback_impl  Modules/_lsprof.c:632:5
    ...  call_one_instrument -> call_instrumentation_vector -> _Py_call_instrumentation (PY_START)
    #19 thread_run          Modules/_threadmodule.c:388:21

  Previous read of size 8 at 0x7fffd8150208 by thread T9:
    #0 flush_unmatched      Modules/_lsprof.c:866:18   (while (pObj->currentProfilerContext))
    #1 profiler_dealloc     Modules/_lsprof.c:984:5
    ...
SUMMARY: ThreadSanitizer: data race Modules/_lsprof.c:314:34 in initContext
```

The race is **not value-benign**. The same repro also reproduces a hard **use-after-free SEGV**: a callback runs `initContext` -> `call_timer` on a profiler whose memory was already freed by a concurrent `profiler_dealloc`, so the freed `self->externalTimer`/type slot is dereferenced:

```
ThreadSanitizer: SEGV on unknown address ... (READ memory access)
    #1 _Py_TYPE_impl        Include/object.h:234:16
    #4 _PyObject_CallNoArgs Include/internal/pycore_call.h:160
    #5 CallExternalTimer    Modules/_lsprof.c:100:9
    #6 call_timer           Modules/_lsprof.c:135:16
    #7 initContext          Modules/_lsprof.c:325:16
    #8 ptrace_enter_call    Modules/_lsprof.c:394:5
    #9 _lsprof_Profiler__pystart_callback_impl  Modules/_lsprof.c:632:5
```

Every run exits 66 (a race is found on essentially every invocation); the specific `flush_unmatched:866` face appears in a large fraction of runs.

## Root cause

`sys.monitoring` events are **interpreter-global**, so a single enabled profiler receives callbacks from all threads. Two problems compound:

1. **Unsynchronized teardown.** Every callback and `enable`/`disable`/`clear` is `@critical_section` on the profiler (Argument Clinic emits `Py_BEGIN_CRITICAL_SECTION(self)`), so they serialize against one another. `profiler_dealloc` (`Modules/_lsprof.c:972`) is **not** guarded and calls `flush_unmatched(self)` (`:984`) and `clearEntries(self)` (`:985`), which read and free `self->currentProfilerContext` (`:866`, `:872`) and `self->profilerEntries` (`:292`). Nothing in `profiler_dealloc` clears the interpreter's monitoring events for the tool, so callbacks can still be dispatched into the object on other threads while it is being freed. The `_PyEval_SetProfile(tstate, NULL, NULL)` at `:978` only unhooks the *current* thread's legacy profile hook — it does not stop the global `sys.monitoring` events that actually drive this profiler. Result: `flush_unmatched`'s `while (pObj->currentProfilerContext)` (`:866`, read) races the callbacks' writes to the same field — `ptrace_leave_call` at `:416` (`= pContext->previous`, seeded) and `initContext` at `:314` (`= self`, confirmed) — and, since teardown *frees* the `ProfilerContext`/`ProfilerEntry` blocks and can free the object itself, this is a use-after-free (the reproduced SEGV).

2. **Shared single-threaded state model.** `self->currentProfilerContext` is one linked-list "call stack" and `self->profilerEntries` one tree, designed for strictly nested single-threaded call/return. Under the global monitoring model, concurrent threads push (`initContext:314`) and pop (`ptrace_leave_call:416`, `Stop:335`) that one stack, and add to (`newProfilerEntry`/`RotatingTree_Add`) / free (`clearEntries`) the one tree. Even setting the dealloc aside, the object's mutable state is not safe for the concurrent callback delivery that an enabled global tool implies.

The relevant field, `self->currentProfilerContext`, is the exact 8-byte location TSan flags in both the seeded and confirmed reports (same address, e.g. `0x7fffd8150208`).

## Impact / severity

Medium-high. It is a genuine **use-after-free** (crash reproduced as a TSan SEGV), not a value-benign cache race like TSAN-0005 — the teardown frees `ProfilerContext`/`ProfilerEntry` memory and the object while another thread dereferences it. Triggering it requires (a) a profiler enabled while other threads run Python (which a single `enable()` in any multithreaded free-threaded program produces, since the tool is global) and (b) the profiler being torn down (or `disable`d and dropped) while a callback is in flight. It cannot corrupt anything on a GIL build (the callbacks and dealloc cannot truly overlap there), so this is specific to free-threading.

Whether "share one `Profiler` across threads" is *supported* is debatable — classic `cProfile` was effectively per-thread (legacy `sys.setprofile` only affects the calling thread). But the 3.12+ monitoring rewrite makes a single enabled profiler implicitly interpreter-wide, so the shared-state exposure is not something the user opts into. Regardless of that debate, the **unsynchronized teardown** (item 1) is a clear FT-safety defect: callbacks are already `@critical_section`-guarded, and `profiler_dealloc` breaking that contract by freeing the same state with no synchronization (and while events are still live) is the actionable bug.

## Suggested fix

- **Guard teardown like everything else, after unhooking.** Before freeing internal state in `profiler_dealloc`, disable the tool globally (clear its `sys.monitoring` events / unregister callbacks) so no callback can be dispatched into the object, then run `flush_unmatched`/`clearEntries` under the object's critical section (or after a stop-the-world / synchronization point). Today `disable()` does the unhook+flush under the critical section correctly; `profiler_dealloc` should not open-code an *un*guarded flush that also skips the global unhook.
- **Close the borrowed-reference window in dispatch.** The seeded face requires a callback to still be executing on the object as its last reference is dropped — i.e. the monitoring dispatch holds a borrowed reference to the profiler/callback across the call. Ensuring the dispatched tool callback is kept alive across the call (or that teardown waits for in-flight callbacks) removes the use-after-free.
- **Longer term**, treat the profiler's `currentProfilerContext`/`profilerEntries`/freelist as shared state under the global-tool model: either make the call-stack state thread-local, or protect all mutators (including dealloc) uniformly, or explicitly restrict a profiler to a single thread and reject concurrent enablement.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, module `profiling.tracing` (the new home of `cProfile`).

On the exact signature: the confirmed capture pairs `flush_unmatched` (`:866`, read of `currentProfilerContext`) with the **enter-side** writer `initContext:314` (`ptrace_enter_call`/`_pystart_callback`) rather than the seeded **leave-side** writer `ptrace_leave_call:416` (`_pyreturn_callback`). Both are writers of the *same* field in the *same* race. The leave-side writer is a strictly rarer manifestation: `ptrace_leave_call` short-circuits at `:409` (`if (pContext == NULL) return;`) when the context stack is empty, so it only writes `:416` when the profiler is torn down **while still enabled with a non-empty live stack** — which needs the dealloc to happen without a preceding `disable()` (whose flush empties the stack) plus a borrowed-reference-across-callback window. That window (the fleet vehicle hit it once via exception-traceback teardown during an import) was not deterministically forcible from Python — an enabled profiler is kept alive by `sys.monitoring`'s strong references to its bound-method callbacks, so reaching "deallocated while enabled with a live return callback" depends on an FT refcount race in the monitoring dispatch. In ~200+ trials the minimal repro reliably reproduced the identical race on the identical field via the enter-side writer, plus the use-after-free SEGV, but did not re-capture the exact `ptrace_leave_call` frame. The underlying bug (unsynchronized profiler teardown racing concurrent monitoring callbacks) is confirmed; the specific partner frame differs.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
