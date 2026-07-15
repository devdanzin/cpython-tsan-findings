import sys, threading, marshal
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# w_complex_object serializes a list by reading PyList_GET_ITEM(v, i) -> ob_item[i]
# with a PLAIN (non-atomic) load and no critical section on the list (marshal.c:605).
# Another thread doing list.append() stores into that same ob_item[] array with an
# ATOMIC release store (_PyList_AppendTakeRef, pycore_list.h:46). Marshalling a shared
# list while a sibling thread appends to it is therefore a data race on ob_item[].
NT_MAR = 3          # threads calling marshal.dumps(shared_list)
NT_APP = 3          # threads calling shared_list.append(...)
NT = NT_MAR + NT_APP
ROUNDS = 3000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def marshaller():
    for _ in range(ROUNDS):
        enter.wait()
        lst = pool[0]
        for _ in range(40):
            marshal.dumps(lst)          # w_complex_object: read ob_item[i] non-atomically
        leave.wait()

def appender():
    for _ in range(ROUNDS):
        enter.wait()
        lst = pool[0]
        for i in range(300):
            lst.append(i)               # _PyList_AppendTakeRef: atomic store to ob_item[]
        leave.wait()

ts = [threading.Thread(target=marshaller) for _ in range(NT_MAR)]
ts += [threading.Thread(target=appender) for _ in range(NT_APP)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [0, 1, 2]                 # fresh small list each round: keeps it growing
    enter.wait()                        # release marshallers + appenders onto same list
    leave.wait()                        # wait for the round to finish
for t in ts: t.join()
print("done, no crash")
