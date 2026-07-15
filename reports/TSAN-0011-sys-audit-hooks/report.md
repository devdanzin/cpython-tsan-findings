# Data race: `sys.addaudithook()` lazily creates `interp->audit_hooks` with no lock, racing `should_audit` (`Python/sysmodule.c:540` vs `:239`)

*`sys_addaudithook_impl` lazily initializes the per-interpreter audit-hook list (`interp->audit_hooks = PyList_New(0)`) with a plain, unlocked pointer store the first time any thread adds a hook. `should_audit` reads that same pointer on every audit event with a plain load. On a free-threaded build, the first-ever `sys.addaudithook()` store races with concurrent audit-event reads (and with sibling `addaudithook` stores). Unlike the C-level audit-hook list, which is guarded by `runtime->audit_hooks.mutex`, the Python-level `interp->audit_hooks` list has no synchronization at all — so concurrent first-time initialization can also silently drop a hook, a security-relevant correctness bug in the PEP 578 audit infrastructure.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Python/sysmodule.c` stores each interpreter's Python-level audit hooks in `interp->audit_hooks` (a `PyObject*` list, `NULL` until the first hook is registered). `sys.addaudithook()` fills it lazily:

```c
static PyObject *
sys_addaudithook_impl(PyObject *module, PyObject *hook)
{
    PyThreadState *tstate = _PyThreadState_GET();
    ...
    PyInterpreterState *interp = tstate->interp;
    if (interp->audit_hooks == NULL) {          /* :539  read  */
        interp->audit_hooks = PyList_New(0);    /* :540  WRITE (no lock) */
        if (interp->audit_hooks == NULL) {
            return NULL;
        }
        PyObject_GC_UnTrack(interp->audit_hooks);
    }
    if (PyList_Append(interp->audit_hooks, hook) < 0) {   /* :548 */
        return NULL;
    }
    Py_RETURN_NONE;
}
```

Every audit event first calls `should_audit`, which reads the same pointer:

```c
static int
should_audit(PyInterpreterState *interp)
{
    ...
    return (interp->runtime->audit_hooks.head
            || interp->audit_hooks               /* :239  read */
            || PyDTrace_AUDIT_ENABLED());
}
```

`interp->audit_hooks` for the main interpreter lives inside the `_PyRuntime` global, so TSan reports the racing location as `global '_PyRuntime'`. Nothing takes a lock around the `:540` store or the `:239` read, so under free-threading the first `sys.addaudithook()` call races both with concurrent `should_audit` reads and with other threads' first-time stores.

## Reproducer

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NADD = 24      # threads slamming the first-time lazy-init store (write @540) at once
NAUD = 8       # threads spinning audit events (should_audit read @239)
barrier = threading.Barrier(NADD + NAUD)

def _hook(*a):
    return None

def adder():
    barrier.wait()
    for _ in range(200):
        sys.addaudithook(_hook)          # write interp->audit_hooks (first time) @540

def auditor():
    barrier.wait()
    for _ in range(200000):
        sys.audit("fusil.tsan.test")     # should_audit read of interp->audit_hooks @239

ts = [threading.Thread(target=adder) for _ in range(NADD)]
ts += [threading.Thread(target=auditor) for _ in range(NAUD)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
```

The write at `:540` is a one-shot per process (once the list exists, later `addaudithook` calls only read the pointer and append). The barrier makes many threads slam the first-time lazy init simultaneously while other threads hammer `should_audit`, so the one-shot store reliably collides.

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, TSan build `debug-ft-nojit-tsan`)

```
WARNING: ThreadSanitizer: data race (pid=1967395)
  Write of size 8 at 0x55555607cf00 by thread T2:
    #0 sys_addaudithook_impl Python/sysmodule.c:540:29   (interp->audit_hooks = PyList_New(0))
    #1 sys_addaudithook      Python/clinic/sysmodule.c.h:63:20
    ...
    #29 thread_run           Modules/_threadmodule.c:388:21

  Previous read of size 8 at 0x55555607cf00 by thread T32:
    #0 should_audit          Python/sysmodule.c:239:24   (|| interp->audit_hooks)
    #1 sys_audit_impl        Python/sysmodule.c:572:10
    #2 sys_audit             Python/clinic/sysmodule.c.h:108:20
    ...
    #27 thread_run           Modules/_threadmodule.c:388:21

  Location is global '_PyRuntime' of size 424320 at 0x55555604df40 (python+0xb28f00)

SUMMARY: ThreadSanitizer: data race Python/sysmodule.c:540:29 in sys_addaudithook_impl
```

Reproduces deterministically (exit 66, 3/3 runs) with the exact seeded signature (write `sys_addaudithook_impl:540`, read `should_audit:239`, on `_PyRuntime`). The seeded vehicle hit the read side from an `import` audit event (`import_find_and_load` → `PySys_Audit` → `should_audit`); the reproducer hits it via `sys.audit()` — the same `should_audit` read of the same field.

## Root cause

`interp->audit_hooks` (`Include/internal/pycore_interp_structs.h:994`, `PyObject *audit_hooks`) is per-interpreter shared mutable state read on the hot path of every audit event and mutated by `sys.addaudithook()`. There is no synchronization on either side:

- **Write side** (`sys_addaudithook_impl`, `:539`–`:548`): a plain `NULL`-check + `PyList_New(0)` store + `PyList_Append`, no lock, no atomics.
- **Read side** (`should_audit`, `:239`): a plain load, called from `sys_audit_tstate` on every audit event.

The asymmetry is the tell: the *C-level* global hook list is a `struct { PyMutex mutex; struct _Py_AuditHookEntry *head; } audit_hooks` (`Include/internal/pycore_runtime_structs.h:264`), and `PySys_AddAuditHook` correctly serializes appends to it with `PyMutex_Lock(&runtime->audit_hooks.mutex)` (`:507`). But the *Python-level* per-interpreter `interp->audit_hooks` list — added/iterated by `sys.addaudithook()` / `sys.audit()` — got no equivalent guard. This looks like an incomplete free-threading migration: the mutex that already exists for audit-hook state is simply not applied to the Python-level list.

The in-code comments about a "benign" audit race (`:292`–`:295`, `:439`–`:442`) reason specifically about the *C-level singly-linked list* — appending a node there leaves a reader either seeing it or not, "not [in] an inconsistent state." That reasoning does **not** cover `interp->audit_hooks`, where the racing operation is a *pointer store during lazy init*, not a link append.

## Impact / severity

Low-to-moderate, and worth attention because it is in security-sensitive infrastructure:

- **The flagged pointer race is value-benign on typical hardware** — an aligned 8-byte load/store won't tear on x86-64/aarch64 — but it is a genuine, standards-level data race (UB in C), and the whole point of PEP 703 is to make these well-defined.
- **Silently dropped audit hook (security-relevant correctness).** The missing lock means the lazy init is not just a benign pointer race: two threads can both observe `interp->audit_hooks == NULL` at `:539`, both `PyList_New(0)` at `:540`, and both `PyList_Append` at `:548`. One list wins the pointer; the other is leaked *with the hook that was appended to it silently discarded*. For PEP 578 auditing — used by security monitors that assume a successfully-returned `addaudithook()` means the hook is installed — a dropped hook is a monitoring gap that fails open and silently.
- **Latent list-internals race (potential crash).** Same missing-lock root cause: `sys_audit_tstate` iterates `is->audit_hooks` (`PyObject_GetIter` + `PyIter_Next`, `:315`–`:345`) while another thread's `PyList_Append` (`:548`) can reallocate the list's backing store. That is a separate signature from the one flagged here, but it is the more dangerous consequence of the same unsynchronized field and would surface under sustained concurrent add + audit.

In practice the exposure is limited because audit hooks are usually installed once, at startup, single-threaded. But the API is documented as callable at any time, audit events fire continuously (imports, `exec`/`compile`/`open`, `sys.audit(...)`), and nothing forbids adding hooks from worker threads — so a program that does so hits this.

## Suggested fix

Serialize `interp->audit_hooks` create+append and make the pointer access atomic. The audit-hook mutex already exists for exactly this purpose; extend it to cover the Python-level list:

```c
/* should_audit(): */
return (interp->runtime->audit_hooks.head
        || FT_ATOMIC_LOAD_PTR_RELAXED(interp->audit_hooks)
        || PyDTrace_AUDIT_ENABLED());

/* sys_addaudithook_impl(): guard lazy-init + append */
PyMutex_Lock(&interp->runtime->audit_hooks.mutex);
int err = 0;
if (FT_ATOMIC_LOAD_PTR_RELAXED(interp->audit_hooks) == NULL) {
    PyObject *hooks = PyList_New(0);
    if (hooks == NULL) { err = -1; goto unlock; }
    PyObject_GC_UnTrack(hooks);
    FT_ATOMIC_STORE_PTR_RELAXED(interp->audit_hooks, hooks);
}
err = PyList_Append(interp->audit_hooks, hook);
unlock:
PyMutex_Unlock(&interp->runtime->audit_hooks.mutex);
if (err < 0) return NULL;
Py_RETURN_NONE;
```

Atomics on the pointer remove the flagged `:540`/`:239` race; the mutex removes the double-create-lost-hook bug. For full safety, `sys_audit_tstate` should also take a snapshot of the list (e.g. a strong reference / copy taken under the same mutex) before iterating, so a concurrent `PyList_Append` can't realloc the backing store under a live iterator (`FT_ATOMIC_*` wrappers and `PyMutex` are the standard free-threading tools for this).

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, vehicle `inst-03/python/sys-warning_threadsanitizer_data_race-tsanNEW`. This is the classic "unsynchronized lazy-init of interpreter-global state under free-threading" class; the existing `runtime->audit_hooks.mutex` shows the maintainers already lock the sibling C-level list, so the Python-level list is a plausible oversight rather than a deliberate "don't share" contract. It is genuinely interpreter-global state — there is no per-object alternative a user could avoid sharing. I could not check the upstream tracker (no network) for an existing report; the audit subsystem's free-threading safety may already be under discussion, so this should be cross-referenced before filing.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
