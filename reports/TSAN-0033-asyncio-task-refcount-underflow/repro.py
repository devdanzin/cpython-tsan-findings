"""
TSAN-0033 - _asyncio.Task refcount-0-while-GC-tracked premature-free abort.

A shared/transient _asyncio.Task is driven to refcount 0 while it is still
GC-tracked, and a concurrent gc.collect() on another thread catches it via the
Py_DEBUG GC invariant check validate_refcounts():

    Python/gc_free_threading.c:1083: validate_refcounts:
      Assertion "_Py_REFCNT(((PyObject*)((op)))) > 0" failed:
      tracked objects must have a reference count > 0
    object type name: _asyncio.Task
    Fatal Python error: _PyObject_AssertFailed   (SIGABRT / exit 134)

Root cause (Modules/_asynciomodule.c, TaskObj_dealloc):
    static void
    TaskObj_dealloc(PyObject *self)
    {
        if (PyObject_CallFinalizerFromDealloc(self) < 0)   // runs Python __del__
            return;
        unregister_task((TaskObj *)self);   // <- cross-thread => _PyEval_StopTheWorld
        PyObject_GC_UnTrack(self);          // <- untrack happens ONLY here, too late
        ...
    }
unregister_task() takes the cross-thread branch (task->task_tid != _Py_ThreadId())
and calls _PyEval_StopTheWorld() while the object is already at refcount 0 but has
NOT yet been PyObject_GC_UnTrack()ed. During that stop-the-world window a
concurrent gc.collect() on another thread runs validate_refcounts() over every
GC-tracked object and finds the refcount-0 Task -> abort.

A Task built with a non-coroutine argument fails Task.__init__ at the coroutine
check *before* task_tid is assigned, so task_tid stays 0 and EVERY such dealloc
takes the StopTheWorld branch -> the window is wide and the crash is reliable at
just 2 threads.  (A well-formed Task deallocated on a thread other than its
creator hits the same branch.)

Run under a free-threaded debug build (GIL off).  This is a Py_DEBUG GC check, so
it fires on the plain debug-ft build too -- NOT a ThreadSanitizer artifact.
"""
import sys
import gc
import threading
import _asyncio

assert not sys._is_gil_enabled(), "need a --disable-gil build with PYTHON_GIL=0"

N = 4
ITERS = 6000
barrier = threading.Barrier(N)


def worker():
    barrier.wait()
    for i in range(ITERS):
        # Construct a Task with a non-coroutine arg: __init__ fails the coroutine
        # check *before* setting task_tid, so the transient Task's dealloc takes
        # unregister_task()'s cross-thread _PyEval_StopTheWorld branch while the
        # object is refcount-0-but-still-GC-tracked.
        try:
            _asyncio.Task([1, 2, 3])
        except Exception:
            pass
        # Concurrent GC on the sibling threads runs validate_refcounts() and
        # catches the refcount-0 tracked Task.
        if i % 16 == 0:
            gc.collect()


threads = [threading.Thread(target=worker, name="t%d" % k) for k in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("no crash (did not reproduce this run)")
