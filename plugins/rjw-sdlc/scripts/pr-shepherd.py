#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
PR shepherd helper — fetches and structures PR data for the pr-shepherd skill.

Read subcommands:
    status [PR]              PR status with actionable assessment; auto-detects PR
    reviews <PR>             All reviews, or one full body via --comment ID
    checks <PR>              Detailed check run information with run IDs
    wait-for-checks <PR>     Poll until checks complete (conflict detection,
                             optional new-review detection via --check-reviews)
    new-reviews <PR>         New review activity (issue + inline comments)

Write subcommands:
    comment <PR> <body|->    Post a top-level comment on the PR
    reply <PR> <id> <body|-> Reply to a review comment (detects type, picks endpoint)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# ── Caching ────────────────────────────────────────────────────────────

CACHE_DIR = Path("/tmp/pr_shepherd_cache")
CACHE_TTL = 120  # 2 minutes — PR state changes faster than issue state
ADDRESS_MARKER_RE = re.compile(r"<!--\s*pr-shepherd-addresses:(\d+)\s*-->")
NO_FINDINGS_RE = re.compile(
    r"\b(?:"
    r"no (?:(?:further|new|outstanding) )?(?:issues|findings)(?: (?:were )?found)?"
    r"|did not find (?:any |further |new )?(?:issues|findings)"
    r")\b",
    re.IGNORECASE,
)
MERGEABLE_RE = re.compile(
    r"\b(?:mergeable as-is|approved as-is|ready to merge|looks good to merge)\b",
    re.IGNORECASE,
)
QUALIFIED_FINDINGS_RE = re.compile(r"^(?:beyond|except|other than)\b", re.IGNORECASE)


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.json"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < CACHE_TTL


# ── Data fetching ──────────────────────────────────────────────────────

def _gh_json(args: list[str], cache_name: str) -> dict | list:
    """Run a gh command that returns JSON, with file-based caching."""
    cached = _cache_path(cache_name)
    if _is_fresh(cached):
        return json.loads(cached.read_text())

    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"Error running gh {' '.join(args)}: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(result.stdout)
    cached.write_text(json.dumps(data))
    return data


def _gh_api(endpoint: str, cache_name: str) -> dict | list:
    """Run a gh api command, with file-based caching."""
    cached = _cache_path(cache_name)
    if _is_fresh(cached):
        return json.loads(cached.read_text())

    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"Error running gh api {endpoint}: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(result.stdout)
    cached.write_text(json.dumps(data))
    return data


_repo_cache: str | None = None


def _get_repo() -> str:
    """Get owner/repo from git remote (cached after first call)."""
    global _repo_cache
    if _repo_cache is not None:
        return _repo_cache
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Error detecting repo: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    _repo_cache = result.stdout.strip()
    return _repo_cache


_user_cache: str | None = None


def _get_current_user() -> str:
    """Get the current GitHub username (cached after first call)."""
    global _user_cache
    if _user_cache is not None:
        return _user_cache
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Error detecting user: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    _user_cache = result.stdout.strip()
    return _user_cache


def _resolve_pr_number(pr_number: int | None) -> int:
    """Use an explicit positive PR number or detect one from the current branch."""
    if pr_number is not None:
        if pr_number <= 0:
            print("Error: PR number must be a positive integer", file=sys.stderr)
            sys.exit(2)
        return pr_number

    cmd = ["gh", "pr", "view", "--json", "number", "--jq", ".number"]
    if _repo_cache:
        cmd.extend(["-R", _repo_cache])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    detected = result.stdout.strip()
    if result.returncode != 0 or not detected.isdigit() or int(detected) <= 0:
        detail = result.stderr.strip()
        message = "Error: could not detect a pull request for the current branch"
        if detail:
            message += f": {detail}"
        print(message, file=sys.stderr)
        sys.exit(2)
    return int(detected)


def fetch_pr_metadata(pr: int) -> dict:
    """Fetch core PR metadata."""
    cmd = [
        "pr", "view", str(pr),
        "--json",
        "number,title,body,author,state,isDraft,baseRefName,headRefName,"
        "mergeable,url,createdAt,updatedAt,labels,additions,deletions,"
        "changedFiles,commits",
    ]
    if _repo_cache:
        cmd.extend(["-R", _repo_cache])
    return _gh_json(cmd, f"pr_{pr}_meta")


def fetch_pr_checks(pr: int) -> list[dict]:
    """Fetch check run results for the PR. Returns [] if no checks exist yet."""
    cached = _cache_path(f"pr_{pr}_checks")
    if _is_fresh(cached):
        return json.loads(cached.read_text())

    cmd = [
        "gh", "pr", "checks", str(pr),
        "--json", "name,state,startedAt,completedAt,link,bucket,description",
    ]
    if _repo_cache:
        cmd.extend(["-R", _repo_cache])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # "no checks reported" is normal for fresh pushes — return empty list
    if result.returncode != 0:
        if "no checks reported" in result.stderr.lower():
            cached.write_text("[]")
            return []
        print(f"Error running gh pr checks: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    if not result.stdout.strip():
        cached.write_text("[]")
        return []
    data = json.loads(result.stdout)
    cached.write_text(json.dumps(data))
    return data


def fetch_pr_reviews(pr: int, repo: str) -> list[dict]:
    """Fetch reviews on the PR (up to 100)."""
    return _gh_api(
        f"repos/{repo}/pulls/{pr}/reviews?per_page=100",
        f"pr_{pr}_reviews",
    )


def fetch_pr_review_comments(pr: int, repo: str) -> list[dict]:
    """Fetch inline review comments on the PR (up to 100)."""
    return _gh_api(
        f"repos/{repo}/pulls/{pr}/comments?per_page=100",
        f"pr_{pr}_review_comments",
    )


def fetch_pr_issue_comments(pr: int, repo: str) -> list[dict]:
    """Fetch top-level issue comments on the PR."""
    return _gh_api(
        f"repos/{repo}/issues/{pr}/comments?per_page=100",
        f"pr_{pr}_issue_comments",
    )


# ── Write operations ───────────────────────────────────────────────────

def _gh_api_post(endpoint: str, fields: dict[str, str]) -> dict:
    """POST to a gh api endpoint with field data. Returns parsed JSON response."""
    cmd = ["gh", "api", endpoint, "-X", "POST"]
    for key, value in fields.items():
        cmd.extend(["-f", f"{key}={value}"])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"Error: gh api POST {endpoint}: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout.strip()}


def _invalidate_cache(pattern: str) -> None:
    """Remove cached files matching a glob pattern."""
    for path in CACHE_DIR.glob(pattern):
        path.unlink(missing_ok=True)


def _read_body(body: str | None, body_file: Path | None) -> str:
    """Resolve a command body from an argument, stdin, or a file."""
    if body_file is not None:
        try:
            return body_file.read_text()
        except OSError as error:
            print(f"Error reading body file {body_file}: {error}", file=sys.stderr)
            sys.exit(1)
    if body is None:
        print("Error: provide a body, `-` for stdin, or --body-file PATH", file=sys.stderr)
        sys.exit(2)
    if body == "-":
        return sys.stdin.read()
    return body


def _with_address_marker(body: str, comment_id: int) -> str:
    """Record which GitHub comment this reply addresses in hidden metadata."""
    return f"{body.rstrip()}\n\n<!-- pr-shepherd-addresses:{comment_id} -->\n"


def _is_inline_review_comment(comment_id: int, pr: int, repo: str) -> bool:
    """Check if a comment ID is an inline review comment (reply-able via thread endpoint)."""
    review_comments = fetch_pr_review_comments(pr, repo)
    return any(c["id"] == comment_id for c in review_comments)


# ── Analysis helpers ───────────────────────────────────────────────────

def _extract_run_id(link: str) -> str | None:
    """Extract GitHub Actions run ID from a check link URL."""
    if "/runs/" not in link:
        return None
    try:
        return link.split("/runs/")[1].split("/")[0].split("?")[0]
    except (IndexError, ValueError):
        return None


def _classify_check(c: dict) -> str:
    """Classify a check into pass/fail/pending/skipped."""
    bucket = (c.get("bucket") or c.get("state", "")).upper()
    if bucket in ("PASS", "SUCCESS"):
        return "pass"
    if bucket in ("FAIL", "FAILURE"):
        return "fail"
    if bucket in ("PENDING", "QUEUED", "IN_PROGRESS"):
        return "pending"
    return "skipped"


def _summarize_checks(checks: list[dict]) -> dict:
    """Summarize checks into counts and failed details (with run_id)."""
    counts = {"pass": 0, "fail": 0, "pending": 0, "skipped": 0}
    failed = []
    for c in checks:
        category = _classify_check(c)
        counts[category] += 1
        if category == "fail":
            entry = {"name": c.get("name", ""), "link": c.get("link", "")}
            run_id = _extract_run_id(c.get("link", ""))
            if run_id:
                entry["run_id"] = run_id
            failed.append(entry)
    return {"counts": counts, "failed": failed}


def _summarize_reviews(reviews: list[dict]) -> list[dict]:
    """Summarize reviews by author, keeping the latest submitted state."""
    by_author: dict[str, dict] = {}
    for r in reviews:
        author = r["user"]["login"]
        state = r["state"]
        if author not in by_author:
            by_author[author] = {
                "author": author,
                "latest_state": state,
                "comment_count": 0,
                "_latest_key": ("", 0),
            }
        latest_key = (r.get("submitted_at") or "", r.get("id") or 0)
        if latest_key >= by_author[author]["_latest_key"]:
            by_author[author]["latest_state"] = state
            by_author[author]["_latest_key"] = latest_key
        if r.get("body"):
            by_author[author]["comment_count"] += 1
    for info in by_author.values():
        del info["_latest_key"]
    return list(by_author.values())


def _group_comment_threads(comments: list[dict]) -> list[dict]:
    """Group inline review comments into threads."""
    comments = sorted(comments, key=lambda c: c["id"])
    threads: dict[int, dict] = {}
    for c in comments:
        parent_id = c.get("in_reply_to_id")
        if parent_id and parent_id in threads:
            threads[parent_id]["replies"].append({
                "id": c["id"],
                "author": c["user"]["login"],
                "body": c["body"],
                "created_at": c["created_at"],
            })
        else:
            thread_id = c["id"]
            threads[thread_id] = {
                "id": thread_id,
                "author": c["user"]["login"],
                "path": c.get("path", ""),
                "line": c.get("line") or c.get("original_line"),
                "diff_hunk": c.get("diff_hunk", ""),
                "body": c["body"],
                "created_at": c["created_at"],
                "replies": [],
            }
    return list(threads.values())


def _find_issue_refs(text: str) -> list[int]:
    """Find issue/PR references in text (#N patterns)."""
    return sorted(set(int(m) for m in re.findall(r'#(\d+)', text or "")))


def _find_addressed_comment_ids(comments: list[dict]) -> set[int]:
    """Read durable addressed-comment markers from GitHub comment bodies."""
    return {
        int(match)
        for comment in comments
        for match in ADDRESS_MARKER_RE.findall(comment.get("body", ""))
    }


def _is_approving_comment(comment: dict) -> bool:
    """Recognize only explicit no-findings + mergeable review conclusions."""
    body = comment.get("body", "")
    no_findings = NO_FINDINGS_RE.search(body)
    if no_findings is None or MERGEABLE_RE.search(body) is None:
        return False
    qualification = body[no_findings.end():].lstrip(" \t\r\n:;,.—–-()")
    return QUALIFIED_FINDINGS_RE.match(qualification) is None


def _find_review_item(
    comment_id: int,
    reviews: list[dict],
    review_comments: list[dict],
    issue_comments: list[dict],
) -> dict | None:
    """Find and normalize one item from any GitHub PR review surface."""
    issue_comment = next(
        (comment for comment in issue_comments if comment["id"] == comment_id),
        None,
    )
    if issue_comment is not None:
        return {
            "type": "issue_comment",
            "id": issue_comment["id"],
            "author": issue_comment.get("user", {}).get("login", ""),
            "body": issue_comment.get("body", ""),
            "created_at": issue_comment.get("created_at", ""),
            "html_url": issue_comment.get("html_url", ""),
        }

    review_comment = next(
        (comment for comment in review_comments if comment["id"] == comment_id),
        None,
    )
    if review_comment is not None:
        return {
            "type": "review_comment",
            "id": review_comment["id"],
            "author": review_comment.get("user", {}).get("login", ""),
            "body": review_comment.get("body", ""),
            "path": review_comment.get("path", ""),
            "line": review_comment.get("line") or review_comment.get("original_line"),
            "diff_hunk": review_comment.get("diff_hunk", ""),
            "created_at": review_comment.get("created_at", ""),
            "html_url": review_comment.get("html_url", ""),
            "in_reply_to_id": review_comment.get("in_reply_to_id"),
        }

    review = next(
        (review for review in reviews if review["id"] == comment_id),
        None,
    )
    if review is None:
        return None
    return {
        "type": "review",
        "id": review["id"],
        "author": review.get("user", {}).get("login", ""),
        "state": review.get("state", ""),
        "body": review.get("body", ""),
        "submitted_at": review.get("submitted_at", ""),
        "html_url": review.get("html_url", ""),
    }


def _check_new_reviews(pr: int, repo: str, since: str, exclude_authors: set[str]) -> dict:
    """Check for new review activity since a timestamp.

    Scans both issue comments and inline review comments.
    """
    _invalidate_cache(f"pr_{pr}_issue_comments*")
    _invalidate_cache(f"pr_{pr}_review_comments*")
    issue_comments = fetch_pr_issue_comments(pr, repo)
    review_comments = fetch_pr_review_comments(pr, repo)

    new_issue = [
        c for c in issue_comments
        if c.get("created_at", "") > since
        and c.get("user", {}).get("login", "") not in exclude_authors
    ]
    new_inline = [
        c for c in review_comments
        if c.get("created_at", "") > since
        and c.get("user", {}).get("login", "") not in exclude_authors
    ]

    new_comments = [
        {
            "id": c["id"],
            "type": "issue_comment",
            "author": c.get("user", {}).get("login", ""),
            "created_at": c.get("created_at", ""),
            "body_preview": (body := c.get("body", ""))[:200] + ("..." if len(body) > 200 else ""),
        }
        for c in new_issue
    ] + [
        {
            "id": c["id"],
            "type": "review_comment",
            "author": c.get("user", {}).get("login", ""),
            "path": c.get("path", ""),
            "created_at": c.get("created_at", ""),
            "body_preview": (body := c.get("body", ""))[:200] + ("..." if len(body) > 200 else ""),
        }
        for c in new_inline
    ]

    return {
        "new_comments": new_comments,
        "count": len(new_comments),
        "issue_comments": len(new_issue),
        "review_comments": len(new_inline),
    }


# ── Subcommands ────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    """PR status with actionable assessment."""
    pr = _resolve_pr_number(args.pr_number)
    repo = _get_repo()

    meta = fetch_pr_metadata(pr)
    checks = fetch_pr_checks(pr)
    reviews = fetch_pr_reviews(pr, repo)
    review_comments = fetch_pr_review_comments(pr, repo)
    issue_comments = fetch_pr_issue_comments(pr, repo)

    check_info = _summarize_checks(checks)
    review_summary = _summarize_reviews(reviews)

    # Count unresolved comment threads (heuristic: last commenter is not PR author)
    threads = _group_comment_threads(review_comments)
    pr_author = meta.get("author", {}).get("login", "")
    addressed_review_comment_ids = _find_addressed_comment_ids(review_comments)
    approving_review_comment_ids = {
        comment["id"]
        for comment in review_comments
        if _is_approving_comment(comment)
    }
    unresolved = []
    for thread in threads:
        last_comment = thread["replies"][-1] if thread["replies"] else thread
        if (
            last_comment["author"] != pr_author
            and last_comment["id"] not in addressed_review_comment_ids
            and last_comment["id"] not in approving_review_comment_ids
        ):
            unresolved.append(thread)

    body_refs = _find_issue_refs(meta.get("body", ""))

    comments_by_author: dict[str, int] = {}
    for c in issue_comments:
        author = c.get("user", {}).get("login", "unknown")
        comments_by_author[author] = comments_by_author.get(author, 0) + 1

    addressed_comment_ids = _find_addressed_comment_ids(issue_comments)
    approving_comment_ids = {
        comment["id"]
        for comment in issue_comments
        if _is_approving_comment(comment)
    }
    actionable_issue_comments = [
        comment
        for comment in issue_comments
        if comment.get("user", {}).get("login", "") != pr_author
        and comment["id"] not in addressed_comment_ids
        and comment["id"] not in approving_comment_ids
    ]

    merge_state = meta.get("mergeable", "UNKNOWN")

    # Build actionable assessment
    action_items = []
    if merge_state == "CONFLICTING":
        action_items.append("resolve merge conflicts")
    if check_info["counts"]["fail"] > 0:
        names = ", ".join(f["name"] for f in check_info["failed"])
        action_items.append(f"fix failing checks: {names}")
    if check_info["counts"]["pending"] > 0:
        action_items.append(f"{check_info['counts']['pending']} checks still pending")
    if len(unresolved) > 0:
        action_items.append(f"address {len(unresolved)} unresolved review threads")
    reviewer_issue_comments = len(actionable_issue_comments)
    if reviewer_issue_comments > 0:
        action_items.append(f"evaluate {reviewer_issue_comments} issue comments from reviewers/bots")

    result = {
        "pr": {
            "number": meta["number"],
            "title": meta["title"],
            "author": pr_author,
            "state": meta["state"],
            "draft": meta.get("isDraft", False),
            "base": meta.get("baseRefName", ""),
            "head": meta.get("headRefName", ""),
            "url": meta.get("url", ""),
            "additions": meta.get("additions", 0),
            "deletions": meta.get("deletions", 0),
            "changed_files": meta.get("changedFiles", 0),
        },
        "merge_state": merge_state,
        "checks": check_info,
        "reviews": {
            "summary": review_summary,
            "has_approvals": any(r["latest_state"] == "APPROVED" for r in review_summary),
            "has_changes_requested": any(r["latest_state"] == "CHANGES_REQUESTED" for r in review_summary),
            "unresolved_threads": len(unresolved),
            "addressed_comment_ids": sorted(addressed_review_comment_ids),
            "approving_comment_ids": sorted(approving_review_comment_ids),
        },
        "issue_comments": {
            "total": len(issue_comments),
            "by_author": comments_by_author,
            "actionable": reviewer_issue_comments,
            "addressed_ids": sorted(addressed_comment_ids),
            "approving_ids": sorted(approving_comment_ids),
        },
        "linked_issues": body_refs,
        "needs_attention": len(action_items) > 0,
        "action_items": action_items,
    }

    if args.brief:
        review_counts = {
            "approved": sum(r["latest_state"] == "APPROVED" for r in review_summary),
            "changes_requested": sum(
                r["latest_state"] == "CHANGES_REQUESTED" for r in review_summary
            ),
            "pending": sum(
                r["latest_state"] in ("PENDING", "COMMENTED") for r in review_summary
            ),
            "unresolved_threads": len(unresolved),
        }
        result = {
            "pr": {
                "number": meta["number"],
                "title": meta["title"],
                "url": meta.get("url", ""),
            },
            "merge_state": merge_state,
            "checks": check_info["counts"],
            "reviews": review_counts,
            "issue_comments": {"actionable": reviewer_issue_comments},
            "needs_attention": len(action_items) > 0,
            "action_items": action_items,
        }

    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_reviews(args: argparse.Namespace) -> None:
    """All review comments: inline threads, top-level reviews, and issue comments."""
    pr = args.pr_number
    repo = _get_repo()

    reviews = fetch_pr_reviews(pr, repo)
    review_comments = fetch_pr_review_comments(pr, repo)
    issue_comments = fetch_pr_issue_comments(pr, repo)

    if args.comment_id is not None:
        result = _find_review_item(
            args.comment_id,
            reviews,
            review_comments,
            issue_comments,
        )
        if result is None:
            print(f"Error: review comment {args.comment_id} not found", file=sys.stderr)
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    top_level = [
        {
            "id": r["id"],
            "author": r["user"]["login"],
            "state": r["state"],
            "body": r.get("body", ""),
            "submitted_at": r.get("submitted_at", ""),
        }
        for r in reviews
        if r.get("body")
    ]

    threads = _group_comment_threads(review_comments)

    issue_level = [
        {
            "id": c["id"],
            "author": c.get("user", {}).get("login", ""),
            "body": c.get("body", ""),
            "created_at": c.get("created_at", ""),
            "html_url": c.get("html_url", ""),
        }
        for c in issue_comments
    ]

    result = {
        "top_level_reviews": top_level,
        "inline_threads": threads,
        "issue_comments": issue_level,
    }

    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_checks(args: argparse.Namespace) -> None:
    """Detailed check run information with run IDs."""
    pr = args.pr_number
    checks = fetch_pr_checks(pr)

    detailed = []
    for c in checks:
        category = _classify_check(c)
        entry = {
            "name": c.get("name", ""),
            "category": category,
            "bucket": c.get("bucket", ""),
            "description": c.get("description", ""),
            "link": c.get("link", ""),
            "started_at": c.get("startedAt", ""),
            "completed_at": c.get("completedAt", ""),
        }
        run_id = _extract_run_id(c.get("link", ""))
        if run_id:
            entry["run_id"] = run_id
        detailed.append(entry)

    counts = {"total": 0, "pass": 0, "fail": 0, "pending": 0, "skipped": 0}
    for d in detailed:
        counts["total"] += 1
        counts[d["category"]] += 1

    result = {
        "checks": detailed,
        "summary": counts,
    }

    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_wait_for_checks(args: argparse.Namespace) -> None:
    """Poll until all checks complete or timeout is reached.

    Detects merge conflicts (which block CI) and exits early.
    When --check-reviews is set, also detects new review comments after completion.
    Includes failed check details (with run_id) so a separate `checks` call is unnecessary.
    """
    pr = args.pr_number
    timeout = args.timeout
    interval = args.interval
    # Adaptive backoff: stay responsive early, decay toward a cap for long CI
    # runs — fixed 30s polling across many concurrent shepherds was a main
    # consumer of the shared 5k/hr GitHub rate limit (flotilla#885).
    poll_cap = max(interval, 120)
    deadline = time.time() + timeout
    wait_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Resolve exclude authors for review checking
    exclude_authors: set[str] = set()
    if args.check_reviews:
        if args.exclude_authors:
            exclude_authors = set(args.exclude_authors)
        else:
            exclude_authors = {_get_current_user()}

    poll_count = 0
    meta = {}
    while True:
        # Checks are the hot datum — fetch fresh every poll. PR metadata
        # (mergeable state) changes rarely mid-wait; refresh it every 3rd
        # poll to halve API traffic (flotilla#885).
        _invalidate_cache(f"pr_{pr}_checks*")
        checks = fetch_pr_checks(pr)
        if poll_count % 3 == 0:
            _invalidate_cache(f"pr_{pr}_meta*")
            meta = fetch_pr_metadata(pr)
        poll_count += 1
        merge_state = meta.get("mergeable", "UNKNOWN")

        # Conflicts block CI — exit early so the shepherd can resolve them
        if merge_state == "CONFLICTING":
            result = {
                "done": False,
                "conflicting": True,
                "merge_state": merge_state,
                "total": len(checks),
                "message": "PR has merge conflicts — resolve before checks can pass",
            }
            json.dump(result, sys.stdout, indent=2)
            print()
            return

        # No checks registered yet — keep waiting (CI hasn't started)
        if not checks:
            remaining = deadline - time.time()
            if remaining <= 0:
                result = {
                    "done": False,
                    "timed_out": True,
                    "merge_state": merge_state,
                    "total": 0,
                    "message": "No checks registered within timeout",
                }
                json.dump(result, sys.stdout, indent=2)
                print()
                return
            print(f"Waiting... no checks registered yet, {int(remaining)}s remaining",
                  file=sys.stderr)
            time.sleep(min(interval, remaining))
            interval = min(int(interval * 1.5), poll_cap)
            continue

        pending = [c for c in checks if _classify_check(c) == "pending"]

        if not pending:
            # All checks complete — build detailed result
            check_info = _summarize_checks(checks)
            result = {
                "done": True,
                "merge_state": merge_state,
                "total": len(checks),
                "passed": check_info["counts"]["pass"],
                "failed": check_info["counts"]["fail"],
                "all_passed": check_info["counts"]["fail"] == 0,
                "failed_checks": check_info["failed"],
            }

            # Check for new reviews if requested
            if args.check_reviews:
                repo = _get_repo()
                result["new_reviews"] = _check_new_reviews(
                    pr, repo, wait_start, exclude_authors,
                )

            json.dump(result, sys.stdout, indent=2)
            print()
            return

        remaining = deadline - time.time()
        if remaining <= 0:
            check_info = _summarize_checks(checks)
            result = {
                "done": False,
                "timed_out": True,
                "merge_state": merge_state,
                "total": len(checks),
                "still_pending": len(pending),
                "pending_names": [c.get("name", "") for c in pending],
                "failed_checks": check_info["failed"],
            }
            json.dump(result, sys.stdout, indent=2)
            print()
            sys.exit(1)

        pending_names = ", ".join(c.get("name", "?") for c in pending)
        print(
            f"Waiting... {len(pending)} pending ({pending_names}), "
            f"{int(remaining)}s remaining",
            file=sys.stderr,
        )
        time.sleep(min(interval, remaining))
        interval = min(int(interval * 1.5), poll_cap)


def cmd_new_reviews(args: argparse.Namespace) -> None:
    """Check for new review activity since a given timestamp.

    Scans both issue comments and inline review comments.
    Defaults to excluding the current GitHub user's comments.
    """
    pr = args.pr_number
    repo = _get_repo()

    exclude_authors: set[str] = set()
    if args.exclude_authors:
        exclude_authors = set(args.exclude_authors)
    elif not args.authors:
        # Default: exclude current user
        exclude_authors = {_get_current_user()}

    result = _check_new_reviews(pr, repo, args.since or "", exclude_authors)

    # Apply --author include filter on top
    if args.authors:
        allowed = set(args.authors)
        result["new_comments"] = [
            c for c in result["new_comments"] if c["author"] in allowed
        ]
        result["count"] = len(result["new_comments"])
        result["issue_comments"] = sum(
            1 for c in result["new_comments"] if c["type"] == "issue_comment"
        )
        result["review_comments"] = sum(
            1 for c in result["new_comments"] if c["type"] == "review_comment"
        )

    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_comment(args: argparse.Namespace) -> None:
    """Post a top-level comment on the PR."""
    pr = args.pr_number
    repo = _get_repo()
    body = _read_body(args.body, args.body_file)

    result = _gh_api_post(f"repos/{repo}/issues/{pr}/comments", {"body": body})
    _invalidate_cache(f"pr_{pr}_issue_comments*")
    comment_url = result.get("html_url", "")
    print(json.dumps({"ok": True, "url": comment_url}))


def cmd_reply(args: argparse.Namespace) -> None:
    """Reply to a review comment, auto-detecting the correct endpoint.

    Inline review comments (code suggestions) -> thread reply endpoint
    Top-level issue comments -> new top-level comment with quote
    """
    pr = args.pr_number
    comment_id = args.comment_id
    body = _with_address_marker(
        _read_body(args.body, args.body_file),
        comment_id,
    )
    repo = _get_repo()

    if _is_inline_review_comment(comment_id, pr, repo):
        result = _gh_api_post(
            f"repos/{repo}/pulls/{pr}/comments/{comment_id}/replies",
            {"body": body},
        )
        _invalidate_cache(f"pr_{pr}_review_comments*")
        comment_url = result.get("html_url", "")
        print(json.dumps({"ok": True, "type": "thread_reply", "url": comment_url}))
    else:
        print(
            f"Comment {comment_id} is not an inline review comment; "
            f"posting as top-level comment instead.",
            file=sys.stderr,
        )
        result = _gh_api_post(f"repos/{repo}/issues/{pr}/comments", {"body": body})
        _invalidate_cache(f"pr_{pr}_issue_comments*")
        comment_url = result.get("html_url", "")
        print(json.dumps({"ok": True, "type": "top_level_fallback", "url": comment_url}))


# ── Main ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PR shepherd helper — fetches and structures PR data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-R", "--repo", metavar="OWNER/REPO",
                        help="GitHub repository (default: auto-detect from git remote)")
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="PR status with actionable assessment")
    p_status.add_argument("pr_number", type=int, nargs="?",
                          help="PR number (default: current branch PR)")
    p_status.add_argument("--brief", action="store_true",
                          help="Only emit fields needed for convergence decisions")
    p_status.set_defaults(func=cmd_status)

    # reviews
    p_reviews = sub.add_parser("reviews", help="All review comments (inline threads + issue comments)")
    p_reviews.add_argument("pr_number", type=int, help="PR number")
    p_reviews.add_argument("--comment", dest="comment_id", type=int,
                           help="Return one full review, inline comment, or issue comment")
    p_reviews.set_defaults(func=cmd_reviews)

    # checks
    p_checks = sub.add_parser("checks", help="Detailed check run info with run IDs")
    p_checks.add_argument("pr_number", type=int, help="PR number")
    p_checks.set_defaults(func=cmd_checks)

    # wait-for-checks
    p_wait = sub.add_parser("wait-for-checks",
                            help="Poll until checks complete (conflict + review detection)")
    p_wait.add_argument("pr_number", type=int, help="PR number")
    p_wait.add_argument("--timeout", type=int, default=900,
                        help="Timeout in seconds (default: 900)")
    p_wait.add_argument("--interval", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    p_wait.add_argument("--check-reviews", action="store_true",
                        help="After checks complete, check for new review comments")
    p_wait.add_argument("--exclude-author", dest="exclude_authors", action="append",
                        help="Exclude from review detection (default: current user)")
    p_wait.set_defaults(func=cmd_wait_for_checks)

    # new-reviews
    p_new = sub.add_parser("new-reviews",
                           help="New review activity (issue + inline comments)")
    p_new.add_argument("pr_number", type=int, help="PR number")
    p_new.add_argument("--since",
                       help="ISO timestamp — only show comments after this time")
    p_new.add_argument("--author", dest="authors", action="append",
                       help="Only include these authors (repeatable)")
    p_new.add_argument("--exclude-author", dest="exclude_authors", action="append",
                       help="Exclude these authors (default: current user)")
    p_new.set_defaults(func=cmd_new_reviews)

    # comment (write)
    p_comment = sub.add_parser("comment", help="Post a top-level comment on the PR")
    p_comment.add_argument("pr_number", type=int, help="PR number")
    comment_body = p_comment.add_mutually_exclusive_group(required=True)
    comment_body.add_argument("body", nargs="?", help="Comment body text, or - for stdin")
    comment_body.add_argument("--body-file", type=Path, help="Read comment body from file")
    p_comment.set_defaults(func=cmd_comment)

    # reply (write)
    p_reply = sub.add_parser("reply", help="Reply to a review comment (auto-detects type)")
    p_reply.add_argument("pr_number", type=int, help="PR number")
    p_reply.add_argument("comment_id", type=int, help="Comment ID to reply to")
    reply_body = p_reply.add_mutually_exclusive_group(required=True)
    reply_body.add_argument("body", nargs="?", help="Reply body text, or - for stdin")
    reply_body.add_argument("--body-file", type=Path, help="Read reply body from file")
    p_reply.set_defaults(func=cmd_reply)

    args = parser.parse_args()

    # Seed repo cache from -R flag so _get_repo() and gh pr commands use it
    if args.repo:
        global _repo_cache
        _repo_cache = args.repo

    args.func(args)


if __name__ == "__main__":
    main()
