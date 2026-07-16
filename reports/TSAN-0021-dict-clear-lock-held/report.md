# Data race: `dict.clear()` reads a shared split-keys `dk_nentries` non-atomically (`dictobject.c:3110`/`3115`)

*`clear_lock_held()` reads `oldkeys->dk_nentries` with a **plain** (non-atomic) load while another thread inserting a new attribute into a **different instance of the same class** bumps that same `dk_nentries` with a **relaxed atomic** store in `split_keys_entry_added()`. Instances of one class share a single `PyDictKeysObject` (the type's `ht_cached_keys` / split keys); the clear path holds only the per-dict critical section, not the shared-keys lock, so the two accesses to the shared count are a data race. Not two threads clearing the same dict — the shared state is the **shared split-keys object**, touched by clear-on-one-instance vs insert-on-another-instance.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

Under free-threading, all instances of a class share one `PyDictKeysObject` (the type's cached "split" keys). The per-instance `__dict__` is a *split dict*: its own values array, but a shared keys object whose `dk_nentries` counts how many attribute slots exist.

* **Writer** — adding a *new* attribute name to any instance calls `insert_split_key()`, which (under `LOCK_KEYS`) grows the shared keys and bumps the count with a relaxed atomic store:
  ```c
  // Objects/dictobject.c:242-250  split_keys_entry_added()
  _Py_atomic_store_ssize_relaxed(&keys->dk_nentries, keys->dk_nentries + 1);   // :248
  ```
* **Reader** — clearing an instance dict (`dict.clear()`, or GC's `dict_tp_clear`) calls `clear_lock_held()`, which reads that same shared count with a *plain* load to bound its cleanup loop:
  ```c
  // Objects/dictobject.c  clear_lock_held()
  clear_embedded_values(oldvalues, oldkeys->dk_nentries);   // :3110  (embedded split dict)
  ...
  n = oldkeys->dk_nentries;                                  // :3115  (non-embedded split dict)
  ```

The clear path holds only `Py_BEGIN_CRITICAL_SECTION(op)` on *its own* dict — that does **not** exclude an insert into a *sibling* instance dict that shares the keys, and it does not hold `LOCK_KEYS(keys)`. So the plain read races with the atomic write on the shared `dk_nentries`. TSan flags it as a mixed atomic/non-atomic access (a data race regardless of value outcome).

It is value-benign in practice: `dk_nentries` is an aligned 8-byte `Py_ssize_t` (no tearing on x86-64), stays within the values array's capacity, and unfilled slots are `NULL` (`Py_XDECREF(NULL)` is safe) — so a stale read cannot over-read or crash. But it is a genuine, TSan-reported data race on shared internal state reachable from ordinary, read-only-looking `dict.clear()` / attribute churn.

## Reproducer

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 8
NWRITERS = 2          # grow shared keys; rest clear instance dicts
ROUNDS = 400
NINST = 24
NNAMES = 25           # < SHARED_KEYS_MAX_SIZE (30) so dicts stay split
box = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    grow = (wid < NWRITERS)
    for _ in range(ROUNDS):
        enter.wait()
        insts, names = box[0]
        if grow:
            for inst in insts:
                for nm in names:
                    try: setattr(inst, nm, 1)   # insert_split_key -> split_keys_entry_added (atomic write dk_nentries)
                    except Exception: pass
        else:
            for inst in insts:
                try: inst.__dict__.clear()      # clear_lock_held reads dk_nentries (plain)
                except Exception: pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    C = type(f"C{r}", (), {})                  # fresh class -> fresh empty shared keys
    insts = [C() for _ in range(NINST)]
    names = [f"a{r}_{k}" for k in range(NNAMES)]
    box[0] = (insts, names)
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Key points: a **fresh class each round** gives fresh empty shared keys, so the writers actually *grow* `dk_nentries` (only the first insertion of a name grows it); keeping `< SHARED_KEYS_MAX_SIZE` (30) names keeps the dicts split; readers and writers hit the *same* shared keys object because they operate on instances of the same class.

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, debug-ft-nojit-tsan)

Detected on every run (exit 66). The racing **pair is invariant**; TSan names as SUMMARY whichever access it catches "current" (it alternates run-to-run between `clear_lock_held` and the writer's inlined `_Py_atomic_store_ssize_relaxed`). Canonical run (SUMMARY = `clear_lock_held`, matching the seeded vehicle):

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x7fffb6867e28 by thread T8:
    #0 clear_lock_held        Objects/dictobject.c:3110:51   (oldkeys->dk_nentries, plain read)
    #1 PyDict_Clear           Objects/dictobject.c:3136:5
    #2 dict_clear_impl        Objects/dictobject.c:4927:5
    #3 dict_clear             Objects/clinic/dictobject.c.h:170:12
    ...                       (Python: inst.__dict__.clear())

  Previous atomic write of size 8 at 0x7fffb6867e28 by thread T1:
    #0 _Py_atomic_store_ssize_relaxed  Include/cpython/pyatomic_gcc.h:513:3
    #1 split_keys_entry_added          Objects/dictobject.c:248:5   (dk_nentries = dk_nentries + 1)
    #2 insert_split_key                Objects/dictobject.c:1940:9
    #3 store_instance_attr_lock_held   Objects/dictobject.c:7396:14
    #4 store_instance_attr_dict        Objects/dictobject.c:7483:15
    ...                                (Python: setattr(inst, name, 1))

SUMMARY: ThreadSanitizer: data race Objects/dictobject.c:3110:51 in clear_lock_held
```

No `Location is …` line is emitted (the shared `PyDictKeysObject` comes from CPython's own allocator / QSBR-delayed pool, which TSan does not attribute to a named heap/global region). Both accesses agree on the same address (`0x7fffb6867e28`), i.e. the shared keys object's `dk_nentries` field. This is the same pairing and address-region as the seeded fleet vehicle (which caught the mirror direction — `clear_lock_held` read at `:3115` via GC `dict_tp_clear`, vs the same `split_keys_entry_added` atomic write via `object.__dir__` -> `merge_class_dict`).

The seeded catalog signature `clear_lock_held | clear_lock_held` is a dedup artifact: the deduper keyed both sides to the SUMMARY function. The real counter-party of the race is `split_keys_entry_added` (via `insert_split_key`), as both the vehicle and this repro show.

## Root cause

Free-threaded split-keys design makes readers of the shared `dk_nentries` lock-free: the writer deliberately increments the count *before* decrementing `dk_usable` "so we never get too small of a value when we're racing with reads" (comment at `dictobject.c:246-247`), and provides an atomic reader macro:

```c
// Objects/dictobject.c
#define LOCK_KEYS(keys)          PyMutex_LockFlags(&keys->dk_mutex, ...)          // :227
#define LOAD_KEYS_NENTRIES(keys) _Py_atomic_load_ssize_relaxed(&keys->dk_nentries)// :237
```

`LOAD_KEYS_NENTRIES` is the intended way to read the shared count without the keys lock, and is used correctly elsewhere (e.g. `dict_equal`, `dictobject.c:4632`). But `clear_lock_held()` reads the field **directly** at `:3110` (embedded split path) and `:3115` (non-embedded split path) while holding only the per-dict critical section — which does not exclude an insert into a *sibling* instance dict that shares the same keys object, and is not `LOCK_KEYS`. So the plain read races with the relaxed atomic write in `split_keys_entry_added()` (`:248`).

Restated: the shared memory is one `PyDictKeysObject.dk_nentries` reached from **two different `PyDictObject`s** (two instances of the same class), not the same dict cleared twice. Two threads clearing the *same* dict would be excluded by that dict's critical section; the gap is specifically clear-vs-insert across instances that share split keys.

## Impact / severity

**Low.** Value-benign and crash-free in practice: 8-byte aligned `Py_ssize_t` (no torn read on x86-64), the count stays within the values array capacity, and unfilled slots are `NULL`, so a stale read neither over-reads nor mis-frees. But it is a real, reliably-reproduced TSan data race (mixed atomic/non-atomic access to shared internal state) on the ordinary `dict.clear()` / attribute-mutation paths — no explicit "unsafe" API. It belongs to the known free-threaded shared-split-keys hardening class (the same family as the `LOAD_KEYS_NENTRIES` / `LOCK_KEYS` machinery already added for split keys).

## Suggested fix

Read the shared count through the existing relaxed-atomic accessor in `clear_lock_held()`, matching the writer's relaxed atomic store and the reader idiom already used at `dictobject.c:4632`:

```c
// Objects/dictobject.c, clear_lock_held()
else if (oldvalues->embedded) {
    clear_embedded_values(oldvalues, LOAD_KEYS_NENTRIES(oldkeys));   // was: oldkeys->dk_nentries  (:3110)
}
else {
    ...
    n = LOAD_KEYS_NENTRIES(oldkeys);                                 // was: oldkeys->dk_nentries  (:3115)
    ...
}
```

Relaxed ordering is sufficient (the count is idempotent/monotonic w.r.t. this read and the loop only bounds a cleanup over per-instance slots). This is TSan-clean and behaviour-preserving.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet `fusil-tsan_fleet_02`, vehicle `inst-03/python/urllib_request-...-tsanNEW` (which reached the reader via cyclic GC `dict_tp_clear` and the writer via `object.__dir__` -> `merge_class_dict` -> `PyDict_Update`). The minimal repro drives the same race directly via `setattr` (grow shared keys) vs `inst.__dict__.clear()`. Audit `clear_lock_held` and its siblings for any other plain reads of shared split-keys fields (`dk_nentries`, `dk_usable`); the resize-path reads at `:2238`/`:2255` operate on exclusively-owned keys and are out of scope here.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
