---
name: triage-issues
description: Use when reviewing, labeling, closing, or organizing GitHub issues — especially after a batch of PRs merged, before planning sprints, or when issue state has drifted from reality
---

# Triaging Issues

Systematic review of open GitHub issues: apply labels, close resolved issues, add dependency relationships, and document labels for agents.

## When to Use

- After a batch of PRs merge (some issues may be auto-closed or now resolved)
- Before planning work (need accurate issue state and dependency graph)
- When issues have accumulated without labels
- When an agent asks "what should I work on?" and the backlog is messy

## Workflow

### 1. Get the lay of the land

```bash
gh label list --limit 50
gh issue list --state open --limit 100 --json number,title,labels
```

Identify: unlabeled issues, issues that might be stale or resolved, label gaps.

### 2. Read issue bodies

Don't label from titles alone — read the body to understand scope, affected files, and relationships. Batch reads in parallel for efficiency.

### 3. Create missing labels

If the existing label set doesn't cover a clear category, create it:

```bash
gh label create "label-name" --description "Short description" --color "HEX"
```

Keep labels orthogonal. Combine them (e.g. `bug` + `ui`) rather than creating hybrids (`ui-bug`).

### 4. Apply labels

Batch with parallel `gh issue edit` calls:

```bash
gh issue edit NUMBER --add-label "label1,label2"
```

### 5. Close resolved issues

Check if issues have been addressed by merged PRs, even if not auto-closed (e.g. PR didn't use "Fixes #N" syntax). Close with a comment explaining what resolved it:

```bash
gh issue close NUMBER --comment "Covered by PR #X / feature Y."
```

### 6. Add dependency relationships

GitHub has a proper "blocked by" data model via the Relationships sidebar. Use the GraphQL API — the `gh` CLI doesn't support it yet.

**Step 1: Get node IDs**

```bash
gh api graphql -f query='
query {
  repository(owner: "OWNER", name: "REPO") {
    i42: issue(number: 42) { id }
    i99: issue(number: 99) { id }
  }
}'
```

**Step 2: Create relationship** (issue 42 is blocked by issue 99)

```bash
gh api graphql -f query='mutation {
  addBlockedBy(input: {
    issueId: "NODE_ID_OF_42",
    blockingIssueId: "NODE_ID_OF_99"
  }) { clientMutationId }
}'
```

To remove: use `removeBlockedBy` with the same input shape.

Do NOT use body text like "Blocked by #99" — it doesn't create a real relationship.

### 7. Document labels

If the repo's CLAUDE.md (or equivalent) doesn't have a label reference table, add one so agents use labels consistently.

## Quick Reference

| Task | Command |
|------|---------|
| List labels | `gh label list` |
| List open issues | `gh issue list --state open --json number,title,labels` |
| Read issue body | `gh issue view NUMBER --json body -q .body` |
| Add labels | `gh issue edit NUMBER --add-label "a,b"` |
| Create label | `gh label create "name" --description "..." --color "HEX"` |
| Close with comment | `gh issue close NUMBER --comment "reason"` |
| Get node ID | `gh api graphql -f query='{ repository(...) { iN: issue(number: N) { id } } }'` |
| Add blocked-by | `gh api graphql -f query='mutation { addBlockedBy(input: {issueId: "...", blockingIssueId: "..."}) { clientMutationId } }'` |
| Check issue state | `gh issue view NUMBER --json state -q .state` |

## Common Mistakes

- **Labeling from titles only** — titles can be misleading. Read the body.
- **Body text for dependencies** — GitHub has real relationship data via GraphQL. Use it.
- **Not checking for resolved issues** — PRs that don't use "Fixes #N" won't auto-close. Manually verify.
- **Creating overlapping labels** — prefer composable labels (`bug` + `ui`) over specific ones (`ui-bug`).
- **Forgetting to document labels** — agents will guess wrong or invent new ones without a reference.
