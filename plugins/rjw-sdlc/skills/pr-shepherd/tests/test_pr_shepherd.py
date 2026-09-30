from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import ClassVar
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr-shepherd.py"


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

                args = sys.argv[1:]
                with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
                    log.write(json.dumps(args) + "\\n")

                if args[:2] == ["api", "graphql"]:
                    print(os.environ.get(
                        "FAKE_GH_REVIEW_THREADS",
                        json.dumps({"data": {"repository": {"pullRequest": {
                            "reviewThreads": {"nodes": []}}}}}),
                    ))
                elif args[:2] == ["repo", "view"]:
                    print("owner/repo")
                elif args[:2] == ["pr", "view"]:
                    if args[2:] == ["--json", "number", "--jq", ".number"]:
                        print(os.environ.get("FAKE_GH_DETECTED_PR", ""))
                    else:
                        print(os.environ.get("FAKE_GH_PR_METADATA", "{}"))
                elif args[:2] == ["pr", "checks"]:
                    print(os.environ.get("FAKE_GH_CHECKS", "[]"))
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
                    if endpoint.rstrip("0123456789").endswith("/pulls/"):
                        value = os.environ.get(
                            "FAKE_GH_PULL", json.dumps({"user": {"login": "author", "id": 1}})
                        )
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

    def run_cli(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def gh_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def pr_metadata(self, author_login: str) -> str:
        return json.dumps(
            {
                "number": self.pr_number,
                "title": "Shepherded PR",
                "body": "",
                "author": {"login": author_login},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "shepherded",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/shepherded",
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            }
        )


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

    def test_reply_records_the_exact_comment_id_it_addresses(self) -> None:
        body = "Fixed the reported race.\n"

        result = self.run_cli(
            "reply", str(self.pr_number), "9001", "--all-handled", "-", input_text=body
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        post = self.gh_calls()[-1]
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
        self.env["FAKE_GH_PR_METADATA"] = json.dumps(
            {
                "number": self.pr_number,
                "title": "Make replies safe",
                "body": "",
                "author": {"login": "author"},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "safe-replies",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/42",
                "additions": 12,
                "deletions": 3,
                "changedFiles": 2,
            }
        )
        self.env["FAKE_GH_CHECKS"] = json.dumps(
            [
                {"name": "test", "bucket": "pass", "link": ""},
                {"name": "review", "bucket": "pending", "link": ""},
            ]
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
        self.env["FAKE_GH_DETECTED_PR"] = str(self.pr_number)
        self.env["FAKE_GH_PR_METADATA"] = json.dumps(
            {
                "number": self.pr_number,
                "title": "Detected PR",
                "body": "",
                "author": {"login": "author"},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "detected",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/detected",
                "additions": 0,
                "deletions": 0,
                "changedFiles": 0,
            }
        )

        result = self.run_cli("status", "--brief")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pr"]["number"], self.pr_number)

    def test_status_does_not_reflag_an_issue_comment_with_an_address_marker(self) -> None:
        self.env["FAKE_GH_PR_METADATA"] = json.dumps(
            {
                "number": self.pr_number,
                "title": "Addressed review",
                "body": "",
                "author": {"login": "author"},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "addressed",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/addressed",
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            }
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
        self.env["FAKE_GH_PR_METADATA"] = json.dumps(
            {
                "number": self.pr_number,
                "title": "Bot approval",
                "body": "",
                "author": {"login": "author"},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "approved",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/approved",
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            }
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
        self.env["FAKE_GH_PR_METADATA"] = json.dumps(
            {
                "number": self.pr_number,
                "title": "Latest review state",
                "body": "",
                "author": {"login": "author"},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "latest-review",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/latest-review",
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            }
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
        self.env["FAKE_GH_PR_METADATA"] = json.dumps(
            {
                "number": self.pr_number,
                "title": "Inline approval",
                "body": "",
                "author": {"login": "author"},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "inline-approval",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/inline-approval",
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            }
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
        self.env["FAKE_GH_PR_METADATA"] = json.dumps(
            {
                "number": self.pr_number,
                "title": "Fresh reply state",
                "body": "",
                "author": {"login": "author"},
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "fresh-reply",
                "mergeable": "MERGEABLE",
                "url": "https://example.test/pr/fresh-reply",
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            }
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
        self.env["FAKE_GH_PR_METADATA"] = self.pr_metadata("app/flotilla-crew")
        self.env["FAKE_GH_PULL"] = json.dumps(
            {"user": {"login": "flotilla-crew[bot]", "id": 309902803}}
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
        self.env["FAKE_GH_PR_METADATA"] = self.pr_metadata("author")
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
        self.env["FAKE_GH_PR_METADATA"] = self.pr_metadata("author")

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
        body = next(arg for arg in self.gh_calls()[-1] if arg.startswith("body="))
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
        self.env["FAKE_GH_PR_METADATA"] = self.pr_metadata("author")

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
        self.env["FAKE_GH_PR_METADATA"] = self.pr_metadata("author")
        self.env["FAKE_GH_CHECKS"] = json.dumps(
            [
                {"name": "test", "bucket": "pass", "link": ""},
                {"name": "review", "bucket": "pending", "link": ""},
            ]
        )

    def assert_one_snapshot(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertTrue(snapshot["no_wait"])
        self.assertFalse(snapshot["done"])
        self.assertEqual(snapshot["pending_names"], ["review"])
        self.assertEqual(snapshot["passed"], 1)
        checks_calls = [c for c in self.gh_calls() if c[:2] == ["pr", "checks"]]
        self.assertEqual(len(checks_calls), 1)

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


if __name__ == "__main__":
    unittest.main()
