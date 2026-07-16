import sys, threading, readline
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# Data race on CPython's readline module-level C global `should_auto_add_history`
# (Modules/readline.c:825, a plain `static int`). readline.set_auto_history()
# stores it with a plain, unsynchronized write (readline.c:843) and is NOT
# @critical_section; two threads toggling it concurrently is a write/write race
# on that global (TSan reports the pair of writes at readline.c:843).

NT = 6                       # total worker threads
ROUNDS = 4000
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(on):
    for _ in range(ROUNDS):
        enter.wait()
        readline.set_auto_history(on)   # write should_auto_add_history (:843)
        readline.set_auto_history(not on)
        leave.wait()

# half the threads drive the flag toward True, half toward False -> the store
# actually changes the word both ways, maximising the racing-write window.
ts = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(NT)]
for t in ts: t.start()
for _ in range(ROUNDS):
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
