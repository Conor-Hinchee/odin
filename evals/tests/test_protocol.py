"""Unit tests for the protocol checker, on small synthetic transcripts (no model calls).

Each rule gets a known-good and a known-bad trace, plus the false positives seen in
real runs (plan HTML with `->` and tags, `wait-for-human --help`).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

EVALS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVALS))
import protocol as P  # noqa: E402

CASE_DIR = EVALS / "cases" / "bugfix-single-file"
TOKENS = P.fixture_tokens(CASE_DIR)
ODIN = "python3 /x/skills/odin/scripts/odin-state.py"


class Trace:
    """Builds a stream-json transcript."""

    def __init__(self):
        self.events, self.n = [], 0

    def bash(self, cmd, bg=None, parent=None):
        self.n += 1
        inp = {"command": cmd}
        if bg is not None:
            inp["run_in_background"] = bg
        self.events.append({"type": "assistant", "parent_tool_use_id": parent,
                            "message": {"content": [{"type": "tool_use", "id": f"t{self.n}", "name": "Bash", "input": inp}]}})
        return self

    def tool(self, name, parent=None, **inp):
        self.n += 1
        self.events.append({"type": "assistant", "parent_tool_use_id": parent,
                            "message": {"content": [{"type": "tool_use", "id": f"t{self.n}", "name": name, "input": inp}]}})
        return self

    def wakeup(self, decision):
        body = "=== 🔔 ODIN REACTIVE PARTNER WAKEUP EVENT ===\n" + json.dumps({"decision": decision}, indent=2)
        self.events.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "x", "content": body}]}})
        return self

    def calls(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(json.dumps(e) for e in self.events))
        return P.load_calls(Path(f.name))


def bootstrap():
    return Trace().bash("ls; git status --short").bash(
        "mkdir -p odin-states && cp /x/references/state-template.html odin-states/t.html")


def good_gated():
    return (bootstrap()
            .bash("cat shop/cart.py tests/test_cart.py")
            .bash(f'{ODIN} update-phase --file odin-states/t.html --phase huginn --status awaiting_human '
                  '--html "<p>Fix shop/cart.py: 5x199 -> discount 49</p>"')
            .bash(f"{ODIN} wait-for-human --help | head -3")
            .bash(f"{ODIN} wait-for-human --file odin-states/t.html --phase huginn-plan", bg=True)
            .wakeup("approve_huginn")
            .tool("Edit", file_path="/tmp/sandbox/repo/shop/cart.py", old_string="a", new_string="b")
            .bash("python3 -m unittest discover tests")
            .bash(f"{ODIN} wait-for-human --file odin-states/t.html --phase heimdall-validation", bg=True)
            .wakeup("approve_pr")
            .bash("git switch -c feat/x && git add shop/cart.py tests/test_cart.py && git commit -m fix")
            .bash("git push -u origin feat/x && gh pr create --fill")
            .bash(f"{ODIN} wait-for-human --file odin-states/t.html --phase human-validation", bg=True)
            .wakeup("approve_signoff"))


class ProtocolTest(unittest.TestCase):
    def test_good_trace_passes_everything(self):
        c = good_gated().calls()
        self.assertEqual(P.check_state_first(c, TOKENS)["status"], "pass")
        self.assertEqual(P.check_watcher_hygiene(c)["status"], "pass")
        self.assertEqual(P.check_plan_pause(c, {"pause_at_plan": True})["status"], "pass")
        self.assertEqual(P.check_no_edits_before_approval(c, TOKENS, {"pause_at_plan": True})["status"], "pass")
        self.assertEqual(P.check_pr_after_approval(c)["status"], "pass")
        self.assertEqual(P.check_staging(c)["status"], "pass")
        self.assertEqual(P.check_valkyries(c, TOKENS, {})["status"], "n/a")

    def test_reading_source_before_state_file(self):
        c = Trace().bash("mkdir -p odin-states; cat shop/*.py | head -60").bash(
            "sed -e s/x/y/ /x/references/state-template.html > odin-states/t.html").calls()
        self.assertEqual(P.check_state_first(c, TOKENS)["status"], "fail")

    def test_no_state_file(self):
        self.assertEqual(P.check_state_first(Trace().bash("cat shop/cart.py").calls(), TOKENS)["status"], "fail")

    def test_detached_watcher(self):
        c = bootstrap().bash(f"{ODIN} wait-for-human --file $F --phase human-validation >/dev/null 2>&1 & true").calls()
        r = P.check_watcher_hygiene(c)
        self.assertEqual(r["status"], "fail")
        self.assertIn("detached", r["detail"])

    def test_nohup_watcher(self):
        c = bootstrap().bash(f"nohup {ODIN} wait-for-human --file $F --phase huginn-plan &").calls()
        self.assertEqual(P.check_watcher_hygiene(c)["status"], "fail")

    def test_duplicate_watcher_same_phase(self):
        c = (bootstrap()
             .bash(f"{ODIN} wait-for-human --file $F --phase human-validation", bg=True)
             .bash(f"{ODIN} wait-for-human --file $F --phase human-validation", bg=True)
             .wakeup("approve_signoff")).calls()
        self.assertIn("second watcher", P.check_watcher_hygiene(c)["detail"])

    def test_help_is_not_a_watcher(self):
        c = bootstrap().bash(f"{ODIN} wait-for-human --help >/dev/null 2>&1; echo ok").calls()
        self.assertEqual(P.check_watcher_hygiene(c)["status"], "n/a")

    def test_plan_pause_both_directions(self):
        paused = good_gated().calls()
        unpaused = bootstrap().bash(f"{ODIN} wait-for-human --file $F --phase heimdall-validation", bg=True).calls()
        self.assertEqual(P.check_plan_pause(unpaused, {"pause_at_plan": True})["status"], "fail")
        self.assertEqual(P.check_plan_pause(paused, {"pause_at_plan": False})["status"], "fail")
        self.assertEqual(P.check_plan_pause(unpaused, {"pause_at_plan": False})["status"], "pass")

    def test_edit_before_approval(self):
        c = (bootstrap()
             .bash("cat > shop/cart.py <<'EOF'\nx = 1\nEOF")
             .bash(f"{ODIN} wait-for-human --file $F --phase huginn-plan", bg=True)
             .wakeup("approve_huginn")).calls()
        self.assertEqual(P.check_no_edits_before_approval(c, TOKENS, {"pause_at_plan": True})["status"], "fail")

    def test_sed_in_place_counts_as_edit(self):
        c = bootstrap().bash("sed -i '' 's/a/b/' shop/cart.py").calls()
        self.assertTrue(P.is_project_write(c[-1], TOKENS))

    def test_state_file_and_plan_html_are_not_edits(self):
        c = Trace().bash(
            f"F=odin-states/t.html; PLAN='<h3>Plan</h3><ol><li>Edit shop/cart.py</li></ol><p>5x199 -> 49</p>'\n"
            f'{ODIN} update-phase --file $F --phase huginn --html "$PLAN"').calls()
        self.assertFalse(P.is_project_write(c[0], TOKENS))

    def test_heredoc_edit_chained_after_odin_call(self):
        c = Trace().bash(f"{ODIN} update-phase --file $F --phase thor --status active && cd ../wt && python3 - <<'E'\n"
                         "p='shop/cart.py'\ns=open(p).read()\nopen(p,'w').write(s)\nE\npython3 -m unittest").calls()
        self.assertTrue(P.is_project_write(c[0], TOKENS))

    def test_multiline_plan_html_is_not_an_edit(self):
        """Exp1: a quoted multi-line --html plan saying "(git rm)" was read as an edit."""
        c = Trace().bash(f"{ODIN} update-phase --file $F --phase huginn --html '<h3>Plan</h3>\n"
                         "<li>Delete shop/cart.py and tests/test_cart.py (git rm).</li>\n<p>done</p>'").calls()
        self.assertFalse(P.is_project_write(c[0], TOKENS))
        c = Trace().bash("echo 'a;b'\ngit rm shop/cart.py").calls()
        self.assertTrue(P.is_project_write(c[0], TOKENS))

    def test_chat_nudge_counts_as_approval(self):
        ev = [{"type": "result"}, {"type": "system", "subtype": "init"}]
        t = (bootstrap().bash(f"{ODIN} wait-for-human --file $F --phase huginn-plan", bg=True))
        t.events += ev
        t.tool("Edit", file_path="/r/shop/cart.py", old_string="a", new_string="b")
        t.bash(f"{ODIN} update-phase --file $F --phase heimdall --status awaiting_human")
        t.events += ev
        t.bash("gh pr create --fill")
        c = t.calls()
        self.assertEqual(P.check_no_edits_before_approval(c, TOKENS, {"pause_at_plan": True})["status"], "pass")
        self.assertEqual(P.check_pr_after_approval(c)["status"], "pass")

    def test_nudge_before_pre_pr_pause_does_not_approve_pr(self):
        t = bootstrap()
        t.events += [{"type": "result"}, {"type": "system", "subtype": "init"}]
        t.bash("gh pr create --fill")
        self.assertEqual(P.check_pr_after_approval(t.calls())["status"], "fail")

    def test_pr_before_gate(self):
        c = bootstrap().bash("gh pr create --fill").calls()
        self.assertEqual(P.check_pr_after_approval(c)["status"], "fail")

    def test_bulk_staging(self):
        for cmd in ("git add -A && git commit -m x", "git add . ; git commit -m x", "git commit -am x", "git add --all"):
            with self.subTest(cmd=cmd):
                self.assertEqual(P.check_staging(Trace().bash(cmd).calls())["status"], "fail")
        self.assertEqual(P.check_staging(Trace().bash("git add shop/cart.py ./tests/test_cart.py").calls())["status"], "pass")

    def test_valkyrie_commit(self):
        c = Trace().bash("git commit -m sneaky", parent="agent1").calls()
        self.assertIn("subagent ran", P.check_valkyries(c, TOKENS, {})["detail"])

    def test_valkyries_overlap_and_fan_out(self):
        c = (Trace()
             .tool("Edit", parent="a1", file_path="/r/shop/cart.py", old_string="a", new_string="b")
             .tool("Edit", parent="a2", file_path="/r/shop/cart.py", old_string="c", new_string="d")).calls()
        r = P.check_valkyries(c, TOKENS, {"parallel_ok": False})
        self.assertEqual(r["status"], "fail")
        self.assertIn("two subagents edited cart.py", r["detail"])
        self.assertIn("fanned out", r["detail"])

    def test_disjoint_valkyries_pass(self):
        c = (Trace()
             .tool("Edit", parent="a1", file_path="/r/shop/cart.py", old_string="a", new_string="b")
             .tool("Edit", parent="a2", file_path="/r/tests/test_cart.py", old_string="c", new_string="d")).calls()
        self.assertEqual(P.check_valkyries(c, TOKENS, {"parallel_ok": True})["status"], "pass")


class FrictionGateTest(unittest.TestCase):
    def state_file(self, ledger: dict) -> Path:
        d = Path(tempfile.mkdtemp()) / "odin-states"
        d.mkdir()
        f = d / "t.html"
        f.write_text('<html><body><script type="application/json" id="odin-iteration-ledger">'
                     + json.dumps(ledger) + "</script></body></html>")
        return f

    def test_incident_without_delta_fails(self):
        f = self.state_file({"iterations": [{"iteration": 1, "events": [{"kind": "incident", "text": "push failed"}]}]})
        self.assertEqual(P.check_friction_gate([f])["status"], "fail")

    def test_waiver_passes(self):
        f = self.state_file({"iterations": [{"iteration": 1, "incident_waiver": "one-off flake",
                                             "events": [{"kind": "test", "outcome": "fail", "text": "flaky"}]}]})
        self.assertEqual(P.check_friction_gate([f])["status"], "pass")


if __name__ == "__main__":
    unittest.main()
