# Data race: `sys.monitoring.use_tool_id()` registers a tool with an unsynchronized check-then-act on the interpreter-global tool table (`instrumentation.c:2190`/`:2194`)

*`monitoring_use_tool_id_impl` tests `interp->monitoring_tool_names[tool_id] != NULL` and then stores `Py_NewRef(name)` into that same slot with no lock, critical section, stop-the-world, or atomic. Two threads calling `sys.monitoring.use_tool_id()` concurrently race on the interpreter-global registry: a plain read of the slot vs a plain write. Beyond the TSan-reported race it is a genuine TOCTOU — both threads can pass the "already in use" guard, defeating the single-owner invariant and leaking a reference to the losing thread's name.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Summary

`sys.monitoring` keeps a per-interpreter table of registered tool names, `interp->monitoring_tool_names[PY_MONITORING_TOOL_IDS]` (`Include/internal/pycore_interp_structs.h:1039`). `monitoring.use_tool_id(tool_id, name)` claims a free slot with a check-then-act that has no synchronization:

```c
static PyObject *
monitoring_use_tool_id_impl(PyObject *module, int tool_id, PyObject *name)
{
    ...
    PyInterpreterState *interp = _PyInterpreterState_GET();
    if (interp->monitoring_tool_names[tool_id] != NULL) {     /* :2190  read  */
        PyErr_Format(PyExc_ValueError, "tool %d is already in use", tool_id);
        return NULL;
    }
    interp->monitoring_tool_names[tool_id] = Py_NewRef(name);  /* :2194  write */
    Py_RETURN_NONE;
}
```

Two threads registering the *same* free `tool_id` concurrently produce a data race on `interp->monitoring_tool_names[tool_id]` (plain read at `:2190` vs plain write at `:2194`). TSan places the racing address in the global `_PyRuntime` object (the interpreter state is embedded there). This is not the benign idempotent-cache pattern of TSAN-0005: the two operations disagree, so the race additionally corrupts the tool-registry invariant (see Impact).

## Reproducer

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# sys.monitoring.use_tool_id(tool_id, name) does an unsynchronized check-then-act on the
# interpreter-global registry interp->monitoring_tool_names[tool_id]:
#     if (interp->monitoring_tool_names[tool_id] != NULL) { raise; }   # :2190  read
#     interp->monitoring_tool_names[tool_id] = Py_NewRef(name);        # :2194  write
# Many threads racing to claim the SAME free tool id read the slot while one writes it.
mon = sys.monitoring
TOOL_ID = 3                 # any 0..5; not reserved
NT = 8
ROUNDS = 6000

enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker():
    for _ in range(ROUNDS):
        enter.wait()            # all workers released together onto a freshly-freed slot
        try:
            mon.use_tool_id(TOOL_ID, "t")   # read :2190 races the winner's write :2194
        except ValueError:
            pass                # "tool 3 is already in use" -> lost the race, expected
        leave.wait()

ts = [threading.Thread(target=worker, name=f"w{i}") for i in range(NT)]
for t in ts:
    t.start()
for r in range(ROUNDS):
    mon.free_tool_id(TOOL_ID)   # NULL the slot so the next round starts from a free id
    enter.wait()
    leave.wait()
for t in ts:
    t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2171512)
  Read of size 8 at 0x5555560b1018 by thread T1:
    #0 monitoring_use_tool_id_impl  Python/instrumentation.c:2190:9   (if (interp->monitoring_tool_names[tool_id] != NULL))
    #1 monitoring_use_tool_id       Python/clinic/instrumentation.c.h:33:20
    #2 _Py_BuiltinCallFast_StackRef Python/ceval.c:817:11
    ...
    #26 thread_run                  Modules/_threadmodule.c:388:21

  Previous write of size 8 at 0x5555560b1018 by thread T5:
    #0 monitoring_use_tool_id_impl  Python/instrumentation.c:2194:44  (interp->monitoring_tool_names[tool_id] = Py_NewRef(name))
    #1 monitoring_use_tool_id       Python/clinic/instrumentation.c.h:33:20
    #2 _Py_BuiltinCallFast_StackRef Python/ceval.c:817:11
    ...
    #26 thread_run                  Modules/_threadmodule.c:388:21

  Location is global '_PyRuntime' of size 424320 at 0x55555604df40 (python+0xb5d018)

SUMMARY: ThreadSanitizer: data race Python/instrumentation.c:2190:9 in monitoring_use_tool_id_impl
```

Reproduces deterministically (exit **66**) in a few seconds; does not crash in the `use`-vs-`use` case. The confirmed signature matches the fuzzer-seeded one exactly (same two functions, read `:2190` vs write `:2194`). The seed vehicle reached the same site indirectly through `_lsprof.Profiler.enable()` -> `PyObject_CallMethod(... "use_tool_id" ...)`; the repro calls the API directly.

## Root cause

`interp->monitoring_tool_names[]` is per-interpreter global state, but the four `sys.monitoring` tool-registry entry points mutate/inspect it with plain (non-atomic, unlocked) accesses:

- `monitoring_use_tool_id_impl` — read `:2190`, write `:2194`
- `monitoring_clear_tool_id_impl` — read `:2216`
- `monitoring_free_tool_id_impl` — read `:2242`, `Py_CLEAR` (decref+NULL) `:2248`
- `monitoring_get_tool_impl` — read `:2270`

None of them takes a lock, enters a critical section, or brackets the access with stop-the-world. This is striking because the *same file* already establishes the correct idiom for interpreter-global monitoring state: `_PyMonitoring_ClearToolId` / `_PyMonitoring_SetEvents` wrap their global mutations in `_PyEval_StopTheWorld(interp)` ... `_PyEval_StartTheWorld(interp)` (e.g. `instrumentation.c:2122`-`2147`), and per-code-object state is guarded by `LOCK_CODE()` (`Py_BEGIN_CRITICAL_SECTION`). The tool-name table was simply never brought under any of these.

## Impact / severity

**Severity: medium** (a real free-threading correctness bug, not the value-benign class of TSAN-0005).

1. **Data race** on `monitoring_tool_names[tool_id]` — plain read vs plain write (TSan-confirmed).
2. **Broken single-owner invariant / reference leak**: when two threads both observe `NULL` at `:2190`, both execute `Py_NewRef(name)` and store at `:2194`. The second store overwrites the first pointer with no `Py_DECREF`, so the first `name` reference is leaked, and both callers return success believing they own the tool id — exactly the state the `"tool %d is already in use"` guard exists to prevent. A debugger and a coverage tool can each think they hold the same id.
3. **Use-after-free / double-free window across siblings**: `free_tool_id`'s `Py_CLEAR(interp->monitoring_tool_names[tool_id])` at `:2248` decrefs and NULLs the slot with no synchronization against a concurrent `use_tool_id`/`get_tool`/`clear_tool_id` reading the same slot — a classic drop-while-reading hazard on a `PyObject*`, which can drop the last reference under another thread's borrowed pointer.

No crash was observed in the captured `use`-vs-`use` scenario, but the invariant break and the `free`/`get` hazard make this more than cosmetic. It is in scope for the same reason as TSAN-0011 (audit hooks): interpreter-global instrumentation state that profilers/debuggers/coverage tools configure, plausibly from more than one thread during startup.

## Suggested fix

Bring the tool-registry accessors under the synchronization already used elsewhere in the file. Two workable options:

- **Stop-the-world (matches the in-file precedent).** Bracket the check-then-act in `use_tool_id`, `clear_tool_id`, `free_tool_id`, and `get_tool` with `_PyEval_StopTheWorld(interp)` / `_PyEval_StartTheWorld(interp)`, the same pattern `_PyMonitoring_ClearToolId` uses at `instrumentation.c:2122`. Straightforward and consistent, at the cost of a brief pause on each (rare) registration call.

- **A dedicated interpreter mutex.** Add a `PyMutex` to the interpreter's instrumentation state and hold it across every access to `monitoring_tool_names[]` (register/clear/free/get). Lighter weight and sufficient if the table is only ever touched via these entry points.

Either way the whole `monitoring_tool_names[]` array should be treated as shared mutable interpreter state and never read or written without holding the chosen synchronization, so the check and the act become atomic with respect to each other and to `free_tool_id`'s `Py_CLEAR`.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), seed vehicle `fusil-tsan_fleet_02/inst-04` via `_lsprof.Profiler.enable()`. Confirmed unfixed on current `main` (`bcf98ddbc40`, Jul 2026): the four entry points are still plain accesses. Resembles the known "sys.monitoring is not free-threading-safe" class and mirrors TSAN-0011 (unsynchronized interpreter-global instrumentation registry); the registration path here is genuinely unsynchronized. When auditing, fix all four accessors together — fixing only `use_tool_id` would leave the `free`/`get`/`clear` hazards live.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
