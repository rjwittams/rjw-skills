# Data and memory

Choose structures from the operations in the workload note. Where there's one, there's many: design for the collection, not the single item.

- **Cost every operation.** For each read, write, update and query: time complexity at the ceiling, update cost, and memory per entry. A structure is right when its worst common operation is cheap, not its first one.
- **Shape follows the query.** If the hot query is "by owner" or "by label", the structure is indexed by owner or label. Scanning a whole kind and filtering is a choice that needs a cardinality bound to justify it.
- **Pack and place.** Decide where each piece of information lives for locality: next to the data it is read with, in the process that reads it most. Pack hot fields densely; keep cold or large fields out of the hot path (interning, IDs instead of copies, columnar layouts, arenas).
- **Walk the candidates** (see the evidence rule in [SKILL.md](SKILL.md)). What survives is the choice.
- **Custom structures stay local.** A purpose-built index, ring, trie or packed table inside one module is an ordinary choice when the candidate walk rejects the library ones. An established crate that provides it beats writing your own.
- **Allocate by lifetime.** Group data that dies together (arenas, nested lifetimes) and release a lifetime in one operation; stable addresses let other structures refer into it.
- **Every per-entry field names its reader.** For each field held per entry, name the operation that reads it; debugging, audit and provenance count. A field no operation reads costs its size times the count, and often work on every read as well (arbitration over a precedence nobody sets). Model at the level consumers read: when they read an entity whole, store the entity, not per-field machinery.
- **Share what repeats.** When the census shows repetition, intern repeated values and share repeated keysets (shapes, as in hidden classes: the keys once per shape, each record's values as an array), with a sweep so the shared table reaches a steady state. Columnar layout wins instead when the hot operation scans one field across records.
- **Defaults carry a threat model.** Standard hashers, allocators and formatters are tuned for cases you may not have, such as DoS-resistant hashing on keys the process itself controls. Name the default in the candidate walk and say why it holds.
- **Share instead of copy.** One owned copy, shared by reference (reference counting, borrowed views, shared byte buffers, slices) to every consumer. A clone is a decision with a size.
- **Stream instead of materialise.** Process results as they arrive (iterators, streams, incremental parsing) and collect only what the consumer must hold at once.
- **Keep the representation the hot path needs.** Decode to the fields that are read, not a generic tree or the whole resource; keep wire bytes and decode on access when that is cheaper (see [PROTOCOLS.md](PROTOCOLS.md)).
- **Memory is a budget.** State resident size as a function of the workload (bytes per entry times count), so any deployment can check its own fit; check it against the reference deployments at the ceiling. Find where it goes with a census (Evidence in [SKILL.md](SKILL.md)).
- **Steady state.** Anything that grows with use (memory, caches, tables, logs, queues, temp files, intern tables) has a retention or eviction rule, so the system runs indefinitely without a human cleaning up.
