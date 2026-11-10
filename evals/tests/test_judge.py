"""Offline tests for the LLM judge: extraction, request shape, error paths, caching and agreement.

A stub client stands in for the Claude API, so these cost nothing and need no key.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

EVALS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVALS))
import judge as J  # noqa: E402

STATE = """<html><body>
<section id="huginn-plan" class="odin-phase">
  <h2>🪶 Phase 1: Huginn (Planner &amp; Architect)</h2><span>DONE</span>
  <div>Iteration 1 — Phase 1: Huginn Intake</div>
  <h3>Plan</h3><ul><li>shop/cart.py: discount the whole subtotal</li></ul>
  <h3>Testing strategy</h3><p>python3 -m unittest discover tests</p>
  <ol class="odin-progress-log"><li>✅ 12:01 ran tests</li></ol>
  <script>var x = "not text";</script>
</section>
<section id="heimdall-validation">
  <p>Tests 6/6 pass.</p>
  <div class="odin-diff-viewer"><pre>+ secret diff body</pre></div>
  <div class="odin-diff-rationale-banner">Coupon now applies to every item.</div>
  <div data-file-notes='{}'></div>
  <p>[2026-11-10 13:00] Approved. For any open questions, go with the defaults you recommended.</p>
</section></body></html>"""

INP = {"request": "Fix the coupon", "repo_context": "Files:\n- shop/cart.py", "plan": "Plan: x",
       "review": "Changed cart.py", "diff": "+x"}


class StubClient:
    def __init__(self, resp=None, exc=None):
        self.calls, self.resp, self.exc = [], resp, exc
        self.beta = NS(messages=NS(parse=self._parse))

    def _parse(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.resp


def response(verdict="pass", stop="end_turn", parsed=True):
    usage = NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=3000)
    out = J.Verdict(reasoning="r", verdict=verdict, evidence="e") if parsed else None
    return NS(parsed_output=out, stop_reason=stop, usage=usage, model=J.JUDGE_MODEL, _request_id="req_1")


class ExtractionTest(unittest.TestCase):
    def test_plan_text_drops_chrome_timeline_and_scripts(self):
        text = J.plan_text(STATE)
        self.assertIn("discount the whole subtotal", text)
        self.assertIn("python3 -m unittest discover tests", text)
        for junk in ("Phase 1: Huginn", "DONE", "Iteration 1", "12:01", "not text"):
            self.assertNotIn(junk, text)

    def test_review_text_keeps_rationale_not_diff_or_partner_note(self):
        text = J.review_text(STATE)
        self.assertIn("Tests 6/6 pass", text)
        self.assertIn("Rationale: Coupon now applies to every item.", text)
        self.assertNotIn("secret diff body", text)
        self.assertNotIn("Approved.", text)
        self.assertNotIn("Per-file notes", text)  # "{}" is no notes

    def test_pr_body_takes_last_body(self):
        d = Path(tempfile.mkdtemp())
        (d / "gh.log").write_text(
            "2026-11-10T18:01:40Z [branch=feat/x] gh pr create --fill\n"
            "2026-11-10T18:01:41Z [branch=feat/x] gh pr create --title T --body first\n"
            "2026-11-10T18:01:45Z [branch=feat/x] gh pr edit 1 --body Line one.\n\n- detail\n")
        self.assertEqual(J.pr_body(d), "Line one.\n\n- detail")
        self.assertEqual(J.pr_body(Path(tempfile.mkdtemp())), "")

    def test_pr_description_help_fill_and_body(self):
        import subprocess
        d = Path(tempfile.mkdtemp())
        repo = d / "repo"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,  # noqa: E731
                                      capture_output=True, text=True)
        g("init", "-q", "-b", "main"); g("commit", "-q", "--allow-empty", "-m", "init")
        g("checkout", "-q", "-b", "feat/x"); g("commit", "-q", "--allow-empty", "-m", "Fix coupon on every item")
        (d / "gh.log").write_text("2026-11-10T18:01:40Z [branch=feat/x] gh pr create --help\n")
        self.assertEqual(J.pr_description(d), (False, ""))
        (d / "gh.log").write_text("2026-11-10T18:01:40Z [branch=feat/x] gh pr create --fill\n")
        self.assertEqual(J.pr_description(d), (True, "Fix coupon on every item"))
        (d / "gh.log").write_text("2026-11-10T18:01:40Z [branch=feat/x] gh pr create --title T --body ### Prompt Summary\n- a\n")
        opened, body = J.pr_description(d)
        self.assertTrue(opened)
        self.assertEqual(J.blind_pr_body(body), "")  # Odin's Prompt-Summary-only body: nothing about the change
        (d / "gh-bodies").mkdir()
        (d / "gh-bodies" / "body.md").write_text("## Summary\n- coupon on every item")
        (d / "gh.log").write_text("2026-11-10T18:01:40Z [branch=feat/x] gh pr create --title T --body x\n"
                                  "2026-11-10T18:01:45Z [branch=feat/x] gh pr edit 1 --body-file /gone/body.md\n")
        self.assertEqual(J.pr_description(d), (True, "## Summary\n- coupon on every item"))

    def test_judge_never_sees_variant_or_model(self):
        msg = J.user_message("review_matches_diff", INP)
        for leak in ("odin", "sonnet", "gated", "waiver", "variant"):
            self.assertNotIn(leak, msg.lower())
        self.assertIn("<actual-diff>", msg)
        self.assertNotIn("<actual-diff>", J.user_message("plan_test_commands", INP))

    def test_applicable(self):
        self.assertFalse(J.applicable("plan_covers_request", {**INP, "plan": ""}))
        self.assertFalse(J.applicable("review_matches_diff", {**INP, "diff": ""}))
        self.assertTrue(J.applicable("review_matches_diff", INP))


class JudgeCallTest(unittest.TestCase):
    def test_request_shape(self):
        stub = StubClient(response())
        res = J.Judge(client=stub).grade("plan_test_commands", INP)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["verdict"], "pass")
        kw = stub.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["output_config"], {"effort": "medium"})
        self.assertEqual(kw["fallbacks"], "default")
        self.assertIn(J.FALLBACK_BETA, kw["betas"])
        self.assertIs(kw["output_format"], J.Verdict)
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("thinking", kw)

    def test_system_prompt_is_identical_across_properties(self):
        """Cache prefix must not vary by property, or every call writes a new cache entry."""
        stub = StubClient(response())
        j = J.Judge(client=stub)
        for prop in J.PROPERTIES:
            j.grade(prop, INP)
        self.assertEqual(len({c["system"][0]["text"] for c in stub.calls}), 1)

    def test_cost(self):
        res = J.Judge(client=StubClient(response())).grade("plan_test_commands", INP)
        # 1000*4 + 200*20 + 3000*0.20 = 8600 per MTok-units -> $0.0086
        self.assertAlmostEqual(res["cost_usd"], 0.0086)

    def test_refusal_is_error_not_fail(self):
        res = J.Judge(client=StubClient(response(stop="refusal", parsed=False))).grade("plan_test_commands", INP)
        self.assertEqual(res["status"], "error")
        self.assertNotIn("verdict", res)

    def test_unparsed_is_error(self):
        res = J.Judge(client=StubClient(response(stop="max_tokens", parsed=False))).grade("plan_test_commands", INP)
        self.assertEqual(res["status"], "error")

    def test_api_exception_is_error(self):
        res = J.Judge(client=StubClient(exc=RuntimeError("overloaded"))).grade("plan_test_commands", INP)
        self.assertEqual(res["status"], "error")
        self.assertIn("overloaded", res["error"])

    def test_cache_hit_skips_call_and_errors_are_not_cached(self):
        cache = Path(tempfile.mkdtemp())
        stub = StubClient(response())
        J.Judge(client=stub, cache_dir=cache).grade("plan_test_commands", INP)
        again = J.Judge(client=stub, cache_dir=cache).grade("plan_test_commands", INP)
        self.assertTrue(again["cached"])
        self.assertEqual(len(stub.calls), 1)
        bad = StubClient(exc=RuntimeError("x"))
        J.Judge(client=bad, cache_dir=cache).grade("plan_covers_request", INP)
        J.Judge(client=bad, cache_dir=cache).grade("plan_covers_request", INP)
        self.assertEqual(len(bad.calls), 2)

    def test_selftest_has_known_negatives(self):
        cases = {name: exp for name, _, exp in J.selftest_inputs()}
        self.assertTrue(all(v == "pass" for v in cases["good"].values()))
        for neg in ("empty", "i-dont-know", "wrong-task", "injection"):
            self.assertTrue(all(v == "fail" for v in cases[neg].values()), neg)


class AgreementTest(unittest.TestCase):
    def test_agreement_and_confusion(self):
        labels = [{"item": "a", "property": "p", "human_verdict": "pass"},
                  {"item": "b", "property": "p", "human_verdict": "fail"},
                  {"item": "c", "property": "p", "human_verdict": "fail"},
                  {"item": "d", "property": "q", "human_verdict": ""},       # unlabelled: skipped
                  {"item": "e", "property": "q", "human_verdict": "pass"}]   # not judged: skipped
        res = J.agreement(labels, {"a": "pass", "b": "pass", "c": "fail", "d": "pass"})
        self.assertEqual(res["total"], {"n": 3, "agree": 2})
        self.assertEqual(res["per_property"]["p"]["judge_pass_human_fail"], 1)
        self.assertAlmostEqual(res["rate"], 2 / 3)


if __name__ == "__main__":
    unittest.main()
