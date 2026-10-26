# Workload model: CI service

Design and review data-touching work with the `production-shape` skill: production shape,
cost test, naive shape, workload note, candidate walk, bounded consumer, snapshot then
deltas, query cover, operation lowering, single writer, steady state, match the defence to
the boundary, evolution level.

This is the root file: it holds every absolute number. Component `WORKLOAD.md` files below
it state their numbers per parent and inherit everything else from here.

## Maturity

Production. Internal service for one engineering organisation; expected life 5+ years.

## Scale drivers

The only place with now / target / ceiling: every number that grows, counts and rates alike.
Everything else refers to these by the name in backticks, like named variables in a
spreadsheet.

| Driver | Now | Target | Ceiling | Growth | Source |
|---|---|---|---|---|---|
| `engineers` | 300 | 600 | 2,000 | +40% / year; acquisition could double it in one step | HR plan, 2026-09 |
| `repos` (active) | 400 | 800 | 3,000 | tracks engineers; monorepo migration would cut it to ~50 | forge API, 2026-10 |
| `runners` | 40 | 80 | 300 | bought to keep queue wait under 2 min at peak | infra inventory, 2026-10 |
| `pushes` (per day; peak per s) | 2k; 0.2/s | 4k; 0.5/s | 14k; 2/s | tracks `engineers` | webhook log, 2026-10 |
| `bot_burst` (pipelines in 5 min, weekly) | 400 | 800 | 3,000 | tracks `repos` | bot schedule |
| `log_viewers` (concurrent) | 30 | 60 | 400 | incidents drive p99 20 per job | estimate |
| `build_lists` (peak per s) | 5 | 10 | 40 | tracks `engineers` | access log |

## Usage

The dominant user-visible operations, by shape. Rates and work refer to scale drivers or
shared entities. Everything not listed is rare or bounded by one of these.

| Operation | Arrival | Rate | Work per operation | Latency | Staleness tolerated |
|---|---|---|---|---|---|
| `push`: push triggers pipeline | open (forge webhook) | `pushes` | `pipeline` x `job` job rows | pipeline visible < 2 s | n/a (write) |
| `bot_day`: dependency-bot day (step) | open, burst | `bot_burst` | `pipeline` x `job` each | queue drains < 1 h | n/a (write) |
| `claim`: runner claims a job | closed: one loop per runner, think time `job.duration` | `runners` loops | 1 claim | < 1 s once a job is ready | none: claimed once |
| `watch_log`: watch a live log | open | `log_viewers` | tail of one job's log | first line < 1 s | < 2 s |
| `list_builds`: list a repo's builds | open, UI page | `build_lists` | 50 rows | < 300 ms | 30 s |

## Shared entities

Per-parent counts used by more than one component. An entity lives in the lowest
`WORKLOAD.md` that is an ancestor of every component using it.

| Entity | Count per parent (mean / p50 / p99) | Size (mean / p99) | Distribution | Source | Retention |
|---|---|---|---|---|---|
| `pipeline` | per push: 1.1 / 1 / 3 | 2 KB | top 5% of repos produce 50% of pipelines (zipf) | owned | 90 days |
| `job` | per pipeline: 9 / 6 / 60; capped at 500 | 1 KB | matrix builds dominate the tail | owned | 90 days |
| `job.duration` | per job: 9 / 4 / 45 min | | long tail from integration suites | owned | |

## Dependencies and budgets

| System | Used for | Budget | Availability |
|---|---|---|---|
| `forge` (hosted git) | push webhooks; pipeline config reads | `forge.budget` = 5,000 API calls/hour per installation | webhooks at-least-once, may arrive late or be dropped; redelivery on request |
| `object_store` | finished logs | billed per request and per GB-month | strongly consistent reads after write |

## Evolution

| Surface | Level | Window | Refinements |
|---|---|---|---|
| Jobs and pipelines store | rollback-safe | one release | readable with stock `psql`; no triggers or functions in the schema |
| Runner protocol | mixed-version | 3 months | runners upgrade on their own schedule |
| Archived logs in object storage | forward-migrated | | plain compressed text, readable without the service |
| `ci status --json` | public contract | 6 months deprecation | scripts and agents depend on it |

## Reference deployments

- **`standard`**: one 16-core / 64 GB (`standard.memory`) VM for the service and its Postgres (co-tenant,
  shares memory), object storage for finished logs, runners on separate hosts.

## Bounded consumers

- Build list page: 50 rows, newest first, cursor paging; shows "50 of ~12k"; 30 s stale is fine.
- Log viewer: renders a window of 10k lines; jump to offset or to first error; live tail < 2 s.
- `ci status --json` (scripts and agents): at most 100 jobs, with `"complete": false` when truncated.

## Recording guidance

Component files below this one follow the standard format, and also:

- Record the object-storage requests each operation makes per invocation: requests are billed, so request count is a cost driver here, not just a latency one.

## Conventions

- Design decisions: PR description.
- Open questions: issue with label `needs-decision`.
- ADRs: `docs/adr/`, MADR format.
- Implementation choices: `docs/implementation-choices/`.

## Validation

Last checked 2026-10-01 against the metrics dashboard (request rates per operation, runner
utilisation). Revise when any "now" drifts past 2x, a scale driver takes a named step, or a
new dominant operation appears.
