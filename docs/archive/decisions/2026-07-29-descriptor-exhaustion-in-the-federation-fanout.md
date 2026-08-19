# The descriptor wedge, and the leak that made it permanent

**2026-07-29** · `server/_overview.py::_each_store`

`overview` opened the whole federation before touching any of it.

Each SQLite WAL connection is three descriptors — db, `-wal`, `-shm` — so on the largest
workspace's 157 graph-bearing members that is a peak of ~471 held for the length of one request.
A federated `search` fans out over the same members and can be doing it at the same time.

`_each_store` now yields in batches of `_FANOUT_WORKERS`. Capping the peak is what keeps the
shortage unreachable rather than merely survivable.

The batch size is `query/search.py`'s `_FANOUT_WORKERS` and not a second constant of its own: it
is the same federation fanned out over the same members, and two knobs for one property drift
apart.

## The leak half

The wedge was reachable at all because a comprehension bound its list only after the last element.
An exception partway through the fan-out orphaned every store already opened — so each failed
request permanently lost descriptors instead of merely spiking them.

`ExitStack.enter_context` takes ownership of each store the moment it exists and unwinds the ones
it holds on any exit. That is a structural fix, not a wider `try`.

`closing(GraphStore(...))` rather than `with GraphStore(...)`: the class exposes `close()` and no
`__enter__`/`__exit__`. The federated search path in `query/search.py` uses the same stdlib adapter
for the same reason.

**The transferable lesson:** a resource acquired inside a comprehension is owned by nothing until
the comprehension finishes. Acquire and register ownership in the same step.
