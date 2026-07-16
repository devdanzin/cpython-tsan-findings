# Data race: a frame's `f_trace` is written by the trace trampoline without the frame's critical section (`sysmodule.c:1125` vs `frameobject.c:1155`)

*`trace_trampoline` stores the local trace function back into `frame->f_trace` (`Py_XSETREF`) on every traced event, holding **no** lock. The Python-level `frame.f_trace` / `frame.f_trace_opcodes` accessors are all `@critical_section`-decorated, so when a second thread reaches into a running thread's frame (e.g. via `sys._current_frames()`) and sets `f_trace_opcodes`, its critical section reads `self->f_trace` while the owning thread races it with an unlocked write. The lock on the setter protects nothing because the trampoline bypasses it.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

Each `PyFrameObject` carries per-frame legacy-tracing state: `PyObject *f_trace` (the frame's local trace function) plus the `f_trace_lines` / `f_trace_opcodes` flags (`Include/internal/pycore_frame.h`). Under `sys.settrace`, whenever a traced event fires in a frame, the interpreter calls `trace_trampoline`, which — if the trace callback returns a non-`None` value — stores that value back into the frame:

```c
// Python/sysmodule.c : trace_trampoline
if (result != Py_None) {
    Py_XSETREF(frame->f_trace, result);   /* :1125  WRITE (no critical section) */
}
```

The Python-visible accessors of that same state are all lock-protected. `frame.f_trace_opcodes`'s setter is `@critical_section` and reads `f_trace`:

```c
// Objects/frameobject.c : frame_trace_opcodes_set_impl  (@critical_section)
if (value == Py_True) {
    self->f_trace_opcodes = 1;
    if (self->f_trace) {                    /* :1155  READ under the frame's critical section */
        return _PyEval_SetOpcodeTrace(self, true);
    }
}
```

A frame is conceptually owned by the thread executing it, but a reference to a *running* thread's frame is reachable from other threads through the public API (`sys._current_frames()`, a stored `sys._getframe()`, a trace callback's `frame` argument…). When thread B does `victim_frame.f_trace_opcodes = True` while thread A (the owner) is executing traced code in that frame, B's `@critical_section` read of `self->f_trace` races A's unlocked `Py_XSETREF(frame->f_trace, …)` write. Because A never takes the frame's critical section, the lock on B's side provides no mutual exclusion — this is a genuine, TSan-reported data race on `frame->f_trace`.

## Reproducer

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NTRACE = 4
NMUT   = 4
ROUNDS = 400
LINES  = 800

stop = threading.Event()
start = threading.Barrier(NTRACE + NMUT)

def tracer(frame, event, arg):
    return tracer                # returning a callable -> trampoline re-stores frame->f_trace

def busy():
    x = 0
    for _ in range(LINES):
        x += 1                   # one LINE event per iteration -> one trampoline f_trace write
    return x

def tracer_worker():
    start.wait()
    sys.settrace(tracer)         # per-thread: traces only THIS thread's frames
    try:
        for _ in range(ROUNDS):
            busy()
    finally:
        sys.settrace(None)

def mutator_worker():
    start.wait()
    while not stop.is_set():
        for f in list(sys._current_frames().values()):
            try:
                f.f_trace_opcodes = True     # frame_trace_opcodes_set_impl reads self->f_trace
                f.f_trace_opcodes = False
            except Exception:
                pass

ts = [threading.Thread(target=tracer_worker, name="tsan_t%d" % i) for i in range(NTRACE)]
ms = [threading.Thread(target=mutator_worker, name="tsan_m%d" % i) for i in range(NMUT)]
for t in ts + ms: t.start()
for t in ts: t.join()
stop.set()
for m in ms: m.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

Exact-signature run (trimmed to the two racing stanzas):

```
WARNING: ThreadSanitizer: data race (pid=2173947)
  Write of size 8 at 0x7fffd4180940 by thread T1:
    #0 trace_trampoline              Python/sysmodule.c:1125:9   (Py_XSETREF(frame->f_trace, result))
    #1 _Py_call_instrumentation_line Python/instrumentation.c:1375:27
    #2 _PyEval_EvalFrameDefault      Python/generated_cases.c.h:7806   (INSTRUMENTED_LINE)
    ...
    #25 thread_run                   Modules/_threadmodule.c:388

  Previous read of size 8 at 0x7fffd4180940 by thread T8:
    #0 frame_trace_opcodes_set_impl  Objects/frameobject.c:1155:19   (if (self->f_trace))
    #1 frame_trace_opcodes_set       Objects/clinic/frameobject.c.h:269
    #2 getset_set                    Objects/descrobject.c:250
    #3 _PyObject_GenericSetAttrWithDict Objects/object.c:2049
    ...
    #29 thread_run                   Modules/_threadmodule.c:388

SUMMARY: ThreadSanitizer: data race Python/sysmodule.c:1125:9 in trace_trampoline
```

Reproduces deterministically in ~0.6 s, exit code 66 (3/3 runs). The same racing address is written by the tracer thread and read by the mutator thread; no "Location is" line is emitted because the `PyFrameObject` lives in an obmalloc (mmap'd) arena that TSan does not track as a named heap block.

### Sibling faces of the same race

Depending on which thread TSan catches first, the same run also surfaces:

* `frame_trace_opcodes_set_impl:1155` (read) ↔ `trace_trampoline:1125` (write) — the seeded signature (above).
* `sys_trace_instruction_func` `Python/legacy_tracing.c:305:41` reads `frame->f_trace_opcodes` (the `char` flag adjacent to `f_trace`) on the eval hot path, racing the mutator's `self->f_trace_opcodes = 1/0` write in `frame_trace_opcodes_set_impl`.

All are the same underlying defect: **per-frame legacy-trace state (`f_trace`, `f_trace_opcodes`, `f_trace_lines`) is mutated on the interpreter's tracing hot path without the per-object lock that the `@critical_section` Python accessors take.**

## Root cause

`trace_trampoline` (`Python/sysmodule.c:1101`) runs on the frame's owning thread every time a traced event fires. On lines 1120 and 1125 it mutates `frame->f_trace`:

```c
if (result == NULL) { ...; Py_CLEAR(frame->f_trace); return -1; }  /* :1120 */
if (result != Py_None) { Py_XSETREF(frame->f_trace, result); }     /* :1125 */
```

Neither mutation is inside a critical section. In contrast, the four Python-level accessors of this state are all `@critical_section` (they lock the frame object):

* `frame_trace_get_impl` / `frame_trace_set_impl` — `Objects/frameobject.c:1855` / `:1873` (both `Py_XSETREF(self->f_trace, …)` / read).
* `frame_trace_opcodes_get_impl` / `frame_trace_opcodes_set_impl` — `Objects/frameobject.c:1131` / `:1144`.

So CPython clearly intends cross-thread access to a frame's trace state to be serialised by the frame's per-object lock — otherwise the `@critical_section` decorators would be pointless. The trampoline (and the eval-loop reads in `instrumentation.c:1366` / `legacy_tracing.c:190,305`) violate that contract by touching the same fields with no lock and no atomics. Two threads sharing a live frame therefore race.

Because the write path is `Py_XSETREF` (which `Py_DECREF`s the previous `f_trace` and stores a new reference), the hazard is not purely value-benign: if a second thread also *writes* `f_trace` — e.g. `frame.f_trace = fn` via `frame_trace_set_impl`, or a second tracer's trampoline — the two unsynchronised `Py_XSETREF`s can double-decref / leak the object they replace, i.e. a refcount-corruption / use-after-free window, not just a torn read. The seeded signature happens to pair the write with a truthiness *read*, which is value-benign on its own, but the field is genuinely unprotected for writes too.

## Impact / severity

**Low–moderate.** It is a real data race (undefined behaviour in C) on `frame->f_trace`, with latent refcount-corruption / use-after-free potential when a frame's trace state is written from two threads concurrently. In practice it requires deliberately reaching into another thread's *currently executing* frame (via `sys._current_frames()` or a stored/`f_back`-reachable frame reference) while that thread is traced — an unusual pattern; ordinary debuggers such as `bdb` only touch the current thread's own frames, so they do not hit it. Hence low real-world exploitability, but a clear FT-correctness gap given the surrounding code already opted into per-object locking.

## Suggested fix

Make the internal trace-state mutations honour the same per-object lock as the Python accessors. In `trace_trampoline`, wrap the `f_trace` mutations in the frame's critical section:

```c
Py_BEGIN_CRITICAL_SECTION(frame);
if (result != Py_None) {
    Py_XSETREF(frame->f_trace, result);
} else {
    Py_DECREF(result);
}
Py_END_CRITICAL_SECTION();
```

(and likewise for the `Py_CLEAR(frame->f_trace)` error path at `:1120`). For the `char` flag reads on the eval hot path (`instrumentation.c:1366` `f_trace_lines`, `legacy_tracing.c:305` `f_trace_opcodes`), where taking a critical section per instruction would be too costly, use `FT_ATOMIC_LOAD`/`STORE` on the flags so those accesses are at least well-defined (they are single-byte, value-idempotent). A plain relaxed atomic on the `f_trace` *pointer* alone is **not** sufficient, because the field participates in refcounting via `Py_XSETREF` — the critical section (or an equivalent atomic-refcount swap) is required to make the paired decref/store safe.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`); the seeding vehicle was a `bdb` target (`inst-04/.../bdb-…-tsanNEW`) whose stress harness ran `sys.settrace`/`bdb` across many threads. This is the legacy `sys.settrace` layer bridged onto the PEP 669 (`sys.monitoring`) instrumentation machinery; the `@critical_section` decorators on the frame accessors are recent free-threading hardening, and this race is where the *internal* tracing path was not brought under the same discipline. It resembles the broader "per-object state not yet fully FT-safe" class tracked by the gh-116738 free-threading audit; `Objects/frameobject.c` / the trace trampoline should be checked against that list. No internet access here to confirm a specific existing issue.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
