import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"
import xml.etree.ElementTree as ET

# A single _elementtree.TreeBuilder carries all of its parse state in plain struct
# fields:  self->data (the pending text/tail collector), self->last, self->this,
# self->last_for_tail, self->index and self->stack (the open-element stack).
#
# Every public feed method mutates those fields IN PLACE with no lock and no
# @critical_section (Modules/_elementtree.c has zero Py_BEGIN_CRITICAL_SECTION):
#   .start()  -> treebuilder_handle_start  : flush_data + Py_CLEAR(self->data),
#                                            writes self->this/last/last_for_tail/index/stack
#   .data()   -> treebuilder_handle_data   : reads self->last, writes self->data
#   .end()    -> treebuilder_handle_end    : flush_data + writes self->last/this/index
#   .comment()-> treebuilder_handle_comment: flush_data (reads/clears self->data)
#   .pi()     -> treebuilder_handle_pi     : flush_data (reads/clears self->data)
# treebuilder_flush_data() reads `if (!self->data)` (:2684) and, via
# treebuilder_extend_element_text_or_tail(), writes self->data with Py_CLEAR (:2644).
#
# Two threads calling ANY of these on the SAME shared builder therefore race on the
# builder's own internal parse state -> ThreadSanitizer data race (treebuilder_* vs
# treebuilder_*). A fresh builder each round keeps the just-started fields hot so the
# window hits reliably.

N = 6                 # worker threads, all hammering ONE shared TreeBuilder
ROUNDS = 4000

box = [None]
enter = threading.Barrier(N + 1)
leave = threading.Barrier(N + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        tb = box[0]
        for i in range(8):
            try:
                tb.start("t%d" % (i & 3), {})   # handle_start (flush_data + writes)
                tb.data("x")                    # handle_data  (reads self->last, writes self->data)
                tb.comment("c")                 # handle_comment (flush_data)
                tb.pi("p", "d")                 # handle_pi     (flush_data)
                tb.end("t%d" % (i & 3))         # handle_end    (flush_data + writes self->last/index)
            except Exception:
                pass
        leave.wait()

threads = [threading.Thread(target=worker, args=(w,)) for w in range(N)]
for t in threads:
    t.start()

for r in range(ROUNDS):
    box[0] = ET.TreeBuilder()   # fresh shared builder each round
    enter.wait()                # release workers onto the fresh builder
    leave.wait()                # wait for them to finish this round
for t in threads:
    t.join()
print("done, no crash")
