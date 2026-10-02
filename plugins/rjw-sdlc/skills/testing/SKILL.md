---
name: testing
description: Use when writing or changing tests, or implementing a feature or fix that needs tests. Decide the scale, state the behaviour in prose before the assertion, prefer property-based and stateful/simulation testing (Hegel) over example tests, keep the mocking budget tight, and run a mutation smoke check before the PR. For autonomous crews — decide and record in the ledger, never wait.
---

# Testing

Write tests that catch the bugs the implementation might contain, not tests that memorise what it does today. This skill replaces red-green-refactor TDD for autonomous crews: there is no human to confirm a seam, so you decide the test design, record the decision, and stall only on a genuine blocker.

The research behind these rules is at flotilla-org/flotilla#2446 — see the "Why" section.

## 1. State the behaviour before the assertion

Before writing any assertion, write one or two sentences of prose naming the behaviour or invariant under test, drawn from the **issue, spec, `CONTEXT.md`/ADR, or doc comment** — never from "whatever the code returns today". A test authored against existing code must assert what *should* hold. If the code's current output disagrees with the stated behaviour, the test is right and the code is the bug.

The dominant failure of agent-written tests is oracles that capture current behaviour and so lock bugs in as the spec. The prose oracle is the guard against it. Put it in a `//` comment above the test or in the property name.

## 2. Choose the scale

Pick the smallest shape that can actually express the behaviour, in this order:

- **Function or type property (default for pure logic and data structures).** Generate inputs for one piece of logic and assert an invariant: round-trip (`decode(encode(x)) == x`), idempotence, ordering/monotonicity, conservation, or "invalid input is rejected". Use an **explicit, non-trivial generator**, and say in a comment why it covers the interesting space (ranges that straddle boundaries, values that collide, both variants of an enum).
- **Scenario or stateful property (the tool for routing, replication, concurrency, lifecycles).** Generate a *sequence of operations* against an in-memory harness and check the invariants after **every** step. In flotilla, **extend the existing generated scenario engine** (`crates/flotilla-daemon/tests/request_session_pair.rs`, `crates/flotilla-daemon/tests/convergence_property.rs`) rather than hand-rolling a mock mesh. This is the deterministic-simulation family (FoundationDB, TigerBeetle) and it has already found real routing bugs here.
- **Metamorphic or differential relation when there is no oracle.** Assert a relation between runs instead of an absolute answer: "adding a duplicate changes nothing", "two routes that should agree, agree", "re-running is stable". Use this when you can't state the right answer but can state how two answers must relate.
- **One plain example test, only for trivial glue** — a thin delegation, a format string, a constant map. Justify it in one line (`// glue: single call-through, no logic to generate over`).

The default for flotilla is Hegel (the `hegeltest` crate, imported as `hegel`). See **[hegel.md](hegel.md)** for the exact API — the `#[hegel::test]` form, generators, the sequence-of-operations idiom, settings/seeds, and `hegel.toml`. Hegel is from the Hypothesis team and has bindings for other languages; reach for the native property library there (Hypothesis in Python) rather than inventing an API.

## 3. Mocking budget

Real collaborators or in-memory fakes by default. Mock **only at a true process or network boundary** (a subprocess, a socket, an HTTP endpoint, the git CLI), and write a one-line rationale at each mock. Agent test suites over-mock — 95% of their doubles are mocks — and mocks that stand in for your own logic test the mock, not the code. Record/replay fixtures are **re-recorded with `REPLAY=record`, never hand-edited** (see flotilla's `CLAUDE.md`).

## 4. Required edge cases

Enumerate these explicitly wherever they apply, as named cases or as generator coverage: **empty, boundary (min/max/off-by-one), duplicate, error/invalid, and concurrent-interleaving** inputs. A stateful property covers interleavings by drawing operation sequences; a function property covers boundaries by generating across them. Name the ones you deliberately skip and why.

## 5. Don't assert on the wrong things

Do not assert on private fields, internal call counts, or serialized byte layout — those pin the implementation, not the behaviour, and break on every refactor. **Exception:** when the layout *is* the contract, assert it — a golden stored-record corpus (flotilla's #2169 decode corpus) is exactly this case, and there the bytes are the behaviour.

## 6. Mutation smoke check (before the PR)

Before opening the PR, introduce at least **2 targeted mutants** in the code under test — flip a comparison (`<` → `<=`), drop a branch, change a `+` to `-`, remove a `!`. For each: run the suite, confirm a test **fails**, then revert. A mutant that survives means a weak or vacuous test — fix the test, don't ship. Record the mutants and their results in the PR body under a **"Mutation check"** heading. This is the single strongest quality gate in the research; it is not optional.

## 7. Failing-before, passing-after (bug fixes)

For a bug fix, confirm the new test **fails against the unpatched code** and passes after the fix. This is a sanity gate proving the test exercises the bug — it is not the method, and it does not replace the mutation check.

## 8. Autonomy

You are a crew with no interactive human. **Decide** the seams, the test scale, the generators, and the mocking yourself, and **record** the decisions in the decision ledger:

```
flotilla artifact put --kind decision-ledger
```

Write down which seams you chose and why, the invariant each property asserts, and any edge case you deliberately skipped. Never wait for confirmation. Stall only for a **genuine blocker** — a contradiction in the spec, a missing dependency you cannot provision, an ambiguity no reading of issue/spec/CONTEXT can resolve:

```
flotilla crew stall --reason "…"
```

"I'd normally ask which seams to test" is not a blocker — decide and record it.

## 9. Vacuous-property check

A property that can never fail tests nothing. Confirm each property *can* fail (the mutation check in §6 does this directly), and avoid generators so narrow that the invariant holds trivially — e.g. a range that excludes every boundary, or a filter that discards every interesting input. Weak generators and vacuous properties are the top failure mode of property tests; the explicit-coverage note in §2 and the mutation check in §6 are the two guards.

## Before you open the PR

- [ ] Every test has a prose oracle from spec/issue/docs, not from current output.
- [ ] Scale chosen deliberately; stateful/concurrent logic goes through the scenario engine, not a bespoke mock.
- [ ] Generators are explicit and non-trivial; coverage of the interesting space is stated.
- [ ] Edge cases (empty, boundary, duplicate, error, interleaving) enumerated or generated.
- [ ] Mocks only at process/network boundaries, each with a rationale.
- [ ] Mutation check done: ≥2 mutants, each caught, reverted, written up in the PR body.
- [ ] Bug fix: test failed before the fix.
- [ ] Decisions recorded in the ledger.

## Why

From the research on autonomous-crew test quality (flotilla-org/flotilla#2446, which links the primary sources — verify before quoting externally):

- **Agent tests assert current behaviour, not intended behaviour** — the dominant failure, which bakes bugs in as the spec. Hence §1.
- **TDD-by-agent shows no quality benefit and can increase regressions.** Red-first proves little when the same context writes both the test and the code; the human-confirmed seam was the only part doing real work, and it can't run in a crew. Hence this skill replaces TDD rather than stripping its confirmation step.
- **Agent suites over-mock** (95% of their doubles are mocks). Hence §3.
- **Property-based, metamorphic, and simulation testing do materially better**; metamorphic/differential relations sidestep the oracle problem. Hence §2.
- **Mutation-guided testing is the strongest single quality gate** — faults as the target, not coverage. Hence §6, and the vacuous-property guard in §9.
- **The weak spots of property testing are poor generators and vacuous properties.** Hence the explicit-generator and can-it-fail rules.
