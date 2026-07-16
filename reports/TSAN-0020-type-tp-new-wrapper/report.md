# Data race inside OpenSSL (libcrypto), not CPython: concurrent `ssl.SSLContext()` construction races OpenSSL's global algorithm-fetch cache (surfaces as `tp_new_wrapper`)

*Two threads each independently constructing their **own** `ssl.SSLContext` reach `SSL_CTX_new()`, which populates OpenSSL's process-global algorithm-fetch / method-store cache: one thread `memcpy`s a freshly `CRYPTO_malloc`'d 104-byte entry into the store (under an OpenSSL `CRYPTO_THREAD` lock) while another `memcmp`s the same entry on a lock-free fast path. The racing memory and the racing code are **entirely inside `libcrypto.so.3` (OpenSSL 3.5.5)** — no CPython object, no CPython type state is involved. `tp_new_wrapper` (`Objects/typeobject.c:10478`, `res = type->tp_new(...)`) is only the nearest **symbolized** frame, because libcrypto is stripped. This is an OpenSSL-internal race reached through a legitimate, supported operation; it is **out of scope** for the CPython free-threading catalog.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Verdict: OUT OF SCOPE (not a CPython bug)

The seeded signature `Objects/typeobject.c:tp_new_wrapper | Objects/typeobject.c:tp_new_wrapper` is **misleading**. Reading the actual TSan `Location`/access stanzas shows the race is not on any Python-level state:

- **Racing memory**: `Location is heap block of size 104 ... allocated by ... CRYPTO_malloc` — an OpenSSL-owned heap block, not a `PyObject` and not a type struct.
- **Racing accesses**: frame `#0` is libc `memcmp`/`memcpy`, frame `#1` is `libcrypto.so.3+0x2509xx` (unsymbolized OpenSSL internals). The first frame TSan *can* symbolize is `#2 tp_new_wrapper` — CPython's generic `__new__` wrapper — purely because the system `libcrypto` has no debug symbols (`<null> <null>`).
- **What CPython does**: each thread calls `ssl.SSLContext(...)`, which bottoms out in `SSL_CTX_new()`. Constructing two *separate* `SSL_CTX` objects concurrently is a documented, thread-safe OpenSSL operation. **Nothing is shared at the Python level** — each thread builds its own distinct context.

So this is neither of the two hypotheses one might reach for:

- It is **not** "concurrent construction of the *same* shared object" (the `cpython#127192` class). Each thread constructs its own object; there is no shared Python instance.
- It is **not** a race on shared CPython *type*-level state (tp_flags, the type version tag, `tp_new`/`__new__` cache, `ht_cached_keys`, mro/subclasses). None of those memory locations is touched — the racing address is inside an OpenSSL heap block.

It is a **third category**: a data race inside a bundled third-party C library (OpenSSL), reached through legitimate independent object construction. If it is a genuine defect it belongs to **OpenSSL**, not CPython. The correct catalog disposition mirrors **TSAN-0003** (the glibc `SemLock`/`__sem_mappings` third-party race): document it and suppress the signature so the fleet stops re-seeding it. (Difference from TSAN-0003: there glibc serializes *both* sides with a lock TSan cannot see, making it a pure false positive; here the **write** holds an OpenSSL `CRYPTO_THREAD` rwlock that TSan *does* see — `mutexes: write M0` — while the **read** is genuinely lock-free, so this is a *real* race inside OpenSSL's lock-free lookup path, still outside CPython.)

## Summary

`ssl.SSLContext` is a Python subclass of the C type `_ssl._SSLContext` (`Lib/ssl.py:422`). Its Python `__new__` (`Lib/ssl.py:430`) calls `super().__new__`:

```python
class SSLContext(_SSLContext):
    def __new__(cls, protocol=None, *args, **kwargs):
        ...
        self = _SSLContext.__new__(cls, protocol)   # Lib/ssl.py:438
```

`_SSLContext.__new__` is the generic C `tp_new_wrapper` (bound in `_ssl._SSLContext`'s dict because that type has a `tp_new` but no Python `__new__`). `tp_new_wrapper` calls the real C `tp_new`:

```c
    /* Objects/typeobject.c, tp_new_wrapper */
    res = type->tp_new(subtype, args_tuple, kwds);   /* :10478  -> _ssl__SSLContext -> SSL_CTX_new() */
```

`SSL_CTX_new()` fetches the context's ciphers/digests/algorithms from OpenSSL's providers. On first touch (cold cache) OpenSSL **populates** a process-global method/algorithm cache — `memcpy`ing a freshly `CRYPTO_malloc`'d entry into the store under a `CRYPTO_THREAD` lock — and concurrently **looks entries up** with `memcmp` on a lock-free fast path. Two threads doing their first `SSL_CTX_new()` at once therefore race on the same 104-byte OpenSSL struct: `memcpy` (write, lock held) vs `memcmp` (read, no lock).

## Reproducer

Minimal, stdlib-only. Each thread constructs its **own** `SSLContext`; the per-round barrier keeps every thread's *first* (cold-cache) construction overlapping, since the OpenSSL cache is first-touch and only races while still cold.

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"
import ssl

NT = 24
ROUNDS = 8
CIPHERS = ["DEFAULT", "ALL", "HIGH", "AES256-SHA",
           "ECDHE-RSA-AES128-GCM-SHA256", "AES128-SHA256"]
step = threading.Barrier(NT)

def worker(wid):
    for r in range(ROUNDS):
        step.wait()                       # all threads first-touch together each round
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # -> tp_new_wrapper -> SSL_CTX_new
            ctx.set_ciphers(CIPHERS[(wid + r) % len(CIPHERS)])
        except Exception:
            pass

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, OpenSSL 3.5.5, glibc 2.43)

Every `libcrypto` offset matches the seeded vehicle report byte-for-byte (`memcmp` caller `+0x250b01`, `memcpy` caller `+0x250916`, `CRYPTO_malloc +0x261d65`, `CRYPTO_THREAD_lock_new +0x276ec5`), and both racing frames symbolize to `tp_new_wrapper typeobject.c:10478`.

```
WARNING: ThreadSanitizer: data race (pid=2154147)
  Read of size 8 at 0x721c000042a8 by thread T15:
    #0 memcmp <null> (python+0x10c32e)
    #1 <null> <null> (libcrypto.so.3+0x250b01)
    #2 tp_new_wrapper Objects/typeobject.c:10478:11              (res = type->tp_new(...))
    #3 cfunction_vectorcall_FASTCALL_KEYWORDS Objects/methodobject.c:465
       ... _ssl._SSLContext.__new__ (tp_new_wrapper) called from ssl.SSLContext.__new__ ...

  Previous write of size 8 at 0x721c000042a8 by thread T24 (mutexes: write M0):
    #0 memcpy <null> (python+0xfae82)
    #1 <null> <null> (libcrypto.so.3+0x250916)
    #2 tp_new_wrapper Objects/typeobject.c:10478:11

  Location is heap block of size 104 at 0x721c00004280 allocated by thread T24:
    #0 malloc
    #1 CRYPTO_malloc <null> (libcrypto.so.3+0x261d65)
    #2 tp_new_wrapper Objects/typeobject.c:10478:11

  Mutex M0 (0x721000002540) created at:
    #1 CRYPTO_THREAD_lock_new <null> (libcrypto.so.3+0x276ec5)
    #2 CRYPTO_THREAD_run_once <null> (libcrypto.so.3+0x276fac)
    #4 tp_new_wrapper Objects/typeobject.c:10478:11

SUMMARY: ThreadSanitizer: data race (python+0x10c32e) in memcmp
```

Exit code 66. Reliable: **6/6** runs with `NT=24` (a plain 8-thread version without the per-round barrier hit ~1/3, because the race window is the single cold-cache first-touch — the barrier maximises overlap on it).

## Root cause

The bug, if any, is in **OpenSSL 3.x**, not CPython:

- `SSL_CTX_new()` -> OpenSSL provider/algorithm fetch (`crypto/evp` + provider method store). The 104-byte `CRYPTO_malloc`'d block is an internal cache/method-store entry.
- The **insert** path takes an OpenSSL `CRYPTO_THREAD` rwlock (M0, created once via `CRYPTO_THREAD_run_once` during module init) and `memcpy`s the entry.
- The **lookup** path `memcmp`s the entry with **no** lock (TSan records no mutex on the read thread). OpenSSL's lock-free read fast path is not ordered against the locked writer for the exact bytes TSan observes (an 8-byte field at entry offset +0x28).

CPython's only involvement is calling a documented, thread-safe OpenSSL entry point (`SSL_CTX_new`) from two threads. The generic `tp_new_wrapper` frame is an artifact of libcrypto being stripped; **any** OpenSSL-backed construction/first-touch (`ssl`, `hashlib`, `hmac`) collapses to this same nearest-symbolized frame, which is why it is a poor dedup key.

## Impact / severity

- **For CPython: none.** No CPython memory races; no CPython invariant is violated. This is not a CPython free-threading defect.
- **For OpenSSL: low.** The value is a first-touch cache-population race; population is idempotent (all threads compute the same method entry), so it is almost certainly benign in practice (torn read of a pointer/flag already being (re)written to the same value), though a strict reading is a genuine lock-free-read-vs-locked-write race. This is well inside OpenSSL's own concurrency domain and, given OpenSSL 3.x's fetch-cache design, is likely already known / considered acceptable upstream.

## Suggested "fix"

There is nothing to fix in CPython. Recommended catalog actions:

1. **Suppress the signature** so the `--tsan` fleet stops re-seeding it, exactly like the TSAN-0003 glibc entry in `catalog/suppressions.txt`. A `race`/`race_top` rule keyed on the OpenSSL construction path, e.g. treat `Objects/typeobject.c:tp_new_wrapper | Objects/typeobject.c:tp_new_wrapper` whose top real frames are `memcmp`/`memcpy` in `libcrypto.so.3` as a third-party (OpenSSL) race. (Note the bare `tp_new_wrapper|tp_new_wrapper` key alone is too broad to suppress blindly — gate it on the `libcrypto.so.3` frames.)
2. Optionally add `libcrypto`/OpenSSL entries to the build's `Tools/tsan/suppressions_free_threading.txt` for local runs (third-party, uninstrumented library).
3. If desired, this can be reported to OpenSSL as an FYI (concurrent `SSL_CTX_new` / algorithm fetch tripping TSan on the method-store lookup), but it is not a CPython issue and should **not** go into the CPython umbrella.

## Notes

- Found by ThreadSanitizer fuzzing (`fusil --tsan`), vehicle `xml_dom_xmlbuilder` (fleet-02, inst-01). The vehicle's own shared objects (`DOMBuilder`/`Options`/`DOMEntityResolver`) are not OpenSSL-backed; the OpenSSL construction was reached transitively deeper in the generated script. The repro here isolates the identical OpenSSL cache race via the cleanest stdlib path (`ssl.SSLContext`); the signature is defined by the OpenSSL internals + `tp_new_wrapper`, not by the Python caller, so the match is exact.
- Same *disposition class* as **TSAN-0003** (third-party library race surfacing under a CPython frame), but a real OpenSSL-internal race rather than a pure TSan-can't-see-the-lock false positive.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. This entry is **out of scope** for that umbrella (OpenSSL-internal, not a CPython bug) and is retained only to document the signature and its suppression.*
