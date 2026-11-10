"""Experiment 2 plumbing: the planmode arm, the reviewing partner and the checkpoints. Free: the model is stubbed."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

EVALS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVALS))
import partner as P  # noqa: E402
import reviewer as V  # noqa: E402
import compare2 as C2  # noqa: E402
import run as R  # noqa: E402

ARGS = NS(odin_dir="/x/odin", model="claude-sonnet-5-5")
CASE = R.load_case("logout-cache")


def write_plan(path, content):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": path, "content": content}}]}}


class PlanModeCommandTest(unittest.TestCase):
    def test_planmode_starts_in_plan_mode_without_plugins(self):
        cmd = R.build_cmd(CASE, "planmode", ARGS)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--plugin-dir", cmd)
        self.assertIn("gh pr create", cmd[cmd.index("-p") + 1])
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project,local")

    def test_resume_can_switch_permission_mode(self):
        cmd = R.build_cmd(CASE, "planmode", ARGS)
        r = R.resume_cmd(cmd, "s1", "Approved. Go ahead.", "bypassPermissions")
        self.assertEqual(r[r.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(r[-2:], ["--resume", "s1"])
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")  # original untouched
        self.assertEqual(R.build_cmd(CASE, "plain", ARGS)[R.build_cmd(CASE, "plain", ARGS).index("--permission-mode") + 1],
                         "bypassPermissions")

    def test_latest_plan(self):
        home = "/Users/u/.claude/plans/fix-x.md"
        events = [write_plan(home, "v1"), {"type": "result", "result": "summary"}, write_plan(home, "v2"),
                  write_plan("/repo/shop/session.py", "code")]
        self.assertEqual(R.latest_plan(events), "v2")
        self.assertEqual(R.latest_plan([{"type": "result", "result": "the plan in chat"}]), "the plan in chat")
        exit_plan = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "ExitPlanMode", "input": {"plan": "from ExitPlanMode"}}]}}
        self.assertEqual(R.latest_plan([exit_plan]), "from ExitPlanMode")
        pointer = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "ExitPlanMode", "input": {"plan": "See plan file: fix logout"}}]}}
        self.assertEqual(R.latest_plan([write_plan(home, "the full plan"), pointer]), "the full plan")  # pre-flight


class ReviewerTest(unittest.TestCase):
    def test_parse_structured_output_and_fallback(self):
        out = {"structured_output": {"verdict": "revise", "reason": "clears all"}, "total_cost_usd": 0.02}
        self.assertEqual(V.parse_plan_verdict(json.dumps(out))["verdict"], "revise")
        self.assertEqual(V.parse_plan_verdict(json.dumps({"result": '{"verdict": "approve", "reason": "ok"}'}))["verdict"],
                         "approve")
        with self.assertRaises(ValueError):
            V.parse_plan_verdict(json.dumps({"result": "I think it's fine"}))

    def test_empty_plan_is_revised_without_a_call(self):
        with mock.patch.object(V.subprocess, "run", side_effect=AssertionError("no call")):
            self.assertEqual(V.review_plan(CASE, "  ")["verdict"], "revise")

    def test_prompt_has_intent_and_no_tools(self):
        cmd = V.plan_cmd("x")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--json-schema", cmd)
        prompt = V.PLAN_PROMPT.format(request=CASE["prompt"], intent=CASE["review"]["intent"], plan="p")
        self.assertIn("user:<id>:", prompt)

    def test_check_code_on_oracle_and_wrong(self):
        for fix, expected in (("oracle", True), ("wrong", False)):
            d = Path(tempfile.mkdtemp())
            import shutil
            shutil.copytree(V.CASES_DIR / "logout-cache" / "repo", d / "code")
            shutil.copy(V.CASES_DIR / "logout-cache" / fix / "shop" / "session.py", d / "code" / "shop" / "session.py")
            self.assertEqual(V.review_code(CASE, d / "code")["hidden_pass"], expected, fix)


class ReviewTurnTest(unittest.TestCase):
    """plain/planmode: plan review (planmode only), then PR review by hidden tests, at most 2 corrections."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.transcript = self.out / "transcript.jsonl"
        self.transcript.write_text(json.dumps(write_plan("/Users/u/.claude/plans/p.md", "call cache.clear()")) + "\n")

    def turn(self, mode, checkpoints, plan_verdict="revise", hidden=False):
        g = {"hidden_pass": hidden, "hidden_tail": "", "graded_ref": "feat/x", "pr_opened": True}
        with mock.patch.object(R.reviewer, "review_plan", return_value={"verdict": plan_verdict, "reason": "r"}), \
             mock.patch.object(R, "grade", return_value=g):
            return R.review_turn(CASE, mode, self.out, self.out, self.transcript, checkpoints, 0.0)

    def test_planmode_flow(self):
        cps = []
        self.assertEqual(self.turn("planmode", cps), (CASE["review"]["correction"], "plan"))
        self.assertEqual(self.turn("planmode", cps, plan_verdict="approve"), (R.APPROVED, "bypassPermissions"))
        msg, perm = self.turn("planmode", cps, hidden=True)
        self.assertIsNone(msg)
        self.assertEqual([(c["kind"], c["correction"]) for c in cps], [("plan", True), ("plan", False), ("pr", False)])

    def test_plain_gets_a_late_correction_then_the_cap(self):
        cps = []
        msg, perm = self.turn("plain", cps)
        self.assertTrue(msg.startswith(CASE["review"]["correction"]))
        self.assertIn("same branch", msg)
        self.turn("plain", cps)
        self.assertEqual(self.turn("plain", cps), (None, None))  # third wrong PR: capped
        self.assertEqual([c["correction"] for c in cps], [True, True, False])
        self.assertTrue(cps[-1]["capped"])

    def test_planmode_cap_moves_on_to_execution(self):
        cps = []
        self.turn("planmode", cps)
        self.turn("planmode", cps)
        self.assertEqual(self.turn("planmode", cps), (R.APPROVED, "bypassPermissions"))
        self.assertTrue(cps[-1]["capped"])


class PartnerReviewTest(unittest.TestCase):
    """Odin: the partner reviews the plan gate and the pre-PR gate through the dashboard decisions."""

    def state(self, worktree=""):
        d = Path(tempfile.mkdtemp())
        f = d / "t.html"
        f.write_text(f'<body data-worktree="{worktree}"><section id="huginn-plan"><h3>Plan</h3>'
                     '<p>Make logout() call cache.clear().</p></section></body>')
        return d, f

    def test_plan_gate(self):
        d, f = self.state()
        with mock.patch.object(V, "review_plan", return_value={"verdict": "revise", "reason": "r"}) as rp:
            dec, notes, rec = P.reviewed_decision(CASE, "huginn-plan", f, d, 0)
        self.assertIn("cache.clear()", rp.call_args[0][1])  # the plan text reached the reviewer
        self.assertEqual((dec, notes, rec["correction"]), ("revise_huginn", CASE["review"]["correction"], True))
        with mock.patch.object(V, "review_plan", return_value={"verdict": "revise", "reason": "r"}):
            dec, notes, rec = P.reviewed_decision(CASE, "huginn-plan", f, d, V.MAX_CORRECTIONS)
        self.assertEqual((dec, rec["capped"]), ("approve_huginn", True))

    def test_pr_gate_tests_the_worktree(self):
        import shutil
        wt = Path(tempfile.mkdtemp()) / "wt"
        shutil.copytree(V.CASES_DIR / "logout-cache" / "repo", wt)
        d, f = self.state(str(wt))
        dec, _, rec = P.reviewed_decision(CASE, "heimdall-validation", f, d, 0)
        self.assertEqual((dec, rec["hidden_pass"], rec["src"]), ("reject_pr", False, str(wt)))
        shutil.copy(V.CASES_DIR / "logout-cache" / "oracle" / "shop" / "session.py", wt / "shop" / "session.py")
        self.assertEqual(P.reviewed_decision(CASE, "heimdall-validation", f, d, 0)[0], "approve_pr")
        self.assertEqual(P.reviewed_decision(CASE, "human-validation", f, d, 0)[0], "approve_signoff")

    def test_partner_checkpoints_from_log(self):
        out = Path(tempfile.mkdtemp())
        (out / "partner.log").write_text("\n".join(json.dumps(r) for r in [
            {"t": "1", "phase": "huginn-plan", "decision": "revise_huginn", "review": {"verdict": "revise", "correction": True}},
            {"t": "2", "phase": "huginn-plan", "decision": "approve_huginn", "review": {"verdict": "approve", "correction": False}},
            {"t": "3", "phase": "heimdall-validation", "decision": "approve_pr", "review": {"hidden_pass": True, "correction": False}},
            {"t": "4", "phase": "human-validation", "decision": "approve_signoff", "review": {}},
        ]))
        cps = R.partner_checkpoints(out)
        self.assertEqual([(c["kind"], c["correction"]) for c in cps], [("plan", True), ("plan", False), ("pr", False)])


class Compare2Test(unittest.TestCase):
    def row(self, cps, hidden=True, cost=0.3):
        return {"case": "logout-cache", "mode": "gated", "checkpoints": cps, "cost_usd": cost, "wall_s": 60,
                "grade": {"hidden_pass": hidden, "repo_checks_pass": True}}

    def test_metrics(self):
        caught = C2.metrics(self.row([{"kind": "plan", "verdict": "revise", "correction": True},
                                      {"kind": "plan", "verdict": "approve", "correction": False},
                                      {"kind": "pr", "hidden_pass": True, "correction": False}]))
        self.assertEqual((caught["wrong_built"], caught["caught_at_plan"], caught["first_plan_ok"], caught["corrections"]),
                         (False, True, False, 1))
        late = C2.metrics(self.row([{"kind": "pr", "hidden_pass": False, "correction": True},
                                    {"kind": "pr", "hidden_pass": True, "correction": False}]))
        self.assertEqual((late["wrong_built"], late["caught_at_plan"], late["first_plan_ok"], late["final_right"]),
                         (True, False, None, True))
        self.assertTrue(C2.metrics(self.row([], hidden=False))["wrong_built"])  # never reviewed: final grade

    def test_compare_renders(self):
        mk = lambda mode, rows: {"dir": None, "rows": [{**r, "mode": mode} for r in rows],  # noqa: E731
                                 "m": [C2.metrics(r) for r in rows], "errors": [], "mode": mode}
        good = self.row([{"kind": "pr", "hidden_pass": True, "correction": False}])
        bad = self.row([{"kind": "pr", "hidden_pass": False, "correction": True}], cost=0.1)
        text = C2.compare([mk("gated", [good] * 3), mk("planmode", [good] * 3), mk("plain", [bad] * 3)])
        self.assertIn("odin vs. planmode vs. plain", text)
        self.assertIn("| odin / plain | 0.100 |", text)


if __name__ == "__main__":
    unittest.main()
