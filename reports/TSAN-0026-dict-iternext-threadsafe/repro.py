import sys
from threading import Barrier, Thread
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# dictiter_iternext_threadsafe (the FT-safe dict-iterator "next") tests
#     if (_PyDict_HasSplitTable(d)) { ... }        # dictobject.c:6043
# and that macro is a PLAIN read of d->ma_values:
#     #define _PyDict_HasSplitTable(d) ((d)->ma_values != NULL)
# Meanwhile a setattr() that overflows an instance's *full* shared (split)
# keys converts the __dict__ split->combined inside dictresize, which
# publishes the new (NULL) ma_values with an ATOMIC release store:
#     set_values(mp, NULL) -> _Py_atomic_store_ptr_release(&mp->ma_values)  # :215 / pyatomic_gcc.h
# Plain read (6043) vs atomic store on the SAME ma_values word == TSan data
# race -- even though the very next line (6044) reads ma_values atomically
# with _Py_atomic_load_ptr_consume. An incomplete atomic conversion.
#
# To make the split->combined conversion the *first* thing a setattr does
# (so no in-place inline-value writes race first), we fill the type's shared
# keys to SHARED_KEYS_MAX_SIZE (30) up front: after that dk_usable==0, so
# insert_split_key() returns DKIX_EMPTY on any new key and setattr goes
# straight to _PyDict_SetItem_LockHeld -> dictresize -> set_values(NULL).

N_ITER = 3
N_MUT = 3
ROUNDS = 6000

BASE = [f"a{i}" for i in range(30)]   # 30 == SHARED_KEYS_MAX_SIZE -> fills shared keys, dk_usable->0
LIVE = BASE[:15]                      # attrs each fresh instance actually carries (live split entries)

box = [None]
enter = Barrier(N_ITER + N_MUT + 1)
leave = Barrier(N_ITER + N_MUT + 1)


class C:
    pass


# Prime the type's shared keys to full so no fresh instance can extend them.
for _ in range(3):
    _p = C()
    for _a in BASE:
        setattr(_p, _a, 0)
    _p.__dict__


def make_split_instance():
    obj = C()
    for a in LIVE:
        setattr(obj, a, 0)     # all present in shared keys -> stays SPLIT
    obj.__dict__               # materialize split dict (store_instance_attr_dict path)
    return obj


def iterator_worker():
    for _ in range(ROUNDS):
        enter.wait()
        d = box[0].__dict__
        try:
            for _k in d:                     # dictiter_iternext_threadsafe: HasSplitTable read @6043
                pass
        except RuntimeError:
            pass                             # "changed size"/"keys changed" during iteration = expected
        leave.wait()


def mutator_worker():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        for n in range(6):
            try:
                setattr(obj, f"z{n}", n)     # new key, shared keys full -> dictresize -> set_values(NULL) @215
            except Exception:
                pass
        leave.wait()


threads = ([Thread(target=iterator_worker) for _ in range(N_ITER)] +
           [Thread(target=mutator_worker) for _ in range(N_MUT)])
for t in threads:
    t.start()

for r in range(ROUNDS):
    box[0] = make_split_instance()
    enter.wait()   # release workers onto the fresh split instance
    leave.wait()   # wait for them to finish this round

for t in threads:
    t.join()
print("done, no crash")
