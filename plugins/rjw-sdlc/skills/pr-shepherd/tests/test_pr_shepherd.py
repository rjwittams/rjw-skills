from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
from types import ModuleType
from typing import ClassVar
import unittest
from unittest import mock
from urllib.parse import quote


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr-shepherd.py"
FIXTURE_DIR = Path(__file__).parent / "fixtures"
ADDRESSING_LABEL = "shepherd: addressing"


class CliHarness(unittest.TestCase):
    """Runs the helper against a fake `gh` on PATH."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.gh_log = self.root / "gh.jsonl"
        self.pr_number = time.time_ns()

        gh = self.root / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                from pathlib import Path
                import sys
                from urllib.parse import unquote

                args = sys.argv[1:]
                with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
                    log.write(json.dumps(args) + "\\n")

                if args[:1] == ["pr"]:
                    # `gh pr view` / `gh pr checks` spend the shared GraphQL
                    # budget; the helper reads through REST instead.
                    print("fake gh: GraphQL-backed `gh pr` command refused", file=sys.stderr)
                    sys.exit(3)
                elif args[:2] == ["api", "graphql"]:
                    print(os.environ.get(
                        "FAKE_GH_REVIEW_THREADS",
                        json.dumps({"data": {"repository": {"pullRequest": {
                            "reviewThreads": {"nodes": []}}}}}),
                    ))
                elif args[:2] == ["repo", "view"]:
                    print("owner/repo")
                elif (args and args[0] == "api" and "-X" in args and "DELETE" in args
                      and "/issues/" in args[1] and "/labels/" in args[1]):
                    # Removing a label from the PR. 404 when it isn't applied.
                    name = unquote(args[1].split("/labels/", 1)[1])
                    applied = json.loads(os.environ.get("FAKE_GH_APPLIED_LABELS", "[]"))
                    if name in applied:
                        print(json.dumps([]))
                    else:
                        print("gh: Label does not exist (HTTP 404)", file=sys.stderr)
                        sys.exit(1)
                elif args and args[0] == "api" and "-X" in args:
                    print(json.dumps({"html_url": "https://example.test/comment/1"}))
                elif args[:2] == ["api", "user"]:
                    if os.environ.get("FAKE_GH_USER_FORBIDDEN"):
                        print("HTTP 403: Resource not accessible by integration", file=sys.stderr)
                        sys.exit(1)
                    print(os.environ.get("FAKE_GH_VIEWER", "author"))
                elif args and args[0] == "api" and args[1].startswith("users/"):
                    login = args[1].removeprefix("users/").replace("%5B", "[").replace("%5D", "]")
                    users = json.loads(os.environ.get("FAKE_GH_USERS", "{}"))
                    if login not in users:
                        print("HTTP 404: Not Found", file=sys.stderr)
                        sys.exit(1)
                    print(json.dumps(users[login]))
                elif args and args[0] == "api":
                    endpoint = args[1]
                    if "/reactions" in endpoint:
                        value = os.environ.get("FAKE_GH_REACTIONS", "[]")
                    elif "/labels/" in endpoint and "/issues/" not in endpoint:
                        # Repo label-existence probe: 404 unless the repo defines it.
                        if os.environ.get("FAKE_GH_REPO_HAS_LABEL"):
                            value = json.dumps({"name": unquote(endpoint.split("/labels/", 1)[1])})
                        else:
                            print("gh: Not Found (HTTP 404)", file=sys.stderr)
                            sys.exit(1)
                    elif endpoint.rstrip("0123456789").endswith("/pulls/"):
                        value = os.environ.get("FAKE_GH_PULL") or json.dumps({
                            "number": int(endpoint.rsplit("/", 1)[1]),
                            "title": "Shepherded PR",
                            "user": {"login": "author", "id": 1, "type": "User"},
                            "state": "open",
                            "head": {"ref": "shepherded", "sha": "headsha"},
                            "base": {"ref": "main"},
                            "mergeable": True,
                        })
                    elif "/pulls?" in endpoint and "head=" in endpoint:
                        value = os.environ.get("FAKE_GH_HEAD_PULLS", "[]")
                    elif "/check-runs?" in endpoint or (
                            "/commits/" in endpoint and "/status?" in endpoint):
                        key, var = (("check_runs", "FAKE_GH_CHECK_RUNS")
                                    if "/check-runs?" in endpoint
                                    else ("statuses", "FAKE_GH_STATUSES"))
                        items = json.loads(os.environ.get(var, "[]"))
                        page = int(endpoint.rsplit("&page=", 1)[1])
                        value = json.dumps({"total_count": len(items),
                                            key: items[(page - 1) * 100:page * 100]})
                    elif "/actions/runs?" in endpoint:
                        runs = json.loads(os.environ.get("FAKE_GH_WORKFLOW_RUNS", "[]"))
                        value = json.dumps({"total_count": len(runs), "workflow_runs": runs})
                    elif "/pulls/" in endpoint and endpoint.endswith("/reviews?per_page=100"):
                        value = os.environ.get("FAKE_GH_REVIEWS", "[]")
                    elif "/pulls/" in endpoint and endpoint.endswith("/comments?per_page=100"):
                        value = os.environ.get("FAKE_GH_REVIEW_COMMENTS", "[]")
                    elif "/issues/" in endpoint and endpoint.endswith("/comments?per_page=100"):
                        value = os.environ.get("FAKE_GH_ISSUE_COMMENTS", "[]")
                    else:
                        value = "[]"
                    print(value)
                else:
                    print(json.dumps([]))
                """
            )
        )
        gh.chmod(0o755)

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.root}{os.pathsep}{self.env['PATH']}"
        self.env["FAKE_GH_LOG"] = str(self.gh_log)
        # Tests run inside flotilla crews too; standalone is the default here.
        self.env.pop("FLOTILLA_CREW_ID", None)
        self.env.pop("PR_SHEPHERD_AS", None)
        self.env.pop("GH_HOST", None)

        # The helper runs inside a checkout of owner/repo on branch
        # `shepherded`; it reads the repository from the git remote.
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.git("init", "-q", "-b", "shepherded")
        self.git("remote", "add", "origin", "git@github.com:owner/repo.git")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.checkout), *args], check=True,
                       capture_output=True)

    def run_cli(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.checkout,
            timeout=10,
        )

    def gh_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def reply_post(self) -> list[str]:
        """The reply's POST call, located by its body= field rather than by
        position: `reply --all-handled` runs a convergence check afterward, so
        it is no longer the last gh invocation."""
        return next(
            call for call in reversed(self.gh_calls())
            if any(arg.startswith("body=") for arg in call)
        )

    def pull(self, author_login: str = "author", **fields: object) -> str:
        """The PR as `GET /repos/{o}/{r}/pulls/{n}` returns it."""
        pull = {
            "number": self.pr_number,
            "title": "Shepherded PR",
            "body": "",
            "user": {"login": author_login, "id": 1, "type": "User"},
            "state": "open",
            "merged": False,
            "draft": False,
            "base": {"ref": "main"},
            "head": {"ref": "shepherded", "sha": "headsha"},
            "mergeable": True,
            "html_url": "https://example.test/pr/shepherded",
            "additions": 1,
            "deletions": 0,
            "changed_files": 1,
            "labels": [],
        }
        pull.update(fields)
        return json.dumps(pull)

    @staticmethod
    def check_run(name: str, conclusion: str | None, run_id: int = 1) -> dict:
        """A REST check run; conclusion None means still in progress."""
        return {
            "id": run_id,
            "name": name,
            "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion,
            "started_at": "2026-07-18T10:00:00Z",
            "completed_at": "2026-07-18T10:05:00Z" if conclusion else None,
            "details_url": f"https://github.com/owner/repo/actions/runs/{900 + run_id}/job/{run_id}",
            "check_suite": {"id": 1},
        }

    def graphql_calls(self) -> list[list[str]]:
        """gh invocations that spend the GraphQL budget."""
        return [
            c for c in self.gh_calls()
            if c[:2] == ["api", "graphql"] or c[:1] == ["pr"] or c[:2] == ["repo", "view"]
        ]


class PrShepherdCliTest(CliHarness):
    def test_comment_reads_body_from_stdin_without_changing_markdown(self) -> None:
        body = "Fixed `CheckoutReconciler`.\n\n- Preserved $literal syntax.\n"

        result = self.run_cli("comment", str(self.pr_number), "-", input_text=body)

        self.assertEqual(result.returncode, 0, result.stderr)
        post = self.gh_calls()[-1]
        self.assertIn(f"body={body}", post)

    def test_comment_reads_body_from_file_without_changing_markdown(self) -> None:
        body = "Filed as #123 for `follow-up`.\n"
        body_file = self.root / "reply.md"
        body_file.write_text(body)

        result = self.run_cli(
            "comment", str(self.pr_number), "--body-file", str(body_file)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        post = self.gh_calls()[-1]
        self.assertIn(f"body={body}", post)

    def test_comment_body_file_dash_reads_stdin(self) -> None:
        body = "Read from `stdin` via --body-file -.\n"

        result = self.run_cli(
            "comment", str(self.pr_number), "--body-file", "-", input_text=body
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        post = self.gh_calls()[-1]
        self.assertIn(f"body={body}", post)

    def test_reply_body_file_dash_reads_stdin(self) -> None:
        body = "Fixed via --body-file -.\n"

        result = self.run_cli(
            "reply", str(self.pr_number), "9001", "--all-handled", "--body-file", "-",
            input_text=body,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "body=Fixed via --body-file -.\n\n"
            "<!-- pr-shepherd-addresses:9001 -->\n",
            self.reply_post(),
        )

    def test_reply_records_the_exact_comment_id_it_addresses(self) -> None:
        body = "Fixed the reported race.\n"

        result = self.run_cli(
            "reply", str(self.pr_number), "9001", "--all-handled", "-", input_text=body
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        post = self.reply_post()
        self.assertIn(
            "body=Fixed the reported race.\n\n"
            "<!-- pr-shepherd-addresses:9001 -->\n",
            post,
        )

    def test_reviews_can_return_one_full_comment_by_id(self) -> None:
        body = "A long review body with `code` that must not be truncated."
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                {
                    "id": 9001,
                    "user": {"login": "reviewer"},
                    "body": body,
                    "created_at": "2026-07-18T10:00:00Z",
                    "html_url": "https://example.test/comment/9001",
                }
            ]
        )

        result = self.run_cli(
            "reviews", str(self.pr_number), "--comment", "9001"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "type": "issue_comment",
                "id": 9001,
                "author": "reviewer",
                "body": body,
                "created_at": "2026-07-18T10:00:00Z",
                "html_url": "https://example.test/comment/9001",
            },
        )

    def test_status_brief_returns_the_convergence_fields_without_raw_detail(self) -> None:
        self.env["FAKE_GH_PULL"] = self.pull(
            title="Make replies safe", head={"ref": "safe-replies", "sha": "headsha"},
            html_url="https://example.test/pr/42",
        )
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps(
            [self.check_run("test", "success", 1), self.check_run("review", None, 2)]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "pr": {
                    "number": self.pr_number,
                    "title": "Make replies safe",
                    "url": "https://example.test/pr/42",
                },
                "merge_state": "MERGEABLE",
                "checks": {"pass": 1, "fail": 0, "pending": 1, "skipped": 0},
                "reviews": {
                    "approved": 0,
                    "changes_requested": 0,
                    "pending": 0,
                    "unresolved_threads": 0,
                    "actionable_bodies": 0,
                },
                "issue_comments": {"actionable": 0},
                "partially_answered": [],
                "mode": "wait",
                "needs_attention": True,
                "action_items": ["1 checks still pending"],
            },
        )

    def test_status_detects_the_current_branch_pr_when_number_is_omitted(self) -> None:
        self.env["FAKE_GH_HEAD_PULLS"] = json.dumps([{"number": self.pr_number}])
        self.env["FAKE_GH_PULL"] = self.pull(
            title="Detected PR", head={"ref": "shepherded", "sha": "headsha"},
            html_url="https://example.test/pr/detected",
        )

        result = self.run_cli("status", "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pr"]["number"], self.pr_number)
        # The current branch's PR is found through REST, by head owner:branch.
        self.assertIn(
            "repos/owner/repo/pulls?head=owner%3Ashepherded&state=open&per_page=10",
            [c[1] for c in self.gh_calls() if c[:1] == ["api"]],
        )
        self.assertEqual(self.graphql_calls(), [])

    def test_status_reports_no_pr_when_the_branch_has_none(self) -> None:
        result = self.run_cli("status", "--brief")

        self.assertEqual(result.returncode, 2)
        self.assertIn("could not detect a pull request", result.stderr)

    def test_status_does_not_reflag_an_issue_comment_with_an_address_marker(self) -> None:
        self.env["FAKE_GH_PULL"] = self.pull(
            title="Addressed review", head={"ref": "addressed", "sha": "headsha"},
            html_url="https://example.test/pr/addressed",
        )
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                {
                    "id": 9001,
                    "user": {"login": "reviewer-bot"},
                    "body": "Please fix the race.",
                    "created_at": "2026-07-18T10:00:00Z",
                },
                {
                    "id": 9002,
                    "user": {"login": "author", "id": 1},
                    "body": (
                        "Fixed the race.\n\n"
                        "<!-- pr-shepherd-addresses:9001 -->\n"
                    ),
                    "created_at": "2026-07-18T10:05:00Z",
                },
            ]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["issue_comments"]["actionable"], 0)
        self.assertFalse(status["needs_attention"])
        self.assertEqual(status["action_items"], [])

    def test_status_ignores_only_unambiguously_approving_bot_comments(self) -> None:
        self.env["FAKE_GH_PULL"] = self.pull(
            title="Bot approval", head={"ref": "approved", "sha": "headsha"},
            html_url="https://example.test/pr/approved",
        )
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                {
                    "id": 9101,
                    "user": {"login": "reviewer-bot"},
                    "body": (
                        "I re-reviewed the fixes. No issues found; ready to merge."
                    ),
                    "created_at": "2026-07-18T10:00:00Z",
                },
                {
                    "id": 9102,
                    "user": {"login": "reviewer-bot"},
                    "body": (
                        "Nothing here blocks merge.\n\n"
                        "### 1. Low edge case\nPlease add a regression test."
                    ),
                    "created_at": "2026-07-18T10:05:00Z",
                },
                {
                    "id": 9103,
                    "user": {"login": "reviewer-bot"},
                    "body": (
                        "No further issues found beyond the missing rollback test. "
                        "This is mergeable as-is."
                    ),
                    "created_at": "2026-07-18T10:10:00Z",
                },
            ]
        )

        result = self.run_cli("status", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["issue_comments"]["actionable"], 2)
        self.assertEqual(status["issue_comments"]["approving_ids"], [9101])
        self.assertEqual(
            status["action_items"],
            ["evaluate 2 issue comments from reviewers/bots"],
        )

    def test_status_uses_the_reviewers_latest_state(self) -> None:
        self.env["FAKE_GH_PULL"] = self.pull(
            title="Latest review state", head={"ref": "latest-review", "sha": "headsha"},
            html_url="https://example.test/pr/latest-review",
        )
        self.env["FAKE_GH_REVIEWS"] = json.dumps(
            [
                {
                    "id": 9301,
                    "user": {"login": "reviewer"},
                    "state": "CHANGES_REQUESTED",
                    "body": "Please fix the race.",
                    "submitted_at": "2026-07-18T10:00:00Z",
                },
                {
                    "id": 9302,
                    "user": {"login": "reviewer"},
                    "state": "APPROVED",
                    "body": "The fix is correct.",
                    "submitted_at": "2026-07-18T10:10:00Z",
                },
            ]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        reviews = json.loads(result.stdout)["reviews"]
        self.assertEqual(reviews["approved"], 1)
        self.assertEqual(reviews["changes_requested"], 0)

    def test_status_treats_an_approving_inline_reply_as_resolved(self) -> None:
        self.env["FAKE_GH_PULL"] = self.pull(
            title="Inline approval", head={"ref": "inline-approval", "sha": "headsha"},
            html_url="https://example.test/pr/inline-approval",
        )
        self.env["FAKE_GH_REVIEW_COMMENTS"] = json.dumps(
            [
                {
                    "id": 9401,
                    "user": {"login": "reviewer-bot"},
                    "body": "Please cover the reconnect race.",
                    "created_at": "2026-07-18T10:00:00Z",
                    "path": "provider.py",
                    "line": 12,
                },
                {
                    "id": 9402,
                    "in_reply_to_id": 9401,
                    "user": {"login": "author", "id": 1},
                    "body": (
                        "Added coverage.\n\n"
                        "<!-- pr-shepherd-addresses:9401 -->\n"
                    ),
                    "created_at": "2026-07-18T10:05:00Z",
                },
                {
                    "id": 9403,
                    "in_reply_to_id": 9401,
                    "user": {"login": "reviewer-bot"},
                    "body": "No issues found; ready to merge.",
                    "created_at": "2026-07-18T10:10:00Z",
                },
            ]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["reviews"]["unresolved_threads"], 0)
        self.assertFalse(status["needs_attention"])

    def test_wait_for_checks_defaults_to_the_observed_review_window(self) -> None:
        result = self.run_cli("wait-for-checks", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default: 900", result.stdout)

    def test_reply_makes_its_address_marker_visible_to_the_next_status(self) -> None:
        self.env["FAKE_GH_PULL"] = self.pull(
            title="Fresh reply state", head={"ref": "fresh-reply", "sha": "headsha"},
            html_url="https://example.test/pr/fresh-reply",
        )
        review_comment = {
            "id": 9201,
            "user": {"login": "reviewer-bot"},
            "body": "Please cover the failure path.",
            "created_at": "2026-07-18T10:00:00Z",
        }
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps([review_comment])

        before = self.run_cli("status", str(self.pr_number), "--brief")
        self.assertEqual(json.loads(before.stdout)["issue_comments"]["actionable"], 1)

        reply = self.run_cli(
            "reply", str(self.pr_number), "9201", "--all-handled", "-",
            input_text="Added coverage."
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                review_comment,
                {
                    "id": 9202,
                    "user": {"login": "author", "id": 1},
                    "body": (
                        "Added coverage.\n\n"
                        "<!-- pr-shepherd-addresses:9201 -->\n"
                    ),
                    "created_at": "2026-07-18T10:05:00Z",
                },
            ]
        )

        after = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(json.loads(after.stdout)["issue_comments"]["actionable"], 0)



    def test_status_recognises_an_app_authors_own_replies(self) -> None:
        # gh renders an App author as "app/<slug>"; the REST API, which
        # authors every comment, calls the same account "<slug>[bot]".
        self.env["FAKE_GH_PULL"] = self.pull(
            user={"login": "flotilla-crew[bot]", "id": 309902803, "type": "Bot"}
        )
        crew = {"login": "flotilla-crew[bot]", "id": 309902803}
        reviewer = {"login": "reviewer-bot", "id": 77}
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                {"id": 9501, "user": crew, "body": "Shepherding this PR.",
                 "created_at": "2026-07-18T10:00:00Z"},
                {"id": 9502, "user": reviewer, "body": "Please add a test.",
                 "created_at": "2026-07-18T10:01:00Z"},
            ]
        )
        self.env["FAKE_GH_REVIEW_COMMENTS"] = json.dumps(
            [
                {"id": 9511, "user": reviewer, "body": "Rename this.",
                 "created_at": "2026-07-18T10:00:00Z", "path": "a.py", "line": 3},
                {"id": 9512, "in_reply_to_id": 9511, "user": crew,
                 "body": "Won't rename: it matches the domain term.\n\n"
                         "<!-- pr-shepherd-addresses:9511 -->\n",
                 "created_at": "2026-07-18T10:05:00Z"},
            ]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["issue_comments"]["actionable"], 1)
        self.assertEqual(status["reviews"]["unresolved_threads"], 0)


    def crew_shepherding_a_human_pr(self) -> None:
        self.env["FAKE_GH_PULL"] = self.pull()
        self.env["FAKE_GH_USERS"] = json.dumps(
            {"flotilla-crew[bot]": {"login": "flotilla-crew[bot]", "id": 309902803}}
        )
        self.env["FAKE_GH_USER_FORBIDDEN"] = "1"
        crew = {"login": "flotilla-crew[bot]", "id": 309902803}
        reviewer = {"login": "reviewer-bot", "id": 77}
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                {"id": 9601, "user": reviewer, "body": "Please add a test.",
                 "created_at": "2026-07-18T10:00:00Z"},
                {"id": 9602, "user": crew, "body": "Shepherding this PR.",
                 "created_at": "2026-07-18T10:01:00Z"},
            ]
        )

    def test_status_treats_the_as_identity_as_our_own(self) -> None:
        self.crew_shepherding_a_human_pr()

        result = self.run_cli(
            "--as", "flotilla-crew[bot]", "status", str(self.pr_number), "--brief"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["issue_comments"]["actionable"], 1)


    def test_new_reviews_excludes_the_env_identity_without_asking_who_we_are(self) -> None:
        self.crew_shepherding_a_human_pr()
        self.env["PR_SHEPHERD_AS"] = "flotilla-crew[bot]"

        result = self.run_cli("new-reviews", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        new = json.loads(result.stdout)["new_comments"]
        self.assertEqual([c["author"] for c in new], ["reviewer-bot"])
        self.assertNotIn(["api", "user", "--jq", ".login"], self.gh_calls())


    def test_wait_for_checks_explains_how_to_name_an_app_identity(self) -> None:
        self.crew_shepherding_a_human_pr()

        result = self.run_cli("wait-for-checks", str(self.pr_number), "--check-reviews")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--as", result.stderr)
        self.assertIn("PR_SHEPHERD_AS", result.stderr)


    def test_an_unknown_as_login_fails_instead_of_matching_nobody(self) -> None:
        self.crew_shepherding_a_human_pr()

        result = self.run_cli(
            "--as", "flotila-crew[bot]", "status", str(self.pr_number), "--brief"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--as flotila-crew[bot]", result.stderr)


class PerFindingTest(CliHarness):
    """Review feedback is tracked per finding; only --all-handled clears a
    comment (rjw-skills#9)."""

    REVIEWER: ClassVar[dict] = {"login": "reviewer", "id": 77}
    AUTHOR: ClassVar[dict] = {"login": "author", "id": 1}

    def setUp(self) -> None:
        super().setUp()
        self.env["FAKE_GH_PULL"] = self.pull()

    def three_finding_review(self, *replies: dict) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                {
                    "id": 9701,
                    "user": self.REVIEWER,
                    "body": (
                        "No blocking issues.\n\n"
                        "1. The retry loop never backs off.\n"
                        "2. `parse` swallows the error.\n\n"
                        "Nits:\n- rename `tmp`"
                    ),
                    "created_at": "2026-07-18T10:00:00Z",
                },
                *replies,
            ]
        )

    def own_reply(self, comment_id: int, body: str) -> dict:
        return {
            "id": comment_id,
            "user": self.AUTHOR,
            "body": body,
            "created_at": "2026-07-18T10:05:00Z",
        }

    def test_reply_to_one_finding_records_it_without_clearing_the_comment(self) -> None:
        result = self.run_cli(
            "reply", str(self.pr_number), "9701", "--finding", "nit-1", "-",
            input_text="Renamed `tmp` to `pending`.",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        post = self.gh_calls()[-1]
        body = next(arg for arg in post if arg.startswith("body="))
        self.assertIn("<!-- pr-shepherd-finding:9701:nit-1 -->", body)
        self.assertNotIn("pr-shepherd-addresses", body)

    def test_reply_can_answer_findings_and_settle_the_comment_at_once(self) -> None:
        result = self.run_cli(
            "reply", str(self.pr_number), "9701",
            "--finding", "1", "--finding", "2", "--all-handled", "-",
            input_text="Both fixed.",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        body = next(arg for arg in self.reply_post() if arg.startswith("body="))
        self.assertTrue(body.endswith(
            "<!-- pr-shepherd-finding:9701:1 -->\n"
            "<!-- pr-shepherd-finding:9701:2 -->\n"
            "<!-- pr-shepherd-addresses:9701 -->\n"
        ))

    def test_reply_must_say_what_it_settles(self) -> None:
        result = self.run_cli(
            "reply", str(self.pr_number), "9701", "-", input_text="Fixed."
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--all-handled", result.stderr)
        self.assertFalse(self.gh_log.exists() and any(
            "-X" in call for call in self.gh_calls()
        ))

    def test_reply_rejects_a_label_that_could_break_the_marker(self) -> None:
        result = self.run_cli(
            "reply", str(self.pr_number), "9701", "--finding", "1 -->", "-",
            input_text="Fixed.",
        )

        self.assertEqual(result.returncode, 2)

    def test_a_partly_answered_comment_stays_actionable(self) -> None:
        # The flotilla#2323 failure: one nit fixed, two findings unanswered.
        self.three_finding_review(
            self.own_reply(9702, "Renamed.\n\n<!-- pr-shepherd-finding:9701:nit-1 -->\n")
        )

        result = self.run_cli("status", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["issue_comments"]["actionable"], 1)
        self.assertEqual(status["partially_answered"], {"9701": ["nit-1"]})
        self.assertTrue(status["needs_attention"])
        self.assertIn(
            "finish 1 partly answered comments (9701): answer every remaining "
            "finding, then reply --all-handled",
            status["action_items"],
        )

    def test_only_the_all_handled_marker_clears_the_comment(self) -> None:
        self.three_finding_review(
            self.own_reply(9702, "Renamed.\n\n<!-- pr-shepherd-finding:9701:nit-1 -->\n"),
            self.own_reply(
                9703,
                "1: backoff added. 2: now re-raised. Both tested.\n\n"
                "<!-- pr-shepherd-finding:9701:1 -->\n"
                "<!-- pr-shepherd-finding:9701:2 -->\n"
                "<!-- pr-shepherd-addresses:9701 -->\n",
            ),
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["issue_comments"]["actionable"], 0)
        self.assertEqual(status["partially_answered"], [])
        self.assertFalse(status["needs_attention"])

    def test_reviews_comment_lists_the_findings_already_answered(self) -> None:
        self.three_finding_review(
            self.own_reply(9702, "Renamed.\n\n<!-- pr-shepherd-finding:9701:nit-1 -->\n")
        )

        result = self.run_cli("reviews", str(self.pr_number), "--comment", "9701")

        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)
        self.assertEqual(item["answered_findings"], ["nit-1"])
        self.assertNotIn("fully_addressed", item)

    def test_an_unmarked_own_reply_does_not_settle_a_thread(self) -> None:
        self.env["FAKE_GH_REVIEW_COMMENTS"] = json.dumps(
            [
                {"id": 9801, "user": self.REVIEWER, "body": "Rename this.",
                 "created_at": "2026-07-18T10:00:00Z", "path": "a.py", "line": 3},
                {"id": 9802, "in_reply_to_id": 9801, "user": self.AUTHOR,
                 "body": "Will do.", "created_at": "2026-07-18T10:05:00Z"},
            ]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(json.loads(result.stdout)["reviews"]["unresolved_threads"], 1)

    def test_a_repeated_finding_reopens_an_addressed_thread(self) -> None:
        self.env["FAKE_GH_REVIEW_COMMENTS"] = json.dumps(
            [
                {"id": 9811, "user": self.REVIEWER, "body": "This leaks the fd.",
                 "created_at": "2026-07-18T10:00:00Z", "path": "a.py", "line": 3},
                {"id": 9812, "in_reply_to_id": 9811, "user": self.AUTHOR,
                 "body": "It's closed by the caller.\n\n<!-- pr-shepherd-addresses:9811 -->\n",
                 "created_at": "2026-07-18T10:05:00Z"},
                {"id": 9813, "in_reply_to_id": 9811, "user": self.REVIEWER,
                 "body": "The error path still leaks the fd.",
                 "created_at": "2026-07-18T10:10:00Z"},
            ]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(json.loads(result.stdout)["reviews"]["unresolved_threads"], 1)

    def test_a_resolved_thread_is_skipped(self) -> None:
        self.env["FAKE_GH_REVIEW_COMMENTS"] = json.dumps(
            [
                {"id": 9821, "user": self.REVIEWER, "body": "Rename this.",
                 "created_at": "2026-07-18T10:00:00Z", "path": "a.py", "line": 3},
                {"id": 9822, "user": self.REVIEWER, "body": "Add a test.",
                 "created_at": "2026-07-18T10:00:00Z", "path": "b.py", "line": 9},
            ]
        )
        self.env["FAKE_GH_REVIEW_THREADS"] = json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
                {"isResolved": True, "comments": {"nodes": [{"databaseId": 9821}]}},
                {"isResolved": False, "comments": {"nodes": [{"databaseId": 9822}]}},
            ]}}}}}
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(json.loads(result.stdout)["reviews"]["unresolved_threads"], 1)


class ActionableRuleTest(CliHarness):
    """Whose feedback counts, aligned with flotilla's crew wake-up rule."""

    def setUp(self) -> None:
        super().setUp()
        self.env["FAKE_GH_PULL"] = self.pull()

    def review(self, review_id: int, login: str, state: str, body: str, at: str,
               user_type: str = "User") -> dict:
        return {
            "id": review_id,
            "user": {"login": login, "id": review_id, "type": user_type},
            "state": state,
            "body": body,
            "submitted_at": f"2026-07-18T{at}Z",
        }

    def test_only_each_reviewers_latest_substantive_review_counts(self) -> None:
        self.env["FAKE_GH_REVIEWS"] = json.dumps(
            [
                # Superseded by the same reviewer's later review.
                self.review(1, "alice", "CHANGES_REQUESTED", "Fix the race.", "10:00:00"),
                self.review(2, "alice", "COMMENTED", "Still missing a test.", "10:05:00"),
                # An empty COMMENTED review (a thread reply) doesn't supersede.
                self.review(3, "alice", "COMMENTED", "", "10:10:00"),
                # Empty approvals are ignored outright.
                self.review(4, "bob", "APPROVED", "", "10:00:00"),
            ]
        )

        result = self.run_cli("status", str(self.pr_number))

        status = json.loads(result.stdout)
        self.assertEqual(status["reviews"]["actionable_review_ids"], [2])
        self.assertIn("address 1 review bodies from reviewers", status["action_items"])

    def test_an_addressed_review_body_is_cleared(self) -> None:
        self.env["FAKE_GH_REVIEWS"] = json.dumps(
            [self.review(2, "alice", "COMMENTED", "Still missing a test.", "10:05:00")]
        )
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [{"id": 9901, "user": {"login": "author", "id": 1},
              "body": "Added.\n\n<!-- pr-shepherd-addresses:2 -->\n",
              "created_at": "2026-07-18T10:06:00Z"}]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(json.loads(result.stdout)["reviews"]["actionable_bodies"], 0)

    def test_review_bots_count_and_other_bots_are_ignored(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                {"id": 9911, "user": {"login": "claude[bot]", "id": 5, "type": "Bot"},
                 "body": "1. Missing test.", "created_at": "2026-07-18T10:00:00Z"},
                {"id": 9912, "user": {"login": "codecov[bot]", "id": 6, "type": "Bot"},
                 "body": "Coverage dropped 0.1%.", "created_at": "2026-07-18T10:00:00Z"},
                {"id": 9913, "user": {"login": "house-reviewer[bot]", "id": 7, "type": "Bot"},
                 "body": "1. Wrong lock order.", "created_at": "2026-07-18T10:00:00Z"},
            ]
        )

        default = json.loads(self.run_cli("status", str(self.pr_number)).stdout)
        configured = json.loads(self.run_cli(
            "--review-bot", "house-reviewer[bot]", "status", str(self.pr_number)
        ).stdout)

        self.assertEqual(default["issue_comments"]["actionable"], 1)
        self.assertEqual(
            default["ignored_bot_authors"], ["codecov[bot]", "house-reviewer[bot]"]
        )
        self.assertEqual(configured["issue_comments"]["actionable"], 2)
        self.assertEqual(configured["ignored_bot_authors"], ["codecov[bot]"])


class NoWaitTest(CliHarness):
    """Under flotilla (FLOTILLA_CREW_ID set) the helper does one pass and
    yields instead of polling."""

    def setUp(self) -> None:
        super().setUp()
        self.env["FAKE_GH_PULL"] = self.pull()
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps(
            [self.check_run("test", "success", 1), self.check_run("review", None, 2)]
        )

    def assert_one_snapshot(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertTrue(snapshot["no_wait"])
        self.assertFalse(snapshot["done"])
        self.assertEqual(snapshot["pending_names"], ["review"])
        self.assertEqual(snapshot["passed"], 1)
        check_run_reads = [
            c for c in self.gh_calls() if c[:1] == ["api"] and "/check-runs?" in c[1]
        ]
        self.assertEqual(len(check_run_reads), 1)
        self.assertEqual(self.graphql_calls(), [])

    def test_wait_for_checks_returns_one_snapshot_under_flotilla(self) -> None:
        self.env["FLOTILLA_CREW_ID"] = "crew-123"

        result = self.run_cli(
            "wait-for-checks", str(self.pr_number), "--check-reviews"
        )

        self.assert_one_snapshot(result)

    def test_no_wait_flag_forces_one_snapshot_standalone(self) -> None:
        result = self.run_cli("--no-wait", "wait-for-checks", str(self.pr_number))

        self.assert_one_snapshot(result)

    def test_status_reports_the_mode(self) -> None:
        standalone = json.loads(
            self.run_cli("status", str(self.pr_number), "--brief").stdout
        )
        self.env["FLOTILLA_CREW_ID"] = "crew-123"
        crew = json.loads(self.run_cli("status", str(self.pr_number), "--brief").stdout)

        self.assertEqual(standalone["mode"], "wait")
        self.assertEqual(crew["mode"], "no-wait")


class AckClearTest(CliHarness):
    """`ack` makes offline review work visible on GitHub (an eyes reaction per
    item plus the `shepherd: addressing` label); `clear` drops the label once
    the iteration converges (rjw-skills#11)."""

    def setUp(self) -> None:
        super().setUp()
        self.env["FAKE_GH_PULL"] = self.pull()

    def post_endpoints(self) -> list[str]:
        return [c[1] for c in self.gh_calls() if c[:1] == ["api"] and "POST" in c]

    def reaction_posts(self) -> list[str]:
        return [e for e in self.post_endpoints() if e.endswith("/reactions")]

    def test_ack_labels_the_pr_creating_the_label_and_reacts_to_comments(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [{"id": 100, "user": {"login": "reviewer"}, "body": "1. fix",
              "created_at": "2026-07-18T10:00:00Z"}]
        )
        self.env["FAKE_GH_REVIEW_COMMENTS"] = json.dumps(
            [{"id": 200, "user": {"login": "reviewer"}, "body": "rename this",
              "created_at": "2026-07-18T10:00:00Z", "path": "a.py", "line": 3}]
        )

        result = self.run_cli(
            "ack", str(self.pr_number), "--comment-ids", "100", "200"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["reacted"], [100, 200])
        self.assertTrue(out["label_set"])
        posts = self.post_endpoints()
        # The repo lacked the label, so it is created, then added to the PR.
        self.assertIn("repos/owner/repo/labels", posts)
        self.assertIn(f"repos/owner/repo/issues/{self.pr_number}/labels", posts)
        # Issue comments and inline review comments each react on their own endpoint.
        self.assertEqual(
            sorted(self.reaction_posts()),
            sorted([
                "repos/owner/repo/issues/comments/100/reactions",
                "repos/owner/repo/pulls/comments/200/reactions",
            ]),
        )

    def test_ack_labels_a_review_body_but_cannot_react_to_it(self) -> None:
        # GitHub's Reactions API has no endpoint for a submitted review's body,
        # so ack labels the PR and reports the id as unreactable rather than
        # firing a reaction that would 404.
        self.env["FAKE_GH_REVIEWS"] = json.dumps(
            [{"id": 300, "user": {"login": "reviewer"}, "state": "COMMENTED",
              "body": "overall note", "submitted_at": "2026-07-18T10:00:00Z"}]
        )

        result = self.run_cli("ack", str(self.pr_number), "--comment-ids", "300")

        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["unreactable"], [300])
        self.assertEqual(out["reacted"], [])
        self.assertEqual(self.reaction_posts(), [])
        self.assertIn(f"repos/owner/repo/issues/{self.pr_number}/labels", self.post_endpoints())

    def test_ack_does_not_recreate_a_label_the_repo_already_has(self) -> None:
        self.env["FAKE_GH_REPO_HAS_LABEL"] = "1"

        result = self.run_cli("ack", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        posts = self.post_endpoints()
        self.assertNotIn("repos/owner/repo/labels", posts)
        # It is still applied to the PR.
        self.assertIn(f"repos/owner/repo/issues/{self.pr_number}/labels", posts)

    def test_ack_skips_an_item_this_account_already_reacted_to(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [{"id": 100, "user": {"login": "reviewer"}, "body": "fix",
              "created_at": "2026-07-18T10:00:00Z"}]
        )
        self.env["FAKE_GH_REACTIONS"] = json.dumps(
            [{"content": "eyes", "user": {"login": "author"}}]
        )

        result = self.run_cli("ack", str(self.pr_number), "--comment-ids", "100")

        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["already_reacted"], [100])
        self.assertEqual(out["reacted"], [])
        self.assertEqual(self.reaction_posts(), [])

    def test_ack_reacts_when_only_another_account_has_reacted(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [{"id": 100, "user": {"login": "reviewer"}, "body": "fix",
              "created_at": "2026-07-18T10:00:00Z"}]
        )
        self.env["FAKE_GH_REACTIONS"] = json.dumps(
            [{"content": "eyes", "user": {"login": "someone-else"}},
             {"content": "heart", "user": {"login": "author"}}]
        )

        result = self.run_cli("ack", str(self.pr_number), "--comment-ids", "100")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reacted"], [100])
        self.assertEqual(
            self.reaction_posts(), ["repos/owner/repo/issues/comments/100/reactions"]
        )

    def test_clear_removes_the_label(self) -> None:
        self.env["FAKE_GH_APPLIED_LABELS"] = json.dumps([ADDRESSING_LABEL])

        result = self.run_cli("clear", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["removed"])
        deletes = [c for c in self.gh_calls() if "DELETE" in c]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(
            deletes[0][1],
            f"repos/owner/repo/issues/{self.pr_number}/labels/{quote(ADDRESSING_LABEL)}",
        )

    def test_clear_is_a_no_op_when_the_label_is_absent(self) -> None:
        # FAKE_GH_APPLIED_LABELS unset: the DELETE 404s and is tolerated.
        result = self.run_cli("clear", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["removed"])

    def test_status_reports_whether_the_addressing_label_is_set(self) -> None:
        # An open finding keeps the label legitimately set, so status reports
        # it rather than self-healing it away (that case is covered below).
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [{"id": 100, "user": {"login": "reviewer"}, "body": "1. fix this",
              "created_at": "2026-07-18T10:00:00Z"}]
        )
        # Distinct PR numbers so the per-PR metadata cache doesn't carry the
        # first probe's labels into the second.
        self.env["FAKE_GH_PULL"] = self.pull(labels=[{"name": ADDRESSING_LABEL}])
        with_label = json.loads(self.run_cli("status", str(self.pr_number)).stdout)
        self.assertTrue(with_label["addressing_label"])
        self.assertNotIn("addressing_label_cleared", with_label)

        self.env["FAKE_GH_PULL"] = self.pull()
        without_label = json.loads(
            self.run_cli("status", str(self.pr_number + 1)).stdout
        )
        self.assertFalse(without_label["addressing_label"])


class AutoClearConvergenceTest(CliHarness):
    """The addressing label clears itself once review findings converge, so a
    no-wait crew that answers every finding and completes doesn't strand it set
    (rjw-skills#12). Convergence is review feedback only — not CI or conflicts.
    """

    LABELLED: ClassVar[list] = [{"name": ADDRESSING_LABEL}]

    def setUp(self) -> None:
        super().setUp()
        # The PR carries the label, and the repo has it applied so a DELETE
        # succeeds rather than 404ing.
        self.env["FAKE_GH_PULL"] = self.pull(labels=self.LABELLED)
        self.env["FAKE_GH_APPLIED_LABELS"] = json.dumps([ADDRESSING_LABEL])

    def label_deletes(self) -> list[list[str]]:
        return [
            c for c in self.gh_calls()
            if "DELETE" in c and "/labels/" in c[1] and "/issues/" in c[1]
        ]

    def open_issue_comment(self, comment_id: int, body: str = "1. fix this") -> dict:
        return {
            "id": comment_id,
            "user": {"login": "reviewer"},
            "body": body,
            "created_at": "2026-07-18T10:00:00Z",
        }

    def test_reply_all_handled_clears_the_label_on_the_last_open_comment(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps([self.open_issue_comment(100)])

        result = self.run_cli(
            "reply", str(self.pr_number), "100", "--all-handled", "-",
            input_text="Fixed.",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["addressing_label"], "cleared")
        self.assertEqual(len(self.label_deletes()), 1)

    def test_reply_all_handled_leaves_the_label_when_a_comment_is_still_open(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [self.open_issue_comment(100), self.open_issue_comment(101, "2. and this")]
        )

        result = self.run_cli(
            "reply", str(self.pr_number), "100", "--all-handled", "-",
            input_text="Fixed one.",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("addressing_label", json.loads(result.stdout))
        self.assertEqual(self.label_deletes(), [])

    def test_reply_finding_only_never_clears_the_label(self) -> None:
        # --finding records progress but leaves the comment open, so there is
        # no convergence to act on and no metadata is even fetched to check.
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps([self.open_issue_comment(100)])

        result = self.run_cli(
            "reply", str(self.pr_number), "100", "--finding", "1", "-",
            input_text="Working on it.",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("addressing_label", json.loads(result.stdout))
        self.assertEqual(self.label_deletes(), [])

    def test_status_clears_the_label_when_nothing_is_outstanding(self) -> None:
        result = self.run_cli("status", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertFalse(status["addressing_label"])
        self.assertTrue(status["addressing_label_cleared"])
        self.assertEqual(len(self.label_deletes()), 1)

    def test_status_brief_also_clears_and_reports_it(self) -> None:
        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["addressing_label_cleared"])
        self.assertEqual(len(self.label_deletes()), 1)

    def test_status_leaves_the_label_when_findings_remain(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps([self.open_issue_comment(100)])

        result = self.run_cli("status", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertTrue(status["addressing_label"])
        self.assertNotIn("addressing_label_cleared", status)
        self.assertEqual(self.label_deletes(), [])

    def test_failing_checks_do_not_keep_the_label_set(self) -> None:
        # The label's claim is "unanswered review points remain", so review
        # convergence clears it even while checks still need fixing.
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps([self.check_run("test", "failure")])

        result = self.run_cli("status", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertTrue(status["addressing_label_cleared"])
        self.assertTrue(status["needs_attention"])
        self.assertIn("fix failing checks: test", status["action_items"])

    def test_nothing_breaks_when_the_label_is_absent(self) -> None:
        # No label on the PR: converged status and reply both no-op on the
        # label without erroring or issuing a DELETE.
        self.env["FAKE_GH_PULL"] = self.pull()
        self.env["FAKE_GH_APPLIED_LABELS"] = json.dumps([])
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps([self.open_issue_comment(100)])

        status = self.run_cli("status", str(self.pr_number))
        reply = self.run_cli(
            "reply", str(self.pr_number), "100", "--all-handled", "-",
            input_text="Fixed.",
        )

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertFalse(json.loads(status.stdout)["addressing_label"])
        self.assertNotIn("addressing_label_cleared", json.loads(status.stdout))
        self.assertNotIn("addressing_label", json.loads(reply.stdout))
        self.assertEqual(self.label_deletes(), [])


def load_helper() -> ModuleType:
    """Import a fresh copy of the helper script (its filename has a hyphen)."""
    spec = importlib.util.spec_from_file_location("pr_shepherd_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RestParityTest(unittest.TestCase):
    """REST answers must reproduce what the GraphQL-backed `gh pr checks` and
    `gh pr view` reported. Each fixture directory holds trimmed REST
    responses and gh's output for the same PR, recorded read-only at the
    same moment: flotilla PRs (Actions jobs, a cancelled job, a job re-run
    by review events, an App author, a merged PR) and a nodejs PR (legacy
    commit statuses, in-progress runs, duplicate workflow runs)."""

    FIXTURES = sorted(p for p in FIXTURE_DIR.iterdir() if p.is_dir())

    def setUp(self) -> None:
        self.helper = load_helper()

    @staticmethod
    def read(directory: Path, name: str) -> dict | list:
        return json.loads((directory / f"{name}.json").read_text())

    def test_check_rows_match_gh_pr_checks(self) -> None:
        for directory in self.FIXTURES:
            with self.subTest(fixture=directory.name):
                expected = self.read(directory, "gh_pr_checks")
                actual = self.helper._checks_from_rest(
                    self.read(directory, "check_runs")["check_runs"],
                    self.read(directory, "status")["statuses"],
                    self.read(directory, "workflow_runs")["workflow_runs"],
                )

                def canonical(rows: list[dict]) -> list[str]:
                    return sorted(json.dumps(row, sort_keys=True) for row in rows)

                self.assertEqual(canonical(actual), canonical(expected))
                # gh orders newest start first; among equal start times its
                # order is unspecified, so only the start sequence is compared.
                self.assertEqual(
                    [row["startedAt"] for row in actual],
                    [row["startedAt"] for row in expected],
                )

    def test_fixtures_cover_the_cases_that_need_care(self) -> None:
        def states(name: str) -> set[str]:
            rows = self.read(FIXTURE_DIR / name, "gh_pr_checks")
            return {row["state"] for row in rows}

        self.assertIn("CANCELLED", states("flotilla-org_flotilla_2891"))
        node_statuses = self.read(FIXTURE_DIR / "nodejs_node_66601", "status")["statuses"]
        self.assertTrue(node_statuses)
        for name in ("flotilla-org_flotilla_2643", "nodejs_node_66601"):
            runs = self.read(FIXTURE_DIR / name, "check_runs")["check_runs"]
            names = [run["name"] for run in runs]
            self.assertLess(len(set(names)), len(names), name)

    def test_metadata_matches_gh_pr_view(self) -> None:
        for directory in self.FIXTURES:
            with self.subTest(fixture=directory.name):
                expected = self.read(directory, "gh_pr_view")
                actual = self.helper._meta_from_pull(self.read(directory, "pull"))
                for field in (
                    "number", "title", "body", "state", "isDraft", "baseRefName",
                    "headRefName", "mergeable", "url", "createdAt", "updatedAt",
                    "additions", "deletions", "changedFiles",
                ):
                    self.assertEqual(actual[field], expected[field], field)
                self.assertEqual(actual["author"]["login"], expected["author"]["login"])
                self.assertEqual(
                    [label["name"] for label in actual["labels"]],
                    [label["name"] for label in expected["labels"]],
                )


class GraphQLBudgetTest(CliHarness):
    """Reads go through REST; GraphQL is reserved for thread resolution."""

    def setUp(self) -> None:
        super().setUp()
        self.env["FAKE_GH_PULL"] = self.pull()
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps(
            [self.check_run("test", "failure", 1), self.check_run("lint", "success", 2)]
        )

    def test_status_without_open_threads_spends_no_graphql(self) -> None:
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [{"id": 100, "user": {"login": "reviewer"}, "body": "1. fix",
              "created_at": "2026-07-18T10:00:00Z"}]
        )

        result = self.run_cli("status", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["checks"]["failed"], [{
            "name": "test",
            "link": "https://github.com/owner/repo/actions/runs/901/job/1",
            "run_id": "901",
        }])
        self.assertEqual(self.graphql_calls(), [])

    def test_status_with_an_open_thread_asks_graphql_once(self) -> None:
        self.env["FAKE_GH_REVIEW_COMMENTS"] = json.dumps(
            [{"id": 200, "user": {"login": "reviewer"}, "body": "rename this",
              "created_at": "2026-07-18T10:00:00Z", "path": "a.py", "line": 3}]
        )

        result = self.run_cli("status", str(self.pr_number), "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reviews"]["unresolved_threads"], 1)
        self.assertEqual([c[:2] for c in self.graphql_calls()], [["api", "graphql"]])

    def test_checks_merge_legacy_statuses_with_check_runs(self) -> None:
        self.env["FAKE_GH_STATUSES"] = json.dumps(
            [{"context": "ci/jenkins", "state": "failure", "description": "2 tests failed",
              "target_url": "https://ci.example.test/job/7"}]
        )

        result = self.run_cli("checks", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["summary"], {"total": 3, "pass": 1, "fail": 2, "pending": 0, "skipped": 0})
        jenkins = next(c for c in out["checks"] if c["name"] == "ci/jenkins")
        self.assertEqual(jenkins["description"], "2 tests failed")
        self.assertEqual(jenkins["link"], "https://ci.example.test/job/7")
        self.assertEqual(self.graphql_calls(), [])

    def test_check_runs_are_read_from_every_page(self) -> None:
        runs = [self.check_run(f"job-{i}", "success", i) for i in range(1, 151)]
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps(runs)

        result = self.run_cli("checks", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["summary"]["pass"], 150)
        pages = [c[1] for c in self.gh_calls() if "/check-runs?" in c[1]]
        self.assertEqual(pages, ["repos/owner/repo/commits/headsha/check-runs?per_page=100&page=1",
                                 "repos/owner/repo/commits/headsha/check-runs?per_page=100&page=2"])

    def test_repo_comes_from_the_git_remote_without_gh_repo_view(self) -> None:
        self.git("remote", "set-url", "origin", "https://github.com/someone/elsewhere.git")
        self.git("remote", "add", "upstream", "git@github.com:owner/repo.git")

        result = self.run_cli("checks", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"repos/owner/repo/pulls/{self.pr_number}",
                      [c[1] for c in self.gh_calls() if c[:1] == ["api"]])
        self.assertNotIn(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                         self.gh_calls())

    def test_repo_falls_back_to_gh_when_no_remote_is_on_github(self) -> None:
        self.git("remote", "set-url", "origin", "https://forgejo.example.test/owner/repo.git")

        result = self.run_cli("checks", str(self.pr_number))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                      self.gh_calls())


class WaitPacingTest(CliHarness):
    """wait-for-checks polls REST every 60s, backs off x1.5 to 180s while
    nothing changes, and resets when something does. Runs in-process on a
    fake clock."""

    def setUp(self) -> None:
        super().setUp()
        self.env["FAKE_GH_PULL"] = self.pull()
        self.helper = load_helper()
        self.helper.CACHE_DIR = self.root / "cache"  # no learned CI duration
        self.helper._repo_cache = "owner/repo"
        self.now = 1_000_000.0
        self.sleeps: list[float] = []
        # After the Nth sleep, these check runs are what GitHub reports.
        self.timeline: dict[int, list[dict]] = {}

    def fake_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if len(self.sleeps) in self.timeline:
            os.environ["FAKE_GH_CHECK_RUNS"] = json.dumps(self.timeline[len(self.sleeps)])

    def wait(self, interval: int = 60, timeout: int = 900) -> dict:
        args = argparse.Namespace(
            pr_number=self.pr_number, timeout=timeout, interval=interval,
            check_reviews=False, exclude_authors=None, no_wait=False,
        )
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.helper.time, "sleep", self.fake_sleep), \
                mock.patch.object(self.helper.time, "time", lambda: self.now), \
                mock.patch.object(self.helper, "_load_ci_ema", lambda: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.helper.cmd_wait_for_checks(args)
        return json.loads(out.getvalue())

    def test_backs_off_while_unchanged_and_resets_on_change(self) -> None:
        build, lint = 1, 2
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps(
            [self.check_run("build", None, build), self.check_run("lint", None, lint)]
        )
        self.timeline = {
            4: [self.check_run("build", "success", build), self.check_run("lint", None, lint)],
            5: [self.check_run("build", "success", build), self.check_run("lint", "failure", lint)],
        }

        result = self.wait()

        self.assertEqual(self.sleeps, [60, 90, 135, 180, 60])
        self.assertTrue(result["done"])
        self.assertEqual(result["failed_checks"][0]["run_id"], "902")
        self.assertEqual(self.graphql_calls(), [])

    def test_interval_can_raise_the_base_but_not_lower_it(self) -> None:
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps([self.check_run("build", None)])
        self.timeline = {3: [self.check_run("build", "success")]}

        self.wait(interval=10)
        lowered = self.sleeps
        self.sleeps = []
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps([self.check_run("build", None)])
        self.wait(interval=200)

        self.assertEqual(lowered, [60, 90, 135])
        self.assertEqual(self.sleeps, [200, 200, 200])

    def test_times_out_after_the_requested_window(self) -> None:
        self.env["FAKE_GH_CHECK_RUNS"] = json.dumps([self.check_run("build", None)])

        with self.assertRaises(SystemExit):
            self.wait(timeout=300)

        self.assertEqual(sum(self.sleeps), 300)


if __name__ == "__main__":
    unittest.main()
