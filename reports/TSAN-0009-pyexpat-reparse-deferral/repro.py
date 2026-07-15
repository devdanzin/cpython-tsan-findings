import sys, threading
import pyexpat
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A pyexpat parser caches "reparse deferral" in the plain 1-byte XML_Bool field
# m_reparseDeferralEnabled (Modules/expat/xmlparse.c:689). SetReparseDeferralEnabled()
# writes it (xmlparse.c:3035) with no lock, while Parse() -> XML_ParseBuffer ->
# callProcessor reads it (xmlparse.c:1136). A parser shared across threads that mixes
# SetReparseDeferralEnabled() with Parse() races on that byte.
NT = 6                     # 1 parser-driver + (NT-1) flag-writers
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

# Well-formed-so-far prefix (root never closed, isfinal=0) so repeated Parse() calls on
# one parser never error -- callProcessor still reads the flag on every call.
CHUNK = b"<r>" + b"<e a='1'/>" * 8

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        p = pool[0]
        if wid == 0:
            for _ in range(6):
                try:
                    p.Parse(CHUNK, 0)        # -> callProcessor: read m_reparseDeferralEnabled
                except Exception:
                    pass
        else:
            for k in range(6):
                try:
                    p.SetReparseDeferralEnabled(bool((wid + k) & 1))  # write m_reparseDeferralEnabled
                except Exception:
                    pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = pyexpat.ParserCreate()          # fresh parser each round (first-touch)
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
