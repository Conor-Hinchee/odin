"""Tests for the Phase 4 friction scan (odin_friction.py) and its Hlidskjalf gate."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "odin" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import odin_friction as F  # noqa: E402

WAKE = 'ODIN REACTIVE PARTNER WAKEUP EVENT\n{"decision": "approve_huginn", "notes": ""}'


def state_html(heimdall="active", pr_url="", worktree=""):
    pr = f' data-pr-url="{pr_url}"' if pr_url else ""
    wt = f' data-worktree="{worktree}"' if worktree else ""
    return f"""<!DOCTYPE html><html><body{wt}>
<section id="huginn-plan" data-status="done"><div class="phase-body"></div></section>
<section id="thor-execution" data-status="done"><div class="phase-body"></div></section>
<section id="heimdall-validation" data-status="{heimdall}"{pr}><div class="phase-body"></div></section>
<section id="hlidskjalf-judge" data-status="active"><div class="phase-body"></div></section>
<section id="human-validation" data-status="pending"><div class="phase-body"></div></section>
</body></html>"""


def tool(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def bash(cmd):
    return tool("Bash", command=cmd)


def result(text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}


def end_turn_session():  # Claude Code session JSONL: the turn-ending assistant message
    return {"type": "assistant", "uuid": "u1", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}}


def end_turn_stream():  # claude -p stream-json
    return {"type": "result", "subtype": "success"}


class FrictionScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "odin-states").mkdir()
        (self.root / "app.py").write_text("x = 1\n")
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "a.py").write_text("y = 2\n")
        self.state = self.root / "odin-states" / "t.html"

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, events, html=None):
        self.state.write_text(html or state_html())
        tr = self.root / "transcript.jsonl"
        tr.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return sorted(f["id"] for f in F.scan(self.state.read_text(), self.state, tr))

    # missing-pr
    def test_missing_pr_when_heimdall_done_without_url(self):
        self.assertEqual([f["id"] for f in F.scan(state_html("done"))], ["missing-pr"])

    def test_no_missing_pr_when_the_human_waived_it(self):
        html = state_html("done").replace("<body>", '<body data-pr-waived="land on main">')
        self.assertEqual(F.scan(html), [])
        self.assertEqual([f["id"] for f in F.scan(state_html("done").replace("<body>", '<body data-pr-waived="">'))],
                         ["missing-pr"], "an empty waiver isn't one")

    def test_no_missing_pr_with_url_or_before_heimdall_done(self):
        self.assertEqual(F.scan(state_html("done", "https://github.com/o/r/pull/1")), [])
        self.assertEqual(F.scan(state_html("awaiting_human")), [])

    # gate-ignored
    def test_gate_ignored_in_both_transcript_formats(self):
        for end in (end_turn_session, end_turn_stream):
            with self.subTest(end=end.__name__):
                self.assertIn("gate-ignored", self.scan([result(WAKE), end()]))

    def test_gate_acted_on_is_not_ignored(self):
        events = [result(WAKE), bash('O=/x/odin-state.py; python3 "$O" update-phase --phase huginn --status done'),
                  end_turn_stream()]
        self.assertNotIn("gate-ignored", self.scan(events))

    # gate-unwatched
    def test_gate_unwatched_when_pause_has_no_watcher(self):
        events = [bash("python3 odin-state.py update-phase --phase huginn --status awaiting_human"), end_turn_session()]
        self.assertEqual(self.scan(events), ["gate-unwatched"])

    def test_gate_watched_is_clean(self):
        events = [bash("python3 odin-state.py update-phase --phase huginn --status awaiting_human"),
                  bash("python3 odin-state.py wait-for-human --phase huginn-plan"), end_turn_session()]
        self.assertEqual(self.scan(events), [])

    # state-file-late
    def test_state_file_late_when_project_read_first(self):
        events = [tool("Read", file_path=str(self.root / "app.py")), bash("cat pkg/*.py"),
                  tool("Write", file_path=str(self.state), content="<html>")]
        found = F.scan(self.scan_html(events), self.state, self.root / "transcript.jsonl")
        late = [f for f in found if f["id"] == "state-file-late"]
        self.assertEqual(len(late), 1)
        self.assertIn("app.py", late[0]["evidence"])
        self.assertIn("pkg/*.py", late[0]["evidence"])

    def scan_html(self, events):
        self.scan(events)
        return self.state.read_text()

    def test_state_file_first_is_clean(self):
        events = [bash("git status; ls"), bash('S=t; cat > "odin-states/$S.html" <<EOF\nx\nEOF'),
                  tool("Read", file_path=str(self.root / "app.py"))]
        self.assertEqual(self.scan(events), [])

    def test_only_events_before_the_retrospective_count(self):
        events = [bash("python3 odin-state.py update-phase --phase hlidskjalf --status active"), result(WAKE),
                  end_turn_stream()]
        self.assertEqual(self.scan(events), [])

    def test_earlier_tasks_in_the_same_session_are_ignored(self):
        other = str(self.root / "odin-states" / "other.html")
        events = [tool("Read", file_path=str(self.root / "app.py")), tool("Write", file_path=other, content="x"),
                  result(WAKE), bash("python3 odin-state.py update-phase --phase hlidskjalf --status done"),
                  end_turn_session(),
                  tool("Write", file_path=str(self.state), content="<html>"), tool("Read", file_path=str(self.root / "app.py"))]
        self.assertEqual(self.scan(events), [])

    def test_find_transcript(self):
        proj = self.root / "projects" / "-some-project"
        proj.mkdir(parents=True)
        (proj / "abc-123.jsonl").write_text("{}\n")
        self.assertEqual(F.find_transcript("abc-123", str(self.root / "projects")), proj / "abc-123.jsonl")
        self.assertIsNone(F.find_transcript("../etc", str(self.root / "projects")))
        self.assertIsNone(F.find_transcript("missing", str(self.root / "projects")))


class FrictionGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = tempfile.TemporaryDirectory()
        old = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "ODIN_CONFIG")}
        os.environ["XDG_CONFIG_HOME"], os.environ["ODIN_CONFIG"] = cls.cfg.name, ""
        try:
            spec = importlib.util.spec_from_file_location("odin_state_friction", SCRIPTS / "odin-state.py")
            cls.S = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.S)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    @classmethod
    def tearDownClass(cls):
        cls.cfg.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "odin-states").mkdir()
        self.state = root / "odin-states" / "t.html"
        self.state.write_text(state_html("done"))
        self.transcript = root / "transcript.jsonl"
        self.transcript.write_text(json.dumps(result(WAKE)) + "\n" + json.dumps(end_turn_stream()) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        return self.S.run_command(self.S.build_parser().parse_args(list(argv)))

    def finish(self, *extra):
        return self.run_cli("update-phase", "--file", str(self.state), "--phase", "hlidskjalf", "--status", "done",
                            "--summary", "retro", "--transcript", str(self.transcript), *extra)

    def test_gate_blocks_until_every_finding_is_named(self):
        self.assertEqual(self.finish(), 1)
        self.run_cli("propose-delta", "--file", str(self.state), "--id", "delta-t-01", "--category", "rule",
                     "--target-path", "skills/odin/SKILL.md",
                     "--failure-pattern", "gate-ignored: a dashboard approval was not acted on",
                     "--proposed-diff", "Treat the wakeup's decision as the human's answer.")
        self.assertEqual(self.finish(), 1, "missing-pr is still unaddressed")
        self.assertEqual(self.finish("--incident-waiver", "missing-pr: the human waived the PR at the gate"), 0)

    def test_pr_waived_stamps_logs_and_clears_missing_pr(self):
        self.transcript.write_text("")
        self.state.write_text(state_html("awaiting_human"))
        self.assertEqual(self.run_cli("update-phase", "--file", str(self.state), "--phase", "heimdall", "--status", "done",
                                      "--pr-waived", "  "), 1)
        self.assertEqual(self.run_cli("update-phase", "--file", str(self.state), "--phase", "heimdall", "--status", "done",
                                      "--pr-waived", "x", "--pr-url", "https://github.com/o/r/pull/1"), 1)
        self.assertEqual(self.run_cli("update-phase", "--file", str(self.state), "--phase", "heimdall", "--status", "done",
                                      "--pr-waived", "Human chose to land on main"), 0)
        html = self.state.read_text()
        self.assertIn('data-pr-waived="Human chose to land on main"', html)
        ledger = self.S.extract_ledger(html, str(self.state))
        self.assertEqual(ledger["pr_waived"], "Human chose to land on main")
        events = [e for it in ledger["iterations"] for e in it.get("events", [])]
        self.assertTrue(any(e.get("text") == "PR waived by the human: Human chose to land on main" for e in events))
        self.assertEqual(self.finish(), 0, "no manual waiver needed")

    def test_friction_scan_command_lists_findings(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.run_cli("friction-scan", "--file", str(self.state), "--transcript", str(self.transcript), "--json")
        self.assertEqual(rc, 0)
        ids = sorted(f["id"] for f in json.loads(buf.getvalue())["findings"])
        self.assertEqual(ids, ["gate-ignored", "missing-pr"])


if __name__ == "__main__":
    unittest.main()
