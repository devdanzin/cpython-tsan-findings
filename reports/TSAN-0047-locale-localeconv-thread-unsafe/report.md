# Data race (heap-use-after-free): `locale.localeconv()` races the non-thread-safe C `localeconv()` static struct (`Modules/_localemodule.c`) — cpython#127081

*`locale.localeconv()` calls the C library `localeconv()`, which returns a pointer to a **single static** `struct lconv`, then `strdup()`s / decodes its string fields. C `localeconv()` is not thread-safe: a concurrent call in another thread overwrites the static struct. Two threads in `locale.localeconv()` race on the shared `lconv` fields — TSan reports a **heap-use-after-free** (one thread's `_PyMem_Strdup` reads a C string another thread's `strdup()`/`localeconv()` overwrote). CPython wraps the non-thread-safe libc call without a lock. This is an instance of [cpython#127081](https://github.com/python/cpython/issues/127081) ("Thread-unsafe libc functions").*

_AI Disclaimer: this report was drafted by Claude Code, which created and ran the fleet that surfaced it; the maintainer reviewed and edited it._

## Summary

`_locale_localeconv_impl` (`Modules/_localemodule.c`) → `locale_decode_monetary` calls libc `localeconv()` and reads/`strdup`s its `struct lconv` string fields (`mon_decimal_point`, `currency_symbol`, …). POSIX allows `localeconv()` to return a pointer to static storage that a later call — including from another thread — may overwrite. With no CPython-side lock, two concurrent `locale.localeconv()` calls race on that static struct, and one thread reads a string the other has already replaced/freed → use-after-free.

## Reproducer

Fleet-surfaced (2 vehicles, via `locale.localeconv()` from multiple threads). Not reproduced in isolation this session: triggering it needs a locale whose **monetary** fields are non-empty (the `C`/`POSIX` default has empty monetary strings, so nothing is `strdup`'d and the race window is absent). The fleet vehicles ran under a locale with monetary data.

```python
# Illustrative (requires a locale with non-empty monetary fields, e.g. a country UTF-8 locale):
import locale, threading
locale.setlocale(locale.LC_ALL, "en_US.UTF-8")   # a locale WITH monetary strings
barrier = threading.Barrier(8)
def worker():
    barrier.wait()
    for _ in range(20000):
        locale.localeconv()
ts = [threading.Thread(target=worker) for _ in range(8)]
for t in ts: t.start()
for t in ts: t.join()
```

TSan `SUMMARY`: `heap-use-after-free … in _PyMem_Strdup` (read) with `locale_decode_monetary` / `_locale_localeconv_impl` on the stack, vs libc `strdup`/`localeconv` (write). (Fleet `tsan_report`.)

## Impact / severity

**Moderate (memory-unsafe: heap-use-after-free)** — but the root cause is libc's non-thread-safe `localeconv()`, and the fix is CPython-side. Filed upstream as cpython#127081.

## Suggested fix

Lock around the C `localeconv()` call **and** the reads/decodes of its result in `_locale_localeconv_impl` (a module- or interpreter-level lock), or snapshot the `struct lconv` under the lock before decoding. The libc function itself cannot be made thread-safe portably (`localeconv_l` / a per-thread copy are the alternatives). This is the CPython-side half of cpython#127081.

## Notes

- **This is an instance of cpython#127081** ("Thread-unsafe libc functions", OPEN) — the umbrella for CPython wrappers of non-thread-safe libc functions. Cataloged for dedup; not a new filing.
- The signature `heap-use-after-free Objects/obmalloc.c:_PyMem_Strdup` is somewhat generic — verify `locale_decode_monetary` / `localeconv` is on the stack before trusting this label.
- Found by `fusil --tsan` (fleet 12), surfaced via `--tsan-no-halt`.

---

*Instance of cpython#127081 (thread-unsafe libc functions). Recorded for the catalog; not a separate filing.*
