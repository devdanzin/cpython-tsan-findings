# Open questions / candidates for the TSan umbrella issue

Findings that are **not** auto-suppressible shared-builtin noise and need a human decision or a
dev ruling before filing. Keep these here so they survive between fleet runs; fold the live ones
into the umbrella issue when it's written.

---

## Q1 — Concurrent `list.sort()` on a shared list (`binarysort | binarysort`)

**Status:** root-caused from source, **not reproduced in isolation**, NOT suppressed. Held for a
dev ruling.

`list_sort_impl` (Objects/listobject.c) takes **no critical section** on the list. It detaches the
items array under no lock —

```c
saved_ob_item = self->ob_item;   // :2969
Py_SET_SIZE(self, 0);            // :2971
self->ob_item = NULL;            // :2972
...
lo.keys = saved_ob_item;         // :2977  (no key= func -> sort the list's OWN array in place)
```

— sorts `saved_ob_item` in place via `binarysort` (`a[L] = pivot`, listobject.c:1918), then
reattaches. Two threads that both read `self->ob_item` in the ~3-instruction window before the
other detaches then rewrite the **same** array concurrently → the `binarysort | binarysort` race.
The observed instance stayed crash-safe (slots hold valid objects, just reordered), but this is
*mutate-while-mutate without a lock*, unlike the read-while-mutate shared-builtin cases.

**Why not suppressed / not reproduced:** the detach window is microscopic — a plain multi-thread
sort loop (4×200k, 16×400k) never hit it; the fleet did once. Suppressing an unreproduced signature
risks masking a real bug, so it stays visible.

**Question for CPython devs:** is concurrent `list.sort()` on a shared list intended to stay
crash-safe? It runs without a per-object critical section and rewrites the detached array in place,
so two racing sorts touch the same slots. Confirm the detach scheme is sufficient (→ "don't do
that", suppress) vs a latent safety gap (→ needs a lock).

---

## ~~Q2~~ RESOLVED — Concurrent `time.tzset()`/`mktime()` = a glibc/TSan false positive, NOT a CPython bug

**Status:** investigated + **reclassified**. Reproduces in **pure C with no Python** →
the race is entirely inside glibc; TSan can't see glibc's internal `tzset_lock`. **Not a CPython
bug, not an umbrella candidate.** Left visible (rare) pending a dedupe-parser tweak to suppress it
cleanly (see tooling note).

`time.tzset()` is a `METH_NOARGS` wrapper over libc `tzset()`, which rewrites the process-global
timezone state (`tzname` strings). 4 threads calling `time.tzset()` produce:

```
Write (T?): free   <- tzset_internal (time/tzset.c:401) <- cfunction_vectorcall_NOARGS
Prev  (T?): malloc <- strdup        (string/strdup.c)   <- cfunction_vectorcall_NOARGS
SUMMARY: ThreadSanitizer: data race ... in free
```

At first glance this looks like a real free/malloc heap race on `tzname`. It is **not**:

- **The identical race reproduces from pure C** (`notes/tzset_glibc_c_repro.c`: 4 pthreads calling
  `tzset()`, no Python) → `data race time/tzset.c:401 in tzset_internal`. So it's a glibc property,
  independent of CPython and of free-threading.
- glibc's public `tzset()` serializes `tzset_internal` with an internal **low-level lock**
  (`tzset_lock`, an `__libc_lock`/futex). TSan does **not** interpose that lock, so it can't
  establish happens-before across the serialized `free`/`strdup` of `tzname` and reports a race that
  can't actually occur. **800k+ concurrent `tzset()` calls never crash** — consistent with the
  writes really being serialized.

**Survey of the tz-dependent `time` functions** (4 threads each, TSan build):

| function | TSan race? | why |
|---|---|---|
| `localtime`, `gmtime`, `strftime`, `ctime`, `asctime` | **no** | after the first parse they only *read* tz state (read-read → no TSan report) |
| `tzset`, `mktime` | yes (false) | *force* a `tzset_internal` rewrite each call; the write is serialized by glibc's unmodeled `tzset_lock` |

So `mktime` trips the same glibc false positive; the read-only converters don't. Nothing here is a
CPython data race.

**Disposition:** not filed against CPython. (If anything it's a glibc/TSan-instrumentation gap;
glibc ships `libc` TSan suppressions upstream for exactly this class.) Should be **suppressed** in
our catalog once the deduper can name the libc site.

**Tooling note (actionable in fusil):** the dedupe signature collapses to the generic
`Objects/methodobject.c:cfunction_vectorcall_NOARGS | ...cfunction_vectorcall_NOARGS` because
`tsan_dedup.parse_report` keeps only CPython-source frames, dropping the libc frames — so the
meaningful racing site (`tzset_internal`) is lost and the signature can't be suppressed without
also masking *real* concurrent-NOARGS-cfunction races. Fix: when the innermost non-interceptor
frame (skip `free`/`malloc`/`memcpy`/`strdup`/… and `<null>`) is a libc frame, keep it as the
racing site so libc-level races get a specific signature (`time/tzset.c:tzset_internal | …`), which
we can then suppress. Until then tzset/mktime stay visible as NEW `cfunction_vectorcall_NOARGS`.

---

## Resolved — faulthandler `is_enabled()` vs `enable()` (already reported)

`Modules/faulthandler.c:faulthandler_enable | faulthandler_is_enabled_impl` — `is_enabled()` reads
the global `fatal_error.enabled` (faulthandler.c:686) while `enable()` writes it (:538), no
synchronization. This is the read-side counterpart of the non-atomic `enabled` flag already tracked
in **python/cpython#151363** ("Data race in `faulthandler.enable()` and `faulthandler.disable()`
with free-threading"; the related watchdog race is #151475). Real bug, **already reported** →
cataloged as a known race (`reports/TSAN-0012-faulthandler-enabled-flag/`) so fleets dedupe it
instead of resurfacing it as new. If filing follow-up, note `is_enabled()` as another unsynchronized
reader of the same flag.
