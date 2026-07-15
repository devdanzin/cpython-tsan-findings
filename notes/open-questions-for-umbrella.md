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

## Q2 — Concurrent `time.tzset()` corrupts libc global tz state (`cfunction_vectorcall_NOARGS`)

**Status:** **CONFIRMED reproducible** (`notes/tzset_race.py`), NEW (no prior upstream issue),
NOT suppressed. Candidate for a real report.

`time.tzset()` is a thin `METH_NOARGS` wrapper over libc `tzset()`, which mutates the process-global
timezone state (`tzname` strings, `timezone`, `daylight`) and is **not safe for concurrent calls**.
Under free-threading it's reachable from pure Python, so 4 threads calling `time.tzset()` race in
glibc `tzset_internal`:

```
Write (T10): free   <- tzset_internal (time/tzset.c:401) <- cfunction_vectorcall_NOARGS
Prev  (T11): malloc <- strdup        (string/strdup.c)   <- cfunction_vectorcall_NOARGS
SUMMARY: ThreadSanitizer: data race ... in free
```

One thread `free()`s the old `tzname` string while another `strdup()`s a new one → a libc heap
free/malloc race = a genuine **crash risk** (double-free / use-after-free in libc), not a benign
reorder. This is analogous to the accepted faulthandler `enable()`/`disable()` global-state race
(python/cpython#151363): CPython mutates process-global state from a builtin without serializing it.

**Broader impact:** `time.localtime()` / `time.strftime()` / `time.mktime()` also consult the same
libc tz state (and libc may call `tzset_internal` internally), so the exposure isn't limited to the
rarely-called `tzset()`. Worth checking whether those race too.

**Question for CPython devs:** should CPython serialize `time.tzset()` (and the tz-dependent time
functions) with a lock, or is concurrent `tzset()` "don't do that" process-global config? Given the
crash-safety guarantee and the libc heap corruption, this looks actionable.

**Tooling note:** the dedupe signature collapses to the generic
`Objects/methodobject.c:cfunction_vectorcall_NOARGS | ...cfunction_vectorcall_NOARGS` because
`tsan_dedup.parse_report` drops non-CPython (libc) frames, so the meaningful racing site
(`tzset_internal`) is stripped. This signature is **not** cataloged (it would mislabel any future
concurrent-NOARGS-cfunction libc race as tzset). Consider teaching the parser to keep the top libc
frame when no deeper CPython frame exists, so libc-level races get a specific signature.

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
