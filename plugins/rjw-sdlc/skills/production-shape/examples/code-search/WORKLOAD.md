# Workload model: code search (desktop tool)

Design and review data-touching work with the `production-shape` skill: production shape,
cost test, naive shape, workload note, candidate walk, bounded consumer, snapshot then
deltas, query cover, operation lowering, single writer, steady state, match the defence to
the boundary, evolution level.

Root file: every absolute number lives here. Component `WORKLOAD.md` files state numbers per
parent and refer to these by the names in backticks.

## Maturity

Production. Distributed to developers; runs on their machines alongside their editor.

## Scale drivers

The only place with now / target / ceiling. One user; the data is what scales. Everything
else refers to these by the name in backticks.

| Driver | Now | Target | Ceiling | Growth | Source |
|---|---|---|---|---|---|
| `repo.files` | 20k | 200k | 2M (large monorepo) | users adopt it on their biggest repo first | user telemetry opt-in, 2026-09 |
| `repo.dirs` | 2k | 20k | 200k | ~1 per 10 files | same |
| `repos.open` | 1 | 3 | 10 | | same |
| `branch_switch.files` | 1k | 10k | 200k | tracks `repo.files` | estimate |

## Shared entities

| Entity | Count per parent (mean / p50 / p99) | Size (mean / p99) | Distribution | Source | Retention |
|---|---|---|---|---|---|
| `file` | per repo: `repo.files` | 8 KB / 400 KB; max indexed 1 MB | generated and vendored files dominate the tail | external: file system | as long as the file exists |
| `change` | per save: 1.2 / 1 / 1k (formatter, codegen) | | | external: file system | until indexed |

## Usage

| Operation | Arrival | Rate | Work per operation | Latency | Staleness tolerated |
|---|---|---|---|---|---|
| `queries`: search as you type | closed: one user, think time ~100 ms | <= 10/s per user | candidates drawn from `repo.files` | first results < 50 ms | reflects a save within 1 s |
| `opens`: open a repo (cold index) | closed, rare | per repo per tool upgrade | all of `repo.files` | usable < 10 s at target; partial results while building | n/a |
| `saves`: save | open, from the editor | ~1 per 10 s | `change` files | | |
| `branch_switch`: branch switch (step) | open, from git | rare | `branch_switch.files` changes | consistent < 5 s at target | partial results flagged until caught up |

## Dependencies and budgets

| System | Used for | Budget | Availability |
|---|---|---|---|
| `fs.watch` (inotify / FSEvents) | change events | Linux: `inotify.max_user_watches`, often 8,192 on older defaults, shared with the editor; macOS: one stream per root | events coalesce and can overflow; overflow means "rescan this subtree" |
| `fd.limit` | open files | macOS default soft limit 256, shared with the process | |
| `disk` (local SSD) | reading sources, index storage | ~1 GB/s sequential read; `stat` ~2 µs warm | page cache shared with the editor and builds |

## Evolution

| Surface | Level | Window | Refinements |
|---|---|---|---|
| Index | rebuildable | | format version in the header; rebuild on mismatch |
| User settings | forward-migrated | | |
| `cs query --json` | public contract | 2 releases deprecation | |

## Reference deployments

- **`laptop`**: 8 cores, 16 GB, SSD. Shared with an IDE, a browser and builds. Tool budget:
  `laptop.memory` = 500 MB resident; background indexing at low priority, at most 2 cores.

## Bounded consumers

- Results list: top 200 by rank, "200 of ~12k matches", more on scroll; 30 ms render budget.
- Preview pane: a window of 2k lines around the match.
- `cs query --json` (scripts and agents): at most 100 matches with `"complete": false`.

## Recording guidance

Component files below this one follow the standard format, and also:

- Record the syscalls each operation makes per invocation (`stat`, `open`, bytes read,
  watches held): on a laptop these are the cost drivers, not network calls.

## Conventions

- Design decisions: PR description.
- ADRs: `docs/adr/`.
- Implementation choices: `docs/implementation-choices/`.

## Validation

Last checked 2026-09 against opt-in telemetry (repo sizes, query latency p99). Revise when
`repo.files` p99 among users moves past the target.
