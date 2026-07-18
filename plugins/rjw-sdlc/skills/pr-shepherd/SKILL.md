---
name: pr-shepherd
description: Shepherd a PR through to merge. Resolves conflicts, investigates CI failures, responds to code reviews (fixing valid feedback, pushing back on incorrect suggestions), files follow-up tickets for out-of-scope work, and reports merge readiness.
argument-hint: "[PR-number]"
---

# PR Shepherd

Shepherd a pull request from submission to merge readiness. Run after submitting a PR, or re-run as it progresses through review cycles.

## Key Principle

**Maximize quality through the review interaction.** Every review comment is an opportunity to improve the PR — the goal is to extract maximum value from reviewer feedback, not to converge to merge as fast as possible. Accept and implement feedback that improves the code. Push back only when you have concrete technical evidence that a suggestion is wrong. Never agree performatively, but equally never dismiss feedback to save iterations.

When handling review feedback, treat each comment as a claim to verify, not an instruction to follow: check it against the actual codebase before implementing or pushing back, and skip performative agreement ("You're absolutely right") in replies — respond with what you verified and what you did.

## PR Detection

If `$ARGUMENTS` contains a number, use that as the PR number. Otherwise, auto-detect from the current branch:

```bash
PR_NUMBER=$(gh pr view --json number --jq '.number' 2>/dev/null)
```

If neither works, ask the user for the PR number.

**Remote usage (no local clone):** If there is no local git repository (e.g., running from Claude Code web), pass `-R OWNER/REPO` to the helper script so it can identify the repository:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py -R owner/repo status $PR_NUMBER
```

All subcommands support `-R` — it must appear **before** the subcommand name.

## Overall Flow

The shepherd runs in a **convergence loop**: assess the PR state, fix what can be fixed, wait for CI to react, then re-assess. The loop exits when the PR is stable (no new actionable items) or a maximum of **5 iterations** is reached.

```
+-- Iteration N ------------------------------------------------+
|  Phase 1: Fetch status (with actionable assessment)           |
|  Phase 2: Assess -- anything in action_items?                 |
|     NO  -> exit loop, go to Merge Readiness                   |
|     YES -> continue                                           |
|  Phase 3: Resolve conflicts (if any)                          |
|  Phase 4: Fix CI failures (if any)                            |
|  Phase 5: Respond to reviews (if any)                         |
|  Phase 6: File follow-up tickets (if any)                     |
|                                                               |
|  Did we push changes?                                         |
|     YES -> wait-for-checks --check-reviews, loop back         |
|     NO  -> exit loop, go to Merge Readiness                   |
+---------------------------------------------------------------+
  Phase 7: Merge Readiness Report
```

## Phase 1: Data Fetching

Run the helper script to get a structured assessment of the PR:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py status $PR_NUMBER
```

The JSON output contains PR metadata, merge state, check results, review summary, issue comments, and — crucially — **`needs_attention`** (boolean) and **`action_items`** (list of what to do). Use `action_items` directly rather than re-deriving the assessment from raw fields.

## Phase 2: Situational Assessment

Present a brief status summary using the status output:

```markdown
## PR #N Status — title (iteration M)

- **Merge state:** MERGEABLE / CONFLICTING
- **Checks:** N passing, N failing, N pending
- **Reviews:** N approved, N changes requested, N pending
- **Unresolved threads:** N

### Action items:
1. [from action_items list]
```

If checks are still pending from a previous push, wait for them first:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py wait-for-checks $PR_NUMBER --timeout 600
```

This command also detects merge conflicts and exits early if found (so the shepherd can resolve them in Phase 3 before waiting further). The output includes `failed_checks` with names, links, and `run_id` for investigation — no separate `checks` call needed.

**Issue comments matter too.** Check `issue_comments` in the status output, not just `unresolved_threads`. Bot reviewers (like claude-review) post as issue comments, not inline review threads. The `reviews` command includes all three types (inline threads, top-level reviews, and issue comments).

**Exit condition:** If `needs_attention` is false (no conflicts, no failed checks, no unresolved review threads, AND no unaddressed issue comments with review content), exit the loop and proceed to Phase 7.

**Staleness detection:** Track which review comment IDs AND issue comment IDs have been addressed in previous iterations. If the only unresolved comments are ones already responded to, treat the PR as stable — do not re-process them. A review round that produces only repeat findings (same reviewer making the same point already addressed) counts as convergence.

Then work through phases 3-6 as needed. Skip phases that don't apply.

## Phase 3: Conflict Resolution

If merge state is CONFLICTING:

1. Merge the base branch into the PR branch:
   ```bash
   git fetch origin main
   git merge origin/main
   ```
2. Resolve conflicts using codebase context to make correct choices
3. For auto-generated files (like `docs/PRIORITY_ISSUES.md`), regenerate rather than manually merge
4. Commit and push
5. Report what was resolved

## Phase 4: CI Investigation

If any checks failed, use the `failed_checks` from the status or wait output (which includes `run_id`):

1. For each failed check, investigate:
   - Use `run_id` to fetch logs: `gh run view <run_id> --log-failed 2>&1 | tail -50`
   - If more detail is needed: `${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py checks $PR_NUMBER`
   - **Test failures:** Read the failing test, understand what it expects, fix the code or test
   - **Lint/typecheck failures:** Fix the code
   - **Infrastructure issues** (flaky test, timeout, rate limit): Report to user, suggest re-run via `gh run rerun <run-id>`
2. Commit fixes and push
3. Report what was fixed vs what needs manual intervention

## Phase 5: Review Response

If there are unresolved review comments:

1. Fetch all review content:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py reviews $PR_NUMBER
   ```
   This returns inline threads, top-level reviews, AND issue comments (which often contain bot review feedback).
2. For each comment thread:
   - **Read** the full comment without reacting
   - **Verify** the suggestion against the codebase
   - **Categorize:**
     - **Fix** — suggestion is valid, implement the change
     - **Pushback** — suggestion is technically wrong, or breaks things
     - **Clarify** — suggestion is ambiguous, need more info
     - **Follow-up** — valid but out of scope for this PR
   - **Before any pushback, apply the reversal test:**
     - State the reviewer's argument in its strongest form
     - State your counter-argument
     - Ask: "If I saw my counter-argument in someone else's code review, would I find it convincing?"
     - Check: does the codebase have an existing convention that supports the reviewer? (e.g., if they suggest an Nx target, do similar targets already exist?)
     - If your pushback relies on invented distinctions or vague claims ("adds complexity", "minimal gain"), it's probably weak — recategorize as Fix
   - **Act:**
     - Fix: implement the change
     - Pushback: prepare technical reasoning that passes the reversal test
     - Clarify: prepare a question
     - Follow-up: prepare a GitHub issue

   **The goal is maximum quality, not minimum iterations.** Every piece of review feedback is an opportunity to improve the PR. The default should be to accept and improve, not to defend and exit. Only push back when you have concrete technical evidence, not when it would be faster to dismiss.

3. Commit all code fixes in a single commit with descriptive message, push

4. Reply to each comment thread using the helper script:
   ```bash
   # Auto-detects comment type and picks the correct endpoint
   ${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py reply $PR_NUMBER $COMMENT_ID "Fixed. [Brief description]"

   # For a top-level comment (not replying to a specific thread)
   ${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py comment $PR_NUMBER "Response text"
   ```
   The `reply` subcommand checks whether the comment is an inline review comment (reply-able via thread endpoint) or a top-level comment (falls back to posting a new top-level comment). This avoids the 404 errors from using the wrong endpoint.

5. Present a summary of actions taken on each comment

## Phase 6: Follow-up Tickets

For review feedback categorized as "follow-up" (valid but out of scope):

1. Create a GitHub issue:
   ```bash
   gh issue create \
     --title "Summary of the suggestion" \
     --body "From PR #N review by @reviewer: [link to comment]

   [Description of what should be done]

   Context: [relevant details from the review comment]" \
     --label "from-review"
   ```
2. Reply to the review comment with the issue link:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py reply $PR_NUMBER $COMMENT_ID "Filed as #M for follow-up."
   ```

Ensure the `from-review` label exists:
```bash
gh label create "from-review" --description "Issue filed from PR review feedback" --color "BFD4F2" 2>/dev/null || true
```

## Convergence: Wait and Re-assess

After completing phases 3-6, if any changes were pushed:

1. **Wait for checks and new reviews in one call:**
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/pr-shepherd.py wait-for-checks $PR_NUMBER --timeout 600 --check-reviews
   ```
   This polls every 30s until all checks complete (or 10 min timeout), detects merge conflicts, and after completion automatically checks for new review comments (excluding the current user). The output includes:
   - `done`, `all_passed`, `failed_checks` (with `run_id` for investigation)
   - `merge_state` (conflict detection)
   - `new_reviews.count` (new review comments since the wait started)

2. **Decide whether to loop:**
   - If `new_reviews.count` is 0, no new reviews — proceed to Phase 7.
   - If `new_reviews.count` > 0, there are new reviews to process — loop back to Phase 1.
   - If checks failed, loop back (Phase 1 will re-assess with the failures in `action_items`).
   - If conflicts detected, loop back (Phase 3 will resolve them).

3. **Increment iteration counter.** If iteration >= 5, exit the loop with a report of what's still pending.

4. **Loop back to Phase 1** — re-fetch status, re-assess. The new review round (triggered by the push) may have new findings.

If no changes were pushed (everything was pushback/follow-up/already addressed), skip the wait and proceed directly to Phase 7.

## Phase 7: Merge Readiness Report

After the loop exits, present final status:

```markdown
## Merge Readiness — PR #N

### Status
- **Checks:** All passing / N failing
- **Reviews:** Approved / Pending / Changes requested
- **Conflicts:** None / Unresolved
- **Review comments:** All addressed / N awaiting response

### Loop Summary
- **Iterations:** N
- **Exit reason:** [converged — no new findings / max iterations / no changes pushed]

### Actions Taken (cumulative)
- Fixed N review comments
- Pushed back on N comments
- Filed N follow-up tickets: #A, #B
- Resolved merge conflicts
- Fixed CI failures: [details]

### Verdict
[Ready to merge / Blocked on: reasons]
```

If the PR is ready to merge, ask the user whether to merge it. Do NOT auto-merge without explicit approval.

## Quick Reference

The helper script does the heavy lifting — use its subcommands, don't reimplement their logic:

| Task | Command |
|------|---------|
| Assess PR | `status PR` — read `action_items` for what needs attention |
| Wait + detect conflicts + check reviews | `wait-for-checks PR --timeout 600 --check-reviews` |
| Investigate CI failure | `gh run view <run_id> --log-failed` (run_id from wait/checks output) |
| Get all review content | `reviews PR` (inline threads + top-level reviews + issue comments) |
| Reply to a comment | `reply PR COMMENT_ID "text"` (auto-detects endpoint) |
| Post top-level comment | `comment PR "text"` |

### Anti-patterns to avoid

- **Don't redirect to temp files** (`> /tmp/...` then Read) — stdout goes directly to the tool result
- **Don't write custom sleep/poll loops** — use `wait-for-checks`
- **Don't call `new-reviews` separately after `wait-for-checks`** — use `--check-reviews`
- **Don't call `checks` after `wait-for-checks`** — `failed_checks` with `run_id` is already in the output
- **Don't pass `--exclude-author $(gh api user ...)`** — current user is auto-detected
- **Don't use `gh pr view --comments | python/jq`** — use `reviews` or `new-reviews`
- **Don't pipe script output through `jq` or `python`** to extract fields — if you need a field the script doesn't provide, the script should be updated

## Repeated Use

This skill is designed for repeated invocation on the same PR. Each run:
- Starts a fresh convergence loop
- Picks up new review comments since last run
- Checks if previously-failing CI now passes
- Detects new merge conflicts
- Reports cumulative progress
