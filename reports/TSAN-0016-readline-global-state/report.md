# Data race: `readline.get_completer()` reads the module-state completer pointer without the critical section its setter holds (`readline.c:899` vs `readline.c:480`)

*`readline` stores its completer/hook callbacks in per-module state (`readlinestate.completer`, a `PyObject*`). `readline.set_completer()` is `@critical_section` and writes the pointer under the module lock via `set_hook()`/`Py_XSETREF` (`readline.c:480`). `readline.get_completer()` is **not** `@critical_section` and reads `state->completer` with a plain, unsynchronized load (`readline.c:899`). A getter thread racing a setter thread is a TSan data race on CPython's own module-state pointer — and, because the getter borrows-then-`Py_NewRef()`s a pointer the setter may concurrently drop the last reference to, a latent use-after-free.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Modules/readline.c` keeps its Python callbacks in heap-allocated per-module state (single-phase init, `PyModule_Create` with `m_size = sizeof(readlinestate)`):

```c
typedef struct {
  PyObject *completion_display_matches_hook;
  PyObject *startup_hook;
  PyObject *pre_input_hook;
  PyObject *completer;      /* the racing field */
  PyObject *begidx;
  PyObject *endidx;
} readlinestate;
```

Every **setter** is generated with the Argument Clinic `@critical_section` directive, so its write takes the module's per-object critical section. The **getters** for the pointer fields are not, so they read the shared pointer with no lock and no atomic:

```c
/* setter — @critical_section (readline.c:867) */
static PyObject *
readline_set_completer_impl(PyObject *module, PyObject *function)
{
    readlinestate *state = get_readline_state(module);
    return set_hook("completer", &state->completer, function);   /* :885 */
}

static PyObject *
set_hook(const char *funcname, PyObject **hook_var, PyObject *function)
{
    ...
    Py_XSETREF(*hook_var, Py_NewRef(function));                  /* :480  WRITE */
    ...
}

/* getter — NO @critical_section (readline.c:889) */
static PyObject *
readline_get_completer_impl(PyObject *module)
{
    readlinestate *state = get_readline_state(module);
    if (state->completer == NULL) {                              /* :899  READ */
        Py_RETURN_NONE;
    }
    return Py_NewRef(state->completer);                          /* :902  READ + incref */
}
```

Two threads — one calling `readline.set_completer(fn)`, one calling `readline.get_completer()` — race on the single `state->completer` pointer word. The setter holds the module critical section; the getter holds nothing, so there is no happens-before between the write and the read. TSan reports it as a data race (8-byte read at `:899` vs 8-byte write at `:480`).

## Reproducer

```python
import sys, threading, readline
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 6                       # total worker threads
ROUNDS = 4000
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def setter():
    for _ in range(ROUNDS):
        enter.wait()
        fn = lambda text, state: None      # fresh each round -> real write + refcount drop
        readline.set_completer(fn)
        readline.set_completer(None)
        leave.wait()

def getter():
    for _ in range(ROUNDS):
        enter.wait()
        readline.get_completer()           # reads state->completer unsynchronized (:899)
        readline.get_completer()
        leave.wait()

ts = [threading.Thread(target=setter if i % 2 == 0 else getter) for i in range(NT)]
for t in ts: t.start()
for _ in range(ROUNDS):
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 \
  TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, build `bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2148588)
  Write of size 8 at 0x7fffb6041018 by thread T5:
    #0 set_hook                    Modules/readline.c:480:9   (Py_XSETREF(*hook_var, Py_NewRef(function)))
    #1 readline_set_completer_impl Modules/readline.c:885:12
    #2 readline_set_completer      Modules/clinic/readline.c.h:632:20
    ...
    #30 thread_run                 Modules/_threadmodule.c:388:21

  Previous read of size 8 at 0x7fffb6041018 by thread T2:
    #0 readline_get_completer_impl Modules/readline.c:899:16  (if (state->completer == NULL))
    #1 readline_get_completer      Modules/clinic/readline.c.h:654:12
    ...
    #29 thread_run                 Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/readline.c:480:9 in set_hook
```

Reproduces deterministically (exit 66) on every run. The two racing frames are always the same pair — `set_hook` (`readline.c:480`, write) and `readline_get_completer_impl` (`readline.c:899`, read) — matching the seeded signature exactly; which one TSan prints in the `SUMMARY` line just depends on which thread it caught first (runs alternate between `readline.c:899 in readline_get_completer_impl` and `readline.c:480 in set_hook`).

## Root cause

The completer/hook pointers live in CPython's own module state, and the module's writers were made free-threading-safe with `@critical_section` but the readers were not. Concretely:

- `set_completer`, `set_startup_hook`, `set_pre_input_hook`, `set_completion_display_matches_hook`, `set_completer_delims` all carry `@critical_section` (readline.c:492/530/556/650/867), so their `Py_XSETREF` writes run under the module lock.
- `get_completer` (readline.c:889), `get_pre_input_hook` (readline.c:581), `get_begidx` (readline.c:618) and `get_endidx` (readline.c:634) carry **no** `@critical_section`, so their `state->...` reads take no lock.

A lock on the writer side alone does not order a lockless reader against it — the reader observes the pointer with no synchronization, which is exactly the data race TSan flags. Beyond the bare race, the getter does `Py_NewRef(state->completer)`: it borrows the pointer and then increfs it. If a setter concurrently runs `Py_XSETREF(*hook_var, new)` — which stores `new` and then `Py_XDECREF`s the old object — and the old completer's last reference was the one in module state, the object is freed while the getter is about to incref it. Because the load is non-atomic the compiler is free to cache it, so this is a genuine (if narrow) use-after-free window, not merely a benign value race.

### Scope: this is CPython module state, not libreadline's globals

The racing memory is `readlinestate.completer`, a `PyObject*` in the block returned by `PyModule_GetState()` — memory CPython allocates and owns. It is **not** one of GNU libreadline's internal C globals (e.g. `rl_line_buffer`, `rl_completer_word_break_characters`, `rl_attempted_completion_function`). So this is a real, self-contained CPython free-threading bug in CPython's C code, fixable entirely within `Modules/readline.c` — distinct from the "thread-unsafe C library" class tracked under gh-127081 (locale/`setlocale` and friends), which is about non-reentrant libc/foreign-library state that CPython cannot fix by locking its own fields. (Separately, GNU libreadline itself is a single-terminal, thread-unsafe interactive library; that is an orthogonal design fact and not what TSan flagged here.)

## Impact / severity

Low-to-moderate. In the common case it is value-benign and crash-free: the pointer is a single aligned 8-byte word, and the previous completer almost always has other live references. The real hazard is the borrowed-reference/use-after-free window described above, which requires a getter to race a setter that drops the completer's last reference — reachable from pure Python by concurrently calling `readline.set_completer()` and `readline.get_completer()` on the shared module. It also violates the reasonable expectation that a plain getter is thread-safe, and is a clean TSan finding on CPython-owned state. Consistent with other TSan module-state findings in this campaign.

## Suggested fix

Give the getters the same per-module critical section their setters already use, so reads and writes are mutually ordered and the borrowed-ref incref happens under the lock. Minimal Argument Clinic change (mirrors what the setters do):

```
/*[clinic input]
@critical_section          <-- add
readline.get_completer

Get the current completer function.
[clinic start generated code]*/
```

Apply the same `@critical_section` to `readline.get_pre_input_hook`, `readline.get_begidx`, and `readline.get_endidx`, whose reads race the same way against their writers (`set_pre_input_hook` and the `on_completion` callback that updates `begidx`/`endidx` at readline.c:1330-1331). A pure `FT_ATOMIC` load/store on the pointer fields would silence TSan but would not by itself close the borrowed-ref window, so the critical section (matching the existing setter side) is the correct, consistent fix.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`). The file has already partially adopted free-threading primitives — `_history_length` uses `FT_ATOMIC_{LOAD,STORE}_INT_RELAXED` (readline.c:450/467) and the setters use `@critical_section` — so this is an incomplete-migration gap on the reader side rather than an untouched module. The same asymmetry (protected setter, unprotected getter) should be audited across every `state->` field: `completer`, `pre_input_hook`, `begidx`, `endidx` all have lockless getters today.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
