# Hegel recipes

Hegel is flotilla's property-based and stateful testing library, from the Hypothesis team. The Cargo dependency is `hegeltest`; the crate is imported as `hegel`:

```toml
# Cargo.toml — pinned exactly, as flotilla does
[dev-dependencies]
hegeltest = "=0.48.1"
```

```rust
use hegel::generators as gs;
```

Real flotilla usage lives in `crates/flotilla-daemon/tests/request_session_pair.rs` and `crates/flotilla-daemon/tests/convergence_property.rs`. Read those before writing a new property — extend the scenario harness there rather than building your own. Every example below matches the API those files use.

## Function or type property

Attribute form: `#[hegel::test]` on a `fn` taking a `hegel::TestCase`. Draw inputs with `tc.draw(<generator>)`, then assert the invariant. The body is synchronous; drive async code with a runtime inside, as flotilla does.

```rust
// Behaviour: encoding a ref name and decoding it round-trips for every
// name the daemon can mint. The oracle is the round-trip law, not a fixed string.
#[hegel::test]
fn ref_name_round_trips(tc: hegel::TestCase) {
    // Explicit, non-trivial generator: lengths that straddle the 0 and 255
    // boundaries, so empty and over-long names are both drawn.
    let len = tc.draw(gs::integers::<usize>().min_value(0).max_value(255));
    let name = sample_ref_name(&tc, len);

    let encoded = encode_ref(&name);
    assert_eq!(decode_ref(&encoded), name, "round-trip must be identity");
}
```

Generators seen in flotilla:

- `gs::integers::<usize>().min_value(0).max_value(2)` — bounded integers; pick bounds that include the boundaries you care about.
- `gs::booleans()` — a coin flip, used both for branch choices and per-step decisions in sequences.

## Scenario or stateful property

The stateful idiom is: **draw a step count, draw a choice per step, then replay the sequence against an in-memory harness, checking invariants after each step.** This is how `generated_partition_heal_restart_preserves_tombstones` and the convoy-admission properties in `request_session_pair.rs` are written.

```rust
// Behaviour: a resource deleted at its authority stays absent across every
// partition/heal/restart interleaving — no tombstone is ever resurrected.
#[hegel::test]
fn deletes_survive_every_interleaving(tc: hegel::TestCase) {
    let home = tc.draw(gs::integers::<usize>().min_value(0).max_value(2));
    let step_count = tc.draw(gs::integers::<usize>().min_value(1).max_value(6));
    // One drawn decision per step — the generated operation sequence.
    let restart_each_step: Vec<bool> = (0..step_count).map(|_| tc.draw(gs::booleans())).collect();

    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().start_paused(true).build().expect("runtime");
    runtime.block_on(async {
        let mut world = spawn_harness(home).await;        // in-memory mesh/topology, not a mock
        for restart in restart_each_step {
            world.author_and_delete().await;
            if restart { world.restart().await; } else { world.partition_and_heal().await; }
            world.assert_deleted_everywhere().await;       // invariant after EVERY step
        }
    });
}
```

Key points:

- The harness is a **real in-memory implementation** (`spawn_in_memory_request_mesh`, `spawn_in_memory_request_topology_*`, `InMemoryBackend`), not a hand-written mock. Extend it when you need a new operation.
- Check the invariant **after every step**, not only at the end — that is what localises the failing prefix.
- Keep per-draw ranges small (flotilla uses `max_value(2)` for host indices, short step counts) so shrinking produces a minimal reproducer fast.

## Explicit runner, settings, and seeds

When you need to set the case count or pin a seed for one property (e.g. a slow scenario), use the builder form instead of the attribute:

```rust
hegel::Hegel::new(|tc: hegel::TestCase| {
    let issuer = tc.draw(gs::integers::<usize>().min_value(0).max_value(2));
    // … exercise and assert …
})
.settings(hegel::Settings::new().test_cases(12).seed(Some(2318)))
.run();
```

Project-wide case counts and seeds live in `hegel.toml` at the repo root:

```toml
[profiles.development]
test_cases = 12
seed = 2368

[profiles.ci]
extends = "development"

[profiles.nightly]
extends = "ci"
test_cases = 40
seed = "none"   # fresh randomness in nightly; dev/ci stay replayable
```

A failing case prints the seed; re-run with that seed (via `Settings::seed` or the profile) to replay the exact counterexample while you fix it.

## Other languages

Hegel shares its lineage with Hypothesis, so the same shapes transfer: Hypothesis in Python (`@given`, strategies, stateful `RuleBasedStateMachine`), and the Hypothesis-family libraries elsewhere. Use the native library for the language under test rather than inventing an API — the method (explicit generators, invariants after each step, shrinking to a minimal counterexample) is what carries over, not the exact calls.
