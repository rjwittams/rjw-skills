# Setup

The skill uses one vocabulary in every repo. Check a repo against it:

```bash
scripts/backlog-triage.py setup OWNER/REPO...
```

It reports, per repo: canonical labels that are missing, `priority:*` labels whose value isn't a dispatch tier (with how many open issues carry each), open issues with no issue type, and issue types missing from the org. Its `actions` list holds one `create_label` action per missing label. Save the output as a plan, `show` it, and `apply` it after approval, like any other plan.

## Canonical labels

| Label | Purpose |
|---|---|
| `triage:clear`, `triage:blocked`, `triage:needs-answer`, `triage:needs-rewrite`, `triage:waiting` | Triage state; exactly one per assessed issue ([SKILL.md](SKILL.md#states)) |
| `needs-triage`, `needs-info`, `ready`, `ready-for-human`, `wontfix`, `duplicate` | Triage roles shared with the mattpocock-skills triage flow. `ready` is earned: the body is a dispatchable contract |

**Older roles an assessment replaces.** `needs-triage` means "not yet assessed", which is now the absence of a `triage:*` label. `needs-info` is `triage:needs-answer` with the reporter as owner. Both stay canonical because the mattpocock-skills triage flow applies them, but setting a `triage:*` label removes them, so an issue never carries both.
| `wayfinder:map`, `wayfinder:build`, `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, `wayfinder:task` | Wayfinder missions and ticket types |

Topic labels (`bug`, `ui`, `cli`, …) stay per repo and are outside this skill.

## Per-repo config: `docs/agents/triage.toml`

```toml
[[component]]            # 10–20 coarse areas; used by `group` for sweep grouping
name = "recording"
description = "Recording and replay of sessions: asciicast output, transcripts, retention."
paths = ["crates/cleat/src/recording.rs", "crates/cleat/src/replay.rs"]

# The label format the repo's dispatcher parses. Required when [needs] is present.
needs_label_pattern = "^needs:[a-z0-9-]+:[a-z0-9.-]+$"

[needs]                  # placement vocabulary; each becomes a needs:<need> label
"platform:windows" = "Needs a Windows host or runner to build or verify"
"hardware:gpu" = "Needs a GPU"
```

(`needs_label_pattern` is a top-level key, so it goes above the first table.)

`needs` are **placement constraints**: things a dispatcher matches against a host or runner. A `needs:*` label is machine input, so the skill writes only labels the repo's config declares, and only in the format the repo's dispatcher reads:

- Copy the vocabulary from the dispatcher's own capability list, not from this example or from `needs:*` labels already in the repo. Write the pattern to match what the dispatcher parses.
- `load_config` refuses a `[needs]` table without `needs_label_pattern`, and any entry whose label doesn't match it.
- `plan` rejects any `needs` value outside the vocabulary. A repo with no `[needs]` table has an empty vocabulary: every `needs` value is rejected, and `plan` leaves the repo's existing `needs:*` labels alone.
- `setup --config` proposes the declared labels and reports `undeclared_needs_labels`: existing `needs:*` labels outside the vocabulary, with their open-issue counts and whether they match the pattern. Renaming or deleting those is a person's call.

**Constraints for people are not needs.** A quiet merge window, a check the operator runs after a deploy, the owner's machine, a live reproduction someone has to catch: no dispatcher can match these to a host. They go in the verdict's `conditions` and appear in the record comment, never on a label.

## Issue types

Org-level: `Task`, `Bug`, `Feature`, `Brainstorm`, `Mission`, `Lane`. Assessment proposes a type for any untyped issue it touches, so a sweep fills the gap as it goes.

## Each repo also needs

- **Native issue dependencies and sub-issues** enabled. They are on for GitHub organisations by default.
- **`docs/agents/triage.toml`**: components, and the needs vocabulary with its label pattern if the repo's dispatcher reads `needs:*` labels (above).
- **`docs/agents/triage-labels.md`** mapping the triage roles to label strings, as flotilla has, so skills that speak in roles find the labels.
- **A token** that can label, comment and add dependencies, for whoever applies plans. Investigators need read access only.

A repo's own gaps (missing labels, invalid priorities, untyped issues) are installation state: record the remediation in an issue in that repo or org, not here.
