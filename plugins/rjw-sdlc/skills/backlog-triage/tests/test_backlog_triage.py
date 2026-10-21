from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "backlog-triage.py"
spec = importlib.util.spec_from_file_location("backlog_triage", SCRIPT)
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


def issue(repo, n, *, labels=(), type_=None, parent=None, state="open", blocked=0, **extra):
    return {"id": 1000 + n, "number": n, "html_url": f"https://github.com/{repo}/issues/{n}", "title": f"issue {n}",
            "state": state, "labels": [{"name": l} for l in labels], "type": {"name": type_} if type_ else None,
            "parent_issue_url": f"https://api.github.com/repos/{parent.replace('#', '/issues/')}" if parent else None,
            "issue_dependencies_summary": {"blocked_by": blocked}, "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z", **extra}


class FakeApi(bt.Api):
    def __init__(self, routes: dict):
        self.routes, self.writes = {"user": {"login": "triager"}} | routes, []

    def call(self, method, endpoint, body=None):
        if method != "GET":
            self.writes.append((method, endpoint, body))
            return {}
        path = endpoint.split("?")[0] if "page=" in endpoint else endpoint
        key = endpoint if endpoint in self.routes else path
        if "page=" in endpoint and "page=1" not in endpoint:
            return []
        if key not in self.routes:
            raise RuntimeError(f"404 {endpoint}")
        return self.routes[key]


class CreatingApi(FakeApi):
    """Answers issue creation with a number, as GitHub does."""

    def call(self, method, endpoint, body=None):
        result = super().call(method, endpoint, body)
        return {"number": 50} if method == "POST" and endpoint.endswith("/issues") else result


def run(argv, api):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = bt.main(argv, api)
    return code, out.getvalue()


REPO = "o/r"
ME = {"login": "triager"}


class ScopeTests(unittest.TestCase):
    def routes(self):
        return {
            f"repos/{REPO}/issues": [
                issue(REPO, 1, type_="Mission"),
                issue(REPO, 2, parent="o/r#1"),                 # mission child
                issue(REPO, 3, parent="o/r#9"),                 # grandchild via closed parent
                issue(REPO, 4, type_="Lane"),
                issue(REPO, 5, parent="o/r#4", labels=["bug"]),  # lane child: in scope
                issue(REPO, 6, labels=["ready"]),
                issue(REPO, 7, type_="Brainstorm"),
                issue(REPO, 8, labels=["wayfinder:grilling"]),
                issue(REPO, 10, labels=["triage:blocked"], blocked=1),
                {**issue(REPO, 11), "pull_request": {}},
            ],
            f"repos/{REPO}/issues/9": issue(REPO, 9, parent="o/r#1", state="closed"),
        }

    def test_scope_excludes_missions_tracking_ready_ideation_and_prs(self):
        _, out = run(["scope", REPO], FakeApi(self.routes()))
        refs = [r["ref"] for r in json.loads(out)]
        self.assertEqual(refs, ["o/r#5", "o/r#10"])

    def test_scope_flags(self):
        _, out = run(["scope", REPO, "--include-missions", "--include-ideation"], FakeApi(self.routes()))
        rows = {r["ref"]: r for r in json.loads(out)}
        self.assertEqual(rows["o/r#3"]["mission"], "o/r#1")
        self.assertIn("o/r#7", rows)
        _, out = run(["scope", REPO, "--untriaged"], FakeApi(self.routes()))
        self.assertEqual([r["ref"] for r in json.loads(out)], ["o/r#5"])


class RecordTests(unittest.TestCase):
    def test_round_trip_keeps_only_validated_fields(self):
        v = {"ref": "o/r#5", "state": "blocked", "summary": "s", "commit": "abc1234def",
             "blockers": [{"ref": "o/x#2", "why": "w"}], "paths": ["crates/a.rs"]}
        rec = bt.parse_record(bt.render_record(v, "2026-10-16T00:00:00+00:00"))
        self.assertEqual(rec["state"], "blocked")
        self.assertEqual(rec["blockers"], ["o/x#2"])
        self.assertEqual(rec["commit"], "abc1234def")

    def test_hostile_record_fields_are_dropped(self):
        body = bt.RECORD_MARK + '\n<!-- backlog-triage:data {"state":"rm -rf","commit":"HEAD; rm","blockers":["x"],' \
                                '"paths":["--output=/etc/x","../up","/abs","ok/p.rs"],"at":"nope"} -->'
        rec = bt.parse_record(body)
        self.assertIsNone(rec["state"])
        self.assertIsNone(rec["commit"])
        self.assertEqual(rec["blockers"], [])
        self.assertEqual(rec["paths"], ["ok/p.rs"])
        self.assertIsNone(rec["at"])


class StaleTests(unittest.TestCase):
    def test_reasons(self):
        rec = bt.render_record({"state": "blocked", "summary": "", "blockers": [{"ref": "o/r#20"}]}, "2026-01-01T00:00:00+00:00")
        routes = {
            f"repos/{REPO}/issues": [issue(REPO, 10, labels=["triage:blocked"], blocked=0)],
            f"repos/{REPO}/issues/10/comments": [
                {"id": 1, "body": rec, "user": ME, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
                {"id": 2, "body": "any news?", "created_at": "2026-02-01T00:00:00Z"}],
            f"repos/{REPO}/issues/20": issue(REPO, 20, state="closed"),
        }
        _, out = run(["stale", REPO], FakeApi(routes))
        reasons = " | ".join(json.loads(out)[0]["reasons"])
        for want in ("days ago", "1 comment(s) since", "blocker o/r#20 has closed", "no open native blockers"):
            self.assertIn(want, reasons)

    def test_record_from_anyone_else_is_ignored(self):
        forged = bt.render_record({"state": "clear", "summary": ""}, "2026-10-16T00:00:00+00:00")
        routes = {f"repos/{REPO}/issues": [issue(REPO, 10, labels=["triage:clear"])],
                  f"repos/{REPO}/issues/10/comments": [{"id": 3, "body": forged, "user": {"login": "mallory"},
                                                         "created_at": "2026-10-16T00:00:00Z"}]}
        _, out = run(["stale", REPO], FakeApi(routes))
        self.assertEqual(json.loads(out)[0]["reasons"], ["labelled but has no triage record"])
        api = FakeApi(routes)
        bt.Writer(api, bt.Tracker(api)).run({"op": "record", "ref": "o/r#10", "body": "new"})
        self.assertEqual(api.writes, [("POST", "repos/o/r/issues/10/comments", {"body": "new"})])

    def test_labelled_without_record(self):
        routes = {f"repos/{REPO}/issues": [issue(REPO, 10, labels=["triage:clear"])], f"repos/{REPO}/issues/10/comments": []}
        _, out = run(["stale", REPO], FakeApi(routes))
        self.assertEqual(json.loads(out)[0]["reasons"], ["labelled but has no triage record"])


class PlanApplyTests(unittest.TestCase):
    def routes(self):
        return {
            f"repos/{REPO}/issues/5": issue(REPO, 5, labels=["triage:needs-rewrite", "bug"]),
            f"repos/{REPO}/issues/5/dependencies/blocked_by": [issue(REPO, 21)],
            f"repos/{REPO}/issues/5/comments": [],
            f"repos/{REPO}/issues/6": issue(REPO, 6, type_="Task"),
            "repos/o/x/issues/2": issue("o/x", 2),
        }

    def plan(self, verdicts):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(verdicts, f)
        _, out = run(["plan", f.name], FakeApi(self.routes()))
        return json.loads(out)

    def test_plan_adds_only_missing_edges_and_rejects_bad_verdicts(self):
        plan = self.plan([
            {"ref": "o/r#5", "state": "blocked", "summary": "s", "type": "Bug",
             "blockers": [{"ref": "o/r#21", "why": "already native"}, {"ref": "o/x#2", "why": "cross-repo"}]},
            {"ref": "o/r#6", "state": "superseded", "superseded_by": "o/r#5", "summary": "dup"},
            {"ref": "o/r#7", "state": "needs-answer"},
        ])
        ops = [(a["op"], a.get("blocker") or a.get("state") or a.get("type") or a.get("reason")) for a in plan["actions"]]
        self.assertEqual(ops, [("blocked_by", "o/x#2"), ("label", "blocked"), ("record", None), ("type", "Bug"),
                               ("close", "duplicate")])
        self.assertEqual(plan["errors"][0]["ref"], "o/r#7")
        self.assertEqual([a["n"] for a in plan["actions"]], [1, 2, 3, 4, 5])

    def test_apply_prints_without_yes_and_writes_selected_with_yes(self):
        plan = self.plan([{"ref": "o/r#5", "state": "blocked", "summary": "s", "blockers": [{"ref": "o/x#2"}]}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(plan, f)
        api = FakeApi(self.routes())
        _, out = run(["apply", f.name], api)
        self.assertEqual(api.writes, [])
        self.assertIn("not applied", out)
        api = FakeApi(self.routes())
        run(["apply", f.name, "--yes", "--only", "1,2"], api)
        self.assertEqual(api.writes, [
            ("POST", "repos/o/r/issues/5/dependencies/blocked_by", {"issue_id": 1002}),
            ("DELETE", "repos/o/r/issues/5/labels/triage%3Aneeds-rewrite", None),
            ("POST", "repos/o/r/issues/5/labels", {"labels": ["triage:blocked"]}),
        ])

    def test_close_upserts_the_record_and_label_drops_replaced_roles(self):
        routes = self.routes() | {f"repos/{REPO}/issues/5/comments": [{"id": 77, "body": bt.RECORD_MARK, "user": ME,
                                                                       "created_at": "x"}]}
        api = FakeApi(routes)
        bt.Writer(api, bt.Tracker(api)).run({"op": "close", "ref": "o/r#5", "reason": "completed", "comment": "c"})
        self.assertEqual(api.writes[0], ("PATCH", "repos/o/r/issues/comments/77", {"body": "c"}))
        api = FakeApi(routes | {f"repos/{REPO}/issues/5": issue(REPO, 5, labels=["needs-triage"])})
        plan = bt.plan_from_verdicts(bt.Tracker(api), [{"ref": "o/r#5", "state": "clear", "summary": "s"}])
        self.assertIn("label", [x["op"] for x in plan["actions"]])
        bt.Writer(api, bt.Tracker(api)).run({"op": "label", "ref": "o/r#5", "state": "clear"})
        self.assertEqual(api.writes[0], ("DELETE", "repos/o/r/issues/5/labels/needs-triage", None))

    def test_record_upserts_existing_comment(self):
        api = FakeApi(self.routes() | {f"repos/{REPO}/issues/5/comments": [{"id": 77, "body": bt.RECORD_MARK, "user": ME, "created_at": "x"}]})
        bt.Writer(api, bt.Tracker(api)).run({"op": "record", "ref": "o/r#5", "body": "new"})
        self.assertEqual(api.writes, [("PATCH", "repos/o/r/issues/comments/77", {"body": "new"})])


class OutcomeTests(unittest.TestCase):
    def routes(self):
        return {
            f"repos/{REPO}/issues/5": issue(REPO, 5, labels=["triage:clear", "needs:old"], type_="Task"),
            f"repos/{REPO}/issues/5/dependencies/blocked_by": [],
            f"repos/{REPO}/issues/6": issue(REPO, 6, labels=["triage:needs-answer"]),
            f"repos/{REPO}/issues/7": issue(REPO, 7),
            f"repos/{REPO}/issues/7/dependencies/blocked_by": [],
            f"repos/{REPO}/issues/8": issue(REPO, 8, state="closed"),
            f"repos/{REPO}/issues/9": issue(REPO, 9),
            f"repos/{REPO}/issues/9/dependencies/blocked_by": [],
        }

    def plan(self, verdicts, vocab=None):
        return bt.plan_from_verdicts(bt.Tracker(FakeApi(self.routes())), verdicts, vocab, {"g": "stack 5 then 7"})

    def test_park_wontfix_needs_split_subissues_and_new_issues(self):
        plan = self.plan([
            {"ref": "o/r#6", "state": "park", "summary": "needs a grill"},
            {"ref": "o/r#9", "state": "wontfix", "summary": "tmux-only flags",
             "new_issues": [{"title": "cleat mouse", "body": "inject mouse events"}]},
            {"ref": "o/r#5", "state": "clear", "summary": "s", "needs": ["windows"], "split": True},
            {"ref": "o/r#7", "state": "mission", "summary": "tracker", "sub_issues": ["o/r#9"]},
            {"ref": "o/r#9", "state": "blocked", "summary": "b", "blockers": [{"ref": "o/r#8", "why": "closed already"}]},
        ], vocab={"windows"})
        ops = [(a["op"], a["ref"]) for a in plan["actions"]]
        self.assertEqual(ops, [
            ("type", "o/r#6"), ("unlabel", "o/r#6"), ("record", "o/r#6"),
            ("create_issue", "o/r#9"), ("close", "o/r#9"),
            ("needs", "o/r#5"), ("record", "o/r#5"),
            ("type", "o/r#7"), ("sub_issue", "o/r#7"), ("record", "o/r#7"),
            ("label", "o/r#9"), ("record", "o/r#9")])
        self.assertEqual(plan["actions"][4]["reason"], "not_planned")
        self.assertIn("already closed", plan["errors"][0]["errors"][0])
        self.assertIn("Split before dispatch", plan["actions"][6]["body"])
        self.assertEqual(bt.parse_record(plan["actions"][6]["body"])["state"], "clear")

    def test_sub_issue_with_another_parent_is_reported_not_proposed(self):
        routes = self.routes()
        routes["repos/o/r/issues/9"] = issue(REPO, 9, parent="x/y#3")
        routes["repos/o/r/issues/8"] = issue(REPO, 8, parent="o/r#7")
        plan = bt.plan_from_verdicts(bt.Tracker(FakeApi(routes)), [
            {"ref": "o/r#7", "state": "mission", "summary": "tracker", "sub_issues": ["o/r#9", "o/r#8"]}])
        self.assertNotIn("sub_issue", [a["op"] for a in plan["actions"]])
        self.assertEqual(len(plan["errors"]), 1)
        self.assertIn("already has parent x/y#3", plan["errors"][0]["errors"][0])

    def test_new_issue_is_filed_assessed(self):
        plan = self.plan([{"ref": "o/r#9", "state": "wontfix", "summary": "s", "new_issues": [
            {"title": "cleat mouse", "body": "b", "type": "Feature", "state": "clear", "summary": "startable",
             "needs": ["windows"]}]}], vocab={"windows"})
        a = plan["actions"][0]
        self.assertEqual((a["type"], a["state"], a["needs"]), ("Feature", "clear", ["windows"]))
        self.assertEqual(bt.parse_record(a["record"])["state"], "clear")
        api = CreatingApi(self.routes())
        bt.Writer(api, bt.Tracker(api)).run(a)
        self.assertEqual(api.writes[0][2]["labels"], ["triage:clear", "needs:windows"])
        self.assertEqual(api.writes[0][2]["type"], "Feature")
        self.assertEqual(api.writes[1][1], "repos/o/r/issues/50/comments")
        bad = self.plan([{"ref": "o/r#9", "state": "wontfix", "summary": "s",
                          "new_issues": [{"title": "t", "body": "b", "state": "blocked"}]}])
        self.assertIn("new issue state", bad["errors"][0]["errors"][0])

    def test_needs_vocabulary_is_enforced(self):
        plan = self.plan([{"ref": "o/r#5", "state": "clear", "summary": "s", "needs": ["gpu"]}], vocab={"windows"})
        self.assertIn("not in the repo's vocabulary", plan["errors"][0]["errors"][0])

    def test_without_a_vocabulary_no_needs_label_is_written(self):
        plan = self.plan([{"ref": "o/r#5", "state": "clear", "summary": "s", "needs": ["quiet-window"]},
                          {"ref": "o/r#9", "state": "wontfix", "summary": "s",
                           "new_issues": [{"title": "t", "body": "b", "needs": ["multi-host"]}]}])
        self.assertEqual(plan["actions"], [])
        errs = [e for x in plan["errors"] for e in x["errors"]]
        self.assertTrue(all("declares no [needs] vocabulary" in e and "conditions" in e for e in errs), errs)
        self.assertEqual(len(errs), 2)

    def test_without_a_vocabulary_existing_needs_labels_are_left_alone(self):
        plan = self.plan([{"ref": "o/r#5", "state": "clear", "summary": "s"}])
        self.assertNotIn("needs", [a["op"] for a in plan["actions"]])

    def test_conditions_go_in_the_record_not_on_a_label(self):
        plan = self.plan([{"ref": "o/r#7", "state": "clear", "summary": "s",
                           "conditions": ["merge alone in a quiet window", "operator checks the fleet after the roll"]}])
        self.assertEqual([a["op"] for a in plan["actions"]], ["label", "record"])
        body = plan["actions"][1]["body"]
        self.assertIn("- operator checks the fleet after the roll", body)
        self.assertNotIn("needs:", body)
        self.assertIn("conditions:", bt.review(plan))
        self.assertIn("conditions must be", self.plan([{"ref": "o/r#7", "state": "clear", "conditions": [""]}])
                      ["errors"][0]["errors"][0])

    def test_namespaced_vocabulary_is_written_as_labels(self):
        plan = self.plan([{"ref": "o/r#5", "state": "clear", "summary": "s", "needs": ["platform:windows"]}],
                         vocab={"platform:windows"})
        self.assertEqual(plan["errors"], [])
        self.assertEqual([a["needs"] for a in plan["actions"] if a["op"] == "needs"], [["platform:windows"]])

    def test_needs_writer_sets_exact_labels(self):
        api = FakeApi(self.routes())
        bt.Writer(api, bt.Tracker(api)).run({"op": "needs", "ref": "o/r#5", "needs": ["windows"]})
        self.assertEqual(api.writes, [("DELETE", "repos/o/r/issues/5/labels/needs%3Aold", None),
                                      ("POST", "repos/o/r/issues/5/labels", {"labels": ["needs:windows"]})])

    def test_review_groups_questions_first_with_action_numbers(self):
        plan = self.plan([{"ref": "o/r#9", "state": "needs-answer", "summary": "s", "question": "Which?"},
                          {"ref": "o/r#7", "state": "clear", "summary": "fine"}])
        text = bt.review(plan)
        self.assertLess(text.index("## Questions"), text.index("## clear"))
        self.assertIn("Which? _[1, 2]_", text)
        self.assertIn("**g**: stack 5 then 7", text)


class GroupTests(unittest.TestCase):
    def test_groups_by_top_component_folds_small_areas_and_splits_evenly(self):
        issues = [issue(REPO, n) for n in range(1, 12)]
        answers = {}
        for n in range(1, 10):            # nine in area a, second choice b
            answers[f"o/r#{n}"] = {"comp:a": 0.9, "comp:b": 0.6, "open_question": 0.2}
        answers["o/r#10"] = {"comp:b": 0.8, "comp:a": 0.7, "open_question": 0.9}   # lone b folds into a
        answers["o/r#11"] = {"comp:a": 0.3, "comp:b": 0.2, "open_question": 0.1}   # unclear, escalated
        out = bt.group_issues(issues, answers, max_size=8, min_size=3)
        sizes = {g["area"]: len(g["refs"]) for g in out["groups"]}
        self.assertEqual(sizes, {"a-1": 5, "a-2": 5, "unclear": 1})
        self.assertEqual(out["issues"]["o/r#11"]["escalate"], ["no component above 0.5 (best a 0.30)"])
        self.assertTrue(out["issues"]["o/r#10"]["hints"][0].startswith("likely needs-answer"))

    def test_group_needs_key(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(SystemExit):
            bt.main(["group", REPO, "--components", "x.toml"], FakeApi({}))


class SetupTests(unittest.TestCase):
    def test_reports_missing_labels_invalid_priorities_and_untyped(self):
        routes = {f"repos/{REPO}/labels": [{"name": n} for n in bt.CANONICAL if n != "triage:clear"] + [{"name": "priority:soon"}],
                  "orgs/o/issue-types": [{"name": t} for t in bt.ISSUE_TYPES if t != "Lane"],
                  f"repos/{REPO}/issues": [issue(REPO, 1, labels=["priority:soon"]), issue(REPO, 2, type_="Task")]}
        _, out = run(["setup", REPO], FakeApi(routes))
        d = json.loads(out)
        r = d["repos"][REPO]
        self.assertEqual(r["missing_labels"], ["triage:clear"])
        self.assertEqual(r["invalid_priority_labels"], {"priority:soon": 1})
        self.assertEqual(r["untyped_open_issues"], 1)
        self.assertEqual(r["missing_issue_types"], ["Lane"])
        self.assertEqual(d["actions"][0]["op"], "create_label")

    def test_reports_needs_labels_outside_the_vocabulary(self):
        names = list(bt.CANONICAL) + ["needs:platform:windows", "needs:multi-host", "needs:platform:macos"]
        routes = {f"repos/{REPO}/labels": [{"name": n} for n in names],
                  "orgs/o/issue-types": [{"name": t} for t in bt.ISSUE_TYPES],
                  f"repos/{REPO}/issues": [issue(REPO, 1, labels=["needs:multi-host"], type_="Task")]}
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "triage.toml"
            cfg.write_text(ConfigTests.PATTERN + '[needs]\n"platform:windows" = "w"\n')
            _, out = run(["setup", REPO, "--config", str(cfg)], FakeApi(routes))
        d = json.loads(out)
        self.assertEqual(d["repos"][REPO]["undeclared_needs_labels"], {
            "needs:multi-host": {"open_issues": 1, "matches_format": False},
            "needs:platform:macos": {"open_issues": 0, "matches_format": True}})
        self.assertEqual(d["actions"], [])


class ConfigTests(unittest.TestCase):
    PATTERN = 'needs_label_pattern = "^needs:[a-z0-9-]+:[a-z0-9.-]+$"\n'

    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "triage.toml"
            p.write_text(text)
            return bt.load_config(p)

    def test_vocabulary_requires_a_label_pattern(self):
        with self.assertRaisesRegex(SystemExit, "requires needs_label_pattern"):
            self.load('[needs]\nwindows = "w"\n')

    def test_vocabulary_entry_must_match_the_pattern(self):
        with self.assertRaisesRegex(SystemExit, "does not match needs_label_pattern"):
            self.load(self.PATTERN + '[needs]\nmulti-host = "m"\n')

    def test_matching_vocabulary_loads(self):
        cfg = self.load(self.PATTERN + '[needs]\n"platform:windows" = "w"\n')
        self.assertEqual(set(cfg["needs"]), {"platform:windows"})

    def test_config_without_needs_needs_no_pattern(self):
        self.assertIsNone(self.load('[[component]]\nname = "a"\n')["needs_label_re"])


if __name__ == "__main__":
    unittest.main()
