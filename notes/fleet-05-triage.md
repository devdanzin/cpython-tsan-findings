# Fleet 05 triage (fusil-tsan_fleet_05, 2026-07-18)

4 instances (each restarted once → `python` + `python-2`), **1297 crash dirs**. This is the
**last fleet run on the pre-iterator-ops emitter** — fusil PR #211 (shared-iterator +
read-while-mutate op classes) landed after it was stopped; fleet 06 is the first with the new ops.
Ingested with the correct `inst-*/python*/*` glob.

## Result

1297 dirs → **27 known races deduped** (~709 vehicles) + 450 suppressed + 102 noparse, and **14 new
signature groups → 0 after triage**. Every "new" group was an additional *face* of an entry we
already hold (13 folded into the catalog) or the subinterpreter machinery (1 suppressed). **No new
bug.** The catalog continues to converge — fleets 01–05 now all ingest 0 new groups.

Known-race tally: TSAN-0001/0002/0004/0005/0007/0009/0013/0014 (40 each — capped at `--tsan-dedup-keep`),
0006 (39), 0008 (39), 0030 (38), 0015 (37), 0010 (36), 0019 (35), 0012 (33), 0016 (33), 0023 (20),
0031 (19), 0024 (18), 0028 (16), 0018 (15), 0026 (13), 0025 (12), 0029 (4), 0011 (2), 0035 (2), 0032 (1).

## Folded — new faces of existing entries (13 signatures)

Added to the respective `meta.json` `signatures` (→ `known_races.tsv`, 104→117 sigs / 33 races):

- **TSAN-0008** (lsprof profiler state): `Stop | ptrace_leave_call`.
- **TSAN-0009** (shared pyexpat parser): `PyExpat_XML_SetParamEntityParsing | (self)`,
  `PyExpat_XML_SetStartElementHandler | (self)` (more handler-setter self-races), `poolAppendChar | poolGrow`
  (expat string-pool grow).
- **TSAN-0012** (faulthandler enabled flag): `faulthandler_disable | faulthandler_enable`.
- **TSAN-0014** (concurrent `list.sort()` of a shared list): `_Py_atomic_load_ptr | sortslice_copy_incr`,
  `… | sortslice_memmove` (siblings of the cataloged `sortslice_copy_decr`).
- **TSAN-0024** (fd closed under concurrent use): `_io_FileIO___init___impl | _Py_read`; and the
  **socket/epoll fd-lifecycle face** `select_epoll_unregister_impl | _socket_socket_close_impl` — TSan
  "Location is file descriptor N": one thread `socket.close()`s the fd while another `epoll_ctl(DEL)`s it.
- **TSAN-0030** (sys.monitoring tool-id registry): `monitoring_clear_tool_id_impl | monitoring_free_tool_id_impl`,
  `monitoring_free_tool_id_impl | monitoring_get_tool_impl`.
- **TSAN-0031** (shared `_elementtree` concurrent mutation): `treebuilder_flush_data | treebuilder_handle_start`;
  and the **Element face** `element_add_subelement | element_resize` (`_elementtree.c:486`) — concurrent grow
  of a shared `Element`'s `_children` array, both reached via `TreeBuilder.start()`. Distinct data structure
  from the TreeBuilder parse-state faces, same "don't share a mutable ElementTree across threads" root.

## Suppressed — subinterpreter machinery (1, out of scope per cpython#143232)

`posixmodule_exec | initialize_structseq_dict` — race on the static global `stat_result_fields`
(`Modules/posixmodule.c:18817`) while the posix module init re-runs under concurrent interpreter
creation (via `_interpreters`). Same class as the fleet-04 `posixmodule_exec | count_members` entry;
added to `catalog/suppressions.txt`.

## noparse (102)

86 are the **identical** GC assertion `_Py_REFCNT(op) > 0 failed: tracked objects must have a reference`
— the free-threading refcount-race abort face: a concurrent refcount race drops a GC-tracked object to
0, and the debug build's `_PyObject_GC_TRACK` catches it. No clean TSan race pair (the producer already
corrupted the refcount; pinning it needs `rr`), so it stays noparse — the known abort category, not a new
race. The other 16 are clean/no-race exit noise.

## After this triage

Re-ingest of fleet-05 → **0 new signature groups** (450 suppressed, all races catalog-matched). Catalog:
`known_races.tsv` regenerated; `suppressions.txt` +1. Fleet 06 (new ops) will fold all of the above.
