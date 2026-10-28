#!/usr/bin/env python3
"""Tests for the Claude Code port: config loading, transcript parsing, the PR hook,
generic telemetry, body env attributes, and config-driven isolation."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "odin" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import odin_config  # noqa: E402
import odin_transcript  # noqa: E402


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_jsonl(path: Path, rows: list) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


USER = lambda text, ts="2026-10-28T10:00:00Z": {  # noqa: E731
    "type": "user", "timestamp": ts, "origin": {"kind": "human"},
    "message": {"role": "user", "content": text},
}
ASSISTANT = lambda blocks, ts="2026-10-28T10:00:05Z": {  # noqa: E731
    "type": "assistant", "timestamp": ts,
    "message": {"role": "assistant", "model": "claude-opus-5-5", "content": blocks},
}
TOOL_RESULT = lambda tool_id, ts="2026-10-28T10:01:00Z": {  # noqa: E731
    "type": "user", "timestamp": ts,
    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]},
}


class TestConfig(unittest.TestCase):
    def test_merge_order_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            home_cfg = Path(tmp) / "xdg" / "odin" / "config.json"
            home_cfg.parent.mkdir(parents=True)
            home_cfg.write_text(json.dumps({"ports": {"live": 9100}, "skill_pills": []}), encoding="utf-8")
            ws = Path(tmp) / "ws"
            (ws / ".odin").mkdir(parents=True)
            (ws / ".odin" / "config.json").write_text(json.dumps({"ports": {"staging": 9101}}), encoding="utf-8")
            explicit = Path(tmp) / "explicit.json"
            explicit.write_text(json.dumps({"repos": {"web": {"path": "~/web"}}}), encoding="utf-8")
            env = {"XDG_CONFIG_HOME": str(Path(tmp) / "xdg"), "ODIN_CONFIG": str(explicit), "ODIN_ROOT": ""}
            with mock.patch.dict(os.environ, env):
                cfg = odin_config.load_config(str(ws))
            self.assertEqual(cfg["ports"], {"live": 9100, "staging": 9101})
            self.assertEqual(cfg["skill_pills"], [])
            self.assertEqual(cfg["root"], str(ws))
            self.assertEqual(cfg["repos"]["web"]["path"], os.path.expanduser("~/web"))
            with mock.patch.dict(os.environ, {**env, "ODIN_PORT": "9200"}):
                self.assertEqual(odin_config.load_config(str(ws))["ports"]["live"], 9200)

    def test_staging_unavailable_outside_git_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_CONFIG_HOME": tmp, "ODIN_PRIMARY_REPO_DIR": tmp}
            with mock.patch.dict(os.environ, env):
                cfg = odin_config.load_config(tmp)
            self.assertFalse(cfg["staging"]["available"])
            self.assertEqual(cfg["staging"]["staging_dir"], tmp.rstrip("/") + "-staging")

    def test_render_ports(self):
        cfg = {"ports": {"live": 9000, "staging": 9001}}
        self.assertEqual(
            odin_config.render_ports("http://127.0.0.1:8788/ and :8789", cfg),
            "http://127.0.0.1:9000/ and :9001",
        )
        default = {"ports": {"live": 8788, "staging": 8789}}
        self.assertEqual(odin_config.render_ports(":8788", default), ":8788")


class TestTranscript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects" / "-tmp-proj"
        self.projects.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name})
        self.env.start()
        odin_transcript._PATH_CACHE.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_prompts_model_and_find(self):
        sid = "11111111-2222-3333-4444-555555555555"
        path = self.projects / f"{sid}.jsonl"
        write_jsonl(path, [
            USER("Build the checkout form"),
            ASSISTANT([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
            TOOL_RESULT("t1"),
            {**USER("hook text"), "origin": {"kind": "hook"}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
            USER("Now add tests"),
        ])
        self.assertEqual(odin_transcript.find_transcript(sid), str(path))
        self.assertEqual(odin_transcript.user_prompts(str(path)), ["Build the checkout form", "Now add tests"])
        self.assertEqual(odin_transcript.last_model(str(path)), "claude-opus-5-5")
        self.assertEqual(odin_transcript.last_activity(str(path))["lastToolName"], "Bash")
        self.assertEqual(odin_transcript.find_transcript("../etc/passwd"), "")

    def test_ask_user_question_pending_then_answered(self):
        path = self.projects / "q.jsonl"
        ask = ASSISTANT([{
            "type": "tool_use", "id": "ask1", "name": "AskUserQuestion",
            "input": {"questions": [{"question": "Which database?", "header": "DB", "options": []}]},
        }])
        write_jsonl(path, [USER("start"), ask])
        pending = odin_transcript.analyze_questions(str(path))
        self.assertTrue(pending["awaitingHuman"])
        self.assertEqual(pending["openQuestions"], ["Which database?"])

        write_jsonl(path, [USER("start"), ask, TOOL_RESULT("ask1")])
        answered = odin_transcript.analyze_questions(str(path))
        self.assertFalse(answered["awaitingHuman"])
        self.assertTrue(answered["questionsResolved"])

    def test_text_open_questions_cleared_by_prompt(self):
        path = self.projects / "t.jsonl"
        text = "## Open Questions\n1. **Use Postgres or SQLite?**\n2. **Keep the old API?**"
        write_jsonl(path, [USER("plan it"), ASSISTANT([{"type": "text", "text": text}])])
        res = odin_transcript.analyze_questions(str(path))
        self.assertTrue(res["awaitingHuman"])
        self.assertEqual(res["openQuestions"], ["Use Postgres or SQLite?", "Keep the old API?"])
        write_jsonl(path, [USER("plan it"), ASSISTANT([{"type": "text", "text": text}]), USER("Postgres, yes")])
        self.assertFalse(odin_transcript.analyze_questions(str(path))["awaitingHuman"])


class TestPrHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hook = load_module("odin_pr_hook_test", "odin-pr-hook.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self.tmp.name) / "s.jsonl"
        write_jsonl(self.transcript, [USER("Fix Rule #3 handling"), ASSISTANT([{"type": "text", "text": "ok"}])])

    def tearDown(self):
        self.tmp.cleanup()
        self.hook.configure(None)

    def payload(self, command):
        return {
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "Open PR"},
            "session_id": "abc-session",
            "transcript_path": str(self.transcript),
        }

    def test_noop_without_pr_config(self):
        self.hook.configure(None)
        self.assertIsNone(self.hook.handle_pre_tool_use(self.payload('gh pr create --title t --body "x"')))

    def test_denies_missing_prompt_summary(self):
        self.hook.configure({"label": "ai-generated"})
        out = self.hook.handle_pre_tool_use(self.payload('gh pr create --title t --body "no summary"'))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("Prompt Summary", hso["permissionDecisionReason"])

    def test_enriches_and_asks(self):
        self.hook.configure({"label": "ai-generated"})
        cmd = 'gh pr create --title "Fix Step #2" --body "## Summary\n- x\n\n### Prompt Summary\n- fix it"'
        out = self.hook.handle_pre_tool_use(self.payload(cmd))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "ask")
        new_cmd = hso["updatedInput"]["command"]
        self.assertEqual(hso["updatedInput"]["description"], "Open PR")
        self.assertIn("--label ai-generated", new_cmd)
        self.assertIn("Fix Step 2", new_cmd)
        self.assertIn("abc-session", new_cmd)
        self.assertIn("Claude Code (claude-opus-5-5)", new_cmd)
        self.assertIn("Verbatim Session Prompts (1)", new_cmd)

    def test_cli_fast_path_and_output(self):
        script = str(SCRIPTS / "odin-pr-hook.py")
        res = subprocess.run(
            [sys.executable, script], input=json.dumps(self.payload("ls -la")),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")


class TestServerGeneric(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_module("odin_server_generic_test", "odin-server.py")

    def test_body_env_reads_and_writes_backend_port(self):
        env = self.server.extract_body_env('<body data-app-port="8081" data-backend-port="3005">', "/x/odin-states/a.html", "(root)")
        self.assertEqual(env["appPort"], 8081)
        self.assertEqual(env["backendPort"], 3005)
        html = '<body><main class="container"><nav class="bifrost-stepper"></nav></main></body>'
        out = self.server.set_body_env_attributes(html, "feat/x", "/tmp/repo", "8080", "3002")
        self.assertIn('data-backend-port="3002"', out)
        self.assertIn("Backend port:", out)

    def test_telemetry_hidden_then_rendered(self):
        original = self.server.CONFIG.get("telemetry")
        try:
            self.server.CONFIG["telemetry"] = None
            self.assertFalse(self.server.get_telemetry_snapshot()["configured"])
            self.server.CONFIG["telemetry"] = {
                "command": """echo '[{"name":"latest","app":"1.2.3"},{"name":"prod","app":"1.2.0"}]'""",
                "title": "Deploys",
            }
            data = self.server.refresh_telemetry(force=True)
            self.assertTrue(data["configured"])
            self.assertEqual(data["title"], "Deploys")
            self.assertEqual([r["name"] for r in data["environments"]], ["latest", "prod"])
        finally:
            self.server.CONFIG["telemetry"] = original

    def test_dev_port_ranges_follow_config(self):
        original = self.server.CONFIG.get("repos")
        try:
            self.server.CONFIG["repos"] = {"api": {"port_range": [4000, 4010]}}
            self.assertTrue(self.server.is_dev_port(4005))
            self.assertFalse(self.server.is_dev_port(8080))
            self.server.CONFIG["repos"] = {}
            self.assertTrue(self.server.is_dev_port(8080))
        finally:
            self.server.CONFIG["repos"] = original


class TestIsolation(unittest.TestCase):
    def test_allocates_primary_then_worktree_when_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "web"
            repo.mkdir()
            git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True)  # noqa: E731
            git("init", "-q", "-b", "main")
            git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
            cfg = Path(tmp) / "cfg.json"
            cfg.write_text(json.dumps({"repos": {"web": {
                "path": str(repo), "base_branch": "main", "port_range": [47110, 47119],
                "start": "PORT={port} npm run dev",
            }}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ODIN_CONFIG": str(cfg), "ODIN_ROOT": tmp, "XDG_CONFIG_HOME": tmp}):
                iso = load_module("odin_isolate_test", "odin-isolate.py")
                clean = iso.allocate_environment(["web"], "feat/x")
                self.assertEqual(clean["mode"], "primary")
                self.assertEqual(clean["appPort"], 47110)
                self.assertEqual(clean["commands"], [f"cd {repo} && PORT=47110 npm run dev"])

                (repo / "dirty.txt").write_text("x", encoding="utf-8")
                dirty = iso.allocate_environment(["web"], "feat/x")
                self.assertEqual(dirty["mode"], "worktree")
                self.assertEqual(dirty["worktree"], f"{repo}-{dirty['appPort']}")
                self.assertNotEqual(dirty["appPort"], 47110)
                self.assertTrue(dirty["hygieneWarnings"])


if __name__ == "__main__":
    unittest.main()
