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
        self.brain = Path(self.tmp.name) / "brain"
        self.brain.mkdir(parents=True)
        self.env = mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": self.tmp.name, "ANTIGRAVITY_BRAIN_DIR": str(self.brain)},
        )
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

    def test_antigravity_transcript_prompts_model_and_questions(self):
        sid = "22222222-3333-4444-5555-666666666666"
        logs_dir = self.brain / sid / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True)
        path = logs_dir / "transcript.jsonl"
        write_jsonl(path, [
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-10-29T10:00:00Z",
                "content": "<USER_SETTINGS_CHANGE>\nUser changed setting `Model Selection` from Auto to Claude Opus 4.6 (Thinking).\n</USER_SETTINGS_CHANGE>\n<USER_REQUEST>\nAlign Odin with both harnesses\n</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-10-29T10:00:05Z",
                "content": "Asking questions",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "ask_question",
                        "args": {"questions": [{"question": "Dual harness support?"}]},
                    }
                ],
            },
        ])
        self.assertEqual(odin_transcript.find_transcript(sid), str(path))
        self.assertEqual(odin_transcript.user_prompts(str(path)), ["Align Odin with both harnesses"])
        self.assertEqual(odin_transcript.last_model(str(path)), "Claude Opus 4.6 (Thinking)")
        self.assertEqual(odin_transcript.last_activity(str(path))["lastToolName"], "ask_question")
        q_state = odin_transcript.analyze_questions(str(path))
        self.assertTrue(q_state["awaitingHuman"])
        self.assertEqual(q_state["openQuestions"], ["Dual harness support?"])

        # Follow-up user input resolves the question
        write_jsonl(path, [
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-10-29T10:00:00Z",
                "content": "<USER_REQUEST>\nAlign Odin with both harnesses\n</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-10-29T10:00:05Z",
                "tool_calls": [{"id": "call_1", "name": "ask_question", "args": {"questions": [{"question": "Dual harness support?"}]}}],
            },
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-10-29T10:01:00Z",
                "content": "<USER_REQUEST>\nYes, support both\n</USER_REQUEST>",
            },
        ])
        q_resolved = odin_transcript.analyze_questions(str(path))
        self.assertFalse(q_resolved["awaitingHuman"])
        self.assertTrue(q_resolved["questionsResolved"])


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

    def test_antigravity_pre_tool_use_and_session_start(self):
        self.hook.configure({"label": "🤖 Gemini Generated 🦾", "heading": "## 🤖 Gemini Session Telemetry"})
        agy_payload = {
            "conversationId": "agy-conv-123",
            "transcriptPath": str(self.transcript),
            "modelName": "Claude Opus 4.6 (Thinking)",
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": 'gh pr create --title "Fix Step #2" --body "## Summary\n- x\n\n### Prompt Summary\n- fix it"'
                },
            },
        }
        out = self.hook.handle_pre_tool_use(agy_payload)
        self.assertEqual(out["decision"], "allow")
        new_cmd = out["overwrite"]["CommandLine"]
        self.assertIn("--label '🤖 Gemini Generated 🦾'", new_cmd)
        self.assertIn("Fix Step 2", new_cmd)
        self.assertIn("agy-conv-123", new_cmd)
        self.assertIn("## 🤖 Gemini Session Telemetry", new_cmd)

        # SessionStart hook outputs JSON injectSteps for Antigravity and plain text for Claude Code
        session_script = str(SCRIPTS / "odin-session-start.py")
        res_agy = subprocess.run(
            [sys.executable, session_script],
            input=json.dumps({"conversationId": "agy-conv-123", "workspacePaths": ["/tmp"]}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res_agy.returncode, 0)
        self.assertEqual(
            json.loads(res_agy.stdout),
            {"injectSteps": [{"systemMessage": {"systemMessage": "Odin session id: agy-conv-123"}}]},
        )
        res_claude = subprocess.run(
            [sys.executable, session_script],
            input=json.dumps({"session_id": "claude-sess-456"}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res_claude.returncode, 0)
        self.assertEqual(res_claude.stdout.strip(), "Odin session id: claude-sess-456")


class TestServerGeneric(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cfg_tmp = tempfile.TemporaryDirectory()
        with mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": cls._cfg_tmp.name, "ODIN_CONFIG": "", "ODIN_ROOT": cls._cfg_tmp.name},
        ):
            cls.server = load_module("odin_server_generic_test", "odin-server.py")

    @classmethod
    def tearDownClass(cls):
        cls._cfg_tmp.cleanup()

    def test_host_and_origin_guards(self):
        s = self.server
        for ok in ("127.0.0.1:8788", "localhost:8788", "odin.localhost:8788", "[::1]:8788", "192.168.1.20:8788", "studio.local:8788", ""):
            self.assertTrue(s.host_allowed(ok), ok)
        # DNS rebinding: an attacker's domain resolving to 127.0.0.1 is refused.
        self.assertFalse(s.host_allowed("evil.example.com:8788"))
        with mock.patch.dict(s.CONFIG["server"], {"allowed_hosts": ["odin.home.arpa", "odin.dev", "staging.odin.dev"]}):
            self.assertTrue(s.host_allowed("odin.home.arpa:8788"))
            self.assertTrue(s.host_allowed("odin.dev"))

        self.assertTrue(s.origin_allowed(None, "127.0.0.1:8788"))
        self.assertTrue(s.origin_allowed("https://192.168.1.20:8788", "192.168.1.20:8788"))
        self.assertTrue(s.origin_allowed("https://odin.dev", "odin.dev"))
        self.assertFalse(s.origin_allowed("https://evil.example.com", "127.0.0.1:8788"))
        self.assertFalse(s.origin_allowed("null", "127.0.0.1:8788"))

    def test_localize_links_and_base_url(self):
        s = self.server
        live, staging = s.PORT, s.STAGING_PORT
        text = f"http://127.0.0.1:{live}/a.html http://127.0.0.1:{staging}/ http://127.0.0.1:3000/"
        out = s.localize_links(text, f"192.168.1.20:{live}")
        self.assertIn(f"http://192.168.1.20:{live}/a.html", out)
        self.assertIn(f"http://192.168.1.20:{staging}/", out)
        self.assertIn("http://127.0.0.1:3000/", out)  # dev-app ports are left alone
        with mock.patch.dict(s.CONFIG["server"], {"allowed_hosts": ["odin.dev", "staging.odin.dev"]}):
            rp_out = s.localize_links(text, "odin.dev", "https")
            self.assertIn("https://odin.dev/a.html", rp_out)
            self.assertIn("https://staging.odin.dev/", rp_out)
            self.assertNotIn(f":{live}", rp_out)
            self.assertIn("http://127.0.0.1:3000/", rp_out)
        with mock.patch.dict(s.CONFIG["server"], {"cert": "/c.pem", "key": "/k.pem"}):
            self.assertIn(f"https://192.168.1.20:{live}/", s.localize_links(f"http://127.0.0.1:{live}/", f"192.168.1.20:{live}"))
            self.assertEqual(odin_config.base_url(s.CONFIG, 9000), "https://127.0.0.1:9000")
        self.assertEqual(odin_config.base_url(s.CONFIG, 9000), "http://127.0.0.1:9000")

    def test_tls_server_and_csrf_over_https(self):
        import shutil
        import socket
        import ssl
        import time
        import urllib.error
        import urllib.request

        if not shutil.which("openssl"):
            self.skipTest("openssl not available")
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=localhost", "-keyout", key, "-out", cert],
                check=True, capture_output=True,
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = dict(os.environ, XDG_CONFIG_HOME=tmp, ODIN_CONFIG="")
            proc = subprocess.Popen(
                [sys.executable, str(SCRIPTS / "odin-server.py"), "--port", str(port), "--dir", tmp,
                 "--cert", cert, "--key", key],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            )
            ctx = ssl.create_default_context(cafile=cert)
            ctx.check_hostname = False

            def call(path, data=None, headers=None):
                req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", data=data, headers=headers or {})
                try:
                    with urllib.request.urlopen(req, timeout=1, context=ctx) as resp:
                        return resp.status, resp.headers
                except urllib.error.HTTPError as err:
                    return err.code, err.headers

            try:
                status = None
                for _ in range(40):
                    try:
                        status, headers = call("/api/projects")
                        break
                    except Exception:
                        time.sleep(0.1)
                self.assertEqual(status, 200)
                self.assertIsNone(headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(call("/api/projects", headers={"Host": "evil.example.com"})[0], 403)
                body = b'{"scope": "*"}'
                self.assertEqual(call("/api/presence", body, {"Origin": "https://evil.example.com"})[0], 403)
                self.assertEqual(call("/api/presence", body, {"Origin": "null"})[0], 403)
                self.assertEqual(call("/api/presence", body, {"Origin": f"https://127.0.0.1:{port}"})[0], 200)
            finally:
                proc.terminate()
                proc.wait(timeout=3)

    def test_go_url_and_presence_coverage(self):
        root = self.server.ROOT_DIR
        state = os.path.join(root, "repo", "odin-states", "my task.html")
        other = os.path.join(root, "repo", "odin-states", "other.html")
        self.assertEqual(self.server.state_file_url_path(state), "/repo/odin-states/my%20task.html")
        self.assertEqual(self.server.state_file_url_path("/elsewhere/x.html"), "/")

        self.server._PRESENCE.clear()
        self.assertFalse(self.server.presence_covers(state, now=100.0))
        # A state page pings with its (browser-encoded) pathname and covers only its own task.
        self.server.record_presence("/repo/odin-states/my%20task.html", now=100.0)
        self.assertTrue(self.server.presence_covers(state, now=110.0))
        self.assertFalse(self.server.presence_covers(other, now=110.0))
        self.assertFalse(self.server.presence_covers(state, now=100.0 + self.server.PRESENCE_TTL + 1))
        # The dashboard covers every task; files outside the root never match a page scope.
        self.server.record_presence("*", now=200.0)
        self.assertTrue(self.server.presence_covers(other, now=205.0))
        self.assertTrue(self.server.presence_covers("/elsewhere/x.html", now=205.0))
        self.server._PRESENCE.clear()

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
