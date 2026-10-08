---
name: pr-shepherd
description: Shepherd a PR through to merge. Resolves conflicts, investigates CI failures, responds to code reviews (fixing valid feedback, pushing back on incorrect suggestions), files follow-up tickets for out-of-scope work, and reports merge readiness.
argument-hint: "[PR-number]"
---

# PR Shepherd

All `scripts/...` paths below are relative to this skill's base directory (announced when the skill loads); resolve them against it.

Shepherd a pull request from submission to merge readiness. Re-run it as the PR moves through review cycles.

## Directives

**Maximize quality through the review interaction.** Treat each comment as a claim to verify against the codebase, not an instruction to follow. Accept feedback that improves the code. Push back only with concrete technical evidence. Do not agree performatively or dismiss feedback to save an iteration.

**Send all helper-script comment bodies through stdin.** Never put review prose in a shell argument: backticked identifiers can be executed by the shell and silently removed from the posted comment. Use a quoted heredoc:

```bash
scripts/pr-shepherd.py reply "$PR_NUMBER" "$COMMENT_ID" --finding 1 - <<'EOF'
Fixed `CheckoutReconciler` by preserving the existing state transition.
EOF
```

**Work per finding, not per comment.** One review comment often carries several findings: numbered or bulleted items, nits, and trailing sections such as "Minor" or "Suggestions". Each finding needs its own answer. The helper can't split a comment into findings reliably, so it never decides that a comment is settled. You tell it, and its markers work like this:

- `reply --finding LABEL` (repeatable) records that this reply answers those findings. The comment **stays** in `action_items`. Use the reviewer's numbering as the label (`1`, `2`), or your own (`nit-1`, `minor-2`) when the reviewer didn't number them.
- `reply --all-handled` writes the `pr-shepherd-addresses:<id>` marker. Only this clears the comment from `action_items`. Pass it only when **every** finding in the comment is handled: fixed, answered with a reason, or deferred to a linked issue. It can go on the reply that answers the last finding.

`reply` refuses to post without one of the two. `status` lists comments with some findings answered but no `--all-handled` under `partially_answered`, and `reviews --comment ID` shows `answered_findings` for that comment. Replies posted by an older helper carry the addresses marker even when they answered only part of a comment. If you find one of those, answer the remaining findings anyway.

**Whose feedback counts.** `status` counts comments from humans and from review bots (a built-in list including `claude[bot]`; add others with `--review-bot LOGIN` before the subcommand). It ignores other bots, such as CI and coverage reporters, which it lists under `ignored_bot_authors`. It also ignores your own comments, empty APPROVED/COMMENTED reviews, any review superseded by the same reviewer's later review, threads GitHub marks resolved, and comments that explicitly conclude "no issues found" and "ready to merge" with nothing qualifying it. A comment that opens "no blocking issues" and then lists findings still counts.

**Name yourself when running as a GitHub App.** The helper treats comments by the PR author and by you as your own, never as feedback to act on. With a user token it asks GitHub who you are. An App installation token can't, so set `PR_SHEPHERD_AS` to the App's bot login (`<app-slug>[bot]`) or pass `--as` before the subcommand:

```bash
scripts/pr-shepherd.py --as "my-app[bot]" status "$PR_NUMBER" --brief
```

## Convergence Loop

`status` reports `mode`. It is `no-wait` when the `FLOTILLA_CREW_ID` environment variable is set (you are running as a flotilla crew) or when you pass `--no-wait` before the subcommand. Otherwise it is `wait`.

Each iteration:

1. Fetch `status --brief` and follow its `action_items`. If it has any actionable review feedback, `ack` the pickup before you start working (section 1.1): a crew's fix/test/push cycle can run for an hour, and until then the PR looks untouched to the owner, who merges from GitHub.
2. Resolve conflicts, investigate CI, and process every finding in every actionable review comment.
3. File follow-up issues for valid out-of-scope work.
4. Commit and push any code changes, then reply to every finding you handled. (If you didn't `ack` in step 1 because new feedback arrived mid-iteration, `ack` the new items now.)
5. Re-run `status --brief`. If any review comment is still actionable or partly answered, go back to step 2, even if no code changed this iteration.
6. **Standalone (`wait`):** if you pushed changes, wait for checks and new reviews (section 6), then start the next iteration. **Under flotilla (`no-wait`):** stop after this pass. Take one `wait-for-checks` snapshot, act on anything it shows that you can fix now, then report and yield. flotilla wakes the crew when checks finish or new review feedback arrives. Don't poll.

The loop exits when no review comment is actionable or partly answered and either nothing was pushed or the checks and reviews after the last push are settled. It also exits after five iterations. Never exit while any finding in any review is unanswered: "no code changes were needed" is not a reason to stop if a finding still lacks a reply.

The pickup label clears itself: `reply --all-handled` and `status` both drop it once review findings have converged, so you don't need to remember `clear` (section 1.1). While any finding is still open — max iterations, blocked, or yielding under no-wait — the label stays set, and the merge-readiness report says so.

## 1.1 Acknowledge and clear the pickup

`ack` makes the work visible on GitHub while you work offline. It adds the `shepherd: addressing` label, which means the PR has unanswered review points and is not merge-ready, and a 👀 reaction to each review item you're about to handle. Pass the ids `status`/`reviews` reported:

```bash
scripts/pr-shepherd.py ack "$PR_NUMBER" --comment-ids 123 456 789
```

Issue comments and inline review comments get the reaction. A submitted review's *body* has no reactions endpoint on GitHub, so `ack` labels the PR and reports that id under `unreactable` instead of reacting; the label (and your eventual reply) is its acknowledgement.

The reaction is idempotent (an item you already reacted to is skipped) and adding the label again is harmless, so re-running `ack` as new feedback arrives is safe.

**Clearing is automatic.** You don't call `clear` in the normal flow. Once review findings have converged — no reviewer comment, review body, or thread open or partly answered — both `reply --all-handled` and `status` drop the label for you: the `--all-handled` reply that settles the last comment clears it (`"addressing_label": "cleared"` in its output), and a later `status` clears it too if one is still set (`addressing_label_cleared: true`), so status is self-healing. Convergence here is **review feedback only**: pending or failing checks and merge conflicts do not keep the label set, because its claim is "unanswered review points remain", not "not yet mergeable for any reason". While a crew stalls or yields with findings still open, the label stays on.

`clear` remains as an explicit escape hatch — to drop the label by hand, or where no converging command will run next. It removes the label unconditionally:

```bash
scripts/pr-shepherd.py clear "$PR_NUMBER"
```

`status` reports `addressing_label` so you can see whether it is currently set.

## 1. Assess

If a PR number was supplied, use it:

```bash
scripts/pr-shepherd.py status "$ARGUMENTS" --brief
```

Otherwise let `status` detect the PR associated with the current branch; it reports a clear error if none exists:

```bash
scripts/pr-shepherd.py status --brief
```

Read `pr.number` from the result and use it as `PR_NUMBER` for subsequent commands. The brief result contains the merge state, check/review counts, `needs_attention`, and the authoritative `action_items` list. Use the full `status` response only when its additional metadata is useful.

Present a short iteration summary:

```markdown
## PR #N Status — title (iteration M)

- **Merge state:** MERGEABLE / CONFLICTING
- **Checks:** N passing, N failing, N pending
- **Reviews:** N approved, N changes requested, N pending
- **Unresolved threads:** N
- **Review bodies / issue comments to answer:** N / N
- **Partly answered comments:** N

### Action items

1. [from `action_items`]
```

Trust the helper's classifications of *whose* comments count, but not as proof that a comment's findings are all answered. That is your call, recorded with `--all-handled`. A comment marked `--all-handled` is not re-flagged. A repeated finding means the earlier reply didn't settle it. Answer it again with the reason, or fix it.

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

Use `scripts/pr-shepherd.py checks "$PR_NUMBER"` only when the status/wait details are insufficient. Commit and push code fixes, and distinguish them from failures needing manual intervention.

## 4. Process Reviews

Fetch all inline threads, top-level reviews, and issue comments:

```bash
scripts/pr-shepherd.py reviews "$PR_NUMBER"
```

When an aggregate entry is truncated or you need to focus on one finding, fetch its complete body directly:

```bash
scripts/pr-shepherd.py reviews "$PR_NUMBER" --comment "$COMMENT_ID"
```

For each actionable comment, list its findings first: every numbered or bulleted item, each nit, and each point in trailing sections such as "Minor", "Nits", or "Suggestions". A preamble such as "no blocking issues" doesn't cancel the findings after it. Then, for each finding:

1. Read it fully and verify its claim against the current code.
2. Categorize it as **Fix**, **Pushback**, **Clarify**, or **Follow-up**.
3. Before pushback, apply the reversal test:
   - State the reviewer's argument in its strongest form.
   - State your counter-argument.
   - Ask whether that counter-argument would convince you in someone else's review.
   - Check whether existing codebase conventions support the reviewer.
   - If the pushback relies on invented distinctions or vague claims such as “adds complexity” or “minimal gain,” recategorize it as Fix.
4. Implement fixes. Prepare concrete reasoning, a question, or a follow-up issue for the other categories.

Commit related review fixes together and push. Then reply to every finding through stdin. Answer findings one reply each, or group them in one reply with a labelled answer per finding. A reply never stands in for findings it doesn't mention.

```bash
scripts/pr-shepherd.py reply "$PR_NUMBER" "$COMMENT_ID" --finding 1 - <<'EOF'
1: Fixed. The state transition now preserves the existing invariant, with a regression test covering the reported case.
EOF

scripts/pr-shepherd.py reply "$PR_NUMBER" "$COMMENT_ID" --finding 2 --finding nit-1 --all-handled - <<'EOF'
2: Not changed. `parse` already re-raises: the `except` block on line 40 ends in `raise`, so the error isn't swallowed.
nit-1: Filed as #123.
EOF
```

Add `--all-handled` only on the reply that leaves no finding in that comment unanswered.

For a top-level comment that is not a response to a specific review comment:

```bash
scripts/pr-shepherd.py comment "$PR_NUMBER" - <<'EOF'
Review-cycle summary goes here.
EOF
```

Report how each finding was handled.

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
From PR #N review by <reviewer — cite WITHOUT a leading @, e.g. "claude-review bot"; a literal @-mention in an issue body can trigger automation>: [comment link]

[What should be done and why]

Context: [relevant details from the review]
EOF
```

Reply to the finding with the issue link using the stdin form above (`--finding LABEL`). A finding deferred to an issue counts as handled.

## 6. Wait and Reassess

After pushing changes, wait for both checks and new reviews:

```bash
scripts/pr-shepherd.py wait-for-checks "$PR_NUMBER" --check-reviews
```

The tool owns its pacing. The default timeout is 900 seconds, and shorter timeouts are raised to 300 so that one call is one real wait. It polls every 60 seconds, backing off ×1.5 up to 180 seconds while nothing changes; `--interval` can raise the 60-second base but not lower it. Make one call and act on its result. Don't loop on short waits.

**Polling uses REST.** The helper reads PRs, checks, reviews and comments through the REST API. The GraphQL budget (5,000 points an hour) is shared by every `gh` command and every agent session on the machine, so in agent loops use `wait-for-checks` and `status --brief` rather than `gh pr checks --watch` or repeated `gh pr view`, both of which spend it. The helper's one GraphQL query reads review-thread resolution; `status` runs it only when an inline thread would otherwise be open, and `wait-for-checks` never runs it.

The result includes conflicts, failed checks with `run_id`, and `new_reviews.count`. Reassess when checks fail, conflicts appear, or new reviews arrive. A wait timeout means “not finished yet,” not “failed.”

**No-wait mode.** When `FLOTILLA_CREW_ID` is set, or with `--no-wait` before the subcommand, `wait-for-checks` doesn't poll. It returns one snapshot with `no_wait: true`, the pending and failed checks, and the merge state. Act on failures and conflicts you can fix now, make sure every finding has a reply, then report and yield. flotilla wakes the crew when checks finish or unaddressed review feedback arrives. Don't loop on `status` or `wait-for-checks` to recreate the wait. Standalone use keeps waiting.

Do not extract helper output with jq/Python/temp files or recreate its polling and review-detection logic; use `status --brief`, `reviews --comment`, and `wait-for-checks --check-reviews`.

## 7. Merge Readiness

When the loop exits, report:

```markdown
## Merge Readiness — PR #N

### Status
- **Checks:** All passing / N failing
- **Reviews:** Approved / Pending / Changes requested
- **Conflicts:** None / Unresolved
- **Review findings:** All answered / N awaiting response

### Loop Summary
- **Iterations:** N
- **Exit reason:** converged / max iterations / yielded (no-wait)
- **`shepherd: addressing` label:** cleared automatically on convergence / left set (findings still open)

### Actions Taken
- Fixed N review findings
- Pushed back on N findings
- Filed N follow-up tickets: #A, #B
- Resolved merge conflicts
- Fixed CI failures: [details]

### Verdict
[Ready to merge / Blocked on: reasons]
```

If the PR is ready, ask the user whether to merge it. Never merge without explicit approval.
