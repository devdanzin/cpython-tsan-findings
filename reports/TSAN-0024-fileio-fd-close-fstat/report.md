# Data race: closing a shared file descriptor object races with reads of its `fd` field (`_io.FileIO` / `select.epoll`)

*Both `_io.FileIO` and `select.epoll` store their OS descriptor in a plain `int` field (`self->fd` / `self->epfd`). `close()` writes the sentinel `-1` into that field with no atomic and no shared lock, while `fileno()` (and, on a fresh `open()`, `_Py_fstat_noraise`) read it. On a free-threaded build, one thread closing a shared descriptor object races with another thread reading its descriptor — a data race on the field, and (once the fd is recycled) an fd-reuse hazard on the descriptor itself.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducers; the maintainer reviewed and edited it._

## Summary

This is a **fd-lifecycle cluster** with three seeded faces, all rooted in the same pattern: a
descriptor-owning object keeps its fd in a plain `int` field that `close()` overwrites with `-1`
while other methods read it, with no synchronization on the field:

| Face | Write side | Read side | What TSan reports |
|------|-----------|-----------|-------------------|
| (a) | `internal_close` (`fileio.c`) `close(fd)` | `_Py_fstat_noraise` (`fileutils.c`) `fstat(fd)` via `FileIO.__init__` | **fd-resource** race (`Location is file descriptor N`) |
| (b) | `internal_close` | `_io_FileIO___init___impl` -> `_Py_fstat_noraise` | read side of the same fd-resource race |
| (c) | `pyepoll_internal_close` (`selectmodule.c`) `self->epfd = -1` | `select_epoll_fileno_impl` reads `self->epfd` | **memory** race on the `epfd` field |

Faces (a)/(b) are the fileio face: TSan's libc fd interceptor flagging `close(N)` racing with a
concurrent `fstat(N)` on a *recycled* descriptor number (the fleet vehicle hit it on fd 2). Face
(c) is the epoll face: a plain **memory** data race on the `self->epfd` struct field.

Both faces are confirmed below with minimal stdlib-only reproducers. The fileio face is confirmed
directly on the underlying unsynchronized `self->fd` field (`internal_close` write vs
`_io_FileIO_fileno_impl` read); the epoll face is confirmed on the exact seeded signature
(`pyepoll_internal_close` vs `select_epoll_fileno_impl`).

## Reproducer

**Fileio face** (`repro.py`) — a shared unbuffered `FileIO`; some threads `close()` it (write
`self->fd = -1`), some read `fileno()` (read `self->fd`):

```python
import sys, threading, tempfile, os
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

path = tempfile.NamedTemporaryFile(delete=False).name
with open(path, "wb") as f:
    f.write(b"x" * 64)

NT = 8
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        f = pool[0]
        try:
            if wid % 2 == 0:
                f.close()       # internal_close: self->fd = -1        (write)
            else:
                f.fileno()      # _io_FileIO_fileno_impl: read self->fd (read)
        except (ValueError, OSError):
            pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = open(path, "rb", buffering=0)   # fresh, open FileIO each round
    enter.wait()
    leave.wait()
    pool[0].close()
for t in ts: t.join()
os.unlink(path)
print("done, no crash")
```

**Epoll face** (`repro_epoll.py`) — identical shape on a shared `select.epoll()`, `close()` vs
`fileno()`.

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, Clang 21.1.8)

**Fileio face** (`repro.py`, exit 66, deterministic):

```
WARNING: ThreadSanitizer: data race
  Write of size 4 at 0x... by thread T3:
    #0 internal_close          Modules/_io/fileio.c:128:18   (self->fd = -1)
    #1 _io_FileIO_close_impl    Modules/_io/fileio.c:187:10
  Previous read of size 4 at 0x... by thread T1:
    #0 _io_FileIO_fileno_impl   Modules/_io/fileio.c:612:15   (if (self->fd < 0) ... PyLong_FromLong(self->fd))
    #1 _io_FileIO_fileno        Modules/_io/clinic/fileio.c.h:161:12
SUMMARY: ThreadSanitizer: data race Modules/_io/fileio.c:612:15 in _io_FileIO_fileno_impl
```

(Runs alternate between `_io_FileIO_fileno_impl:612` and `internal_close:126` as the reported
access — always the same `self->fd` word.)

**Epoll face** (`repro_epoll.py`, exit 66, deterministic — this is the exact seeded signature (c)):

```
WARNING: ThreadSanitizer: data race
  Write of size 4 at 0x... by thread T1:
    #0 pyepoll_internal_close    Modules/selectmodule.c:1335:20   (self->epfd = -1)
    #1 select_epoll_close_impl   Modules/selectmodule.c:1447:15
  Read of size 4 at 0x... by thread T2:
    #0 select_epoll_fileno_impl  Modules/selectmodule.c:1477:15   (if (self->epfd < 0))
    #1 select_epoll_fileno       Modules/clinic/selectmodule.c.h:679:12
SUMMARY: ThreadSanitizer: data race Modules/selectmodule.c:1335:20 in pyepoll_internal_close
```

**Seeded fileio face (a)** — the fleet vehicle (`configparser`, concurrent `open()`/`close()`
across the stress threads) instead tripped TSan's *fd-resource* interceptor, `Location is file
descriptor 2`:

```
  Write of size 8 by thread T4:   #0 close  #1 internal_close  Modules/_io/fileio.c:132:15  (close(fd))
  Previous read of size 8 by T1:  #0 fstat64 #1 _Py_fstat_noraise Python/fileutils.c:1285:12
                                  #2 _io_FileIO___init___impl  Modules/_io/fileio.c:477:20
  Location is file descriptor 2 created by main thread
SUMMARY: ThreadSanitizer: data race ... in close
```

## Root cause

**Fileio face.** `PyFileIO` stores its descriptor in a plain field:

```c
typedef struct {
    PyObject_HEAD
    int fd;                 /* Modules/_io/fileio.c:65 */
    ...
} fileio;
```

`internal_close` reads and then clobbers it with no lock/atomic, then closes the fd:

```c
static int
internal_close(fileio *self)
{
    ...
    if (self->fd >= 0) {          /* :126  read  self->fd */
        int fd = self->fd;        /* :127  read  self->fd */
        self->fd = -1;            /* :128  WRITE self->fd = -1  (plain, no lock/atomic) */
        /* fd is accessible and someone else may have closed it */   /* <-- known hazard */
        Py_BEGIN_ALLOW_THREADS
        err = close(fd);          /* :132  close(fd) -- the fd-resource write */
        ...
```

`_io_FileIO_fileno_impl` reads the same field with no lock:

```c
static PyObject *
_io_FileIO_fileno_impl(fileio *self)
{
    if (self->fd < 0)                            /* :612  READ self->fd */
        return err_closed();
    return PyLong_FromLong((long) self->fd);     /* :614  READ self->fd */
}
```

`FileIO` has **no `@critical_section` on any method** — `close`, `fileno`, `read`, `write`,
`seek`, `__init__` all touch `self->fd` (and the fd it names) unsynchronized. Two symptoms:

1. **Memory race on `self->fd`** — `close()` writing `-1` while `fileno()` reads it (confirmed
   above).
2. **fd-resource race** — `internal_close` calls `close(fd)` inside `Py_BEGIN_ALLOW_THREADS`;
   meanwhile a fresh `open(..., buffering=0)` runs `_Py_fstat_noraise(self->fd, ...)`
   (`fileio.c:477`) on a descriptor that the OS may have just recycled to the number being
   closed. TSan tracks each fd as a sync resource and flags `close(N)` vs `fstat(N)` — the
   seeded face (a). The in-tree comment at `fileio.c:129` already acknowledges "someone else may
   have closed it".

**Epoll face.** `pyEpoll_Object` is the same shape:

```c
typedef struct {
    PyObject_HEAD
    SOCKET epfd;            /* Modules/selectmodule.c:1315 */
} pyEpoll_Object;

static int
pyepoll_internal_close(pyEpoll_Object *self) {
    if (self->epfd >= 0) {
        int epfd = self->epfd;
        self->epfd = -1;     /* :1335  WRITE (plain) */
        ... close(epfd) ...
    }
}

static PyObject *
select_epoll_fileno_impl(pyEpoll_Object *self) {
    if (self->epfd < 0)      /* :1477  READ (plain) */
        return pyepoll_err_closed();
    return PyLong_FromLong(self->epfd);
}
```

Here there is a **smoking gun**: `select.epoll.close` *is* decorated `@critical_section`
(`selectmodule.c:1435`), but `select.epoll.fileno` — and `register`/`modify`/`unregister`/
`poll`/`__enter__`/`__exit__` — are **not**. A critical section on the writer alone provides no
mutual exclusion against readers that don't take it, and the field is a plain `int` besides. So
`close()` (under the critical section) writing `self->epfd = -1` races with a lock-free
`fileno()` read.

## Impact / severity

**Low-to-medium.** The field write is value-benign in isolation (`close()` stores `-1`; a racing
reader sees either the old fd or `-1`, both single aligned words). The real hazard is the
**fd-reuse / descriptor confusion** the unsynchronized lifecycle enables: a reader that latched
`self->fd == N` (or a fresh `open()` that the kernel just handed the recycled number `N`) can
`fstat`/`read`/`write`/`epoll_ctl` a descriptor that another thread already closed and that now
names a *different* file — a use-after-close analog for OS descriptors. No crash was observed in
the TSan build; the consequence is silent operation on the wrong fd, not memory corruption.

This requires *sharing one descriptor object across threads and using it while another thread
closes it* — partly "don't do that", but the missing field synchronization is a genuine
free-threading gap versus the many CPython objects that were FT-hardened (atomic/locked fd
fields), and the epoll asymmetric-critical-section is clearly unintended.

## Suggested fix

- **Epoll (face c, strongest):** either make every `self->epfd` access atomic
  (`FT_ATOMIC_LOAD_INT`/`FT_ATOMIC_STORE_INT`, relaxed), or add `@critical_section` to `fileno`
  and the other `epfd` readers so they are mutually exclusive with the already-critical-sectioned
  `close`. Atomics are the lighter fix for a trivial getter; matching the critical section is the
  more complete fix (it also serializes the close-then-`epoll_ctl` window).
- **FileIO (faces a/b):** make `self->fd` atomic (relaxed load/store), so `close`/`fileno`/`init`
  agree on the field without UB. The remaining fd-resource-reuse race is inherent to closing a
  descriptor another thread is still using; the honest mitigation is documentation ("do not use a
  file object from another thread while closing it"), matching the existing `fileio.c:129`
  comment. Full serialization would require a per-object lock around every fd-using method.

## Notes

- Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet `fusil-tsan_fleet_02`. The vehicle
  (`configparser`) hit the fileio fd-resource face incidentally via concurrent `open()`/`close()`
  in the shared-object stress harness.
- **Per-face scope calls:**
  - **(c) epoll `close` vs `fileno`** — *real, in-scope FT gap.* Clean unsynchronized memory race
    on `self->epfd`; the writer takes a critical section the readers don't. Strongest face.
  - **(a) fileio `internal_close` vs `_Py_fstat_noraise`** — *in-scope, borderline.* A close-vs-use
    race; the underlying `self->fd` field is genuinely unsynchronized (confirmed directly), but the
    seeded fd-resource manifestation is the fd-reuse hazard, which is partly inherent to descriptor
    lifecycle and already flagged by an in-tree comment.
  - **(b) fileio `_io_FileIO___init___impl` vs `_Py_fstat_noraise`** — in this vehicle the
    `__init__`/`fstat` is a *fresh* `open()` that is the read side of the (a) fd-reuse race, not a
    re-init of one object. A literal *concurrent `__init__` on one shared FileIO* (re-open) would be
    **out of scope** (cf. cpython#127192 — concurrent object re-initialization is unsupported).
- Reproduced stdlib-only. The epoll face is the exact seeded signature; the fileio face is
  confirmed on the root-cause `self->fd` field. The seeded fileio *fd-resource* collision (a) needs
  an exact fd-number reuse and was not force-reproducible in a bounded harness (3.2M free-running
  `open`/`close` iterations did not trip it), but the fleet caught it and the field race is the same
  root cause.
- Audit sibling descriptor objects for the same plain-`int`-fd pattern (`_io.FileIO`,
  `select.epoll`, `select.kqueue`, `select.devpoll`, socket/`_socket`).

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
