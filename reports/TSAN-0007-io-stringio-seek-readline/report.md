# Data race: `io.StringIO` iteration is unlocked and races with `seek()` on `self->pos` (`stringio.c`)

*Every user-facing `StringIO` method (`seek`, `read`, `readline`, `write`, `truncate`, `tell`, …) is `@critical_section`-annotated, so they mutually exclude on the object. But the `tp_iternext` slot `stringio_iternext` is a hand-written slot that takes **no** critical section: it calls `_stringio_readline`, which reads and advances `self->pos` (and touches `self->buf`) while holding no lock. So iterating a shared `StringIO` (`readlines()`/`list()`/`for line in s`) races with a concurrent `seek()` that *does* hold the critical section — the lock on one side is useless when the other side never takes it.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Modules/_io/stringio.c` hardens `StringIO` for free-threading by wrapping every method impl in a per-object critical section (the Argument Clinic `@critical_section` directive expands to `Py_BEGIN_CRITICAL_SECTION(self)` around the impl — see `clinic/stringio.c.h:226` for `seek`). This makes concurrent calls to those methods data-race-free on the object's internal fields (`buf`, `pos`, `string_size`).

The iterator protocol slot was missed. `stringio_iternext` (registered as `Py_tp_iternext`) is not clinic-generated and takes no critical section:

```c
static PyObject *
stringio_iternext(PyObject *op)
{
    stringio *self = stringio_CAST(op);
    CHECK_INITIALIZED(self);
    CHECK_CLOSED(self);
    ENSURE_REALIZED(self);
    if (Py_IS_TYPE(self, self->module_state->PyStringIO_Type)) {
        line = _stringio_readline(self, -1);   /* :421 — NO critical section held */
    }
    ...
}
```

`_stringio_readline` reads `self->pos` (`:365`) and advances it with `self->pos += len` (`:383`), all unlocked. A per-object critical section only excludes *other* critical sections on the same object, so the unlocked iterator races with any locked method that mutates the same fields — e.g. `seek()` doing `self->pos = pos` (`:543`). Iteration reaches this path via `for line in s`, `list(s)`, `tuple(s)`, and `s.readlines()` (which does `list.extend(iter(s))` → `stringio_iternext`).

## Reproducer

```python
import sys, threading, io
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT_SEEK = 3          # threads writing self->pos via seek() (critical-section held)
NT_ITER = 3          # threads reading/advancing self->pos via iteration (NO critical section)
NT = NT_SEEK + NT_ITER
ROUNDS = 3000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def seeker():
    for _ in range(ROUNDS):
        enter.wait()
        s = pool[0]
        for _ in range(40):
            s.seek(0)              # _io_StringIO_seek_impl: writes self->pos (:543)
        leave.wait()

def iterator():
    for _ in range(ROUNDS):
        enter.wait()
        s = pool[0]
        for _ in range(40):
            s.seek(0)              # reset (protected) so readlines yields the full buffer
            s.readlines()          # -> stringio_iternext -> _stringio_readline reads/advances self->pos
        leave.wait()

ts  = [threading.Thread(target=seeker)   for _ in range(NT_SEEK)]
ts += [threading.Thread(target=iterator) for _ in range(NT_ITER)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = io.StringIO("line\n" * 200)   # fresh shared StringIO each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

Reproduces deterministically in ~0.6 s, exit 66, no crash.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, BuildId 210f4dee…)

The confirmed run caught the **write/write** variant on `self->pos` — `seek()` storing `pos` vs the iterator advancing `pos += len` — same field, same two functions as the fleet seed:

```
WARNING: ThreadSanitizer: data race (pid=1963687)
  Write of size 8 at 0x7fffb6311838 by thread T1:
    #0 _io_StringIO_seek_impl   Modules/_io/stringio.c:543:15   (self->pos = pos)
    #1 _io_StringIO_seek        Modules/_io/clinic/stringio.c.h:226:20
    ...
    #29 thread_run              Modules/_threadmodule.c:388:21

  Previous write of size 8 at 0x7fffb6311838 by thread T6:
    #0 _stringio_readline       Modules/_io/stringio.c:383:15   (self->pos += len)
    #1 stringio_iternext        Modules/_io/stringio.c:421:16
    #2 list_extend_iter_lock_held Objects/listobject.c:1318:26
    ...
    #10 _io__IOBase_readlines_impl Modules/_io/iobase.c:731:25
    ...
    #39 thread_run              Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/_io/stringio.c:543:15 in _io_StringIO_seek_impl
```

The fleet vehicle (`email_parser` / inst-04) caught the sibling **read/write** variant of the same race — `_stringio_readline:365` (`if (self->pos >= self->string_size)`, a read) vs `_io_StringIO_seek_impl:543` (the write). Both are the same unlocked-`self->pos` bug through `stringio_iternext`; TSan just reports whichever of the read/write pair it observes first.

## Root cause

The `stringio` struct (`stringio.c`):

```c
typedef struct {
    PyObject_HEAD
    Py_UCS4 *buf;
    Py_ssize_t pos;          /* the racing field (size 8) */
    Py_ssize_t string_size;
    size_t buf_size;
    ...
} stringio;
```

`pos` is the current stream position. It is written by `seek` (`:543`), `read` (`:353`), `write`, `truncate`, and advanced by `_stringio_readline` (`:383`); read by `read`/`readline`/`tell`/`_stringio_readline`. Every one of those entry points is `@critical_section`-annotated **except** the iterator slot:

- `_io.StringIO.seek` → `@critical_section` (`:494`); clinic wraps the impl in `Py_BEGIN_CRITICAL_SECTION(self)` (`clinic/stringio.c.h:226`).
- `_io.StringIO.readline` → `@critical_section` (`:388`); its impl `_io_StringIO_readline_impl` calls `_stringio_readline` **with the lock held**.
- `stringio_iternext` (`:410`, wired at `Py_tp_iternext`, `:1091`) → **no** `Py_BEGIN_CRITICAL_SECTION`; calls `_stringio_readline` (`:421`) with **no lock**.

A per-object critical section is a mutex keyed on the object; it only serializes against other critical sections on that same object. Because `stringio_iternext` never enters the section, its accesses to `self->pos` (and `self->buf`, `self->string_size`) run concurrently with a locked `seek()`/`write()`/`truncate()` — a genuine data race. This is an *omission/asymmetry* in the free-threading hardening, not a case where the object was never meant to be safe: the surrounding code demonstrates the intent that these operations be TSan-clean under concurrent use, and the iterator slot was simply overlooked.

## Impact / severity

- **Confirmed (value-benign):** the race on `self->pos` (`Py_ssize_t`, aligned 8-byte word) does not tear or crash on this build. Logically it can drop a `seek`/`readline` position update, yielding duplicated/garbled lines — a correctness bug for anyone iterating a `StringIO` shared with a writer, but not memory-unsafe by itself.
- **Potential escalation (crash-class, by inspection — not reproduced here):** the *same* missing lock also leaves `self->buf` and `self->string_size` unprotected in the iterator. `_stringio_readline` computes `start = self->buf + self->pos`, writes a temporary NUL terminator `*end = '\0'` (`:374`) and restores it (`:378`). A concurrent `truncate()`/`write()` takes the critical section and can call `resize_buffer` → `PyMem_Realloc(self->buf, …)` (`:116`) and reassign `self->buf` (`:122`), freeing the old buffer. The unlocked iterator, holding a stale `start`/`end` into the freed buffer, then does an out-of-bounds/UAF write of `'\0'`. That is heap corruption, not merely a benign value race. The fleet fuzzer only surfaced the `pos` field, but the fix and the risk cover `buf`/`string_size` too.

Severity: **low as observed, medium as latent** (unlocked iterator + a lock-holding `truncate`/`write` is a plausible use-after-free). It is not "you must never share a `StringIO`" behaviour of the kind we suppress for plain builtins — CPython explicitly annotated every sibling method to make this object concurrency-safe, so the unlocked iterator is a real gap in that guarantee.

## Suggested fix

Add a per-object critical section to `stringio_iternext`, matching every other entry point:

```c
static PyObject *
stringio_iternext(PyObject *op)
{
    PyObject *line;
    stringio *self = stringio_CAST(op);
    CHECK_INITIALIZED(self);
    CHECK_CLOSED(self);
    ENSURE_REALIZED(self);
    Py_BEGIN_CRITICAL_SECTION(self);
    if (Py_IS_TYPE(self, self->module_state->PyStringIO_Type)) {
        line = _stringio_readline(self, -1);
    }
    else {
        line = PyObject_CallMethodNoArgs(op, &_Py_ID(readline));
        ...
    }
    Py_END_CRITICAL_SECTION();
    ...
}
```

Put the section in `stringio_iternext` (the unlocked caller), **not** in the shared helper `_stringio_readline`: the helper is also called from `_io_StringIO_readline_impl`, which already holds the section, and CPython critical sections are not recursive. (`CHECK_*`/`ENSURE_REALIZED` may stay outside; the buffer-touching read must be inside.) This is the same shape the `@critical_section` clinic directive already generates for the other methods.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, vehicle `inst-04/python/email_parser-…-tsanNEW` (a `StringIO` reached via the `email` parser, hammered with `list()`/method calls from several threads). The bug is a good example of the "half-locked object" class: adding `@critical_section` to the methods but leaving a raw type slot unlocked defeats the lock, because a critical section only excludes other critical sections on the same object. Worth auditing other `_io` types (and any type that annotates methods with `@critical_section`) for `tp_iternext` / other hand-written slots that bypass the section.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
