#!/usr/bin/env python3
"""Tests for the progress timeline: `log`, `set-env`, update-phase keeping the
log, notify-on-change, the state-file lock, and the automatic progress hook."""

import argparse
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ODIN_ROOT = Path(__file__).resolve().parent.parent / "skills" / "odin"
STATE_SCRIPT = ODIN_ROOT / "scripts" / "odin-state.py"
HOOK_SCRIPT = ODIN_ROOT / "scripts" / "odin-progress-hook.py"
TEMPLATE = ODIN_ROOT / "references" / "state-template.html"
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def section(content: str, sec_id: str) -> str:
    return re.search(rf'<section[^>]*id="{sec_id}"[\s\S]*?</section>', content).group(0)


def log_texts(content: str, sec_id: str) -> list[tuple[str, str]]:
    return [
        (kind, html.unescape(text))
        for kind, text in re.findall(
            r'<li data-kind="([^"]*)"[^>]*>[\s\S]*?<span class="odin-log-text">([^<]*)</span>', section(content, sec_id)
        )
    ]


class ProgressBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.odin_state = load_module("odin_state_progress", STATE_SCRIPT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.states_dir = self.root / "proj" / "odin-states"
        self.states_dir.mkdir(parents=True)
        self.repo = self.root / "proj"
        content = (
            TEMPLATE.read_text(encoding="utf-8")
            .replace("<Task Name>", "Progress Test")
            .replace("<task-slug>", "progress-test")
            .replace("<absolute-worktree-path>", str(self.repo))
            .replace("<session-id>", SESSION_ID)
        )
        self.path = self.states_dir / "progress-test.html"
        self.path.write_text(content, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> None:
        args = self.odin_state.build_parser().parse_args(list(argv))
        with mock.patch.object(self.odin_state, "trigger_notify"):
            self.assertEqual(self.odin_state.run_command(args), 0)

    def content(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def ledger_events(self) -> list[dict]:
        ledger = self.odin_state.extract_ledger(self.content(), str(self.path))
        return [e for it in ledger["iterations"] for e in it.get("events", [])]


class TestProgressLog(ProgressBase):
    def test_log_entries_survive_update_phase_and_summary_keeps_body(self):
        self.run_cli("update-phase", "--file", str(self.path), "--phase", "thor", "--status", "active", "--html", "<p>rich body</p>")
        self.run_cli("log", "--file", str(self.path), "--phase", "thor", "--kind", "commit", "--text", "abc123 first")
        self.run_cli("log", "--file", str(self.path), "--phase", "thor", "--kind", "finding", "--text", "radial glow missing")
        # A summary-only update must not wipe the richer body or the timeline.
        self.run_cli("update-phase", "--file", str(self.path), "--phase", "thor", "--status", "active", "--summary", "still going")
        thor = section(self.content(), "thor-execution")
        self.assertIn("rich body", thor)
        self.assertNotIn("<p>still going</p>", thor)
        # Replacing the body keeps the timeline too.
        self.run_cli("update-phase", "--file", str(self.path), "--phase", "thor", "--status", "done", "--html", "<p>final</p>")
        content = self.content()
        self.assertIn("final", section(content, "thor-execution"))
        self.assertNotIn("rich body", section(content, "thor-execution"))
        self.assertEqual(
            [k for k, _ in log_texts(content, "thor-execution")], ["phase", "commit", "finding", "phase"]
        )
        self.assertEqual(content.count('class="odin-progress-log"'), 1)
        self.assertIn('id="odin-progress-log-styles"', content)

    def test_log_creates_card_records_ledger_and_skips_duplicates(self):
        self.run_cli("log", "--file", str(self.path), "--phase", "thor", "--kind", "test", "--outcome", "pass", "--text", "30/30")
        self.run_cli("log", "--file", str(self.path), "--phase", "thor", "--kind", "test", "--outcome", "pass", "--text", "30/30")
        self.run_cli("log", "--file", str(self.path), "--phase", "thor", "--kind", "test", "--text", "31/31", "--detail", "line1\nline2")
        content = self.content()
        self.assertEqual(log_texts(content, "thor-execution"), [("test", "30/30"), ("test", "31/31")])
        self.assertIn('data-outcome="pass"', content)
        self.assertIn("line1\nline2", content)
        self.assertEqual([e["text"] for e in self.ledger_events()], ["30/30", "31/31"])

    def test_log_defaults_to_the_current_phase(self):
        self.run_cli("update-phase", "--file", str(self.path), "--phase", "heimdall", "--status", "awaiting_human", "--summary", "gate")
        self.run_cli("log", "--file", str(self.path), "--kind", "decision", "--text", "PR paused")
        self.assertIn(("decision", "PR paused"), log_texts(self.content(), "heimdall-validation"))

    def test_update_phase_notifies_and_logs_only_on_status_change(self):
        def update(status: str, summary: str) -> int:
            args = self.odin_state.build_parser().parse_args(
                ["update-phase", "--file", str(self.path), "--phase", "thor", "--status", status, "--summary", summary]
            )
            with mock.patch.object(self.odin_state, "trigger_notify") as notify:
                self.odin_state.run_command(args)
            return notify.call_count

        self.assertEqual(update("active", "start"), 1)
        self.assertEqual(update("active", "progress"), 0)
        self.assertEqual(update("done", "finished"), 1)
        phases = [t for k, t in log_texts(self.content(), "thor-execution") if k == "phase"]
        self.assertEqual(len(phases), 2)
        self.assertTrue(phases[1].endswith("→ done: finished"))

    def test_step_back_logs_the_new_iteration(self):
        self.run_cli("update-phase", "--file", str(self.path), "--phase", "thor", "--status", "done", "--summary", "it1")
        self.run_cli("step-back", "--file", str(self.path), "--reason", "copy tweaks", "--root-cause", "missed spec")
        events = self.ledger_events()
        self.assertTrue(any(e["kind"] == "phase" and "Iteration 2 started" in e["text"] for e in events))

    def test_set_env_restamps_body_and_env_bar(self):
        self.run_cli(
            "set-env", "--file", str(self.path), "--branch", "feat/moved", "--worktree", "/tmp/wt/moved",
            "--app-port", "3100", "--reason", "dev server conflict",
        )
        content = self.content()
        body = re.search(r"<body\b[^>]*>", content).group(0)
        self.assertIn('data-branch="feat/moved"', body)
        self.assertIn('data-worktree="/tmp/wt/moved"', body)
        self.assertIn('data-app-port="3100"', body)
        self.assertIn(">🌿 feat/moved<", content)
        self.assertIn(">🗂️ moved<", content)
        self.assertIn(">🌐 app :3100<", content)
        self.assertTrue(any(e["kind"] == "env" and "dev server conflict" in e["text"] for e in self.ledger_events()))

    def test_concurrent_log_writers_do_not_lose_entries(self):
        procs = [
            subprocess.Popen(
                [sys.executable, str(STATE_SCRIPT), "log", "--file", str(self.path), "--phase", "thor", "--text", f"entry {i}"],
                stdout=subprocess.DEVNULL,
            )
            for i in range(8)
        ]
        for p in procs:
            self.assertEqual(p.wait(), 0)
        texts = {t for _, t in log_texts(self.content(), "thor-execution")}
        self.assertEqual(texts, {f"entry {i}" for i in range(8)})
        self.assertEqual(len(self.ledger_events()), 8)


class TestProgressHook(ProgressBase):
    """Drives odin-progress-hook.py as Claude Code would: JSON on stdin."""

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "Add the thing"],
            check=True,
        )
        self.run_cli("update-phase", "--file", str(self.path), "--phase", "thor", "--status", "active", "--summary", "go")

    def hook(self, payload: dict) -> None:
        env = {**os.environ, "ODIN_ROOT": str(self.root)}
        base = {"session_id": SESSION_ID, "cwd": str(self.repo)}
        res = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)], input=json.dumps({**base, **payload}), text=True, capture_output=True, env=env
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")

    def bash(self, command: str, event: str = "PostToolUse", **extra) -> None:
        self.hook({"hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": command}, **extra})

    def test_commits_are_logged_once_by_sha(self):
        self.bash('git commit -m "Add the thing"', tool_response={"stdout": ""})
        self.bash('git commit -m "Add the thing"', tool_response={"stdout": ""})
        commits = [e for e in self.ledger_events() if e["kind"] == "commit"]
        self.assertEqual(len(commits), 1)
        self.assertTrue(commits[0]["text"].endswith(" Add the thing"))

    def test_check_commands_log_pass_and_fail(self):
        self.bash("cd /x && npm test", tool_response={"stdout": "a\nb\n  107 passed (40s)"})
        self.bash("npx tsc --noEmit", event="PostToolUseFailure", error="Exit code 2\nsrc/a.ts(1,1): error TS2322")
        self.bash("ls -la", tool_response={"stdout": ""})
        self.bash('python3 odin-state.py log --text "npm test passed"', tool_response={"stdout": ""})
        tests = [e for e in self.ledger_events() if e["kind"] == "test"]
        self.assertEqual([(e["outcome"], e["text"]) for e in tests], [("pass", "✅ npm test"), ("fail", "❌ npx tsc --noEmit")])
        self.assertIn("107 passed", tests[0]["detail"])
        self.assertIn("TS2322", tests[1]["detail"])

    def test_subagent_dispatch_and_return_are_paired(self):
        self.hook({
            "hook_event_name": "PostToolUse", "tool_name": "Agent",
            "tool_input": {"description": "Kara: type components", "subagent_type": "general-purpose"},
            "tool_response": {"status": "async_launched", "agentId": "a1"},
        })
        self.hook({"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "general-purpose", "last_assistant_message": "8 files clean"})
        self.hook({"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "general-purpose", "last_assistant_message": "again"})
        texts = [e["text"] for e in self.ledger_events() if e["kind"] == "valkyrie"]
        self.assertEqual(texts, ["Dispatched Kara: type components (general-purpose)", "Kara: type components (general-purpose) returned"])

    def test_ignores_other_sessions_inactive_tasks_and_bad_input(self):
        self.hook({"hook_event_name": "SubagentStop", "session_id": "someone-else", "agent_id": "x"})
        env = {**os.environ, "ODIN_ROOT": str(self.root)}
        res = subprocess.run([sys.executable, str(HOOK_SCRIPT)], input="not json", text=True, capture_output=True, env=env)
        self.assertEqual((res.returncode, res.stdout), (0, ""))
        self.run_cli("update-phase", "--file", str(self.path), "--phase", "human", "--status", "done", "--summary", "signed off")
        content = self.content()
        for sec in ("huginn-plan", "thor-execution", "heimdall-validation", "hlidskjalf-judge"):
            content = re.sub(rf'(<section[^>]*id="{sec}"[^>]*data-status=")[^"]*', r"\1done", content)
        self.path.write_text(content, encoding="utf-8")
        before = len(self.ledger_events())
        self.bash("npm test", tool_response={"stdout": "ok"})
        self.assertEqual(len(self.ledger_events()), before)


if __name__ == "__main__":
    unittest.main()
