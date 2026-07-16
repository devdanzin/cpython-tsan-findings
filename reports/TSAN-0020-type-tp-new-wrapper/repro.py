import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"
import ssl

# ssl.SSLContext is a Python subclass of the C type _ssl._SSLContext. Its Python __new__
# (slot_tp_new) calls super().__new__(...) -- i.e. _ssl._SSLContext.__new__, which is the
# generic C wrapper tp_new_wrapper (Objects/typeobject.c:10478, `type->tp_new(...)`). That
# reaches SSL_CTX_new() in libcrypto/libssl, which populates OpenSSL's process-global
# algorithm-fetch / method-store cache: it memcpy()s a freshly CRYPTO_malloc'd 104-byte
# entry into the store (under an OpenSSL CRYPTO_THREAD lock) and looks entries up with
# memcmp() (on a lock-free fast path). That cache is first-touch: it is written only while
# still cold, so the race window is the *first* concurrent construction. We therefore keep
# all threads stepping in lockstep through the first few constructions (per-round barrier)
# so their cold-cache accesses overlap: concurrent memcmp (read) vs memcpy (write) on the
# same 104-byte OpenSSL struct.
#
# NOTE: each thread builds its OWN, separate SSLContext (separate SSL_CTX). Nothing is
# shared at the Python level -- the only shared state is OpenSSL's internal global cache,
# which OpenSSL is responsible for synchronizing. The tp_new_wrapper frame is only the
# nearest *symbolized* frame; libcrypto is stripped ("<null> <null>"). See report.md.

NT = 24
ROUNDS = 8
CIPHERS = ["DEFAULT", "ALL", "HIGH", "AES256-SHA",
           "ECDHE-RSA-AES128-GCM-SHA256", "AES128-SHA256"]
step = threading.Barrier(NT)


def worker(wid):
    for r in range(ROUNDS):
        step.wait()                       # all threads first-touch together each round
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # -> tp_new_wrapper -> SSL_CTX_new
            ctx.set_ciphers(CIPHERS[(wid + r) % len(CIPHERS)])
        except Exception:
            pass


ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print("done, no crash")
