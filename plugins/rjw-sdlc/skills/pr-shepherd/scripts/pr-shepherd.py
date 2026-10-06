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
    reply <PR> <id> <body|-> Reply to a review comment (detects type, picks endpoint);
                             needs --finding LABEL (repeatable) and/or --all-handled
    ack <PR> [--comment-ids] Acknowledge pickup: add the `shepherd: addressing`
                             label and 👀-react to the named review items
    clear <PR>               Remove the `shepherd: addressing` label (explicit;
                             `status` and `reply --all-handled` also clear it
                             automatically once review findings have converged)

Pickup signal: while a crew works a review offline (fix, test, push, reply can
take an hour), the PR looks untouched. `ack` makes the work visible on GitHub —
a `shepherd: addressing` label meaning "unanswered review points remain; not
merge-ready", plus an eyes reaction on each comment it is handling (a review
body has no reactions endpoint, so the label covers it). The label clears
itself once review findings converge: `reply --all-handled` and `status` both
drop it when no reviewer comment is open or partly answered (review feedback
only — pending or failing checks don't keep it set). `clear` is the explicit
form; a stalled or blocked crew, with findings still open, leaves it set.

Identity: comments by the PR author and by you are never actionable. Under a
GitHub App installation token, name yourself with --as <app-slug>[bot] or
PR_SHEPHERD_AS; such tokens can't look themselves up.

Findings: a comment stays in action items until a reply marks it
--all-handled. --finding replies record progress without clearing it.

Reviewers: humans and review bots (built-in list plus --review-bot LOGIN).
Other bots are ignored, as are empty APPROVED/COMMENTED reviews, superseded
reviews, and resolved threads.

No-wait mode: with --no-wait, or when FLOTILLA_CREW_ID is set, wait-for-checks
returns one snapshot instead of polling; the caller replies and yields.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

# ── Caching ────────────────────────────────────────────────────────────

CACHE_DIR = Path("/tmp/pr_shepherd_cache")
CACHE_TTL = 120  # 2 minutes — PR state changes faster than issue state
# Marker semantics (rjw-skills#9). A review comment often carries several
# findings, and the helper can't split them reliably, so it never decides
# for itself that a comment is settled:
#   pr-shepherd-finding:<id>:<label>  one finding in comment <id> answered;
#                                     the comment stays actionable.
#   pr-shepherd-addresses:<id>        every finding in comment <id> handled;
#                                     only this clears the comment. `reply`
#                                     writes it only under --all-handled.
# flotilla (flotilla-org/flotilla#2300) reads the addresses marker to decide
# whether a crew still owes a review reply, so its form must stay stable.
ADDRESS_MARKER_RE = re.compile(r"<!--\s*pr-shepherd-addresses:(\d+)\s*-->")
FINDING_MARKER_RE = re.compile(r"<!--\s*pr-shepherd-finding:(\d+):([A-Za-z0-9._-]+)\s*-->")
FINDING_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Bots whose comments are review feedback. Every other bot (CI reporters,
# coverage, dependency bots) is ignored; add more with --review-bot.
DEFAULT_REVIEW_BOTS = frozenset({
    "claude[bot]",
    "copilot-pull-request-reviewer[bot]",
    "coderabbitai[bot]",
    "chatgpt-codex-connector[bot]",
    "gemini-code-assist[bot]",
    "cursor[bot]",
    "greptile-apps[bot]",
})
# Running as a flotilla crew: flotilla wakes the crew when checks finish or
# review feedback lands, so the helper does one pass and yields.
NO_WAIT_ENV = "FLOTILLA_CREW_ID"
# Pickup signal (rjw-skills#11). `ack` marks a PR as being worked on so the
# owner, who merges from GitHub, can tell "a crew is on it" from "ignored"
# during the long offline fix/test/push window. The label means the PR still
# has unanswered review points and is not merge-ready. Clearing is deterministic
# (rjw-skills#12): `status` and `reply --all-handled` drop the label the moment
# review findings converge, so no-wait crews that answer every finding and
# complete don't strand it set. A stalled crew with findings still open leaves
# it on; `clear` is the explicit form.
ADDRESSING_LABEL = "shepherd: addressing"
ADDRESSING_LABEL_DESCRIPTION = "A crew is processing review feedback; not merge-ready"
ADDRESSING_LABEL_COLOR = "fbca04"
# The reaction `ack` leaves on each item it picks up (GitHub reaction content).
PICKUP_REACTION = "eyes"
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


def _ci_ema_path() -> Path:
    try:
        repo = _get_repo().replace("/", "_")
    except Exception:
        repo = (_repo_cache or "default").replace("/", "_")
    return _cache_path(f"ci_ema_{repo}")


def _load_ci_ema() -> float | None:
    """Expected seconds for this repo's checks to complete, learned from
    prior waits (EMA). No API cost — each completed wait is an observation."""
    try:
        return float(json.loads(_ci_ema_path().read_text())["ema_seconds"])
    except Exception:
        return None


def _record_ci_duration(seconds: float) -> None:
    prior = _load_ci_ema()
    ema = seconds if prior is None else 0.7 * prior + 0.3 * seconds
    try:
        _ci_ema_path().write_text(json.dumps({"ema_seconds": round(ema, 1)}))
    except OSError:
        pass


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


_as_login: str | None = None


def _get_as_identity() -> dict | None:
    """Resolve the --as / PR_SHEPHERD_AS login to its GitHub user, if given.

    An App installation token can't ask GitHub who it is (`/user` 403s), so
    crews name themselves. Resolving the login to a user id both gives the
    ownership checks something exact to compare and makes a mistyped login
    fail here instead of silently matching nobody.
    """
    if not _as_login:
        return None
    result = subprocess.run(
        ["gh", "api", f"users/{quote(_as_login)}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Error resolving --as {_as_login}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


_user_cache: str | None = None


def _get_current_user() -> str:
    """Get the current GitHub username (cached after first call).

    An explicit --as identity wins and skips the `/user` lookup, which
    App installation tokens aren't permitted to make.
    """
    global _user_cache
    if _user_cache is not None:
        return _user_cache
    as_identity = _get_as_identity()
    if as_identity:
        _user_cache = as_identity["login"]
        return _user_cache
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(
            f"Error detecting user: {result.stderr}"
            "GitHub App installation tokens can't look themselves up; name the "
            "identity with --as <app-slug>[bot] or PR_SHEPHERD_AS.",
            file=sys.stderr,
        )
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


def fetch_pr_author(pr: int, repo: str) -> dict:
    """Fetch the PR author as the REST API names it.

    `gh pr view` renders an App author as "app/<slug>", but comments come
    from the REST API, where the same account is "<slug>[bot]". Taking the
    author from REST keeps both sides in one vocabulary, and its numeric
    id is what ownership checks compare.
    """
    pull = _gh_api(f"repos/{repo}/pulls/{pr}", f"pr_{pr}_pull")
    assert isinstance(pull, dict)
    return pull["user"]


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


RESOLVED_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes { isResolved comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}
"""


def fetch_resolved_thread_root_ids(pr: int, repo: str) -> set[int]:
    """Return the root comment ids of review threads GitHub marks resolved.

    Resolution is only visible through GraphQL. If the query fails, treat
    every thread as unresolved: over-reporting a thread is safe, silently
    dropping one isn't.
    """
    cached = _cache_path(f"pr_{pr}_resolved_threads")
    if _is_fresh(cached):
        return set(json.loads(cached.read_text()))
    owner, _, name = repo.partition("/")
    result = subprocess.run(
        [
            "gh", "api", "graphql",
            "-f", f"query={RESOLVED_THREADS_QUERY}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"number={pr}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        if result.returncode != 0:
            raise ValueError(result.stderr.strip())
        threads = json.loads(result.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        resolved = {
            comments[0]["databaseId"]
            for thread in threads
            if thread.get("isResolved")
            and (comments := thread.get("comments", {}).get("nodes"))
        }
    except (ValueError, KeyError, TypeError) as error:
        print(
            f"Warning: could not read thread resolution ({error}); "
            "treating all threads as unresolved",
            file=sys.stderr,
        )
        return set()
    cached.write_text(json.dumps(sorted(resolved)))
    return resolved


# ── Write operations ───────────────────────────────────────────────────

def _gh_api_request(
    endpoint: str,
    method: str,
    fields: dict[str, str] | None = None,
    tolerate: tuple[str, ...] = (),
) -> dict | list | None:
    """Call a gh api endpoint with an explicit method and field data.

    Returns the parsed JSON response, or None when the call fails with an HTTP
    status named in `tolerate` (e.g. "404" for a DELETE of something absent).
    """
    cmd = ["gh", "api", endpoint, "-X", method]
    for key, value in (fields or {}).items():
        cmd.extend(["-f", f"{key}={value}"])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        if any(status in result.stderr for status in tolerate):
            return None
        print(f"Error: gh api {method} {endpoint}: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout.strip()}


def _gh_api_post(endpoint: str, fields: dict[str, str]) -> dict:
    """POST to a gh api endpoint with field data. Returns parsed JSON response."""
    result = _gh_api_request(endpoint, "POST", fields)
    assert isinstance(result, dict)
    return result


def _gh_api_get_live(endpoint: str) -> dict | list | None:
    """GET a gh api endpoint without caching, returning None on any failure.

    Used for state that must be read fresh and where "couldn't read it" is a
    safe answer — a reaction listing, a label-existence probe."""
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _reactions_endpoint(repo: str, item: dict) -> str | None:
    """The reactions endpoint for a review item, chosen by its surface.

    Issue comments and inline review comments each have a reactions endpoint.
    A submitted review's *body* has none — GitHub's Reactions API covers issue
    comments and pull-request review comments, not reviews — so this returns
    None for a review body, and the caller labels it without a reaction.
    """
    kind = item["type"]
    item_id = item["id"]
    if kind == "issue_comment":
        return f"repos/{repo}/issues/comments/{item_id}/reactions"
    if kind == "review_comment":
        return f"repos/{repo}/pulls/comments/{item_id}/reactions"
    return None


def _has_own_pickup_reaction(endpoint: str, me: str) -> bool:
    """True if this account already left the pickup reaction on the item."""
    reactions = _gh_api_get_live(f"{endpoint}?per_page=100")
    if not isinstance(reactions, list):
        return False
    return any(
        r.get("content") == PICKUP_REACTION and (r.get("user") or {}).get("login") == me
        for r in reactions
    )


def _ensure_addressing_label(repo: str) -> None:
    """Create the addressing label if the repo doesn't define it yet."""
    if _gh_api_get_live(f"repos/{repo}/labels/{quote(ADDRESSING_LABEL)}") is not None:
        return
    _gh_api_request(
        f"repos/{repo}/labels",
        "POST",
        {
            "name": ADDRESSING_LABEL,
            "description": ADDRESSING_LABEL_DESCRIPTION,
            "color": ADDRESSING_LABEL_COLOR,
        },
        tolerate=("422",),  # already created, e.g. by a concurrent shepherd
    )


def _add_addressing_label(repo: str, pr: int) -> None:
    """Add the addressing label to the PR (a no-op if already present)."""
    _gh_api_request(
        f"repos/{repo}/issues/{pr}/labels",
        "POST",
        {"labels[]": ADDRESSING_LABEL},
    )


def _remove_addressing_label(repo: str, pr: int) -> bool:
    """Remove the addressing label; return whether it had been set."""
    removed = _gh_api_request(
        f"repos/{repo}/issues/{pr}/labels/{quote(ADDRESSING_LABEL)}",
        "DELETE",
        tolerate=("404",),  # the label isn't on the PR — nothing to clear
    )
    return removed is not None


def _addressing_label_set(meta: dict) -> bool:
    """Whether the PR carries the addressing label, per its metadata."""
    return any(
        (label.get("name") if isinstance(label, dict) else label) == ADDRESSING_LABEL
        for label in meta.get("labels") or []
    )


def _clear_addressing_label(repo: str, pr: int) -> bool:
    """Remove the addressing label and refresh cached metadata; return whether
    it had been set. Shared by `clear` and by the convergence self-heal in
    `status` and `reply`."""
    removed = _remove_addressing_label(repo, pr)
    if removed:
        _invalidate_cache(f"pr_{pr}_meta*")
    return removed


def _invalidate_cache(pattern: str) -> None:
    """Remove cached files matching a glob pattern."""
    for path in CACHE_DIR.glob(pattern):
        path.unlink(missing_ok=True)


def _read_body(body: str | None, body_file: Path | None) -> str:
    """Resolve a command body from an argument, stdin, or a file."""
    if body_file is not None:
        # `--body-file -` follows the gh convention: read standard input.
        if str(body_file) == "-":
            return sys.stdin.read()
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


def _with_reply_markers(
    body: str, comment_id: int, findings: list[str], all_handled: bool
) -> str:
    """Record, in hidden metadata, which findings of which comment this
    reply answers, and whether it settles the whole comment."""
    markers = [f"<!-- pr-shepherd-finding:{comment_id}:{label} -->" for label in findings]
    if all_handled:
        markers.append(f"<!-- pr-shepherd-addresses:{comment_id} -->")
    return f"{body.rstrip()}\n\n" + "\n".join(markers) + "\n"


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
    """Read the fully-addressed markers from GitHub comment bodies."""
    return {
        int(match)
        for comment in comments
        for match in ADDRESS_MARKER_RE.findall(comment.get("body") or "")
    }


def _find_answered_findings(comments: list[dict]) -> dict[int, list[str]]:
    """Map comment id -> labels of findings answered so far, in reply order."""
    answered: dict[int, list[str]] = {}
    for comment in comments:
        for comment_id, label in FINDING_MARKER_RE.findall(comment.get("body") or ""):
            labels = answered.setdefault(int(comment_id), [])
            if label not in labels:
                labels.append(label)
    return answered


def _is_bot(user: dict) -> bool:
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


class Feedback:
    """Decides whose comments are review feedback and which are settled.

    The "actionable" rule matches flotilla's crew wake-up rule:
    - our own comments (PR author, --as identity) are never feedback;
    - humans and configured review bots are reviewers; other bots are ignored;
    - only a reviewer's latest review counts, and empty APPROVED/COMMENTED
      reviews (thread replies create those) are ignored;
    - resolved threads are skipped;
    - a comment stays actionable until a fully-addressed marker names it or
      it is an unambiguous no-findings approval.
    """

    def __init__(
        self,
        own_ids: set[int],
        review_bots: set[str],
        marker_sources: list[dict],
        extra_addressed: set[int] | None = None,
    ) -> None:
        self.own_ids = own_ids
        self.review_bots = review_bots
        # `extra_addressed` names comments to treat as settled on top of the
        # markers already on GitHub: used right after a `reply --all-handled`
        # so convergence reflects the comment this reply just settled without
        # waiting for GitHub to echo the new reply back.
        self.addressed = _find_addressed_comment_ids(marker_sources) | (extra_addressed or set())
        self.answered = _find_answered_findings(marker_sources)
        self.ignored_bots: set[str] = set()

    def is_reviewer(self, user: dict) -> bool:
        if user.get("id") in self.own_ids:
            return False
        if _is_bot(user) and user.get("login", "") not in self.review_bots:
            self.ignored_bots.add(user.get("login", ""))
            return False
        return True

    def is_open(self, comment: dict) -> bool:
        """A reviewer's comment that is neither fully addressed nor approving."""
        return (
            self.is_reviewer(comment.get("user") or {})
            and comment["id"] not in self.addressed
            and not _is_approving_comment(comment)
        )

    def partially_answered(self, comment_ids: list[int]) -> list[int]:
        return sorted(i for i in comment_ids if i in self.answered)

    def latest_reviews(self, reviews: list[dict]) -> list[dict]:
        """Each reviewer's latest review that carries feedback or a verdict."""
        latest: dict[str, dict] = {}
        for review in reviews:
            state = review.get("state", "")
            body = (review.get("body") or "").strip()
            if state in ("PENDING", "DISMISSED"):
                continue
            if state in ("APPROVED", "COMMENTED") and not body:
                continue
            if not self.is_reviewer(review.get("user") or {}):
                continue
            login = review["user"]["login"]
            key = (review.get("submitted_at") or "", review.get("id") or 0)
            if login not in latest or key >= latest[login]["_key"]:
                latest[login] = {**review, "_key": key}
        return [{k: v for k, v in r.items() if k != "_key"} for r in latest.values()]

    def open_threads(
        self, review_comments: list[dict], resolved_roots: set[int]
    ) -> tuple[list[dict], list[int]]:
        """Unresolved threads with a reviewer comment still open, plus the
        ids of those open comments.

        Every reviewer comment in the thread counts, not only the last: a
        reviewer who repeats a finding after our reply has not been settled.
        """
        by_id = {comment["id"]: comment for comment in review_comments}
        open_threads = []
        open_ids = []
        for thread in _group_comment_threads(review_comments):
            if thread["id"] in resolved_roots:
                continue
            ids = [thread["id"], *(reply["id"] for reply in thread["replies"])]
            still_open = [i for i in ids if self.is_open(by_id[i])]
            if still_open:
                open_threads.append(thread)
                open_ids.extend(still_open)
        return open_threads, open_ids


def _parse_review_bots(extra: list[str] | None) -> set[str]:
    return set(DEFAULT_REVIEW_BOTS) | set(extra or [])


def _no_wait(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_wait", False) or os.environ.get(NO_WAIT_ENV))


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

def _assess_pr(
    pr: int,
    repo: str,
    args: argparse.Namespace,
    extra_addressed: set[int] | None = None,
) -> dict:
    """Fetch a PR and compute the review/check facts `status` and convergence need.

    `extra_addressed` names comments to treat as fully addressed on top of the
    markers already on GitHub — used right after a `reply --all-handled` posts,
    so convergence reflects the comment this reply just settled without waiting
    for GitHub to echo the new reply back.

    The returned `review_findings_open` is the convergence signal the addressing
    label tracks: True while any reviewer comment, review body, or unresolved
    thread is still open or only partly answered. It covers review feedback
    only — not CI failures, pending checks, or merge conflicts — because the
    label means "unanswered review points remain", not "not yet mergeable for
    any reason".
    """
    meta = fetch_pr_metadata(pr)
    checks = fetch_pr_checks(pr)
    reviews = fetch_pr_reviews(pr, repo)
    review_comments = fetch_pr_review_comments(pr, repo)
    issue_comments = fetch_pr_issue_comments(pr, repo)

    check_info = _summarize_checks(checks)
    review_summary = _summarize_reviews(reviews)

    # Comments by these user ids are ours, never actionable.
    own_ids = {fetch_pr_author(pr, repo)["id"]}
    as_identity = _get_as_identity()
    if as_identity:
        own_ids.add(as_identity["id"])

    # Replies land on whichever surface they answer (a reply to a review
    # body is an issue comment), so read markers from both.
    feedback = Feedback(
        own_ids,
        _parse_review_bots(args.review_bots),
        issue_comments + review_comments,
        extra_addressed,
    )

    unresolved, open_review_comment_ids = feedback.open_threads(
        review_comments, fetch_resolved_thread_root_ids(pr, repo)
    )
    addressed_review_comment_ids = _find_addressed_comment_ids(review_comments)
    approving_review_comment_ids = {
        comment["id"]
        for comment in review_comments
        if _is_approving_comment(comment)
    }

    actionable_reviews = [
        review for review in feedback.latest_reviews(reviews) if feedback.is_open(review)
    ]

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
        comment for comment in issue_comments if feedback.is_open(comment)
    ]
    open_ids = (
        open_review_comment_ids
        + [review["id"] for review in actionable_reviews]
        + [comment["id"] for comment in actionable_issue_comments]
    )
    partially_answered = feedback.partially_answered(open_ids)
    reviewer_issue_comments = len(actionable_issue_comments)

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
    if actionable_reviews:
        action_items.append(f"address {len(actionable_reviews)} review bodies from reviewers")
    if reviewer_issue_comments > 0:
        action_items.append(f"evaluate {reviewer_issue_comments} issue comments from reviewers/bots")
    if partially_answered:
        ids = ", ".join(str(i) for i in partially_answered)
        action_items.append(
            f"finish {len(partially_answered)} partly answered comments ({ids}): "
            "answer every remaining finding, then reply --all-handled"
        )

    # Review feedback only — the label's claim is about review points, so CI
    # and conflict action items deliberately don't keep it set.
    review_findings_open = bool(
        unresolved or actionable_reviews or actionable_issue_comments or partially_answered
    )

    return {
        "meta": meta,
        "check_info": check_info,
        "review_summary": review_summary,
        "unresolved": unresolved,
        "actionable_reviews": actionable_reviews,
        "reviewer_issue_comments": reviewer_issue_comments,
        "addressed_review_comment_ids": addressed_review_comment_ids,
        "approving_review_comment_ids": approving_review_comment_ids,
        "addressed_comment_ids": addressed_comment_ids,
        "approving_comment_ids": approving_comment_ids,
        "partially_answered": partially_answered,
        "answered": feedback.answered,
        "ignored_bots": feedback.ignored_bots,
        "comments_by_author": comments_by_author,
        "body_refs": body_refs,
        "total_issue_comments": len(issue_comments),
        "merge_state": merge_state,
        "action_items": action_items,
        "review_findings_open": review_findings_open,
    }


def cmd_status(args: argparse.Namespace) -> None:
    """PR status with actionable assessment."""
    pr = _resolve_pr_number(args.pr_number)
    repo = _get_repo()

    a = _assess_pr(pr, repo, args)
    meta = a["meta"]
    check_info = a["check_info"]
    review_summary = a["review_summary"]
    unresolved = a["unresolved"]
    actionable_reviews = a["actionable_reviews"]
    reviewer_issue_comments = a["reviewer_issue_comments"]
    partially_answered = a["partially_answered"]
    merge_state = a["merge_state"]
    action_items = a["action_items"]

    pr_author = meta.get("author", {}).get("login", "")

    # Self-heal the pickup label: once review findings have converged its claim
    # ("unanswered review points remain") is false, so drop it even if checks
    # are still pending or failing (section 1.1). A later status is then
    # consistent without anyone remembering to run `clear`.
    label_set = _addressing_label_set(meta)
    addressing_cleared = False
    if label_set and not a["review_findings_open"]:
        if _clear_addressing_label(repo, pr):
            label_set = False
            addressing_cleared = True

    mode = "no-wait" if _no_wait(args) else "wait"

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
            "actionable_review_ids": sorted(review["id"] for review in actionable_reviews),
            "addressed_comment_ids": sorted(a["addressed_review_comment_ids"]),
            "approving_comment_ids": sorted(a["approving_review_comment_ids"]),
        },
        "issue_comments": {
            "total": a["total_issue_comments"],
            "by_author": a["comments_by_author"],
            "actionable": reviewer_issue_comments,
            "addressed_ids": sorted(a["addressed_comment_ids"]),
            "approving_ids": sorted(a["approving_comment_ids"]),
        },
        "partially_answered": {
            str(i): a["answered"][i] for i in partially_answered
        },
        "ignored_bot_authors": sorted(a["ignored_bots"]),
        "addressing_label": label_set,
        "linked_issues": a["body_refs"],
        "mode": mode,
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
            "actionable_bodies": len(actionable_reviews),
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
            "partially_answered": sorted(partially_answered),
            "mode": mode,
            "needs_attention": len(action_items) > 0,
            "action_items": action_items,
        }

    # Only when this call actually dropped the label, so convergent runs that
    # never had it (the common case) keep their output shape unchanged.
    if addressing_cleared:
        result["addressing_label_cleared"] = True

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
        markers = issue_comments + review_comments
        answered = _find_answered_findings(markers).get(args.comment_id, [])
        if answered:
            result["answered_findings"] = answered
        if args.comment_id in _find_addressed_comment_ids(markers):
            result["fully_addressed"] = True
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


def _check_snapshot(pr: int) -> None:
    """No-wait mode: report the checks once and tell the caller to yield.

    Under flotilla the crew is woken when checks finish or review feedback
    arrives, so polling here only holds the crew's turn open and spends the
    shared GitHub rate limit.
    """
    _invalidate_cache(f"pr_{pr}_checks*")
    _invalidate_cache(f"pr_{pr}_meta*")
    checks = fetch_pr_checks(pr)
    merge_state = fetch_pr_metadata(pr).get("mergeable", "UNKNOWN")
    check_info = _summarize_checks(checks)
    pending = [c.get("name", "") for c in checks if _classify_check(c) == "pending"]
    result = {
        "done": bool(checks) and not pending,
        "no_wait": True,
        "conflicting": merge_state == "CONFLICTING",
        "merge_state": merge_state,
        "total": len(checks),
        "passed": check_info["counts"]["pass"],
        "failed": check_info["counts"]["fail"],
        "still_pending": len(pending),
        "pending_names": pending,
        "failed_checks": check_info["failed"],
        "message": (
            f"no-wait mode ({NO_WAIT_ENV} set or --no-wait): one snapshot, no polling. "
            "Act on failures or conflicts, reply to every finding, then yield; "
            "flotilla wakes the crew when checks finish or feedback arrives."
        ),
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
    if _no_wait(args):
        _check_snapshot(pr)
        return
    timeout = args.timeout
    # Timing is the tool's concern, not the caller's (flotilla#885): agents
    # were inventing schedules (--interval 10 --timeout 50, re-invoked in a
    # loop). The tool owns its pacing outright — --interval is ignored, and
    # sub-5-minute timeouts are raised so one call means one real wait.
    # There is deliberately no override: a previous env-var escape hatch
    # (PR_SHEPHERD_FAST) was discovered and used by every agent within a day
    # of shipping — advisory infrastructure gets bypassed (flotilla#812).
    # Humans who genuinely need manual pacing can edit this script.
    interval = 30
    if args.interval != 30:
        print("Note: --interval is tool-managed and was ignored", file=sys.stderr)
    if timeout < 300:
        print(f"Note: --timeout {int(timeout)}s raised to 300s — one call, one real wait", file=sys.stderr)
        timeout = 300
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
    # Expectation-first polling (flotilla#885): after confirming checks are
    # pending, sleep to ~90% of the learned expected CI duration before
    # entering the poll/backoff cadence — aim at the completion time instead
    # of sampling uniformly.
    expected = _load_ci_ema()
    expectation_slept = False
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
            _record_ci_duration(time.time() - (deadline - timeout))
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
        if expected is not None and not expectation_slept:
            expectation_slept = True
            # Anchor on the CI run's actual start so repeated short-timeout
            # invocations still converge on the expected completion time.
            started = [c.get("startedAt") for c in checks if c.get("startedAt")]
            try:
                earliest = min(
                    datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
                    for s in started
                )
                elapsed = time.time() - earliest
            except ValueError:
                elapsed = time.time() - (deadline - timeout)
            if timeout < 0.5 * expected:
                print(f"Note: expected CI duration ~{int(expected)}s for this repo; "
                      f"--timeout {int(timeout)}s will return before completion — "
                      "prefer a single wait-for-checks with a larger --timeout over re-invoking",
                      file=sys.stderr)
            head_start = 0.9 * expected - elapsed
            if head_start > interval:
                print(f"Expected CI duration ~{int(expected)}s; sleeping {int(head_start)}s toward it",
                      file=sys.stderr)
                time.sleep(min(head_start, remaining))
                continue
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


def _autoclear_after_reply(repo: str, pr: int, comment_id: int, args: argparse.Namespace) -> str | None:
    """Drop the addressing label when this `--all-handled` reply converges the PR.

    Treats `comment_id` as settled (its addresses marker was just posted) and
    recomputes review-finding convergence. Returns "cleared" when it removed
    the label, else None. A cheap metadata read short-circuits when the label
    isn't set, so the common case spends only one extra call.
    """
    meta = fetch_pr_metadata(pr)
    if not _addressing_label_set(meta):
        return None
    assessment = _assess_pr(pr, repo, args, extra_addressed={comment_id})
    if assessment["review_findings_open"]:
        return None
    return "cleared" if _clear_addressing_label(repo, pr) else None


def cmd_reply(args: argparse.Namespace) -> None:
    """Reply to a review comment, auto-detecting the correct endpoint.

    Inline review comments (code suggestions) -> thread reply endpoint
    Top-level issue comments -> new top-level comment with quote
    """
    pr = args.pr_number
    comment_id = args.comment_id
    findings = args.findings or []
    if not findings and not args.all_handled:
        print(
            "Error: say what this reply settles: --finding LABEL for each finding "
            "it answers, and/or --all-handled once every finding in the comment "
            "is fixed, answered, or deferred to an issue",
            file=sys.stderr,
        )
        sys.exit(2)
    bad = [label for label in findings if not FINDING_LABEL_RE.match(label)]
    if bad:
        print(
            f"Error: finding labels may use only letters, digits, '.', '_' and '-': {bad}",
            file=sys.stderr,
        )
        sys.exit(2)
    body = _with_reply_markers(
        _read_body(args.body, args.body_file),
        comment_id,
        findings,
        args.all_handled,
    )
    repo = _get_repo()

    if _is_inline_review_comment(comment_id, pr, repo):
        result = _gh_api_post(
            f"repos/{repo}/pulls/{pr}/comments/{comment_id}/replies",
            {"body": body},
        )
        _invalidate_cache(f"pr_{pr}_review_comments*")
        comment_url = result.get("html_url", "")
        response = {"ok": True, "type": "thread_reply", "url": comment_url}
    else:
        print(
            f"Comment {comment_id} is not an inline review comment; "
            f"posting as top-level comment instead.",
            file=sys.stderr,
        )
        result = _gh_api_post(f"repos/{repo}/issues/{pr}/comments", {"body": body})
        _invalidate_cache(f"pr_{pr}_issue_comments*")
        comment_url = result.get("html_url", "")
        response = {"ok": True, "type": "top_level_fallback", "url": comment_url}

    # Settling the last open comment converges the PR; clear the pickup label
    # so the owner, who merges from GitHub, isn't told it's still not ready.
    if args.all_handled:
        cleared = _autoclear_after_reply(repo, pr, comment_id, args)
        if cleared:
            response["addressing_label"] = cleared

    print(json.dumps(response))


def cmd_ack(args: argparse.Namespace) -> None:
    """Acknowledge pickup: label the PR and react to the items being handled.

    The label marks the PR not-merge-ready while a crew works; the reaction
    shows which items it is addressing. Both are idempotent: adding a present
    label is a no-op, and a reaction this account already left is skipped.
    """
    pr = _resolve_pr_number(args.pr_number)
    repo = _get_repo()

    _ensure_addressing_label(repo)
    _add_addressing_label(repo, pr)
    _invalidate_cache(f"pr_{pr}_meta*")

    reacted: list[int] = []
    already_reacted: list[int] = []
    unreactable: list[int] = []
    not_found: list[int] = []
    if args.comment_ids:
        reviews = fetch_pr_reviews(pr, repo)
        review_comments = fetch_pr_review_comments(pr, repo)
        issue_comments = fetch_pr_issue_comments(pr, repo)
        me = _get_current_user()
        for comment_id in args.comment_ids:
            item = _find_review_item(comment_id, reviews, review_comments, issue_comments)
            if item is None:
                not_found.append(comment_id)
                print(f"Warning: review item {comment_id} not found on PR #{pr}; skipping",
                      file=sys.stderr)
                continue
            endpoint = _reactions_endpoint(repo, item)
            if endpoint is None:
                # A review body has no reactions endpoint; the label covers it.
                unreactable.append(comment_id)
                print(f"Note: review item {comment_id} is a {item['type']}, which has no "
                      "reactions endpoint; covered by the label only", file=sys.stderr)
                continue
            if _has_own_pickup_reaction(endpoint, me):
                already_reacted.append(comment_id)
                continue
            _gh_api_request(endpoint, "POST", {"content": PICKUP_REACTION})
            reacted.append(comment_id)

    print(json.dumps({
        "ok": True,
        "label": ADDRESSING_LABEL,
        "label_set": True,
        "reacted": reacted,
        "already_reacted": already_reacted,
        "unreactable": unreactable,
        "not_found": not_found,
    }))


def cmd_clear(args: argparse.Namespace) -> None:
    """Clear the addressing label explicitly.

    A no-op when the label isn't set. `status` and `reply --all-handled` clear
    it on their own once review findings converge, so this is mainly an escape
    hatch — to drop it by hand, or where no converging command will run next.
    It removes the label unconditionally; it does not re-check convergence.
    """
    pr = _resolve_pr_number(args.pr_number)
    repo = _get_repo()
    removed = _clear_addressing_label(repo, pr)
    print(json.dumps({
        "ok": True,
        "label": ADDRESSING_LABEL,
        "removed": removed,
    }))


# ── Main ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PR shepherd helper — fetches and structures PR data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-R", "--repo", metavar="OWNER/REPO",
                        help="GitHub repository (default: auto-detect from git remote)")
    parser.add_argument("--as", dest="as_login", metavar="LOGIN",
                        default=os.environ.get("PR_SHEPHERD_AS") or None,
                        help="GitHub login to act as, e.g. my-app[bot] "
                             "(default: $PR_SHEPHERD_AS, else the token's user). "
                             "Required for GitHub App installation tokens")
    parser.add_argument("--no-wait", action="store_true",
                        help=f"One pass, no polling: wait-for-checks returns a single "
                             f"snapshot (default when ${NO_WAIT_ENV} is set)")
    parser.add_argument("--review-bot", dest="review_bots", action="append", metavar="LOGIN",
                        help="Treat this bot's comments as review feedback (repeatable; "
                             "added to the built-in review bots). Other bots are ignored")
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
    # Accepted only so stale caller habits don't crash; ignored and hidden.
    p_wait.add_argument("--interval", type=int, default=30, help=argparse.SUPPRESS)
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
    comment_body.add_argument("--body-file", type=Path, help="Read comment body from file, or - for stdin")
    p_comment.set_defaults(func=cmd_comment)

    # reply (write)
    p_reply = sub.add_parser("reply", help="Reply to a review comment (auto-detects type)")
    p_reply.add_argument("pr_number", type=int, help="PR number")
    p_reply.add_argument("comment_id", type=int, help="Comment ID to reply to")
    reply_body = p_reply.add_mutually_exclusive_group(required=True)
    reply_body.add_argument("body", nargs="?", help="Reply body text, or - for stdin")
    reply_body.add_argument("--body-file", type=Path, help="Read reply body from file, or - for stdin")
    p_reply.add_argument("--finding", dest="findings", action="append", metavar="LABEL",
                         help="Label of a finding this reply answers, e.g. 1, 2, nit-3 "
                              "(repeatable). Leaves the comment actionable")
    p_reply.add_argument("--all-handled", action="store_true",
                         help="Every finding in the comment is now fixed, answered, or "
                              "deferred to an issue; clears it from action items")
    p_reply.set_defaults(func=cmd_reply)

    # ack (write)
    p_ack = sub.add_parser("ack",
                           help="Acknowledge pickup: label the PR and react to items being handled")
    p_ack.add_argument("pr_number", type=int, nargs="?",
                       help="PR number (default: current branch PR)")
    p_ack.add_argument("--comment-ids", dest="comment_ids", type=int, nargs="+", metavar="ID",
                       help="Review item ids to react to: issue comments, review bodies, "
                            "or inline review comments (the ids reported by status/reviews)")
    p_ack.set_defaults(func=cmd_ack)

    # clear (write)
    p_clear = sub.add_parser("clear",
                             help="Remove the addressing label once the iteration converges")
    p_clear.add_argument("pr_number", type=int, nargs="?",
                         help="PR number (default: current branch PR)")
    p_clear.set_defaults(func=cmd_clear)

    args = parser.parse_args()

    # Seed repo cache from -R flag so _get_repo() and gh pr commands use it
    if args.repo:
        global _repo_cache
        _repo_cache = args.repo
    global _as_login
    _as_login = args.as_login

    args.func(args)


if __name__ == "__main__":
    main()
