#!/usr/bin/env python3
"""
Unit & Integration Tests for Odin:
- Reactive Partner Bridge (write_wakeup_event, wait-for-human, pause/resume)
- Phase 1 Huginn Co-Editable Partner Blueprint & Skill Pills
- Phase 3 Heimdall Pre-PR Code Review Gate
- Live Session Telemetry & :8789 Staging Snapshot / Promote
"""

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ODIN_ROOT = Path(__file__).resolve().parent.parent / "skills" / "odin"
STATE_SCRIPT = ODIN_ROOT / "scripts" / "odin-state.py"
SERVER_SCRIPT = ODIN_ROOT / "scripts" / "odin-server.py"

SAMPLE_STATE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Odin v3 Test State</title>
</head>
<body data-worktree="/tmp/example/odin" data-branch="feat/odin-v3-reactive-partner-staging">
  <header>
    <div>
      <h1>Odin v3 Reactive Partner Bridge</h1>
      <div class="meta-bar">Last Updated: <span id="last-updated">2026-10-20 15:00:00</span></div>
    </div>
    <div id="global-status" class="status-badge">AWAITING HUMAN</div>
  </header>
  <section id="huginn-plan" class="phase-card" data-status="awaiting_human">
    <div class="phase-header">
      <div class="phase-title">1. Huginn (Thought)</div>
      <div class="phase-status">awaiting_human</div>
    </div>
    <div class="phase-body">
      <ul class="checklist">
        <li class="done">Initialize Odin repo</li>
        <li>Verify reactive wakeup bridge</li>
      </ul>
    </div>
  </section>
  <section id="thor-execution" class="phase-card" data-status="pending">
    <div class="phase-header">
      <div class="phase-title">2. Thor (Thunder)</div>
      <div class="phase-status">pending</div>
    </div>
    <div class="phase-body"></div>
  </section>
  <section id="heimdall-validation" class="phase-card" data-status="pending">
    <div class="phase-header">
      <div class="phase-title">3. Heimdall (Vigilance)</div>
      <div class="phase-status">pending</div>
    </div>
    <div class="phase-body"></div>
  </section>
  <section id="muninn-memory" class="phase-card" data-status="pending">
    <div class="phase-header">
      <div class="phase-title">4. Muninn (Memory)</div>
      <div class="phase-status">pending</div>
    </div>
    <div class="phase-body"></div>
  </section>
  <section id="human-validation" class="phase-card" data-status="pending">
    <div class="phase-header">
      <div class="phase-title">5. Human Validation</div>
      <div class="phase-status">pending</div>
    </div>
    <div class="phase-body"></div>
  </section>
</body>
</html>
"""


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestOdin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.odin_state = load_module("odin_state_v3", STATE_SCRIPT)
        cls.odin_server = load_module("odin_server_v3", SERVER_SCRIPT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.states_dir = Path(self.tmp.name) / "odin-states"
        self.states_dir.mkdir(parents=True, exist_ok=True)
        self.sample_html_path = self.states_dir / "sample-v3-feature.html"
        self.sample_html_path.write_text(SAMPLE_STATE_HTML, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_top_banners_injects_telemetry_staging_and_huginn_partner_bar(self):
        content = self.sample_html_path.read_text(encoding="utf-8")
        content = content.replace(
            'id="heimdall-validation" class="phase-card" data-status="pending"',
            'id="heimdall-validation" class="phase-card" data-status="awaiting_human"',
        )
        ledger = self.odin_state.extract_ledger(content, str(self.sample_html_path))
        ledger["selected_skills"] = ["/code-review", "/chrome-devtools"]
        ledger["partner_notes"] = "Use dedicated worktree and run unit tests first."

        synced = self.odin_state.sync_top_banners_in_html(content, ledger)

        self.assertIn('id="odin-live-telemetry-bar"', synced)
        self.assertIn('id="odin-staging-dialog" closedby="any"', synced)
        self.assertNotIn("🚀 Promote Staging Fix", synced)
        self.assertIn("🚀 Promote to Live (:8788)", synced)
        self.assertIn('t.stagingLive && location.port !== "8789"', synced)
        self.assertIn('id="odin-huginn-partner-banner"', synced)
        self.assertIn('id="odin-heimdall-gate-banner"', synced)
        self.assertIn('data-skill="/code-review"', synced)
        self.assertIn("Use dedicated worktree and run unit tests first.", synced)
        self.assertIn('id="odin-top-banner-actions"', synced)

    def test_wakeup_event_and_wait_for_human_consume_existing(self):
        wakeup_file = self.odin_state.write_wakeup_event(
            self.sample_html_path,
            decision="approve_huginn",
            phase="huginn-plan",
            notes="Approved with /code-review skill",
            selected_skills=["/code-review"],
        )
        self.assertTrue(wakeup_file.exists())

        args = argparse.Namespace(
            html_file=str(self.sample_html_path),
            file=str(self.sample_html_path),
            phase="huginn-plan",
            session_id="test-session-1234",
            timeout=2.0,
            poll_interval=0.05,
            consume_existing=True,
        )
        rc = self.odin_state.cmd_wait_for_human(args)
        self.assertEqual(rc, 0)
        self.assertFalse(wakeup_file.exists(), "Wakeup file should be consumed and unlinked")

    def test_pause_and_resume_watcher_state(self):
        p1 = self.odin_state.set_pause_state(True, reason="Unit test pause", states_dir=self.states_dir)
        self.assertTrue(p1["paused"])
        self.assertTrue(self.odin_state.get_pause_state(self.states_dir)["paused"])

        p2 = self.odin_state.set_pause_state(False, reason="Unit test resume", states_dir=self.states_dir)
        self.assertFalse(p2["paused"])
        self.assertFalse(self.odin_state.get_pause_state(self.states_dir)["paused"])

    def test_snapshot_and_promote_staging_isolated(self):
        fake_repo = Path(self.tmp.name) / "odin-repo"
        fake_staging = Path(self.tmp.name) / "odin-staging"
        (fake_repo / "scripts").mkdir(parents=True)
        (fake_repo / "SKILL.md").write_text("# Odin Live v3\n", encoding="utf-8")
        (fake_repo / "scripts" / "odin-state.py").write_text("# state script v1\n", encoding="utf-8")

        snap_res = self.odin_state.snapshot_staging(
            active_state_rel="odin-states/sample-v3-feature.html",
            repo_dir=str(fake_repo),
            staging_dir=str(fake_staging),
            states_src_dir=str(self.states_dir),
            start_server=False,
        )
        self.assertTrue(snap_res["ok"])
        self.assertTrue(snap_res["pausedLive"])
        self.assertTrue((fake_staging / "odin-states" / "sample-v3-feature.html").exists())
        self.assertIn("8789", snap_res["stagingStateUrl"])
        self.assertIn("sample-v3-feature.html", snap_res["debugPrompt"])
        self.assertTrue(self.odin_state.get_pause_state(self.states_dir)["paused"])

        # Modify a file in staging and promote back to live repo
        (fake_staging / "SKILL.md").write_text("# Odin Staging Fix Promoted\n", encoding="utf-8")
        prom_res = self.odin_state.promote_staging(
            repo_dir=str(fake_repo),
            staging_dir=str(fake_staging),
            states_src_dir=str(self.states_dir),
        )
        self.assertTrue(prom_res["ok"])
        self.assertFalse(prom_res["pausedLive"])
        self.assertEqual((fake_repo / "SKILL.md").read_text(encoding="utf-8"), "# Odin Staging Fix Promoted\n")
        self.assertFalse(self.odin_state.get_pause_state(self.states_dir)["paused"])

    def test_snapshot_copies_subworkspace_state_and_excludes_staging_in_audit(self):
        fake_repo = Path(self.tmp.name) / "odin-repo"
        fake_staging = Path(self.tmp.name) / "odin-staging"
        (fake_repo / "scripts").mkdir(parents=True, exist_ok=True)
        (fake_repo / "SKILL.md").write_text("# Odin Live v3\n", encoding="utf-8")

        sub_states = Path(self.tmp.name) / "sub-workspace" / "odin-states"
        sub_states.mkdir(parents=True, exist_ok=True)
        sub_html = sub_states / "subworkspace-feature.html"
        sub_html.write_text(self.sample_html_path.read_text(encoding="utf-8"), encoding="utf-8")

        snap_res = self.odin_state.snapshot_staging(
            active_state_rel="sub-workspace/odin-states/subworkspace-feature.html",
            repo_dir=str(fake_repo),
            staging_dir=str(fake_staging),
            states_src_dir=str(self.states_dir),
            start_server=False,
        )
        self.assertTrue(snap_res["ok"])
        self.assertTrue((fake_staging / "odin-states" / "subworkspace-feature.html").exists())
        self.assertTrue(snap_res["stagingStateUrl"].endswith("/odin-states/subworkspace-feature.html"))

        # Verify run_mimir_audit on parent tmp dir excludes odin-staging duplicates
        audit_root = self.odin_state.run_mimir_audit(self.tmp.name)
        scanned_files = [p["state_file"] for p in audit_root["projects"]]
        self.assertFalse(any("/odin-staging/" in f.replace("\\", "/") for f in scanned_files))

    def test_odin_server_cli_port_and_dir_flags(self):
        import socket
        import subprocess
        import sys
        import time
        import urllib.request

        parsed = self.odin_server.parse_server_args(["--port", "8789", "--dir", self.tmp.name])
        self.assertEqual(parsed.port, 8789)
        self.assertEqual(parsed.root_dir, self.tmp.name)

        # Find an ephemeral free TCP port and verify odin-server.py binds to --port and serves --dir
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            ephemeral_port = s.getsockname()[1]

        proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT), "--port", str(ephemeral_port), "--dir", self.tmp.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Ignore the user's own config (e.g. TLS enabled) so the plain-HTTP probe below works.
            env=dict(os.environ, XDG_CONFIG_HOME=self.tmp.name, ODIN_CONFIG=""),
        )
        try:
            payload = None
            for _ in range(30):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{ephemeral_port}/api/projects", timeout=0.5) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                        break
                except Exception:
                    time.sleep(0.1)
            self.assertIsNotNone(payload, "Ephemeral staging server failed to respond on --port")
            self.assertTrue(payload["ok"])
            self.assertEqual(os.path.realpath(payload["rootDir"]), os.path.realpath(self.tmp.name))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

    def test_session_telemetry_reports_armed_and_paused(self):
        watcher_file = self.states_dir / ".odin-watcher-sample-v3-feature.json"
        watcher_file.write_text(
            json.dumps(
                {
                    "slug": "sample-v3-feature",
                    "pid": os.getpid(),
                    "phase": "huginn-plan",
                    "heartbeatAt": self.odin_state.now_iso(),
                }
            ),
            encoding="utf-8",
        )
        t_armed = self.odin_server.get_session_telemetry(
            str(self.sample_html_path),
            "sample-v3-feature.html",
            listeners={},
        )
        self.assertTrue(t_armed["watcherArmed"])
        self.assertEqual(t_armed["agentState"], "armed")

        self.odin_state.set_pause_state(True, states_dir=self.states_dir)
        t_paused = self.odin_server.get_session_telemetry(
            str(self.sample_html_path),
            "sample-v3-feature.html",
            listeners={8789: {"pid": 99999, "command": "Python"}},
        )
        self.assertTrue(t_paused["paused"])
        self.assertEqual(t_paused["agentState"], "paused")
        self.assertTrue(t_paused["stagingLive"])

    def test_light_mode_contrast_overhaul(self):
        dashboard_html = (ODIN_ROOT / "resources" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="color-scheme" content="light dark">', dashboard_html)
        self.assertIn('html[data-theme="light"] .open-questions-banner', dashboard_html)
        self.assertIn("color: #78350f;", dashboard_html)
        self.assertIn('html[data-theme="light"] .badge-awaiting_human', dashboard_html)
        self.assertIn("--gold: #b45309;", dashboard_html)

        skill_md = (ODIN_ROOT / "references" / "state-template.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="color-scheme" content="light dark">', skill_md)
        self.assertIn('html[data-theme="light"] .open-questions-box', skill_md)
        self.assertIn("--gold: #b45309;", skill_md)

        raw = self.sample_html_path.read_text(encoding="utf-8")
        ledger = self.odin_state.extract_ledger(raw, str(self.sample_html_path))
        synced_once = self.odin_state.sync_top_banners_in_html(raw, ledger)
        synced_twice = self.odin_state.sync_top_banners_in_html(synced_once, ledger)
        self.assertEqual(synced_twice.count('id="odin-light-theme-fixes"'), 1)
        self.assertIn('html[data-theme="light"] .open-questions-box', synced_twice)
        self.assertIn('id="odin-live-telemetry-bar" style="background:var(--surface-elevated);', synced_twice)

    def test_git_worktree_snapshot_and_promote_fast_forward(self):
        import subprocess

        git_repo = Path(self.tmp.name) / "git-odin"
        git_staging = Path(self.tmp.name) / "git-odin-staging"
        (git_repo / "resources").mkdir(parents=True, exist_ok=True)
        (git_repo / "scripts").mkdir(parents=True, exist_ok=True)
        (git_repo / ".gitignore").write_text("odin-states/\n__pycache__/\n", encoding="utf-8")
        (git_repo / "SKILL.md").write_text("# Odin Initial Main\n", encoding="utf-8")
        (git_repo / "resources" / "dashboard.html").write_text("<html>Dashboard v1</html>", encoding="utf-8")

        subprocess.run(["git", "init", "-b", "main"], cwd=str(git_repo), check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "initial"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )

        # Dirty working tree on main + archived state file with legacy :8788 link
        (git_repo / "SKILL.md").write_text("# Odin Dirty Main Working Tree\n", encoding="utf-8")
        archive_dir = self.states_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived_state = archive_dir / "archived-feature.html"
        archived_state.write_text(
            '<html><body><header class="odin-header"><a class="btn" href="http://127.0.0.1:8788/" style="text-decoration:none">🏛️ All Projects</a></header></body></html>',
            encoding="utf-8",
        )

        snap = self.odin_state.snapshot_staging(
            active_state_rel="odin-states/sample-v3-feature.html",
            repo_dir=str(git_repo),
            staging_dir=str(git_staging),
            states_src_dir=str(self.states_dir),
            start_server=False,
        )
        self.assertTrue(snap["ok"])
        self.assertTrue(snap["stagingCommit"])

        # Both main and staging worktree should be clean and at the same SHA
        st_main = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo), capture_output=True, text=True).stdout.strip()
        st_stag = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_staging), capture_output=True, text=True).stdout.strip()
        self.assertEqual(st_main, "")
        self.assertEqual(st_stag, "")
        sha_main = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(git_repo), capture_output=True, text=True).stdout.strip()
        sha_stag = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(git_staging), capture_output=True, text=True).stdout.strip()
        self.assertEqual(sha_main, sha_stag)

        # Archived state file and relative "/" link normalization
        mirrored_archived = git_staging / "odin-states" / "archive" / "archived-feature.html"
        self.assertTrue(mirrored_archived.is_file())
        self.assertIn('href="/"', mirrored_archived.read_text(encoding="utf-8"))

        # Edit code + sandbox state in staging, then promote
        (git_staging / "SKILL.md").write_text("# Odin Upgraded In Staging 8789\n", encoding="utf-8")
        (git_staging / "resources" / "dashboard.html").write_text("<html>Dashboard v2 Promoted</html>", encoding="utf-8")
        (git_staging / "odin-states" / "sample-v3-feature.html").write_text("<html>SANDBOX CLICK EDIT</html>", encoding="utf-8")

        prom = self.odin_state.promote_staging(
            repo_dir=str(git_repo),
            staging_dir=str(git_staging),
            states_src_dir=str(self.states_dir),
        )
        self.assertTrue(prom["ok"])
        self.assertTrue(prom["gitMerged"])
        self.assertEqual((git_repo / "SKILL.md").read_text(encoding="utf-8"), "# Odin Upgraded In Staging 8789\n")
        self.assertEqual((self.states_dir / "index.html").read_text(encoding="utf-8"), "<html>Dashboard v2 Promoted</html>")
        # Live state file must NOT be overwritten by staging sandbox edits
        self.assertNotIn("SANDBOX CLICK EDIT", self.sample_html_path.read_text(encoding="utf-8"))

        # Both main and staging worktree must end at the exact same promoted commit SHA with clean git status
        st_main_after = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo), capture_output=True, text=True).stdout.strip()
        st_stag_after = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_staging), capture_output=True, text=True).stdout.strip()
        self.assertEqual(st_main_after, "")
        self.assertEqual(st_stag_after, "")
        sha_main_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(git_repo), capture_output=True, text=True).stdout.strip()
        sha_stag_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(git_staging), capture_output=True, text=True).stdout.strip()
        self.assertEqual(sha_main_after, sha_stag_after)
        self.assertNotEqual(sha_main_after, sha_main)

    def test_staging_banner_and_relative_all_projects_navigation(self):
        dashboard_html = (ODIN_ROOT / "resources" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="staging-mode-banner"', dashboard_html)
        self.assertNotIn('id="promote-staging-btn"', dashboard_html)
        self.assertIn('id="banner-promote-staging-btn"', dashboard_html)
        self.assertIn('location.port === "8789"', dashboard_html)
        self.assertIn("runPromoteStaging(", dashboard_html)

        legacy_html = (
            '<!DOCTYPE html><html><head><title>Test</title></head><body>'
            '<header class="odin-header"><a class="btn" href="http://127.0.0.1:8788/" style="text-decoration:none">🏛️ All Projects</a></header>'
            '<main class="container"><section id="huginn-plan" data-status="active"><h2>Phase 1</h2><p>Plan</p></section></main>'
            '</body></html>'
        )
        ledger = self.odin_state.extract_ledger(legacy_html, str(self.sample_html_path))
        synced = self.odin_state.sync_top_banners_in_html(legacy_html, ledger)
        self.assertIn('href="/" onclick="if(location.protocol===\'file:\'){location.href=\'http://127.0.0.1:8788/\';return false;}"', synced)
        self.assertIn("odin-staging-sandbox-banner", synced)
        self.assertIn('data-odin-top-action="resnapshot-staging"', synced)

    def test_upgrade_poller_does_not_corrupt_diff_tables_and_theme_toggle_bound(self):
        html_with_diff = (
            '<!DOCTYPE html><html lang="en" data-theme="dark"><head><title>Diff Test</title></head><body>'
            '<header class="odin-header"><button id="odin-theme-btn">☀️ Theme</button></header>'
            '<main class="container">'
            '<section id="huginn-plan" data-status="done"><h2>Phase 1</h2><p>Plan</p></section>'
            '<section id="thor-execution" data-status="done"><h2>Phase 2</h2><p>Exec</p></section>'
            '<section id="heimdall-validation" data-status="done">'
            '<table><tbody>'
            '<tr><td class="odin-code">ODIN_SAFE_POLLER_FN = &quot;&quot;&quot;\n'
            '      const lastServerSnapshots = {};\n'
            '      async function pollLatestState() {\n'
            '        if (window.__odinCoEditActive) return;\n'
            '      }</td></tr>'
            '</tbody></table>'
            '</section>'
            '<section id="hlidskjalf-judge" data-status="done"><h2>Phase 4</h2><p>Judge</p></section>'
            '<section id="human-validation" data-status="awaiting_human"><h2>Phase 5</h2><p>Human</p></section>'
            '</main>'
            '<script>\n'
            '      const THEME_KEY = "odin_ui_theme";\n'
            '      async function pollLatestState() {\n'
            '        console.log("old poller");\n'
            '      }\n'
            '      // Controls wiring\n'
            '      const muteBtn = document.getElementById("odin-mute-btn");\n'
            '</script>'
            '</body></html>'
        )
        ledger = self.odin_state.extract_ledger(html_with_diff, str(self.sample_html_path))
        synced = self.odin_state.sync_top_banners_in_html(html_with_diff, ledger)
        self.assertIn('<section id="hlidskjalf-judge" data-status="done">', synced)
        self.assertIn('<section id="human-validation" data-status="awaiting_human">', synced)
        self.assertIn("</main>", synced)
        self.assertIn('const THEME_KEY = "odin_ui_theme";', synced)
        self.assertIn("__odinThemeControllerBound", synced)
        self.assertIn('id="odin-theme-init"', synced)
        self.assertNotIn('console.log("old poller");', synced)

    def test_mimir_spawn_delta_ticket_and_parent_completion_sync(self):
        # 1. Propose a delta on the parent ticket
        p_args = argparse.Namespace(
            html_file=str(self.sample_html_path),
            file=str(self.sample_html_path),
            id="DELTA-TEST-SPAWNER",
            repo="odin",
            target="SKILL.md",
            target_path=None,
            priority="p0",
            category="workflow",
            root_cause="Approve button only toggled JSON status without spawning a ticket",
            failure_pattern=None,
            diff="Add spawn-delta-ticket workflow",
            proposed_diff=None,
            iteration=1,
        )
        self.assertEqual(self.odin_state.cmd_propose_delta(p_args), 0)

        # 2. Spawn dedicated child Delta Ticket
        res = self.odin_state.spawn_mimir_delta_ticket(
            delta_id="DELTA-TEST-SPAWNER",
            scan_root=self.tmp.name,
            parent_file=str(self.sample_html_path),
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "in_progress")
        self.assertEqual(res["spawnedTicketId"], "odin-states/mimir-delta-test-spawner.html")

        child_path = Path(res["spawnedTicketFile"])
        self.assertTrue(child_path.is_file())
        child_html = child_path.read_text(encoding="utf-8")
        self.assertIn('id="odin-child-delta-origin-banner"', child_html)
        self.assertIn("DELTA-TEST-SPAWNER", child_html)

        # Verify parent ticket was NOT stepped back (stays in Iteration 1) and delta is in_progress
        parent_html = self.sample_html_path.read_text(encoding="utf-8")
        parent_ledger = self.odin_state.extract_ledger(parent_html, str(self.sample_html_path))
        self.assertEqual(parent_ledger["current_iteration"], 1)
        parent_deltas = self.odin_state._collect_all_deltas(parent_ledger)
        self.assertEqual(parent_deltas[0]["status"], "in_progress")
        self.assertEqual(parent_deltas[0]["spawned_ticket_id"], "odin-states/mimir-delta-test-spawner.html")
        self.assertIn("🎫 Open Delta Ticket", parent_html)
        self.assertIn("⚡ Approve &amp; Spawn Delta Ticket", parent_html)

        # Verify wakeup file was written for parent ticket
        wakeup_file = self.states_dir / ".odin-wakeup-sample-v3-feature.json"
        self.assertTrue(wakeup_file.is_file())
        wakeup_data = json.loads(wakeup_file.read_text(encoding="utf-8"))
        self.assertEqual(wakeup_data["decision"], "mimir_spawn_delta_ticket")
        self.assertEqual(wakeup_data["spawnedTicketId"], "odin-states/mimir-delta-test-spawner.html")

        # 3. Complete child ticket (hlidskjalf-judge -> done) and verify parent delta auto-syncs to applied
        u_args = argparse.Namespace(
            html_file=str(child_path),
            file=str(child_path),
            phase="hlidskjalf-judge",
            status="done",
            iteration=None,
            files=None,
            html_file_fragment=None,
            summary="Completed spawned delta ticket",
            html="<p>Delta verified and applied.</p>",
            title=None,
            branch=None,
            worktree=None,
            app_port=None,
            backend_port=None,
            session_id=None,
            with_diff=False,
            diff_context=12,
            diff_rationale=None,
            file_note=None,
            repo_dir=None,
            pr_url=None,
        )
        self.assertEqual(self.odin_state.cmd_update_phase(u_args), 0)

        parent_html_after = self.sample_html_path.read_text(encoding="utf-8")
        parent_ledger_after = self.odin_state.extract_ledger(parent_html_after, str(self.sample_html_path))
        self.assertEqual(parent_ledger_after["current_iteration"], 1)
        parent_deltas_after = self.odin_state._collect_all_deltas(parent_ledger_after)
        self.assertEqual(parent_deltas_after[0]["status"], "applied")
        self.assertIn("✅ APPROVED &amp; APPLIED", parent_html_after)
        self.assertIn("🎫 Open Delta Ticket", parent_html_after)

    def test_h2_badge_template_never_swallows_adjacent_sections_or_duplicates_top_banners(self):
        badge_tpl_path = self.states_dir / "demo-badge-template.html"
        badge_tpl_path.write_text(
            """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head><meta charset="utf-8" /><title>Odin — Demo Task</title></head>
<body>
  <header class="odin-topbar">
    <div class="odin-brand">
      <span id="odin-overall-badge" class="badge-awaiting_human">❓ OPEN QUESTIONS</span>
    </div>
    <nav class="odin-stepper">
      <a href="#huginn-plan" class="step-pill" id="pill-huginn-plan" data-status="awaiting_human"><span>1. Huginn</span><span class="pill-state">OPEN QUESTIONS</span></a>
      <a href="#thor-execution" class="step-pill" id="pill-thor-execution" data-status="pending"><span>2. Thor</span><span class="pill-state">PENDING</span></a>
      <a href="#heimdall-validation" class="step-pill" id="pill-heimdall-validation" data-status="pending"><span>3. Heimdall</span><span class="pill-state">PENDING</span></a>
      <a href="#hlidskjalf-judge" class="step-pill" id="pill-hlidskjalf-judge" data-status="pending"><span>4. Hlidskjalf</span><span class="pill-state">PENDING</span></a>
      <a href="#human-validation" class="step-pill" id="pill-human-validation" data-status="pending"><span>5. Human Sign-Off</span><span class="pill-state">PENDING</span></a>
    </nav>
  </header>
  <h1>⚡ Odin Allfather — Demo Task</h1>
  <div id="odin-top-command-banners"></div>
  <section id="huginn-plan" data-status="awaiting_human">
    <h2>🐦‍⬛ Phase 1: Huginn <span class="badge-awaiting_human">❓ OPEN QUESTIONS</span></h2>
    <h3>Root Cause Analysis</h3>
    <p>Detailed initial analysis preserved.</p>
  </section>
  <section id="thor-execution" data-status="pending">
    <h2>🔨 Phase 2: Thor <span class="badge-pending">PENDING</span></h2>
    <p>Awaiting completion of Phase 1.</p>
  </section>
  <section id="heimdall-validation" data-status="pending">
    <h2>👁️ Phase 3: Heimdall <span class="badge-pending">PENDING</span></h2>
    <p>Awaiting Phase 2 execution.</p>
  </section>
  <section id="hlidskjalf-judge" data-status="pending">
    <h2>👑 Phase 4: Hlidskjalf <span class="badge-pending">PENDING</span></h2>
    <p>Awaiting Phase 3 validation.</p>
  </section>
  <section id="human-validation" data-status="pending">
    <h2>🛡️ Phase 5: Human Validation <span class="badge-pending">PENDING</span></h2>
    <p>Awaiting AI phases completion.</p>
  </section>
</body>
</html>""",
            encoding="utf-8",
        )

        def run_update(phase: str, status: str, html_frag: str):
            args = argparse.Namespace(
                html_file=str(badge_tpl_path),
                file=str(badge_tpl_path),
                phase=phase,
                status=status,
                iteration=1,
                files=None,
                html_file_fragment=None,
                summary=f"{phase} -> {status}",
                html=html_frag,
                title=f"Iter 1 — {phase}",
                branch=None,
                worktree=None,
                app_port=None,
                backend_port=None,
                session_id=None,
                with_diff=False,
                diff_context=12,
                diff_rationale=None,
                file_note=None,
                repo_dir=None,
                pr_url=None,
            )
            self.assertEqual(self.odin_state.cmd_update_phase(args), 0)

        run_update("huginn-plan", "done", "<p>Aligned on scope.</p>")
        run_update(
            "thor-execution",
            "active",
            '<p>Writing tests.</p><details class="odin-diff-file-card" open><summary>Diff</summary><div>code</div></details>',
        )
        run_update(
            "thor-execution",
            "done",
            '<p>Tests & fix complete.</p><details class="odin-diff-file-card" open><summary>Diff2</summary><div>fixed</div></details>',
        )
        run_update("heimdall-validation", "done", "<p>11/11 passed.</p>")
        run_update("hlidskjalf-judge", "done", "<p>Audit passed.</p>")
        run_update("human-validation", "awaiting_human", "<p>Ready for sign-off.</p>")

        final_html = badge_tpl_path.read_text(encoding="utf-8")
        self.assertEqual(final_html.count('id="odin-top-command-banners"'), 1)
        self.assertIn("<h3>Root Cause Analysis</h3>", final_html)
        self.assertNotIn("Awaiting completion of Phase 1.", final_html)
        self.assertIn('<section id="huginn-plan" data-status="done">', final_html)
        self.assertIn('<section id="thor-execution" data-status="done">', final_html)
        self.assertIn('<section id="heimdall-validation" data-status="done">', final_html)
        self.assertIn('<section id="hlidskjalf-judge" data-status="done">', final_html)
        self.assertIn('<section id="human-validation" data-status="awaiting_human">', final_html)
        self.assertIn('id="pill-thor-execution" data-status="done"', final_html)
        self.assertIn('id="pill-hlidskjalf-judge" data-status="done"', final_html)
        self.assertIn('id="odin-overall-badge" class="badge-awaiting_human">👑 AWAITING SIGN-OFF</span>', final_html)

    def test_sticky_diff_header_approve_and_comment_drawer(self):
        diff_styles = self.odin_state.ODIN_DIFF_STYLES
        self.assertIn("overflow: clip;", diff_styles)
        self.assertIn("overflow-anchor: none;", diff_styles)
        self.assertIn("position: sticky;", diff_styles)
        self.assertIn("container-type: scroll-state;", diff_styles)
        self.assertIn("@container odin-diff-file-header scroll-state(stuck: top)", diff_styles)

        banner_script = self.odin_state.ODIN_TOP_BANNER_SCRIPT
        self.assertIn("data-odin-approve-file", banner_script)
        self.assertIn("data-file-comment-drawer", banner_script)
        self.assertIn("data-odin-approved-counter", banner_script)
        self.assertIn("odin-diff-saved-file-comment-banner", banner_script)
        self.assertIn("(File Comment)", banner_script)
        self.assertIn("}, true);", banner_script)
        self.assertIn("getVisibleDiffLineForCard", banner_script)

    def test_diff_multiline_selection_and_code_quote_comments(self):
        diff_styles = self.odin_state.ODIN_DIFF_STYLES
        self.assertIn(".odin-diff-viewer tr.odin-diff-row.is-selected", diff_styles)
        self.assertIn(".odin-diff-viewer tr.odin-diff-row.odin-diff-row-has-comment", diff_styles)
        self.assertIn(".odin-comment-quote-preview", diff_styles)
        self.assertIn(".odin-comment-quoted-code", diff_styles)
        self.assertIn(".odin-diff-line-range-picker", diff_styles)

        banner_script = self.odin_state.ODIN_TOP_BANNER_SCRIPT
        self.assertIn("extractMultiRowCode", banner_script)
        self.assertIn("getRowsBetween", banner_script)
        self.assertIn("getRowsForRangeInTable", banner_script)
        self.assertIn("data-odin-range-start", banner_script)
        self.assertIn("data-odin-range-end", banner_script)
        self.assertIn("data-odin-insert-inline-quote", banner_script)
        self.assertIn("data-odin-insert-suggestion", banner_script)
        self.assertIn("odin-diff-row-has-comment", banner_script)
        self.assertIn("` \" + inline + \" `", banner_script)




if __name__ == "__main__":
    unittest.main()
