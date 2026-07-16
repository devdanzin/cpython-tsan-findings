import sys, threading
from xml.etree.ElementTree import XMLParser
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# XMLParser._setevents(events_queue, events_to_report) iterates events_to_report with the C
# macro PySequence_Fast_GET_ITEM (reads list->ob_item[i] directly, no per-object lock).
# PySequence_Fast returns a real list UNCHANGED, so events_to_report IS the caller's list.
# If another thread appends to that same shared list, list_resize reallocates + memcpys the
# ob_item buffer out from under the reader -> data race (and a potential use-after-free).
# This is the path XMLPullParser(events=<list>) / iterparse take internally.
NT_READ = 4
NT_APPEND = 4
ROUNDS = 4000
REPEAT = 40          # _setevents calls per reader per round (widen the read window)
APPENDS = 400        # appends per appender per round (force list_resize)

cur = [None]         # the fresh shared list for the current round
enter = threading.Barrier(NT_READ + NT_APPEND + 1)
leave = threading.Barrier(NT_READ + NT_APPEND + 1)

def reader():
    p = XMLParser()      # own parser -> only the shared list races, not parser state
    q = []
    for _ in range(ROUNDS):
        enter.wait()
        L = cur[0]
        for _ in range(REPEAT):
            try:
                p._setevents(q, L)          # reads L->ob_item[i] via PySequence_Fast_GET_ITEM
            except BaseException:
                pass
        leave.wait()

def appender():
    for _ in range(ROUNDS):
        enter.wait()
        L = cur[0]
        try:
            for _ in range(APPENDS):
                L.append("end")             # list.append -> list_resize memcpy of ob_item
        except BaseException:
            pass
        leave.wait()

ts = [threading.Thread(target=reader) for _ in range(NT_READ)]
ts += [threading.Thread(target=appender) for _ in range(NT_APPEND)]
for t in ts:
    t.start()
for r in range(ROUNDS):
    cur[0] = ["end"] * 8                    # fresh, short list -> keeps resizing each round
    enter.wait()
    leave.wait()
for t in ts:
    t.join()
print("done, no race detected")
