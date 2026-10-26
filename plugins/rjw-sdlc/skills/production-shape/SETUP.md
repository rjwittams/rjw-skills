# Setting up a workload model

Run this when the repository has no root `WORKLOAD.md`. Humans own the root numbers: you propose, the user confirms. The format is in [WORKLOAD-FORMAT.md](WORKLOAD-FORMAT.md); the [examples](examples/) show three finished models.

1. **Look for one.** Check for `WORKLOAD.md` at the repository root and for a link to one from `CODING_STANDARDS.md`, `AGENTS.md` or `CLAUDE.md`. Done when you have found one (stop here and use it) or confirmed there is none.
2. **When nobody can be asked, stop at the gap.** In an autonomous run, record the missing model where the project records open questions, size for the largest plausible deployment, and continue the work. Setup needs a human.
3. **Agree maturity first.** Prototype, personal tool, internal or production, and expected lifespan: it sets how high the ceiling goes.
4. **Draft the scale drivers and usage with the user.** Take candidates from the project's domain glossary (`CONTEXT.md`), its main collections and its user-visible operations. Propose now, target and ceiling for each driver (measured from real data where you can), and the shape of each dominant operation. Apply the entry rule: only what would change a design choice. Done when the user has confirmed or corrected every row.
5. **Agree dependencies, evolution, reference deployments and conventions.** External services and OS or hardware limits with their budgets; an evolution level per surface that outlives a process ([EVOLUTION.md](EVOLUTION.md)), matched to the maturity; one or more example deployments with this system's share; where design decisions, open questions, ADRs and implementation-choice records go, unless the agent instructions already say.
6. **Write the root `WORKLOAD.md`** from the skeleton below, and add Recording guidance if the project has a cost driver the standard format does not name.
7. **Link it** with one bullet in `CODING_STANDARDS.md`; where there is none, in `AGENTS.md`, then `CLAUDE.md`. Ask before creating a new file for the link. The bullet: "Data-touching design and review: use the `production-shape` skill, with scales from [WORKLOAD.md](WORKLOAD.md) and any `WORKLOAD.md` nearer the code."

Done when the root file exists, the user has confirmed its numbers and conventions, and one standards or agents file links it.

**Component files** are drafted when work first needs one: per-parent numbers for the component's own entities, operations and stores, with sources marked, reviewed with the change that adds them.

## Root skeleton

```markdown
# Workload model: <system>

Design and review data-touching work with the `production-shape` skill: production shape,
cost test, naive shape, workload note, candidate walk, bounded consumer, snapshot then
deltas, source liveness, removal is a message, query cover, operation lowering, single writer,
steady state, census, match the defence to the boundary, evolution level.

## Maturity

## Scale drivers

| Driver | Now | Target | Ceiling | Growth | Source |
|---|---|---|---|---|---|

## Shared entities

| Entity | Count per parent (mean / p50 / p99) | Size (mean / p99) | Distribution | Source | Retention |
|---|---|---|---|---|---|

## Usage

| Operation | Arrival | Rate | Work per operation | Latency | Staleness tolerated |
|---|---|---|---|---|---|

## Dependencies and budgets

| System | Used for | Budget | Availability |
|---|---|---|---|

## Evolution

| Surface | Level | Window | Refinements |
|---|---|---|---|

## Reference deployments

## Bounded consumers

## Recording guidance

## Conventions

- Design decisions:
- Open questions:
- ADRs:
- Implementation choices:

## Validation
```
