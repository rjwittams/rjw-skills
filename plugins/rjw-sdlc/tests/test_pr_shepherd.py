from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr-shepherd.py"


class PrShepherdCliTest(unittest.TestCase):
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

                if args[:2] == ["repo", "view"]:
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
                elif args and args[0] == "api":
                    endpoint = args[1]
                    if "/pulls/" in endpoint and endpoint.endswith("/reviews?per_page=100"):
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
            "reply", str(self.pr_number), "9001", "-", input_text=body
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
                },
                "issue_comments": {"actionable": 0},
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
                    "user": {"login": "author"},
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
                    "user": {"login": "author"},
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
            "reply", str(self.pr_number), "9201", "-", input_text="Added coverage."
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.env["FAKE_GH_ISSUE_COMMENTS"] = json.dumps(
            [
                review_comment,
                {
                    "id": 9202,
                    "user": {"login": "author"},
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


if __name__ == "__main__":
    unittest.main()
