import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0 on a --disable-gil build"

# One shared list, sorted concurrently by several threads with no user lock.
# list.sort() takes NO critical section on the list -- it detaches the items array
# (ob_item=NULL, size=0) and sorts saved_ob_item IN PLACE. Two sorters that both grab
# ob_item in the window before the other detaches rewrite the same array concurrently
# -> the binarysort | binarysort race (crash-safe, but a data race).
shared = [f"{i:04d}" for i in range(512)]
N = 200_000
NT = 4
start = threading.Barrier(NT)

def sorter(k):
    start.wait()
    for i in range(N):
        shared.sort(reverse=bool((i + k) & 1))   # alternate order so each sort does real work

ts = [threading.Thread(target=sorter, args=(k,)) for k in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
