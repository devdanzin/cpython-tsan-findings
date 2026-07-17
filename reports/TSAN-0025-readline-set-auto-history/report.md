# Data race: `readline.set_auto_history()` writes the module-global `should_auto_add_history` flag with no synchronization (`readline.c:843`)

*`readline` gates its "automatically add typed lines to history" behaviour on a file-scope C global `static int should_auto_add_history` (`readline.c:825`). `readline.set_auto_history(bool)` stores into it with a plain, unsynchronized assignment (`readline.c:843`) and its Argument Clinic function is **not** `@critical_section`; the flag is later read, also unsynchronized, in `call_readline()` (`readline.c:1584`). Two threads calling `set_auto_history()` concurrently are a TSan write/write data race on that global — and a `set_auto_history()` writer racing an interactive `input()`/`readline()` reader is the write/read variant. Distinct variable from the `readlinestate` completer/hook pointers of gh-153291; the correct fix here is `FT_ATOMIC` relaxed access, mirroring the sibling `_history_length` global in the same file.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Summary

`Modules/readline.c` keeps a single module-level flag controlling whether lines entered at the readline prompt are auto-appended to history:

```c
static int should_auto_add_history = 1;      /* readline.c:825 */
```

The public setter writes it directly, with no lock and no atomic. Its Argument Clinic block carries **no** `@critical_section` directive (contrast the very next function in the file, `readline.get_completer_delims`, which does):

```c
/*[clinic input]
readline.set_auto_history
    enabled as _should_auto_add_history: bool
    /
Enables or disables automatic history.
[clinic start generated code]*/

static PyObject *
readline_set_auto_history_impl(PyObject *module, int _should_auto_add_history)
{
    should_auto_add_history = _should_auto_add_history;   /* readline.c:843  WRITE (4 bytes) */
    Py_RETURN_NONE;
}
```

The flag is consumed, also unsynchronized, in the core input routine `call_readline()`:

```c
n = strlen(p);
if (should_auto_add_history && n > 0) {                   /* readline.c:1584  READ */
    ...
    add_history(p);
}
```

Two threads calling `readline.set_auto_history(...)` on the shared `readline` module race on the single 4-byte global (write/write). A thread calling `set_auto_history()` while another blocks in `input()`/`readline()` is the write/read variant against `call_readline()`. TSan reports it as a data race on `global 'should_auto_add_history'`, both accesses 4 bytes at `readline.c:843`.

## Reproducer

```python
import sys, threading, readline
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 6                       # total worker threads
ROUNDS = 4000
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(on):
    for _ in range(ROUNDS):
        enter.wait()
        readline.set_auto_history(on)   # write should_auto_add_history (:843)
        readline.set_auto_history(not on)
        leave.wait()

# half the threads drive the flag toward True, half toward False -> the store
# actually changes the word both ways, maximising the racing-write window.
ts = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(NT)]
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

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2169689)
  Write of size 4 at 0x7ffff630f794 by thread T5:
    #0 readline_set_auto_history_impl Modules/readline.c:843:29   (should_auto_add_history = _should_auto_add_history)
    #1 readline_set_auto_history      Modules/clinic/readline.c.h:571:20
    ...
    #25 thread_run                    Modules/_threadmodule.c:388:21

  Previous write of size 4 at 0x7ffff630f794 by thread T4:
    #0 readline_set_auto_history_impl Modules/readline.c:843:29   (should_auto_add_history = _should_auto_add_history)
    #1 readline_set_auto_history      Modules/clinic/readline.c.h:571:20
    ...
    #25 thread_run                    Modules/_threadmodule.c:388:21

  Location is global 'should_auto_add_history' of size 4 at 0x7ffff630f794 (readline...so+0xe794)

SUMMARY: ThreadSanitizer: data race Modules/readline.c:843:29 in readline_set_auto_history_impl
```

Reproduces deterministically (exit 66 on every run; two consecutive runs both flagged the same write/write pair on `global 'should_auto_add_history'`). The `Location is global '...'` line names the exact racing object, matching the seeded signature (both frames `readline_set_auto_history_impl` at `readline.c:843`).

## Root cause

`should_auto_add_history` is a **file-scope C global** owned by CPython's `Modules/readline.c` (defined `readline.c:825`, initialised to `1`). It is written by `set_auto_history()` (`readline.c:843`) and read by `call_readline()` (`readline.c:1584`), both with plain, non-atomic, unlocked accesses. Nothing establishes happens-before between a writer thread and any other writer or reader thread, so under free-threading these are data races on a shared word.

This is an **incomplete free-threading migration** in a file that has already *partially* adopted the primitives. The immediately analogous global in the same file — `static int _history_length` (`readline.c:320`) — was converted to atomics: it is written via `FT_ATOMIC_STORE_INT_RELAXED(_history_length, length)` in `set_history_length` (`readline.c:450`) and read via `FT_ATOMIC_LOAD_INT_RELAXED(_history_length)` (`readline.c:361/420/467`). `should_auto_add_history` is the same shape — a plain `int` flag driven by a Python-visible setter and read on the input path — but it was left as a bare global. Likewise the module's pointer setters were made `@critical_section`, but `set_auto_history` was not.

### Scope: CPython's own global, not libreadline's, and not the `readlinestate` struct

The racing memory is `should_auto_add_history`, a static in CPython's `Modules/readline.c` (`Location is global 'should_auto_add_history'`, in the `readline...so` image). It is **not** a GNU libreadline internal global (no `_rl_*` / `rl_*`), so it is fully fixable inside `Modules/readline.c` — distinct from the thread-unsafe-C-library class tracked under gh-127081 (locale/`setlocale`, libreadline reentrancy). It is also **not** part of the per-module `readlinestate` heap block; it is a process-wide file-scope static.

### Relationship to gh-153291 / TSAN-0016 (the sibling readline race this campaign found)

Same *file* and same broad *class* (unsynchronized CPython-owned readline module state, incomplete FT migration), but a **distinct variable and a distinct fix**:

- gh-153291 (PR gh-153362, "Fix data race in `readline.get_completer()` and `get_pre_input_hook()`" — this campaign's TSAN-0016) concerns the **`readlinestate` pointer fields** (`state->completer`, `state->pre_input_hook`, ...): `PyObject*` in per-module heap state whose *getters* lacked the `@critical_section` their setters hold. Its fix adds `@critical_section` to the getters and carries a latent borrowed-ref/use-after-free angle.
- TSAN-0025 (this report) concerns **`should_auto_add_history`**: a plain file-scope `static int` flag, not in `readlinestate`, not a pointer, no refcount/UAF angle. The gh-153291 patch does not touch it, and `@critical_section` is the wrong tool (the flag is a process-global, not per-module-object state). The right fix is `FT_ATOMIC` relaxed access, matching `_history_length`.

So this is a **separate, still-open** readline free-threading gap, part of the same incomplete-migration cleanup but not covered by the gh-153291 fix.

## Impact / severity

Low. The flag is a single aligned 4-byte word holding a boolean; torn reads/writes are not a practical concern on the target and the race is value-benign (no refcount, no pointer, no allocation involved) — worst case a concurrent `input()` observes a stale enable/disable decision for one line of history. There is no crash or memory-safety hazard, unlike the borrowed-ref angle of gh-153291. It remains a genuine TSan data race on CPython-owned shared state reachable entirely from pure Python (`readline.set_auto_history()` plus a second `set_auto_history()` or an `input()`), and it violates the reasonable expectation that a small module-config setter is thread-safe. Consistent with the other readline module-state findings in this campaign.

## Suggested fix

Give `should_auto_add_history` the same relaxed-atomic treatment its sibling `_history_length` already has in this file — a mechanical, low-risk change:

```c
/* write, readline.c:843 */
FT_ATOMIC_STORE_INT_RELAXED(should_auto_add_history, _should_auto_add_history);

/* read, readline.c:1584 */
if (FT_ATOMIC_LOAD_INT_RELAXED(should_auto_add_history) && n > 0) {
```

Relaxed ordering is sufficient: the flag is an independent boolean with no ordering dependency on other state. (Adding `@critical_section` to `set_auto_history` would silence the write/write case against other setters but would not order the write against the lockless `call_readline()` read, and does not match the process-global nature of the variable — the `FT_ATOMIC` approach used for `_history_length` is the correct, consistent one.)

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet `fusil-tsan_fleet_02`. This is the second distinct readline free-threading gap this campaign surfaced (after the gh-153291 `readlinestate` getters). The pattern to audit across `readline.c` is "plain `static` module globals driven by Python setters": `_history_length` was converted to `FT_ATOMIC`, the completer/hook pointers are `@critical_section` (setters) / gh-153291-pending (getters), but `should_auto_add_history` was missed. It should be folded into the same readline FT cleanup.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
