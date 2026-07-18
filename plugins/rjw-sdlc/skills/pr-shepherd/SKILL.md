---
name: pr-shepherd
description: Shepherd a PR through to merge. Resolves conflicts, investigates CI failures, responds to code reviews (fixing valid feedback, pushing back on incorrect suggestions), files follow-up tickets for out-of-scope work, and reports merge readiness.
argument-hint: "[PR-number]"
---

# PR Shepherd

Shepherd a pull request from submission to merge readiness. Re-run it as the PR moves through review cycles.

## Directives

**Maximize quality through the review interaction.** Treat each comment as a claim to verify against the codebase, not an instruction to follow. Accept feedback that improves the code. Push back only with concrete technical evidence. Do not agree performatively or dismiss feedback to save an iteration.

**Send all helper-script comment bodies through stdin.** Never put review prose in a shell argument: backticked identifiers can be executed by the shell and silently removed from the posted comment. Use a quoted heredoc:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py reply "$PR_NUMBER" "$COMMENT_ID" - <<'EOF'
Fixed `CheckoutReconciler` by preserving the existing state transition.
EOF
```

The helper records addressed comment IDs in the posted replies. Its `status` command uses those durable markers and excludes unambiguously approving bot reviews from `action_items`.

## Convergence Loop

Run at most five iterations:

1. Fetch `status --brief` and follow its `action_items`.
2. Resolve conflicts, investigate CI, and process review feedback that applies.
3. File follow-up issues for valid out-of-scope work.
4. Commit and push any code changes.
5. If changes were pushed, wait for checks and reviews, then reassess. If no changes were pushed, report readiness.

The loop exits when `needs_attention` is false, no code changes were needed, or five iterations have completed.

## 1. Assess

If a PR number was supplied, use it:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py status "$ARGUMENTS" --brief
```

Otherwise let `status` detect the PR associated with the current branch; it reports a clear error if none exists:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py status --brief
```

Read `pr.number` from the result and use it as `PR_NUMBER` for subsequent commands. The brief result contains the merge state, check/review counts, `needs_attention`, and the authoritative `action_items` list. Use the full `status` response only when its additional metadata is useful.

Present a short iteration summary:

```markdown
## PR #N Status — title (iteration M)

- **Merge state:** MERGEABLE / CONFLICTING
- **Checks:** N passing, N failing, N pending
- **Reviews:** N approved, N changes requested, N pending
- **Unresolved threads:** N

### Action items

1. [from `action_items`]
```

Trust the helper's classifications. Replies made through `reply` are not re-flagged; a new comment that merely repeats already-verified feedback requires no new code change.

## 2. Resolve Conflicts

If the PR conflicts with its base, fetch the base and merge or rebase according to repository convention. Resolve with codebase context, regenerate generated files instead of hand-merging them, then commit and push.

## 3. Investigate CI

For each failed check, use the `run_id` in the status or wait result:

```bash
gh run view <run_id> --log-failed
```

- Test failure: read the failing test and fix the code or the test according to intended behavior.
- Lint/typecheck failure: fix the code.
- Infrastructure failure such as a timeout, rate limit, or known flake: report it and recommend `gh run rerun <run-id>`.

Use `${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py checks "$PR_NUMBER"` only when the status/wait details are insufficient. Commit and push code fixes, and distinguish them from failures needing manual intervention.

## 4. Process Reviews

Fetch all inline threads, top-level reviews, and issue comments:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py reviews "$PR_NUMBER"
```

When an aggregate entry is truncated or you need to focus on one finding, fetch its complete body directly:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py reviews "$PR_NUMBER" --comment "$COMMENT_ID"
```

For each actionable comment:

1. Read it fully and verify its claim against the current code.
2. Categorize it as **Fix**, **Pushback**, **Clarify**, or **Follow-up**.
3. Before pushback, apply the reversal test:
   - State the reviewer's argument in its strongest form.
   - State your counter-argument.
   - Ask whether that counter-argument would convince you in someone else's review.
   - Check whether existing codebase conventions support the reviewer.
   - If the pushback relies on invented distinctions or vague claims such as “adds complexity” or “minimal gain,” recategorize it as Fix.
4. Implement fixes. Prepare concrete reasoning, a question, or a follow-up issue for the other categories.

Commit related review fixes together and push. Then reply to every handled comment through stdin:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py reply "$PR_NUMBER" "$COMMENT_ID" - <<'EOF'
Fixed. The state transition now preserves the existing invariant, with a regression test covering the reported case.
EOF
```

For a top-level comment that is not a response to a specific review comment:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py comment "$PR_NUMBER" - <<'EOF'
Review-cycle summary goes here.
EOF
```

Report how each comment was handled.

## 5. File Follow-up Issues

For valid feedback outside the PR's scope, ensure the label exists and create a linked issue:

```bash
gh label create from-review \
  --description "Issue filed from PR review feedback" \
  --color BFD4F2 2>/dev/null || true

gh issue create \
  --title "Summary of the suggestion" \
  --label from-review \
  --body-file - <<'EOF'
From PR #N review by @reviewer: [comment link]

[What should be done and why]

Context: [relevant details from the review]
EOF
```

Reply to the review comment with the issue link using the stdin form above.

## 6. Wait and Reassess

After pushing changes, wait for both checks and new reviews. The default timeout is 900 seconds and the default interval is 30 seconds:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py wait-for-checks "$PR_NUMBER" --check-reviews
```

For harnesses that should yield control frequently, use short polls and re-invoke:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py wait-for-checks "$PR_NUMBER" \
  --timeout 50 --interval 10 --check-reviews
```

The result includes conflicts, failed checks with `run_id`, and `new_reviews.count`. Reassess when checks fail, conflicts appear, or new reviews arrive. A wait timeout means “not finished yet,” not “failed.”

Do not extract helper output with jq/Python/temp files or recreate its polling and review-detection logic; use `status --brief`, `reviews --comment`, and `wait-for-checks --check-reviews`.

## 7. Merge Readiness

When the loop exits, report:

```markdown
## Merge Readiness — PR #N

### Status
- **Checks:** All passing / N failing
- **Reviews:** Approved / Pending / Changes requested
- **Conflicts:** None / Unresolved
- **Review comments:** All addressed / N awaiting response

### Loop Summary
- **Iterations:** N
- **Exit reason:** converged / max iterations / no changes pushed

### Actions Taken
- Fixed N review comments
- Pushed back on N comments
- Filed N follow-up tickets: #A, #B
- Resolved merge conflicts
- Fixed CI failures: [details]

### Verdict
[Ready to merge / Blocked on: reasons]
```

If the PR is ready, ask the user whether to merge it. Never merge without explicit approval.
