"""Experiment 1 plumbing: the plain arm's command line, loop status, blinding, and the comparison stats. Free."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

EVALS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVALS))
import compare as C  # noqa: E402
import judge as J  # noqa: E402
import run as R  # noqa: E402

CASE = {"id": "bugfix-single-file", "prompt": "Fix the coupon.", "budget_usd": 3}
ARGS = NS(odin_dir="/x/odin", model="claude-sonnet-5-5")


class CommandLineTest(unittest.TestCase):
    def test_plain_has_no_plugin_and_asks_for_a_pr(self):
        cmd = R.build_cmd(CASE, "plain", ARGS)
        prompt = cmd[cmd.index("-p") + 1]
        self.assertNotIn("--plugin-dir", cmd)
        self.assertFalse(prompt.startswith("/odin"))
        self.assertTrue(prompt.startswith("Fix the coupon."))
        self.assertIn("gh pr create", prompt)

    def test_odin_arms_load_the_plugin(self):
        for mode in ("gated", "waiver"):
            cmd = R.build_cmd(CASE, mode, ARGS)
            self.assertEqual(cmd[cmd.index("--plugin-dir") + 1], "/x/odin")
            self.assertTrue(cmd[cmd.index("-p") + 1].startswith("/odin:odin Fix the coupon."))
        self.assertIn("I waive plan review", R.build_cmd(CASE, "waiver", ARGS)[2])

    def test_every_arm_is_isolated_and_pinned(self):
        for mode in ("gated", "waiver", "plain"):
            cmd = R.build_cmd(CASE, mode, ARGS)
            self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project,local")
            self.assertIn("--strict-mcp-config", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
            self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "3")

    def test_plain_finishes_by_opening_a_pr(self):
        out = Path(tempfile.mkdtemp())
        self.assertEqual(R.loop_status("plain", {}, out)["finished"], False)
        (out / "gh.log").write_text("2026-11-10T18:01:40Z [branch=feat/x] gh pr create --fill\n")
        st = R.loop_status("plain", {}, out)
        self.assertTrue(st["finished"])
        self.assertEqual(st["stalled_at"], [])

    def test_gated_loop_status_unchanged(self):
        phases = {"huginn-plan": "done", "thor-execution": "done", "heimdall-validation": "awaiting_human"}
        st = R.loop_status("gated", phases, Path(tempfile.mkdtemp()))
        self.assertFalse(st["finished"])
        self.assertEqual(st["stopped_before"], "heimdall-validation")
        self.assertEqual(st["stalled_at"], ["heimdall-validation"])


STALLED_STATE = """<section id="huginn-plan" data-status="awaiting_human"></section>
<section id="thor-execution" data-status="pending"></section>"""


class NudgeTest(unittest.TestCase):
    def trial(self, state=STALLED_STATE, watcher=False):
        out = Path(tempfile.mkdtemp())
        (out / "repo" / "odin-states").mkdir(parents=True)
        (out / "repo" / "odin-states" / "t.html").write_text(state)
        if watcher:
            (out / "repo" / "odin-states" / ".odin-watcher-t.json").write_text("{}")
        return out

    def test_resume_cmd_replaces_prompt_and_keeps_flags(self):
        cmd = R.build_cmd(CASE, "gated", ARGS)
        r = R.resume_cmd(cmd, "sess-1", R.NUDGE)
        self.assertEqual(r[r.index("-p") + 1], R.NUDGE)
        self.assertEqual(r[-2:], ["--resume", "sess-1"])
        self.assertIn("--plugin-dir", r)
        self.assertEqual(cmd[cmd.index("-p") + 1].split()[0], "/odin:odin")  # original untouched

    def test_stalled_gate_detection(self):
        self.assertEqual(R.stalled_gate("gated", self.trial()), "huginn-plan")
        self.assertEqual(R.stalled_gate("gated", self.trial(watcher=True)), "")  # still waiting on the dashboard
        self.assertEqual(R.stalled_gate("plain", self.trial()), "")
        self.assertEqual(R.stalled_gate("gated", self.trial(STALLED_STATE.replace("awaiting_human", "done"))), "")

    def test_totals_cost_is_cumulative_turns_add_up(self):
        res = [{"total_cost_usd": 0.18, "num_turns": 3, "usage": {"input_tokens": 10}, "modelUsage": {"claude-sonnet-5-5": {}}},
               {"total_cost_usd": 0.30, "num_turns": 4, "usage": {"input_tokens": 5}, "modelUsage": {"claude-sonnet-5-5": {}}}]
        t = R.totals(res)
        self.assertAlmostEqual(t["cost_usd"], 0.30)  # last cumulative value, not 0.48
        self.assertEqual(t["num_turns"], 7)
        self.assertEqual(t["usage"]["input_tokens"], 15)
        self.assertEqual(t["models_served"], ["claude-sonnet-5-5"])
        self.assertIsNone(R.totals([])["cost_usd"])

    def test_session_id(self):
        self.assertEqual(R.session_id([{"type": "system", "subtype": "init", "session_id": "abc"}]), "abc")


class FakeGhTest(unittest.TestCase):
    """The fake gh must not report a PR that was never created (pre-flight: Odin skipped `pr create` because of it)."""

    def test_pr_view_only_after_create(self):
        import os
        import subprocess
        d = Path(tempfile.mkdtemp())
        env = {**os.environ, "PATH": f"{EVALS / 'bin'}:{os.environ['PATH']}", "ODIN_EVAL_GH_LOG": str(d / "gh.log")}
        sh = lambda *a: subprocess.run(a, cwd=d, env=env, capture_output=True, text=True)  # noqa: E731
        sh("git", "init", "-q", "-b", "main")
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
        sh("git", "checkout", "-q", "-b", "feat/x")
        self.assertEqual(sh("gh", "pr", "view", "--json", "url").returncode, 1)
        self.assertEqual(sh("gh", "pr", "list", "--json", "url").stdout.strip(), "[]")
        self.assertEqual(sh("gh", "pr", "create", "--fill").returncode, 0)
        self.assertIn("pull/1", sh("gh", "pr", "view", "--json", "url").stdout)
        self.assertEqual(sh("gh", "pr", "create", "--fill").returncode, 1)  # already exists
        sh("git", "checkout", "-q", "main")
        self.assertEqual(sh("gh", "pr", "view").returncode, 1)  # per branch


class BlindingTest(unittest.TestCase):
    def test_prompt_summary_and_attribution_are_stripped(self):
        body = ("Coupon now applies to every item.\n\n### Prompt Summary\n- fix the coupon\n- add tests\n\n"
                "### Testing\n- unittest passes\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")
        out = J.blind_pr_body(body)
        self.assertIn("Coupon now applies to every item.", out)
        self.assertIn("### Testing", out)
        self.assertNotIn("Prompt Summary", out)
        self.assertNotIn("fix the coupon", out)
        self.assertNotIn("Generated with", out)

    def test_summary_at_end(self):
        self.assertEqual(J.blind_pr_body("Did X.\n\n### Prompt Summary\n- a"), "Did X.")


class StatsTest(unittest.TestCase):
    def test_fisher_known_values(self):
        self.assertAlmostEqual(C.fisher_exact(3, 1, 1, 3), 0.4857, places=3)   # tea-tasting table
        self.assertAlmostEqual(C.fisher_exact(18, 0, 18, 0), 1.0)               # both at ceiling
        self.assertLess(C.fisher_exact(18, 0, 6, 12), 0.001)

    def test_bootstrap_ratio(self):
        ratio, lo, hi = C.bootstrap_ratio([0.3, 0.32, 0.35, 0.31], [0.1, 0.11, 0.12, 0.1])
        self.assertAlmostEqual(ratio, 0.315 / 0.105, places=3)
        self.assertLessEqual(lo, ratio)
        self.assertGreaterEqual(hi, ratio)
        self.assertEqual(C.bootstrap_ratio([], [1.0]), (None, None, None))


if __name__ == "__main__":
    unittest.main()
