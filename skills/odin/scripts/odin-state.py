#!/usr/bin/env python3
"""
Odin v2 Atomic Iteration & Mímir/Kvasir Harness Ledger Manager (odin-state.py)

Enforces append-only iteration cards (<details class="odin-iteration-card" data-iteration="N">)
and synchronized JSON ledger (<script type="application/json" id="odin-iteration-ledger">)
across Odin state HTML files, and powers the Mímir/Kvasir Harness Audit workflow.
"""

import argparse
import contextlib
import datetime
import fcntl
import difflib
import html as html_mod
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from odin_config import base_url, load_config, render_ports  # noqa: E402

CONFIG = load_config()
AI_DEV_ROOT = CONFIG["root"]
LIVE_PORT = CONFIG["ports"]["live"]
STAGING_PORT = CONFIG["ports"]["staging"]
ODIN_PRIMARY_REPO_DIR = CONFIG["staging"]["repo_dir"]
ODIN_REPO_DIR = ODIN_PRIMARY_REPO_DIR
ODIN_STAGING_DIR = CONFIG["staging"]["staging_dir"]
STAGING_BRANCH = CONFIG["staging"]["branch"]
NOTIFY_SCRIPT = str(Path(__file__).resolve().parent / "odin-notify.sh")

DEFAULT_SKILL_PILLS = CONFIG["skill_pills"]

PHASE_MAP = {
    "huginn": ("huginn-plan", "step-huginn", "Phase 1: Huginn Intake"),
    "thor": ("thor-execution", "step-thor", "Phase 2: Thor & Valkyries"),
    "heimdall": ("heimdall-validation", "step-heimdall", "Phase 3: Heimdall Verification"),
    "hlidskjalf": ("hlidskjalf-judge", "step-hlidskjalf", "Phase 4: Hlidskjalf Judgment"),
    "human": ("human-validation", "step-human", "Phase 5: Human Validation"),
}

SECTION_REGEX_MAP = {
    "huginn-plan": r"(?:huginn-plan|huginn-intake)",
    "huginn-intake": r"(?:huginn-plan|huginn-intake)",
}

PHASE_ALIASES = {
    "huginn": "huginn",
    "huginn-intake": "huginn",
    "huginn-plan": "huginn",
    "thor": "thor",
    "thor-execution": "thor",
    "heimdall": "heimdall",
    "heimdall-validation": "heimdall",
    "hlidskjalf": "hlidskjalf",
    "hlidskjalf-judge": "hlidskjalf",
    "human": "human",
    "human-validation": "human",
}


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def now_human() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def trigger_notify(title: str, subtitle: str, sound_mode: str = "phase", state_file: str = "") -> None:
    if os.path.exists(NOTIFY_SCRIPT):
        try:
            subprocess.Popen(
                ["bash", NOTIFY_SCRIPT, title, subtitle, sound_mode, str(state_file)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def extract_ledger(content: str, file_path: str = "") -> dict:
    match = re.search(
        r'<script[^>]*id=["\']odin-iteration-ledger["\'][^>]*>(.*?)</script>',
        content,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    slug = Path(file_path).stem if file_path else "unknown"
    title_m = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.DOTALL | re.IGNORECASE)
    title = html_mod.unescape(re.sub(r"<[^>]+>", "", title_m.group(1)).strip()) if title_m else slug
    sess_m = re.search(
        r'<meta\s+name=["\']odin-session-id["\']\s+content=["\']([^"\']+)["\']',
        content,
        re.IGNORECASE,
    )
    session_id = sess_m.group(1).strip() if sess_m else ""
    iter_m = re.search(r'id=["\']iteration-counter["\'][^>]*>\s*(\d+)\s*/\s*(\d+)', content)
    cur_iter = int(iter_m.group(1)) if iter_m else 1
    max_iter = int(iter_m.group(2)) if iter_m else 5

    return {
        "schema_version": "2.0",
        "slug": slug,
        "title": title,
        "session_id": session_id,
        "current_iteration": cur_iter,
        "max_iterations": max_iter,
        "iterations": [
            {
                "iteration": cur_iter,
                "started_at": now_iso(),
                "trigger": "Initial or legacy iteration state",
                "status": "active",
                "phases": {
                    "huginn": {"status": "done"},
                    "thor": {"status": "pending"},
                    "heimdall": {"status": "pending"},
                    "hlidskjalf": {"status": "pending"},
                    "human": {"status": "pending"},
                },
                "harness_delta_proposals": [],
            }
        ],
    }


def _collect_all_deltas(ledger: dict) -> list[dict]:
    deltas = []
    for it_obj in ledger.get("iterations", []):
        for d in it_obj.get("harness_delta_proposals", []):
            deltas.append(d)
    for d in ledger.get("harness_delta_proposals", []):
        if not any(existing.get("id") == d.get("id") for existing in deltas):
            deltas.append(d)
    return deltas


ODIN_TOP_BANNER_SCRIPT = """  <script id="odin-top-banner-actions">
    (() => {
      window.__odinV2CoEditPatched = true;
      const API_BASE = location.protocol.startsWith("http") ? "" : "http://127.0.0.1:8788";
      const rawPath = decodeURIComponent(location.pathname || "").replace(/^\\/+/, "");
      const rootMarker = "__ODIN_ROOT_NAME__/";
      const rootIdx = rawPath.indexOf(rootMarker);
      const relId = rootIdx >= 0
        ? rawPath.slice(rootIdx + rootMarker.length)
        : (rawPath || ("odin-states/" + (location.pathname.split("/").pop() || "")));
      let isEditMode = false;
      let isRawSourceMode = false;
      window.__odinCoEditActive = false;

      // Ensure dialog light-dismiss fallback per modern-web-guidance (light-dismiss-a-dialog)
      function setupDialogLightDismiss(dialog) {
        if (!dialog || dialog.dataset.lightDismissBound) return;
        dialog.dataset.lightDismissBound = "true";
        if (!("closedBy" in HTMLDialogElement.prototype)) {
          dialog.addEventListener("click", (event) => {
            const rect = dialog.getBoundingClientRect();
            const clickedInDialog =
              rect.top <= event.clientY &&
              event.clientY <= rect.top + rect.height &&
              rect.left <= event.clientX &&
              event.clientX <= rect.left + rect.width;
            if (!clickedInDialog) dialog.close();
          });
        }
      }

      function getSelectedSkills(containerSelector) {
        const root = containerSelector ? document.querySelector(containerSelector) : document;
        if (!root) return [];
        const pills = Array.from(root.querySelectorAll(".odin-skill-pill.active[data-skill]")).map(
          (el) => el.getAttribute("data-skill")
        );
        const customInput = root.querySelector("[data-odin-custom-skill]");
        if (customInput && customInput.value.trim()) {
          customInput.value
            .split(/[\\s,]+/)
            .map((s) => s.trim())
            .filter(Boolean)
            .forEach((s) => {
              const norm = s.startsWith("/") ? s : "/" + s;
              if (!pills.includes(norm)) pills.push(norm);
            });
        }
        return pills;
      }

      function getHuginnPhaseBody() {
        const sec = document.getElementById("huginn-plan") || document.getElementById("huginn-intake");
        if (!sec) return null;
        let body = sec.querySelector(".phase-body");
        if (!body) {
          body = document.createElement("div");
          body.className = "phase-body";
          const header = sec.querySelector(".phase-header, h2");
          const nodesToMove = [];
          let curr = header ? header.nextSibling : sec.firstChild;
          while (curr) {
            const next = curr.nextSibling;
            if (!(curr.nodeType === 1 && curr.hasAttribute("data-odin-transient"))) {
              nodesToMove.push(curr);
            }
            curr = next;
          }
          nodesToMove.forEach((n) => body.appendChild(n));
          sec.appendChild(body);
        }
        return body;
      }

      function ensureHuginnHeaderEditButton() {
        const header = document.querySelector("#huginn-plan .phase-header, #huginn-intake .phase-header");
        if (!header || header.querySelector('[data-odin-header-coedit="true"]')) return;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-gold";
        btn.setAttribute("data-odin-partner-action", "toggle_edit");
        btn.setAttribute("data-odin-header-coedit", "true");
        btn.setAttribute("data-odin-transient", "true");
        btn.style.cssText = "font-size:0.76rem;padding:0.28rem 0.7rem;font-weight:700;margin-left:auto;";
        btn.textContent = "✏️ Co-Edit Blueprint Inline";
        header.appendChild(btn);
      }

      function syncCoEditUI() {
        window.__odinCoEditActive = Boolean(isEditMode || isRawSourceMode);
        const body = getHuginnPhaseBody();
        const sec = document.getElementById("huginn-plan") || document.getElementById("huginn-intake");
        ensureHuginnHeaderEditButton();

        if (body) {
          body.contentEditable = isEditMode ? "true" : "false";
          if (isEditMode) {
            body.setAttribute("spellcheck", "true");
            body.style.outline = "2px dashed rgba(245, 158, 11, 0.85)";
            body.style.outlineOffset = "8px";
            body.style.borderRadius = "10px";
            body.style.padding = "0.75rem";
            body.style.background = "rgba(245, 158, 11, 0.04)";
            body.style.cursor = "text";
          } else {
            body.style.outline = "";
            body.style.outlineOffset = "";
            body.style.borderRadius = "";
            body.style.padding = "";
            body.style.background = "";
            body.style.cursor = "";
          }
        }

        document.querySelectorAll('[data-odin-partner-action="toggle_edit"]').forEach((btn) => {
          btn.textContent = isEditMode ? "✅ Editing Blueprint Inline (Click to Lock)" : "✏️ Co-Edit Blueprint Inline";
          btn.style.borderColor = isEditMode ? "rgba(16,185,129,0.75)" : "";
          btn.style.background = isEditMode ? "rgba(16,185,129,0.22)" : "";
          btn.style.color = isEditMode ? "var(--emerald)" : "";
        });

        document.querySelectorAll('[data-odin-partner-action="toggle_raw"]').forEach((btn) => {
          btn.textContent = isRawSourceMode ? "👁️ Preview Rendered Blueprint" : "</> HTML Source";
        });

        let toolbar = document.getElementById("odin-coedit-active-toolbar");
        if (window.__odinCoEditActive && sec && body) {
          if (!toolbar) {
            toolbar = document.createElement("div");
            toolbar.id = "odin-coedit-active-toolbar";
            toolbar.setAttribute("data-odin-transient", "true");
            toolbar.contentEditable = "false";
            toolbar.style.cssText =
              "margin:0 0 0.9rem 0;padding:0.7rem 0.95rem;border-radius:10px;background:rgba(245,158,11,0.14);border:1px solid rgba(245,158,11,0.65);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.6rem;";
            toolbar.innerHTML =
              '<span style="font-size:0.82rem;color:var(--gold);font-weight:700;">✏️ Inline Co-Edit Mode Active — Click anywhere in the blueprint below to edit text, tables, or code directly (auto-poll paused while editing).</span>' +
              '<div style="display:flex;gap:0.45rem;flex-wrap:wrap;">' +
              '<button type="button" class="btn" data-odin-partner-action="save_draft" style="font-size:0.76rem;padding:0.3rem 0.65rem;">💾 Save Draft (No Wake)</button>' +
              '<button type="button" class="btn btn-gold" data-odin-partner-action="revise_huginn" style="font-size:0.76rem;padding:0.3rem 0.65rem;font-weight:700;">🪶 Ask Huginn to Revise</button>' +
              '<button type="button" class="btn" data-odin-partner-action="approve_huginn" style="font-size:0.76rem;padding:0.3rem 0.75rem;border-color:rgba(16,185,129,0.65);background:rgba(16,185,129,0.22);color:var(--emerald);font-weight:700;">⚡ Save Edits &amp; Launch Phase 2</button>' +
              '<button type="button" class="btn" data-odin-partner-action="toggle_edit" style="font-size:0.76rem;padding:0.3rem 0.65rem;">🔒 Lock</button>' +
              "</div>";
            sec.insertBefore(toolbar, body);
          }
        } else if (toolbar) {
          toolbar.remove();
        }
      }

      function syncCheckboxesToAttributes(root) {
        if (!root) return;
        root.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
          if (cb.checked) {
            cb.setAttribute("checked", "checked");
          } else {
            cb.removeAttribute("checked");
          }
        });
      }

      // Ensure phase-body wrapper and header co-edit button exist at startup
      getHuginnPhaseBody();
      ensureHuginnHeaderEditButton();

      // Normalize any legacy hardcoded :8788 "All Projects" header links so :8789 stays on :8789
      if (location.protocol.startsWith("http")) {
        document.querySelectorAll('.odin-header a[href^="http://127.0.0.1:8788"]').forEach((a) => {
          const u = a.getAttribute("href") || "";
          if (u === "http://127.0.0.1:8788" || u === "http://127.0.0.1:8788/") {
            a.setAttribute("href", "/");
          }
        });
      }

      // Render high-visibility Staging (:8789) Sandbox Banner & title badge when running on port 8789
      if (location.port === "8789") {
        if (!document.title.startsWith("🧪 [STAGING :8789]")) {
          document.title = `🧪 [STAGING :8789] ${document.title}`;
        }
        const brandEl = document.querySelector(".brand-title");
        if (brandEl && !brandEl.textContent.includes("[STAGING :8789]")) {
          brandEl.innerHTML = `<span style="color:var(--violet);">🧪 [STAGING :8789]</span> ` + brandEl.innerHTML;
        }
        const topBanners = document.getElementById("odin-top-command-banners");
        if (topBanners && !document.getElementById("odin-staging-sandbox-banner")) {
          const sb = document.createElement("div");
          sb.id = "odin-staging-sandbox-banner";
          sb.style.cssText =
            "background:linear-gradient(90deg,rgba(168,85,247,0.24),rgba(56,189,248,0.14));border:1px solid rgba(168,85,247,0.72);border-left:5px solid var(--violet);border-radius:12px;padding:0.75rem 1.05rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.75rem;box-shadow:0 8px 20px rgba(0,0,0,0.16);";
          sb.innerHTML =
            '<div style="display:flex;align-items:center;gap:0.65rem;flex-wrap:wrap;">' +
            '<span class="badge-active" style="background:rgba(168,85,247,0.28);color:var(--violet);border-color:rgba(168,85,247,0.75);font-size:0.76rem;font-weight:800;">🧪 STAGING (:8789) — SANDBOX MODE</span>' +
            '<span style="font-size:0.8rem;color:var(--text);">Isolated Git worktree (<code>staging/odin-8789</code>) &amp; mirrored state snapshot. Clicks here will <strong>not</strong> overwrite Live (<code>:8788</code>) state files.</span>' +
            "</div>" +
            '<div style="display:flex;align-items:center;gap:0.45rem;flex-wrap:wrap;">' +
            '<button type="button" class="btn" data-odin-top-action="resnapshot-staging" style="font-size:0.75rem;padding:0.3rem 0.65rem;border-color:rgba(168,85,247,0.65);color:var(--violet);font-weight:700;">🔄 Reset/Re-Snapshot from Live (:8788)</button>' +
            '<button type="button" class="btn" data-odin-top-action="promote-staging" style="font-size:0.75rem;padding:0.3rem 0.75rem;border-color:rgba(16,185,129,0.65);background:rgba(16,185,129,0.2);color:var(--emerald);font-weight:700;">🚀 Promote to Live (:8788)</button>' +
            `<a class="btn" href="http://127.0.0.1:8788/${relId}" style="font-size:0.75rem;padding:0.3rem 0.65rem;text-decoration:none;border-color:rgba(56,189,248,0.55);color:var(--cyan);font-weight:700;">🏛️ Open Live (:8788) ↗</a>` +
            "</div>";
          topBanners.insertBefore(sb, topBanners.firstChild);
        }
        const openStagingModalBtn = document.querySelector('#odin-live-telemetry-bar [data-odin-top-action="open-staging-modal"]');
        if (openStagingModalBtn) openStagingModalBtn.style.display = "none";
      }

      // Live checkbox persistence inside #huginn-plan
      document.addEventListener("change", (e) => {
        if (e.target.matches('#huginn-plan input[type="checkbox"]')) {
          if (e.target.checked) {
            e.target.setAttribute("checked", "checked");
          } else {
            e.target.removeAttribute("checked");
          }
        }
      });

      // Poll live session telemetry & watcher heartbeat every 3.5s
      async function refreshTelemetry() {
        const badgeEl = document.getElementById("odin-telemetry-badge");
        const detailEl = document.getElementById("odin-telemetry-detail");
        const pauseBtn = document.querySelector('[data-odin-top-action="toggle-pause"]');
        if (!badgeEl || !detailEl) return;
        try {
          const res = await fetch(`${API_BASE}/api/session-telemetry?id=${encodeURIComponent(relId)}`, { cache: "no-store" });
          if (!res.ok) return;
          const t = await res.json();
          badgeEl.style.background = "";
          badgeEl.style.color = "";
          badgeEl.style.borderColor = "";
          if (t.paused) {
            badgeEl.className = "badge-pending";
            badgeEl.textContent = "⏸️ WATCHERS PAUSED";
            if (pauseBtn) pauseBtn.textContent = "▶️ Resume Watcher";
          } else if (t.watcherArmed) {
            badgeEl.className = "badge-done";
            badgeEl.style.background = "rgba(16,185,129,0.22)";
            badgeEl.style.color = "var(--emerald)";
            badgeEl.style.borderColor = "rgba(16,185,129,0.65)";
            badgeEl.textContent = "🟢 PARTNER TURN (Armed & Listening in Odin)";
            if (pauseBtn) pauseBtn.textContent = "⏸️ Pause Watcher";
          } else if (t.agentState === "executing") {
            badgeEl.className = "badge-active";
            badgeEl.textContent = `⚡ AGENT EXECUTING${t.lastToolName ? ` (${t.lastToolName})` : ""}`;
            if (pauseBtn) pauseBtn.textContent = "⏸️ Pause Watcher";
          } else if (t.agentState === "awaiting_human") {
            badgeEl.className = "badge-awaiting_human";
            badgeEl.textContent = "🟡 YOUR TURN IN ODIN (Click Save/Approve to Wake)";
            if (pauseBtn) pauseBtn.textContent = "⏸️ Pause Watcher";
          } else if (t.agentState === "done") {
            badgeEl.className = "badge-done";
            badgeEl.textContent = "✅ ALL PHASES COMPLETE";
            if (pauseBtn) pauseBtn.textContent = "⏸️ Pause Watcher";
          } else {
            badgeEl.className = "badge-active";
            badgeEl.textContent = t.statusLabel || "🛰️ TELEMETRY ONLINE";
          }

          const parts = [];
          if (t.sessionIdShort) parts.push(`Session <code>${t.sessionIdShort}</code>`);
          if (t.lastToolName) parts.push(`Tool: <code>${t.lastToolName}</code>`);
          if (t.lastActivityAgo) parts.push(`Last activity: ${t.lastActivityAgo}`);
          if (t.stagingLive && location.port !== "8789") {
            parts.push(`<a href="http://127.0.0.1:8789/${relId}" target="_blank" rel="noopener" style="color:var(--violet);font-weight:700;">🧪 Staging :8789 LIVE ↗</a>`);
          }
          detailEl.innerHTML = parts.length ? parts.join(" &bull; ") : "Connected to local Odin Reactive Partner Bridge";
        } catch (_) {}
      }

      setTimeout(refreshTelemetry, 250);
      setInterval(refreshTelemetry, 3500);

      document.addEventListener("click", async (e) => {
        // Skill pill toggle
        const skillPill = e.target.closest(".odin-skill-pill[data-skill]");
        if (skillPill) {
          skillPill.classList.toggle("active");
          const isActive = skillPill.classList.contains("active");
          skillPill.style.background = isActive ? "rgba(56,189,248,0.24)" : "var(--surface-elevated)";
          skillPill.style.borderColor = isActive ? "var(--cyan)" : "rgba(148,163,184,0.35)";
          skillPill.style.color = isActive ? "var(--cyan)" : "var(--text)";
          return;
        }

        // Huginn Co-Edit Mode toggle
        const editToggleBtn = e.target.closest('[data-odin-partner-action="toggle_edit"]');
        if (editToggleBtn) {
          const body = getHuginnPhaseBody();
          if (!body) return;
          if (isRawSourceMode) {
            const ta = document.getElementById("odin-huginn-raw-source");
            if (ta) body.innerHTML = ta.value;
            isRawSourceMode = false;
          }
          isEditMode = !isEditMode;
          syncCoEditUI();
          if (isEditMode) {
            const sec = document.getElementById("huginn-plan") || body;
            const rect = sec.getBoundingClientRect();
            if (rect.top < 0 || rect.top > window.innerHeight * 0.55) {
              sec.scrollIntoView({ behavior: "smooth", block: "start" });
            }
            body.focus({ preventScroll: true });
          }
          return;
        }

        // Huginn Raw HTML Source toggle
        const rawToggleBtn = e.target.closest('[data-odin-partner-action="toggle_raw"]');
        if (rawToggleBtn) {
          const body = getHuginnPhaseBody();
          if (!body) return;
          if (!isRawSourceMode) {
            syncCheckboxesToAttributes(body);
            const cloned = body.cloneNode(true);
            cloned.querySelectorAll("[data-odin-transient]").forEach((n) => n.remove());
            const currentHtml = cloned.innerHTML;
            isEditMode = false;
            const ta = document.createElement("textarea");
            ta.id = "odin-huginn-raw-source";
            ta.value = currentHtml.trim();
            ta.style.cssText = "width:100%;min-height:340px;background:#070b14;color:#e2e8f0;border:1px solid #38bdf8;border-radius:10px;padding:0.85rem;font-family:monospace;font-size:0.82rem;line-height:1.5;";
            body.innerHTML = "";
            body.appendChild(ta);
            isRawSourceMode = true;
            syncCoEditUI();
            ta.focus();
          } else {
            const ta = document.getElementById("odin-huginn-raw-source");
            if (ta) body.innerHTML = ta.value;
            isRawSourceMode = false;
            syncCoEditUI();
          }
          return;
        }

        // Quote highlighted selection into Partner Notes
        const quoteBtn = e.target.closest('[data-odin-partner-action="quote_selection"]');
        if (quoteBtn) {
          const sel = window.getSelection ? String(window.getSelection()).trim() : "";
          const notesInput = document.getElementById("odin-partner-notes-input");
          if (!notesInput) return;
          if (!sel) {
            alert("Highlight any text in the Huginn Blueprint first, then click '💬 Quote Highlighted Text'.");
            return;
          }
          const prefix = notesInput.value.trim() ? notesInput.value.trim() + "\\n\\n" : "";
          notesInput.value = `${prefix}> "${sel}"\\nPartner Decision: `;
          notesInput.focus();
          return;
        }

        // Partner Save / Approve & Launch Phase 2 / Ask Huginn to Revise
        const partnerSubmitBtn = e.target.closest('[data-odin-partner-action="approve_huginn"], [data-odin-partner-action="revise_huginn"], [data-odin-partner-action="save_draft"]');
        if (partnerSubmitBtn) {
          const mode = partnerSubmitBtn.getAttribute("data-odin-partner-action");
          const body = getHuginnPhaseBody();
          if (!body) return;
          if (isRawSourceMode) {
            const ta = document.getElementById("odin-huginn-raw-source");
            if (ta) body.innerHTML = ta.value;
            isRawSourceMode = false;
          }
          isEditMode = false;
          syncCoEditUI();
          syncCheckboxesToAttributes(body);
          // Strip existing injected partner banner or transient nodes if inside phase-body
          const cloned = body.cloneNode(true);
          cloned.querySelectorAll(".odin-inline-partner-box, [data-odin-transient]").forEach((n) => n.remove());
          const planHtml = cloned.innerHTML.trim();
          const partnerNotes = (document.getElementById("odin-partner-notes-input")?.value || "").trim();
          const selectedSkills = getSelectedSkills("#odin-huginn-partner-banner");

          const origLabel = partnerSubmitBtn.textContent;
          partnerSubmitBtn.disabled = true;
          partnerSubmitBtn.textContent = "⏳ Saving...";
          try {
            const res = await fetch(`${API_BASE}/api/projects/action`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                action: "save_partner_plan",
                id: relId,
                decision: mode,
                planHtml,
                partnerNotes,
                selectedSkills
              })
            });
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || "Failed to save Huginn Partner Plan");
            if (mode === "save_draft") {
              if (typeof window.__odinResetServerSnapshot === "function") {
                window.__odinResetServerSnapshot("huginn-plan");
              }
              partnerSubmitBtn.textContent = "✅ Draft Saved to Disk!";
              setTimeout(() => {
                partnerSubmitBtn.textContent = origLabel;
                partnerSubmitBtn.disabled = false;
              }, 2000);
            } else {
              location.reload();
            }
          } catch (err) {
            alert("Huginn Partner Save Error: " + (err.message || err));
            partnerSubmitBtn.textContent = origLabel;
            partnerSubmitBtn.disabled = false;
          }
          return;
        }

        const spawnDeltaBtn = e.target.closest("[data-odin-delta-spawn]");
        if (spawnDeltaBtn) {
          const deltaId = spawnDeltaBtn.getAttribute("data-odin-delta-spawn");
          const origLabel = spawnDeltaBtn.textContent;
          spawnDeltaBtn.disabled = true;
          spawnDeltaBtn.textContent = "⏳ Spawning Delta Ticket...";
          try {
            const res = await fetch(`${API_BASE}/api/mimir/apply`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                action: "mimir_spawn_delta_ticket",
                deltaId,
                filePath: relId
              })
            });
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || "Failed to spawn Mímir Delta Ticket");
            if (data.agentPrompt && navigator.clipboard) {
              try { await navigator.clipboard.writeText(data.agentPrompt); } catch (_) {}
            }
            location.reload();
          } catch (err) {
            alert("Mímir Delta Spawn Error: " + (err.message || err));
            spawnDeltaBtn.textContent = origLabel;
            spawnDeltaBtn.disabled = false;
          }
          return;
        }

        const deltaBtn = e.target.closest("[data-odin-delta-btn]");
        if (deltaBtn) {
          const deltaId = deltaBtn.getAttribute("data-delta-id");
          const status = deltaBtn.getAttribute("data-odin-delta-btn") || "applied";
          deltaBtn.disabled = true;
          try {
            const res = await fetch(`${API_BASE}/api/mimir/apply`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action: "mimir_mark_status", deltaId, deltaIds: [deltaId], status, filePath: relId })
            });
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || "Failed to update delta");
            location.reload();
          } catch (err) {
            alert("Mímir Update Error: " + (err.message || err));
            deltaBtn.disabled = false;
          }
          return;
        }

        const copyBtn = e.target.closest("[data-odin-delta-copy]");
        if (copyBtn) {
          const pre = copyBtn.closest(".odin-top-delta-item")?.querySelector("pre");
          if (pre && navigator.clipboard) {
            await navigator.clipboard.writeText(pre.textContent || "");
            const orig = copyBtn.textContent;
            copyBtn.textContent = "✅ Copied!";
            setTimeout(() => { copyBtn.textContent = orig; }, 1800);
          }
          return;
        }

        const topActionBtn = e.target.closest("[data-odin-top-action]");
        if (topActionBtn) {
          const act = topActionBtn.getAttribute("data-odin-top-action");
          if (act === "toggle-pause") {
            topActionBtn.disabled = true;
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "toggle_pause", id: relId })
              });
              await res.json();
              await refreshTelemetry();
            } catch (err) {
              alert("Pause Toggle Error: " + (err.message || err));
            } finally {
              topActionBtn.disabled = false;
            }
            return;
          }

          if (act === "open-staging-modal") {
            topActionBtn.disabled = true;
            const origLabel = topActionBtn.textContent;
            topActionBtn.textContent = "⏳ Snapshotting to :8789...";
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "snapshot_staging", id: relId })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Staging snapshot failed");
              const dialog = document.getElementById("odin-staging-dialog");
              const promptBox = document.getElementById("odin-staging-prompt-box");
              const linkEl = document.getElementById("odin-staging-open-link");
              if (promptBox) promptBox.value = data.debugPrompt || "";
              if (linkEl) linkEl.href = data.stagingStateUrl || data.stagingUrl || "http://127.0.0.1:8789/";
              if (data.debugPrompt && navigator.clipboard) {
                try { await navigator.clipboard.writeText(data.debugPrompt); } catch (_) {}
              }
              if (dialog) {
                setupDialogLightDismiss(dialog);
                dialog.showModal();
              }
              await refreshTelemetry();
            } catch (err) {
              alert("Staging Snapshot Error: " + (err.message || err));
            } finally {
              topActionBtn.textContent = origLabel;
              topActionBtn.disabled = false;
            }
            return;
          }

          if (act === "copy-staging-prompt") {
            const promptBox = document.getElementById("odin-staging-prompt-box");
            if (promptBox && navigator.clipboard) {
              await navigator.clipboard.writeText(promptBox.value || "");
              const orig = topActionBtn.textContent;
              topActionBtn.textContent = "✅ Debug Prompt Copied!";
              setTimeout(() => { topActionBtn.textContent = orig; }, 1800);
            }
            return;
          }

          if (act === "close-staging-modal") {
            const dialog = document.getElementById("odin-staging-dialog");
            if (dialog) dialog.close();
            return;
          }

          if (act === "resnapshot-staging") {
            if (!confirm("Reset & re-snapshot staging (:8789) from Live (:8788) main + odin-states/? Any unpromoted edits in odin-staging will be reset.")) return;
            topActionBtn.disabled = true;
            const origLabel = topActionBtn.textContent;
            topActionBtn.textContent = "⏳ Re-Snapshotting...";
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "snapshot_staging", id: relId })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Re-snapshot failed");
              setTimeout(() => location.reload(), 500);
            } catch (err) {
              alert("Re-Snapshot Error: " + (err.message || err));
              topActionBtn.textContent = origLabel;
              topActionBtn.disabled = false;
            }
            return;
          }

          if (act === "promote-staging") {
            if (!confirm("Promote staging worktree commits to Live (:8788) main and resume watchers?")) return;
            topActionBtn.disabled = true;
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "promote_staging", id: relId })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Promote staging failed");
              alert("✅ Staging changes fast-forward merged to Live (:8788) main and watchers resumed!");
              if (location.port === "8789") {
                setTimeout(() => {
                  location.href = `http://127.0.0.1:8788${location.pathname}${location.search}${location.hash}`;
                }, 650);
              } else {
                setTimeout(() => location.reload(), 650);
              }
            } catch (err) {
              alert("Promote Staging Error: " + (err.message || err));
              topActionBtn.disabled = false;
            }
            return;
          }

          if (act === "approve-heimdall-pr") {
            topActionBtn.disabled = true;
            const inlineInput = document.querySelector("[data-odin-gate-note-input]");
            const topInput = document.getElementById("odin-heimdall-note-input");
            const rawNote = ((inlineInput?.value || topInput?.value) || "").trim();
            const reviewBlock = typeof window.__odinBuildReviewCommentsSummary === "function"
              ? window.__odinBuildReviewCommentsSummary()
              : "";
            const combinedNote = [rawNote, reviewBlock].filter(Boolean).join("\\n\\n");
            const selectedSkills = getSelectedSkills("#odin-heimdall-gate-banner");
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action: "heimdall_gate_decision",
                  id: relId,
                  decision: "approve_pr",
                  humanNote: combinedNote || "Approved Heimdall Pre-PR Code Review Gate in Odin.",
                  selectedSkills
                })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Heimdall PR approval failed");
              if (typeof window.__odinClearReviewComments === "function") window.__odinClearReviewComments();
              location.reload();
            } catch (err) {
              alert("Heimdall Approval Error: " + (err.message || err));
              topActionBtn.disabled = false;
            }
            return;
          }

          if (act === "reject-heimdall-pr") {
            const inlineInput = document.querySelector("[data-odin-gate-note-input]");
            const topInput = document.getElementById("odin-heimdall-note-input");
            let rawNote = ((inlineInput?.value || topInput?.value) || "").trim();
            const reviewBlock = typeof window.__odinBuildReviewCommentsSummary === "function"
              ? window.__odinBuildReviewCommentsSummary()
              : "";
            if (!rawNote && !reviewBlock) {
              rawNote = prompt("Enter Code Review Revision Note (or click any diff line to add inline comments first):") || "";
            }
            const combinedNote = [rawNote.trim(), reviewBlock].filter(Boolean).join("\\n\\n");
            if (!combinedNote.trim()) return;
            const selectedSkills = getSelectedSkills("#odin-heimdall-gate-banner");
            topActionBtn.disabled = true;
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action: "heimdall_gate_decision",
                  id: relId,
                  decision: "reject_pr",
                  humanNote: combinedNote.trim(),
                  selectedSkills
                })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Heimdall step-back failed");
              if (typeof window.__odinClearReviewComments === "function") window.__odinClearReviewComments();
              location.reload();
            } catch (err) {
              alert("Heimdall Step-Back Error: " + (err.message || err));
              topActionBtn.disabled = false;
            }
            return;
          }

          if (act === "approve-signoff") {
            topActionBtn.disabled = true;
            const selectedSkills = getSelectedSkills("#odin-top-signoff-banner");
            const reviewBlock = typeof window.__odinBuildReviewCommentsSummary === "function"
              ? window.__odinBuildReviewCommentsSummary()
              : "";
            const baseNote = "Approved and signed off via Odin Command Center.";
            const combinedNote = [baseNote, reviewBlock].filter(Boolean).join("\\n\\n");
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action: "update_project",
                  id: relId,
                  phases: {
                    "huginn-plan": "done",
                    "thor-execution": "done",
                    "heimdall-validation": "done",
                    "hlidskjalf-judge": "done",
                    "human-validation": "done"
                  },
                  humanNote: combinedNote,
                  selectedSkills
                })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Sign-off failed");
              location.reload();
            } catch (err) {
              alert("Sign-Off Error: " + (err.message || err));
              topActionBtn.disabled = false;
            }
          } else if (act === "reject-signoff") {
            const reviewBlock = typeof window.__odinBuildReviewCommentsSummary === "function"
              ? window.__odinBuildReviewCommentsSummary()
              : "";
            let reason = prompt("Enter Human Rejection / Revision Note (inline review comments attach automatically):", "") || "";
            const combinedReason = [reason.trim(), reviewBlock].filter(Boolean).join("\\n\\n");
            if (!combinedReason.trim()) return;
            const selectedSkills = getSelectedSkills("#odin-top-signoff-banner");
            topActionBtn.disabled = true;
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action: "update_project",
                  id: relId,
                  phases: { "human-validation": "reject" },
                  humanNote: combinedReason.trim(),
                  selectedSkills
                })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Reject failed");
              location.reload();
            } catch (err) {
              alert("Reject Error: " + (err.message || err));
              topActionBtn.disabled = false;
            }
          }
        }
      });

      // Interactive Code Review (Sticky File Header, File Approvals, Whole-File & Inline Line Comments)
      if (!window.__odinDiffReviewControllerBound) {
        window.__odinDiffReviewControllerBound = true;
        window.__odinStickyDiffReviewV1 = true;
        window.__odinStickyDiffReviewV2 = true;
        const REVIEW_STORAGE_KEY = `odin_diff_review_${relId}`;
        const APPROVED_STORAGE_KEY = `odin_diff_approved_${relId}`;

        const syncStickyHeaderOffset = () => {
          const topBar = document.querySelector(".odin-topbar, .odin-header");
          const h = topBar ? Math.ceil(topBar.getBoundingClientRect().height) : 0;
          document.documentElement.style.setProperty("--odin-sticky-top-offset", `${Math.max(0, h)}px`);
          return h;
        };

        const syncStuckFileSummaries = () => {
          const offset = syncStickyHeaderOffset();
          document.querySelectorAll("details.odin-diff-file-card > summary").forEach((sum) => {
            const card = sum.parentElement;
            if (!card || !card.open) {
              sum.classList.remove("is-stuck");
              return;
            }
            const sRect = sum.getBoundingClientRect();
            const cRect = card.getBoundingClientRect();
            const isStuck = Math.abs(sRect.top - offset) <= 3 && cRect.top < offset - 4 && cRect.bottom > sRect.bottom + 16;
            sum.classList.toggle("is-stuck", isStuck);
          });
        };

        window.addEventListener("scroll", syncStuckFileSummaries, { passive: true });
        window.addEventListener("resize", syncStuckFileSummaries, { passive: true });
        if ("ResizeObserver" in window) {
          const ro = new ResizeObserver(() => syncStuckFileSummaries());
          const topBarEl = document.querySelector(".odin-topbar, .odin-header");
          if (topBarEl) ro.observe(topBarEl);
        }

        const getVisibleDiffLineForCard = (card) => {
          const fallback = parseInt(card.getAttribute("data-first-line") || "1", 10) || 1;
          const sum = card.querySelector(":scope > summary");
          if (!sum) return fallback;
          const sumBottom = sum.getBoundingClientRect().bottom;
          const viewer = card.closest(".odin-diff-viewer");
          const mode = viewer?.getAttribute("data-view-mode") || "split";
          const tblSelector = mode === "unified" ? "table.odin-diff-unified-table" : "table.odin-diff-split-table";
          const rows = card.querySelectorAll(`${tblSelector} tr.odin-diff-row`);
          for (const r of rows) {
            const rect = r.getBoundingClientRect();
            if (rect.bottom > sumBottom + 6 && rect.top < window.innerHeight) {
              return parseInt(r.getAttribute("data-line") || String(fallback), 10) || fallback;
            }
          }
          return fallback;
        };

        const loadApprovedFiles = () => {
          try {
            const raw = localStorage.getItem(APPROVED_STORAGE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
          } catch {
            return [];
          }
        };
        const saveApprovedFiles = (list) => {
          try {
            localStorage.setItem(APPROVED_STORAGE_KEY, JSON.stringify(Array.from(new Set(list))));
          } catch {}
          renderApprovedFiles();
        };

        const renderApprovedFiles = () => {
          const approvedSet = new Set(loadApprovedFiles());
          const allPaths = new Set();
          document.querySelectorAll("details.odin-diff-file-card").forEach((card) => {
            const fp = card.getAttribute("data-file-path") || "";
            if (fp) allPaths.add(fp);
            const isApp = fp && approvedSet.has(fp);
            card.setAttribute("data-file-approved", isApp ? "true" : "false");
            card.querySelectorAll("[data-odin-approve-file]").forEach((btn) => {
              btn.classList.toggle("is-approved", Boolean(isApp));
              btn.setAttribute("aria-pressed", isApp ? "true" : "false");
              btn.innerHTML = isApp ? "✅ Approved" : "☑️ Approve File";
            });
          });
          const totalCount = allPaths.size;
          let approvedCount = 0;
          allPaths.forEach((p) => {
            if (approvedSet.has(p)) approvedCount += 1;
          });
          document.querySelectorAll("[data-odin-approved-counter]").forEach((el) => {
            el.textContent = `✅ ${approvedCount} / ${totalCount} File${totalCount === 1 ? "" : "s"} Approved`;
            el.classList.toggle("all-approved", totalCount > 0 && approvedCount === totalCount);
          });
        };

        const loadComments = () => {
          try {
            const raw = localStorage.getItem(REVIEW_STORAGE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
          } catch {
            return [];
          }
        };
        const saveComments = (list) => {
          try {
            localStorage.setItem(REVIEW_STORAGE_KEY, JSON.stringify(list));
          } catch {}
          renderSavedComments();
        };
        window.__odinClearReviewComments = () => {
          try { localStorage.removeItem(REVIEW_STORAGE_KEY); } catch {}
          renderSavedComments();
        };
        window.__odinBuildReviewCommentsSummary = () => {
          const list = loadComments();
          if (!list.length) return "";
          const lines = list.map((c) => {
            const isFile = c.line === "FILE" || !c.line;
            const loc = isFile
              ? (c.repo ? `${c.repo}/${c.filePath} (File Comment)` : `${c.filePath} (File Comment)`)
              : (c.repo ? `${c.repo}/${c.filePath}:${c.line}` : `${c.filePath}:${c.line}`);
            const codePart = !isFile && c.codeSnippet ? ` (${c.codeSnippet.slice(0, 72)})` : "";
            return `- [${loc}]${codePart}: ${c.comment}`;
          });
          return `Code Review Comments (${list.length}):\\n` + lines.join("\\n");
        };

        const escapeHtml = (str) =>
          String(str || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");

        let selectedDiffRows = [];
        let selectionAnchorRow = null;

        const clearRowSelection = () => {
          document.querySelectorAll("tr.odin-diff-row.is-selected").forEach((r) => r.classList.remove("is-selected"));
          selectedDiffRows = [];
        };

        const setRowSelection = (rows) => {
          clearRowSelection();
          selectedDiffRows = (rows || []).filter(Boolean);
          selectedDiffRows.forEach((r) => r.classList.add("is-selected"));
        };

        const extractRowCode = (row) => {
          if (!row) return "";
          const codeCells = Array.from(row.querySelectorAll("td.odin-code"));
          const cell = codeCells.find((c) => !c.classList.contains("odin-empty") && c.textContent.trim().length > 0) || codeCells[codeCells.length - 1];
          if (!cell) return "";
          const text = cell.textContent || "";
          return text.replace(/^[+-]\\s*/, "").trimEnd();
        };

        const extractMultiRowCode = (rows) => {
          if (!rows || !rows.length) return { lines: [], inline: "", multiline: "", quoteSnippet: "" };
          const lines = rows.map((r) => extractRowCode(r)).filter((l) => l !== "");
          if (!lines.length) return { lines: [], inline: "", multiline: "", quoteSnippet: "" };

          let inline = lines.join(" ");
          inline = inline.replace(/\\s{2,}/g, " ").trim();

          const multiline = lines.join("\\n");
          const quoteSnippet = "` " + inline + " `";
          return { lines, inline, multiline, quoteSnippet };
        };

        const getRowsBetween = (startRow, endRow) => {
          if (!startRow || !endRow) return startRow ? [startRow] : [];
          if (startRow === endRow) return [startRow];
          const table = startRow.closest("table") || startRow.parentElement;
          if (!table || (endRow.closest("table") !== table && endRow.parentElement !== table)) {
            return [startRow];
          }
          const allRows = Array.from(table.querySelectorAll("tr.odin-diff-row"));
          const idx1 = allRows.indexOf(startRow);
          const idx2 = allRows.indexOf(endRow);
          if (idx1 === -1 || idx2 === -1) return [startRow];
          const low = Math.min(idx1, idx2);
          const high = Math.max(idx1, idx2);
          return allRows.slice(low, high + 1);
        };

        const getRowsForRangeInTable = (table, startLineNum, endLineNum) => {
          if (!table) return [];
          const allRows = Array.from(table.querySelectorAll("tr.odin-diff-row"));
          const s = Math.min(startLineNum, endLineNum);
          const e = Math.max(startLineNum, endLineNum);
          return allRows.filter((r) => {
            const ln = parseInt(r.getAttribute("data-line") || "0", 10);
            return ln >= s && ln <= e;
          });
        };

        const renderSavedComments = () => {
          document.querySelectorAll("tr.odin-diff-saved-comment-row, div.odin-diff-saved-file-comment-banner").forEach((n) => n.remove());
          document.querySelectorAll("tr.odin-diff-row.odin-diff-row-has-comment").forEach((r) => r.classList.remove("odin-diff-row-has-comment"));
          const list = loadComments();
          document.querySelectorAll("[data-odin-review-counter]").forEach((el) => {
            el.textContent = `💬 ${list.length} Review Comment${list.length === 1 ? "" : "s"}`;
          });
          document.querySelectorAll("[data-odin-clear-comments]").forEach((btn) => {
            btn.style.display = list.length > 0 ? "inline-flex" : "none";
          });

          // Update per-file comment count badges & whole-file comment banners
          document.querySelectorAll("details.odin-diff-file-card").forEach((card) => {
            const fp = card.getAttribute("data-file-path") || "";
            const fileComments = list.filter((c) => c.filePath === fp);
            card.querySelectorAll("[data-odin-comment-file]").forEach((btn) => {
              btn.innerHTML = fileComments.length > 0
                ? `💬 Comment (${fileComments.length})`
                : `💬 Comment on File`;
            });
            const wholeFileComments = fileComments.filter((c) => c.line === "FILE" || !c.line);
            if (wholeFileComments.length > 0) {
              const drawerEl = card.querySelector("[data-file-comment-drawer]") || card.querySelector(":scope > summary");
              wholeFileComments.forEach((item) => {
                const banner = document.createElement("div");
                banner.className = "odin-diff-saved-file-comment-banner";
                banner.setAttribute("data-odin-transient", "true");
                banner.style.cssText = "padding:0.65rem 1.05rem;background:rgba(168,85,247,0.14);border-bottom:1px solid rgba(168,85,247,0.45);border-left:4px solid #c084fc;display:flex;align-items:flex-start;justify-content:space-between;gap:0.75rem;flex-wrap:wrap;";
                banner.innerHTML = `
                  <div>
                    <div style="font-size:0.75rem;font-weight:800;color:#c084fc;margin-bottom:0.2rem;">
                      💬 Whole-File Review Comment on <code>${escapeHtml(item.repo ? item.repo + "/" + item.filePath : item.filePath)}</code>
                    </div>
                    <div style="font-size:0.84rem;color:#f8fafc;white-space:pre-wrap;line-height:1.45;">${escapeHtml(item.comment)}</div>
                  </div>
                  <div style="display:flex;gap:0.4rem;">
                    <button type="button" class="odin-diff-action-btn odin-diff-comment-btn" data-odin-edit-file-comment="${escapeHtml(item.id)}">✏️ Edit</button>
                    <button type="button" class="odin-diff-action-btn" style="color:#fb7185;border:1px solid rgba(244,63,94,0.45);background:rgba(244,63,94,0.14);" data-odin-delete-comment="${escapeHtml(item.id)}">🗑️ Remove</button>
                  </div>
                `;
                if (drawerEl) {
                  drawerEl.insertAdjacentElement("afterend", banner);
                }
              });
            }
          });

          list.filter((item) => item.line && item.line !== "FILE").forEach((item) => {
            let startLine = item.startLine;
            let endLine = item.endLine;
            if (!startLine || !endLine) {
              if (String(item.line).includes("-")) {
                const parts = String(item.line).split("-");
                startLine = parseInt(parts[0], 10) || 1;
                endLine = parseInt(parts[1], 10) || startLine;
              } else {
                startLine = endLine = parseInt(item.line, 10) || 1;
              }
            }

            const fileCards = document.querySelectorAll(`details.odin-diff-file-card[data-file-path="${CSS.escape(item.filePath)}"]`);
            fileCards.forEach((card) => {
              card.querySelectorAll("tr.odin-diff-row").forEach((row) => {
                const ln = parseInt(row.getAttribute("data-line") || "0", 10);
                if (ln >= startLine && ln <= endLine) {
                  row.classList.add("odin-diff-row-has-comment");
                }
              });

              const anchorRow =
                card.querySelector(`tr.odin-diff-row[data-line="${CSS.escape(String(endLine))}"]`) ||
                card.querySelector(`tr.odin-diff-row[data-line="${CSS.escape(String(startLine))}"]`);

              if (anchorRow) {
                const colSpan = anchorRow.children.length || 4;
                const tr = document.createElement("tr");
                tr.className = "odin-diff-saved-comment-row";
                tr.setAttribute("data-odin-transient", "true");
                tr.setAttribute("data-comment-id", item.id);
                const lineLabel = startLine === endLine ? String(startLine) : `${startLine}-${endLine}`;
                tr.innerHTML = `<td colspan="${colSpan}">
                  <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:0.75rem;flex-wrap:wrap;">
                    <div>
                      <div style="font-size:0.75rem;font-weight:800;color:#c084fc;margin-bottom:0.2rem;">
                        💬 Code Review Comment on <code>${escapeHtml(item.repo ? item.repo + "/" + item.filePath : item.filePath)}:${escapeHtml(lineLabel)}</code>
                      </div>
                      <div style="font-size:0.84rem;color:#f8fafc;white-space:pre-wrap;line-height:1.45;">${escapeHtml(item.comment)}</div>
                    </div>
                    <div style="display:flex;gap:0.4rem;">
                      <button type="button" class="odin-diff-action-btn odin-diff-comment-btn" data-odin-edit-comment="${escapeHtml(item.id)}">✏️ Edit</button>
                      <button type="button" class="odin-diff-action-btn" style="color:#fb7185;border:1px solid rgba(244,63,94,0.45);background:rgba(244,63,94,0.14);" data-odin-delete-comment="${escapeHtml(item.id)}">🗑️ Remove</button>
                    </div>
                  </div>
                </td>`;
                anchorRow.insertAdjacentElement("afterend", tr);
              }
            });
          });
        };

        const openLineCommentEditor = (targetRows, existingItem = null) => {
          document.querySelectorAll("tr.odin-diff-comment-editor-row").forEach((n) => n.remove());
          const rows = Array.isArray(targetRows) ? targetRows.filter(Boolean) : [targetRows].filter(Boolean);
          if (!rows.length) return;

          const leadRow = rows[0];
          const lastRow = rows[rows.length - 1];
          const filePath = leadRow.getAttribute("data-file-path") || "";
          const repo = leadRow.getAttribute("data-repo") || "";
          const table = leadRow.closest("table") || leadRow.parentElement;

          let startLine = parseInt(leadRow.getAttribute("data-line") || "1", 10);
          let endLine = parseInt(lastRow.getAttribute("data-line") || String(startLine), 10);
          if (startLine > endLine) {
            const tmp = startLine; startLine = endLine; endLine = tmp;
          }

          setRowSelection(rows);

          const { lines, inline, multiline, quoteSnippet } = extractMultiRowCode(rows);
          const lineDisplay = startLine === endLine ? `L${startLine}` : `L${startLine}-${endLine}`;
          const lineKey = startLine === endLine ? String(startLine) : `${startLine}-${endLine}`;
          const colSpan = lastRow.children.length || 4;

          const editorTr = document.createElement("tr");
          editorTr.className = "odin-diff-comment-editor-row";
          editorTr.setAttribute("data-odin-transient", "true");
          editorTr.innerHTML = `<td colspan="${colSpan}">
            <div style="display:flex;flex-direction:column;gap:0.55rem;">
              <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;">
                <div style="display:flex;align-items:center;gap:0.6rem;flex-wrap:wrap;">
                  <span style="font-size:0.8rem;font-weight:800;color:#fbbf24;">
                    💬 Add Code Review Comment — <code>${escapeHtml(repo ? repo + "/" + filePath : filePath)}:${escapeHtml(lineDisplay)}</code>
                  </span>
                  <div class="odin-diff-line-range-picker" style="display:inline-flex;align-items:center;gap:0.35rem;font-size:0.74rem;color:#cbd5e1;">
                    <span>Lines:</span>
                    <input type="number" data-odin-range-start value="${startLine}" style="width:58px;padding:0.15rem 0.35rem;background:#090d16;color:#f8fafc;border:1px solid #475569;border-radius:5px;font-size:0.75rem;font-family:monospace;" />
                    <span>to</span>
                    <input type="number" data-odin-range-end value="${endLine}" style="width:58px;padding:0.15rem 0.35rem;background:#090d16;color:#f8fafc;border:1px solid #475569;border-radius:5px;font-size:0.75rem;font-family:monospace;" />
                    <button type="button" class="odin-diff-btn" data-odin-expand-range-next style="padding:0.15rem 0.45rem;font-size:0.72rem;" title="Include next line">+ Line</button>
                  </div>
                </div>
                <span style="font-size:0.72rem;color:#94a3b8;">Press ⌘+Enter / Ctrl+Enter to save • Esc to cancel</span>
              </div>

              <!-- Quoted Code Preview Bar -->
              <div class="odin-comment-quote-preview">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:0.5rem;margin-bottom:0.25rem;">
                  <span style="font-size:0.73rem;color:#c084fc;font-weight:700;">📋 Quoted Code (${escapeHtml(lineDisplay)}):</span>
                  <div style="display:flex;gap:0.35rem;flex-wrap:wrap;">
                    <button type="button" class="odin-diff-btn" data-odin-insert-inline-quote style="padding:0.12rem 0.45rem;font-size:0.7rem;">Insert \\` Quote \\`</button>
                    <button type="button" class="odin-diff-btn" data-odin-insert-suggestion style="padding:0.12rem 0.45rem;font-size:0.7rem;">Suggest Fix</button>
                    <button type="button" class="odin-diff-btn" data-odin-insert-md-quote style="padding:0.12rem 0.45rem;font-size:0.7rem;">Quote &gt;</button>
                    <button type="button" class="odin-diff-btn" data-odin-clear-ta style="padding:0.12rem 0.45rem;font-size:0.7rem;">Clear</button>
                  </div>
                </div>
                <code class="odin-comment-quoted-code">${escapeHtml(quoteSnippet || "(no code on selected lines)")}</code>
              </div>

              <textarea data-odin-line-comment-ta rows="4" placeholder="Review notes for ${escapeHtml(lineDisplay)}... e.g. hey we dont need the trim" style="width:100%;background:#050811;color:#f8fafc;border:1px solid #c084fc;border-radius:8px;padding:0.55rem 0.75rem;font-family:monospace;font-size:0.81rem;resize:vertical;line-height:1.45;"></textarea>

              <div style="display:flex;gap:0.5rem;justify-content:space-between;align-items:center;">
                <span style="font-size:0.72rem;color:#94a3b8;">Tip: Shift+click or drag across lines to select multi-line ranges.</span>
                <div style="display:flex;gap:0.5rem;">
                  <button type="button" class="odin-diff-btn" data-odin-cancel-line-comment>Cancel</button>
                  <button type="button" class="odin-diff-action-btn odin-diff-comment-btn" data-odin-save-line-comment style="padding:0.32rem 0.8rem;">💾 Save Comment</button>
                </div>
              </div>
            </div>
          </td>`;

          lastRow.insertAdjacentElement("afterend", editorTr);
          const ta = editorTr.querySelector("[data-odin-line-comment-ta]");

          if (ta) {
            if (existingItem) {
              ta.value = existingItem.comment || "";
            } else if (quoteSnippet) {
              ta.value = `${quoteSnippet}\\n\\n`;
            }
            requestAnimationFrame(() => {
              ta.focus();
              ta.selectionStart = ta.selectionEnd = ta.value.length;
            });

            ta.addEventListener("keydown", (ev) => {
              if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
                ev.preventDefault();
                editorTr.querySelector("[data-odin-save-line-comment]")?.click();
              } else if (ev.key === "Escape") {
                clearRowSelection();
                editorTr.remove();
              }
            });
          }

          // Quote helper buttons
          editorTr.querySelector("[data-odin-insert-inline-quote]")?.addEventListener("click", () => {
            if (!ta || !quoteSnippet) return;
            const prefix = ta.value.trim().length > 0 ? "\\n\\n" : "";
            ta.value += `${prefix}${quoteSnippet}\\n`;
            ta.focus();
            ta.selectionStart = ta.selectionEnd = ta.value.length;
          });

          editorTr.querySelector("[data-odin-insert-suggestion]")?.addEventListener("click", () => {
            if (!ta || !quoteSnippet) return;
            const prefix = ta.value.trim().length > 0 ? "\\n\\n" : "";
            ta.value += `${prefix}${quoteSnippet}\\n\\nSuggestion:\\n\\` ${inline} \\`\\n`;
            ta.focus();
            ta.selectionStart = ta.selectionEnd = ta.value.length;
          });

          editorTr.querySelector("[data-odin-insert-md-quote]")?.addEventListener("click", () => {
            if (!ta || !inline) return;
            const prefix = ta.value.trim().length > 0 ? "\\n\\n" : "";
            ta.value += `${prefix}> ${inline}\\n\\n`;
            ta.focus();
            ta.selectionStart = ta.selectionEnd = ta.value.length;
          });

          editorTr.querySelector("[data-odin-clear-ta]")?.addEventListener("click", () => {
            if (ta) {
              ta.value = "";
              ta.focus();
            }
          });

          // Dynamic line range inputs
          const updateRangeFromInputs = () => {
            const startInp = editorTr.querySelector("[data-odin-range-start]");
            const endInp = editorTr.querySelector("[data-odin-range-end]");
            const sVal = parseInt(startInp?.value || "1", 10);
            const eVal = parseInt(endInp?.value || String(sVal), 10);
            const newRows = getRowsForRangeInTable(table, sVal, eVal);
            if (newRows.length) {
              const currentText = ta ? ta.value : "";
              openLineCommentEditor(newRows, existingItem ? { ...existingItem, comment: currentText } : null);
            }
          };

          editorTr.querySelector("[data-odin-range-start]")?.addEventListener("change", updateRangeFromInputs);
          editorTr.querySelector("[data-odin-range-end]")?.addEventListener("change", updateRangeFromInputs);
          editorTr.querySelector("[data-odin-expand-range-next]")?.addEventListener("click", () => {
            const endInp = editorTr.querySelector("[data-odin-range-end]");
            if (endInp) {
              endInp.value = String(parseInt(endInp.value || "1", 10) + 1);
              updateRangeFromInputs();
            }
          });

          editorTr.querySelector("[data-odin-cancel-line-comment]")?.addEventListener("click", () => {
            clearRowSelection();
            editorTr.remove();
          });

          editorTr.querySelector("[data-odin-save-line-comment]")?.addEventListener("click", () => {
            const val = (ta?.value || "").trim();
            if (!val) return;
            const commentId = `${repo}:${filePath}:${lineKey}`;
            const oldId = existingItem ? existingItem.id : commentId;
            const list = loadComments().filter((c) => c.id !== oldId && c.id !== commentId);
            list.push({
              id: commentId,
              repo,
              filePath,
              line: lineKey,
              startLine: Number(startLine),
              endLine: Number(endLine),
              codeSnippet: inline,
              comment: val,
              updatedAt: new Date().toISOString()
            });
            clearRowSelection();
            editorTr.remove();
            saveComments(list);
          });
        };

        // Sync top note input and inline gate note input bidirectionally
        document.addEventListener("input", (ev) => {
          if (ev.target && ev.target.matches("[data-odin-gate-note-input]")) {
            const topIn = document.getElementById("odin-heimdall-note-input");
            if (topIn && topIn.value !== ev.target.value) topIn.value = ev.target.value;
          } else if (ev.target && ev.target.id === "odin-heimdall-note-input") {
            document.querySelectorAll("[data-odin-gate-note-input]").forEach((inp) => {
              if (inp.value !== ev.target.value) inp.value = ev.target.value;
            });
          }
        });

        document.addEventListener("keydown", (ev) => {
          if (ev.target && ev.target.matches("[data-odin-drawer-comment-ta]")) {
            if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
              ev.preventDefault();
              const drawer = ev.target.closest("[data-file-comment-drawer]");
              drawer?.querySelector("[data-odin-save-drawer-comment]")?.click();
            } else if (ev.key === "Escape") {
              const drawer = ev.target.closest("[data-file-comment-drawer]");
              if (drawer) drawer.hidden = true;
            }
          }
        });

        document.addEventListener("click", async (ev) => {
          const clearBtn = ev.target.closest("[data-odin-clear-comments]");
          if (clearBtn) {
            window.__odinClearReviewComments();
            return;
          }

          const delBtn = ev.target.closest("[data-odin-delete-comment]");
          if (delBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const id = delBtn.getAttribute("data-odin-delete-comment");
            saveComments(loadComments().filter((c) => c.id !== id));
            return;
          }

          const editFileCommentBtn = ev.target.closest("[data-odin-edit-file-comment]");
          if (editFileCommentBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const id = editFileCommentBtn.getAttribute("data-odin-edit-file-comment");
            const existing = loadComments().find((c) => c.id === id);
            const card = editFileCommentBtn.closest("details.odin-diff-file-card");
            const drawer = card?.querySelector("[data-file-comment-drawer]");
            if (existing && card && drawer) {
              card.open = true;
              drawer.hidden = false;
              const ta = drawer.querySelector("[data-odin-drawer-comment-ta]");
              if (ta) {
                ta.value = existing.comment || "";
                requestAnimationFrame(() => ta.focus());
              }
            }
            return;
          }

          const editCommentBtn = ev.target.closest("[data-odin-edit-comment]");
          if (editCommentBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const id = editCommentBtn.getAttribute("data-odin-edit-comment");
            const existing = loadComments().find((c) => c.id === id);
            const savedRow = editCommentBtn.closest("tr.odin-diff-saved-comment-row");
            const card = editCommentBtn.closest("details.odin-diff-file-card");
            if (existing && card) {
              let startLine = existing.startLine;
              let endLine = existing.endLine;
              if (!startLine || !endLine) {
                if (String(existing.line).includes("-")) {
                  const parts = String(existing.line).split("-");
                  startLine = parseInt(parts[0], 10) || 1;
                  endLine = parseInt(parts[1], 10) || startLine;
                } else {
                  startLine = endLine = parseInt(existing.line, 10) || 1;
                }
              }
              const table = savedRow?.closest("table") || card.querySelector("table.odin-diff-split-table") || card.querySelector("table.odin-diff-unified-table");
              const matchingRows = getRowsForRangeInTable(table, startLine, endLine);
              if (matchingRows.length) {
                openLineCommentEditor(matchingRows, existing);
              } else if (savedRow?.previousElementSibling?.matches("tr.odin-diff-row")) {
                openLineCommentEditor([savedRow.previousElementSibling], existing);
              }
            }
            return;
          }

          const approveFileBtn = ev.target.closest("[data-odin-approve-file]");
          if (approveFileBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const card = approveFileBtn.closest("details.odin-diff-file-card");
            const viewer = approveFileBtn.closest(".odin-diff-viewer");
            const filePath = approveFileBtn.getAttribute("data-odin-approve-file") || card?.getAttribute("data-file-path") || "";
            if (!filePath) return;
            const current = new Set(loadApprovedFiles());
            const nowApproved = !current.has(filePath);
            if (nowApproved) {
              current.add(filePath);
            } else {
              current.delete(filePath);
            }
            saveApprovedFiles(Array.from(current));
            if (nowApproved && card) {
              card.open = false;
              const sum = card.querySelector(":scope > summary");
              if (sum) sum.classList.remove("is-stuck");
              if (viewer) {
                const cards = Array.from(viewer.querySelectorAll("details.odin-diff-file-card"));
                const idx = cards.indexOf(card);
                const nextUnapproved =
                  cards.slice(idx + 1).find((c) => c.getAttribute("data-file-approved") !== "true") ||
                  cards.find((c) => c.getAttribute("data-file-approved") !== "true");
                const targetEl = nextUnapproved || card;
                requestAnimationFrame(() => {
                  const offset = syncStickyHeaderOffset() + 12;
                  const topY = targetEl.getBoundingClientRect().top + window.scrollY - offset;
                  window.scrollTo({ top: Math.max(0, topY), behavior: "smooth" });
                });
              }
            }
            return;
          }

          const closeDrawerBtn = ev.target.closest("[data-odin-close-drawer]");
          if (closeDrawerBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const drawer = closeDrawerBtn.closest("[data-file-comment-drawer]");
            if (drawer) drawer.hidden = true;
            return;
          }

          const saveDrawerBtn = ev.target.closest("[data-odin-save-drawer-comment]");
          if (saveDrawerBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const drawer = saveDrawerBtn.closest("[data-file-comment-drawer]");
            const card = saveDrawerBtn.closest("details.odin-diff-file-card");
            if (!drawer || !card) return;
            const ta = drawer.querySelector("[data-odin-drawer-comment-ta]");
            const val = (ta?.value || "").trim();
            if (!val) return;
            const filePath = card.getAttribute("data-file-path") || "";
            const repo = card.getAttribute("data-repo") || "";
            const line = "FILE";
            const id = `${repo}:${filePath}:FILE`;
            const list = loadComments().filter((c) => c.id !== id);
            list.push({
              id,
              repo,
              filePath,
              line,
              codeSnippet: "",
              comment: val,
              updatedAt: new Date().toISOString()
            });
            if (ta) ta.value = "";
            drawer.hidden = true;
            saveComments(list);
            return;
          }

          const fileCommentBtn = ev.target.closest("[data-odin-comment-file]");
          if (fileCommentBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const card = fileCommentBtn.closest("details.odin-diff-file-card");
            if (card) {
              card.open = true;
              const drawer = card.querySelector("[data-file-comment-drawer]");
              if (drawer) {
                drawer.hidden = !drawer.hidden;
                if (!drawer.hidden) {
                  const ta = drawer.querySelector("[data-odin-drawer-comment-ta]");
                  if (ta && !ta.value.trim()) {
                    const fp = card.getAttribute("data-file-path") || "";
                    const repo = card.getAttribute("data-repo") || "";
                    const existing = loadComments().find((c) => c.id === `${repo}:${fp}:FILE`);
                    if (existing) ta.value = existing.comment || "";
                  }
                  requestAnimationFrame(() => ta?.focus());
                }
                return;
              }
            }
            return;
          }

          const editFileBtn = ev.target.closest("[data-odin-edit-file]");
          if (editFileBtn) {
            ev.preventDefault();
            ev.stopPropagation();
            const card = editFileBtn.closest("details.odin-diff-file-card");
            const viewer = editFileBtn.closest(".odin-diff-viewer");
            if (!card || !viewer) return;
            card.open = true;
            const slot = card.querySelector(".odin-inline-file-editor-slot");
            if (!slot) return;
            if (slot.querySelector(".odin-inline-file-editor")) {
              slot.innerHTML = "";
              editFileBtn.classList.remove("active");
              window.__odinCoEditActive = Boolean(document.querySelector(".odin-inline-file-editor"));
              return;
            }
            const filePath = editFileBtn.getAttribute("data-odin-edit-file") || card.getAttribute("data-file-path") || "";
            const worktree = editFileBtn.getAttribute("data-worktree") || card.getAttribute("data-worktree") || viewer.getAttribute("data-worktree") || "";
            const targetLine = getVisibleDiffLineForCard(card);
            const contextLines = parseInt(viewer.getAttribute("data-context-lines") || "12", 10) || 12;

            const origText = editFileBtn.textContent;
            editFileBtn.disabled = true;
            editFileBtn.textContent = "⏳ Loading file...";
            try {
              const res = await fetch(`${API_BASE}/api/projects/action`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action: "read_repo_file",
                  id: relId,
                  worktree,
                  filePath
                })
              });
              const data = await res.json();
              if (!data.ok) throw new Error(data.error || "Failed to read file");
              window.__odinCoEditActive = true;
              editFileBtn.classList.add("active");
              const lineCount = (data.content || "").split("\\n").length;
              slot.innerHTML = `
                <div class="odin-inline-file-editor" data-odin-transient="true">
                  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.6rem;margin-bottom:0.55rem;">
                    <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;">
                      <span class="badge-active" style="font-size:0.73rem;">✏️ INLINE FILE EDITOR</span>
                      <code style="color:#fbbf24;font-weight:700;">${escapeHtml(filePath)}</code>
                      <span style="font-size:0.74rem;color:#94a3b8;">(${lineCount} lines • Jumps to L${targetLine} • ⌘S / Ctrl+S to save &amp; refresh diff)</span>
                    </div>
                    <div style="display:flex;gap:0.45rem;align-items:center;">
                      <span data-odin-editor-status style="font-size:0.75rem;color:#34d399;font-weight:700;"></span>
                      <button type="button" class="odin-diff-btn" data-odin-close-file-editor>❌ Close Editor</button>
                      <button type="button" class="odin-diff-action-btn odin-diff-edit-btn" data-odin-save-file-editor style="padding:0.32rem 0.85rem;">💾 Save File &amp; Refresh Diff (⌘S)</button>
                    </div>
                  </div>
                  <textarea data-odin-file-editor-ta spellcheck="false"></textarea>
                </div>
              `;
              const ta = slot.querySelector("[data-odin-file-editor-ta]");
              if (ta) {
                ta.value = data.content || "";
                requestAnimationFrame(() => {
                  const sum = card.querySelector(":scope > summary");
                  const sumHeight = sum ? sum.getBoundingClientRect().height : 48;
                  const topOffset = syncStickyHeaderOffset() + sumHeight + 8;
                  const slotRect = slot.getBoundingClientRect();
                  if (slotRect.top < topOffset || slotRect.top > window.innerHeight * 0.6) {
                    window.scrollTo({ top: Math.max(0, slotRect.top + window.scrollY - topOffset), behavior: "smooth" });
                  }
                  ta.focus({ preventScroll: true });
                  const approxLineHeight = 19.8;
                  ta.scrollTop = Math.max(0, (targetLine - 5) * approxLineHeight);
                });
                ta.addEventListener("keydown", (ke) => {
                  if (ke.key === "Tab") {
                    ke.preventDefault();
                    const start = ta.selectionStart;
                    const end = ta.selectionEnd;
                    ta.value = ta.value.substring(0, start) + "  " + ta.value.substring(end);
                    ta.selectionStart = ta.selectionEnd = start + 2;
                  } else if ((ke.metaKey || ke.ctrlKey) && ke.key.toLowerCase() === "s") {
                    ke.preventDefault();
                    slot.querySelector("[data-odin-save-file-editor]")?.click();
                  }
                });
              }
              slot.querySelector("[data-odin-close-file-editor]")?.addEventListener("click", () => {
                slot.innerHTML = "";
                editFileBtn.classList.remove("active");
                window.__odinCoEditActive = Boolean(document.querySelector(".odin-inline-file-editor"));
              });
              slot.querySelector("[data-odin-save-file-editor]")?.addEventListener("click", async (se) => {
                const saveBtn = se.currentTarget;
                const statusEl = slot.querySelector("[data-odin-editor-status]");
                saveBtn.disabled = true;
                saveBtn.textContent = "⏳ Saving & Recomputing Diff...";
                try {
                  const saveRes = await fetch(`${API_BASE}/api/projects/action`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      action: "save_repo_file",
                      id: relId,
                      worktree,
                      filePath,
                      content: ta ? ta.value : "",
                      contextLines
                    })
                  });
                  const saveData = await saveRes.json();
                  if (!saveData.ok) throw new Error(saveData.error || "Failed to save file");
                  if (typeof window.__odinResetServerSnapshot === "function") {
                    window.__odinResetServerSnapshot("heimdall-validation");
                  }
                  if (saveData.diffViewerHtml) {
                    const tmp = document.createElement("div");
                    tmp.innerHTML = saveData.diffViewerHtml.trim();
                    const newViewer = tmp.querySelector(".odin-diff-viewer");
                    if (newViewer) {
                      viewer.replaceWith(newViewer);
                    }
                  }
                  window.__odinCoEditActive = Boolean(document.querySelector(".odin-inline-file-editor"));
                  renderSavedComments();
                  renderApprovedFiles();
                } catch (sErr) {
                  if (statusEl) {
                    statusEl.style.color = "#fb7185";
                    statusEl.textContent = "⚠️ " + (sErr.message || sErr);
                  } else {
                    alert("File Save Error: " + (sErr.message || sErr));
                  }
                  saveBtn.disabled = false;
                  saveBtn.textContent = "💾 Save File & Refresh Diff (⌘S)";
                }
              });
            } catch (err) {
              alert("Inline Editor Error: " + (err.message || err));
            } finally {
              editFileBtn.disabled = false;
              editFileBtn.textContent = origText;
            }
            return;
          }

          const diffRow = ev.target.closest("tr.odin-diff-row");
          if (diffRow && !ev.target.closest("button, a, textarea, input, .odin-diff-comment-editor-row")) {
            if (ev.shiftKey && selectionAnchorRow && selectionAnchorRow.closest("details") === diffRow.closest("details")) {
              const rows = getRowsBetween(selectionAnchorRow, diffRow);
              setRowSelection(rows);
              openLineCommentEditor(rows);
            } else {
              selectionAnchorRow = diffRow;
              setRowSelection([diffRow]);
              openLineCommentEditor([diffRow]);
            }
          }
        }, true);

        let isMouseDownOnRow = false;
        let dragAnchorRow = null;

        document.addEventListener("mousedown", (ev) => {
          const row = ev.target.closest("tr.odin-diff-row");
          if (row && !ev.target.closest("button, a, textarea, input, .odin-diff-comment-editor-row")) {
            isMouseDownOnRow = true;
            dragAnchorRow = row;
          }
        });

        document.addEventListener("mousemove", (ev) => {
          if (!isMouseDownOnRow || !dragAnchorRow) return;
          const row = ev.target.closest("tr.odin-diff-row");
          if (row && row.closest("details") === dragAnchorRow.closest("details")) {
            const rows = getRowsBetween(dragAnchorRow, row);
            if (rows.length > 1) {
              setRowSelection(rows);
            }
          }
        });

        document.addEventListener("mouseup", (ev) => {
          if (isMouseDownOnRow && dragAnchorRow) {
            const row = ev.target.closest("tr.odin-diff-row");
            if (row && row.closest("details") === dragAnchorRow.closest("details")) {
              const rows = getRowsBetween(dragAnchorRow, row);
              if (rows.length > 1) {
                selectionAnchorRow = dragAnchorRow;
                setRowSelection(rows);
                openLineCommentEditor(rows);
              }
            }
          }
          isMouseDownOnRow = false;
          dragAnchorRow = null;
        });

        const initReviewUI = () => {
          syncStuckFileSummaries();
          renderSavedComments();
          renderApprovedFiles();
        };
        if (document.readyState === "loading") {
          document.addEventListener("DOMContentLoaded", initReviewUI);
        } else {
          initReviewUI();
        }
      }

      if (!window.__odinThemeControllerBound) {
        window.__odinThemeControllerBound = true;
        const THEME_STORAGE_KEY = "odin_ui_theme";
        const applyOdinTheme = (theme, persist = false) => {
          const resolved = theme === "light" ? "light" : "dark";
          document.documentElement.setAttribute("data-theme", resolved);
          let meta = document.querySelector('meta[name="color-scheme"]');
          if (!meta && document.head) {
            meta = document.createElement("meta");
            meta.name = "color-scheme";
            document.head.prepend(meta);
          }
          if (meta) meta.content = resolved;
          const btn = document.getElementById("odin-theme-btn");
          if (btn) {
            btn.setAttribute("aria-pressed", resolved === "light" ? "true" : "false");
            btn.setAttribute(
              "title",
              resolved === "light" ? "Switch to dark theme" : "Switch to light theme"
            );
            btn.textContent = resolved === "light" ? "🌙 Dark" : "☀️ Light";
          }
          if (persist) {
            try {
              localStorage.setItem(THEME_STORAGE_KEY, resolved);
            } catch {}
          }
        };

        let initialTheme = document.documentElement.getAttribute("data-theme");
        try {
          const stored = localStorage.getItem(THEME_STORAGE_KEY);
          if (stored === "light" || stored === "dark") initialTheme = stored;
        } catch {}
        applyOdinTheme(initialTheme || "dark", false);

        window.addEventListener(
          "click",
          (e) => {
            const themeBtn = e.target && e.target.closest ? e.target.closest("#odin-theme-btn") : null;
            if (!themeBtn) return;
            e.preventDefault();
            e.stopImmediatePropagation();
            const current = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
            applyOdinTheme(current === "light" ? "dark" : "light", true);
          },
          true
        );

        window.addEventListener("storage", (e) => {
          if (e.key === THEME_STORAGE_KEY) {
            applyOdinTheme(e.newValue === "light" ? "light" : "dark", false);
          }
        });
      }
    })();
  </script>"""
ODIN_TOP_BANNER_SCRIPT = render_ports(ODIN_TOP_BANNER_SCRIPT, CONFIG).replace(
    "__ODIN_ROOT_NAME__", json.dumps(os.path.basename(AI_DEV_ROOT.rstrip("/")))[1:-1]
)


def _render_skill_pills_html(selected_skills: list | None = None) -> str:
    selected = {
        (s.get("id") or s.get("name") or s.get("skill") or "") if isinstance(s, dict) else str(s)
        for s in (selected_skills or [])
    }
    pills = []
    for sk_item in DEFAULT_SKILL_PILLS:
        sk = sk_item.get("id") or sk_item.get("label") or "" if isinstance(sk_item, dict) else str(sk_item)
        is_act = sk in selected
        cls = "odin-skill-pill active" if is_act else "odin-skill-pill"
        bg = "rgba(56,189,248,0.24)" if is_act else "var(--surface-elevated)"
        border = "var(--cyan)" if is_act else "rgba(148,163,184,0.35)"
        color = "var(--cyan)" if is_act else "var(--text)"
        pills.append(
            f'<button type="button" class="{cls}" data-skill="{html_mod.escape(sk)}" '
            f'style="cursor:pointer;font-family:monospace;font-size:0.74rem;padding:0.22rem 0.55rem;border-radius:999px;'
            f'border:1px solid {border};background:{bg};color:{color};transition:all 0.15s;">'
            f'⚡ {html_mod.escape(sk)}</button>'
        )
    pills.append(
        '<input type="text" data-odin-custom-skill placeholder="+ /custom-skill" '
        'style="background:var(--surface-elevated);color:var(--text);border:1px solid rgba(148,163,184,0.35);border-radius:999px;'
        'padding:0.2rem 0.65rem;font-family:monospace;font-size:0.74rem;width:135px;" />'
    )
    return "\n            ".join(pills)


ODIN_SAFE_POLLER_FN = """      const lastServerSnapshots = {};
      window.__odinResetServerSnapshot = (id) => {
        if (id) delete lastServerSnapshots[id];
      };

      async function pollLatestState() {
        if (window.__odinCoEditActive) return;
        const activeEl = document.activeElement;
        if (
          activeEl &&
          (activeEl.isContentEditable ||
            activeEl.tagName === "TEXTAREA" ||
            (activeEl.tagName === "INPUT" && activeEl.type !== "checkbox"))
        ) {
          return;
        }

        const urls = [];
        if (location.protocol.startsWith("http")) {
          urls.push(location.href.split("#")[0] + "?_t=" + Date.now());
        } else {
          urls.push(`http://127.0.0.1:8788/odin-states/${SLUG}?_t=${Date.now()}`);
        }

        for (const url of urls) {
          try {
            const res = await fetch(url, { cache: "no-store" });
            if (!res.ok) continue;
            const html = await res.text();
            const doc = new DOMParser().parseFromString(html, "text/html");
            let changed = false;

            const inBanners = doc.getElementById("odin-top-command-banners");
            const exBanners = document.getElementById("odin-top-command-banners");
            if (inBanners && exBanners) {
              ["odin-heimdall-gate-banner", "odin-top-signoff-banner", "odin-top-mimir-banner"].forEach((bId) => {
                const inChild = doc.getElementById(bId);
                const exChild = document.getElementById(bId);
                const inHtml = inChild ? inChild.outerHTML : "";
                if (!(bId in lastServerSnapshots)) {
                  lastServerSnapshots[bId] = inHtml;
                } else if (lastServerSnapshots[bId] !== inHtml) {
                  lastServerSnapshots[bId] = inHtml;
                  if (inChild && exChild) {
                    exChild.outerHTML = inHtml;
                  } else if (inChild && !exChild) {
                    exBanners.insertAdjacentHTML("beforeend", inHtml);
                  } else if (!inChild && exChild) {
                    exChild.remove();
                  }
                }
              });
            }

            PHASES.forEach(({ id }) => {
              const incoming = doc.getElementById(id);
              const existing = document.getElementById(id);
              if (incoming && existing) {
                const inStatus = incoming.getAttribute("data-status") || "";
                const exStatus = existing.getAttribute("data-status") || "";
                const sig = inStatus + "::" + incoming.innerHTML;
                if (!(id in lastServerSnapshots)) {
                  lastServerSnapshots[id] = sig;
                  if (inStatus && inStatus !== exStatus) {
                    existing.setAttribute("data-status", inStatus);
                    changed = true;
                  }
                } else if (lastServerSnapshots[id] !== sig) {
                  lastServerSnapshots[id] = sig;
                  existing.innerHTML = incoming.innerHTML;
                  if (inStatus) existing.setAttribute("data-status", inStatus);
                  changed = true;
                }
              }
            });

            if (changed) syncStepperAndDiff(true);
            return;
          } catch {}
        }
      }"""


def _ensure_huginn_phase_body(content: str) -> str:
    def _wrap(body: str) -> str:
        # Convert plain-text markdown-style checkboxes `<li>[ ] ` / `<li>[x] ` into interactive checkboxes
        body = re.sub(
            r"(<li\b[^>]*>)\s*\[\s*\]\s*",
            r'\1<input type="checkbox" style="margin-right:0.45rem;accent-color:#f59e0b;cursor:pointer;" /> ',
            body,
            flags=re.IGNORECASE,
        )
        body = re.sub(
            r"(<li\b[^>]*>)\s*\[[xX]\]\s*",
            r'\1<input type="checkbox" checked="checked" style="margin-right:0.45rem;accent-color:#f59e0b;cursor:pointer;" /> ',
            body,
            flags=re.IGNORECASE,
        )
        if re.search(r'<div\b[^>]*class=["\'][^"\']*\bphase-body\b[^"\']*["\']', body, re.IGNORECASE):
            return body
        stripped = body.strip()
        if not stripped:
            return '\n      <div class="phase-body"></div>\n    '
        return f'\n      <div class="phase-body">\n        {stripped}\n      </div>\n    '

    return mutate_section_body(content, "huginn-plan", _wrap)


ODIN_LIGHT_THEME_STYLES = """<style id="odin-light-theme-fixes">
    :root,
    html[data-theme="dark"] {
      color-scheme: dark;
      --emerald: #34d399;
      --crimson: #fb7185;
      --violet: #c084fc;
    }
    html[data-theme="light"] {
      color-scheme: light;
      --bg: var(--bg-light, #f8fafc);
      --surface: var(--surface-light, #ffffff);
      --surface-elevated: var(--surface-elevated-light, #f1f5f9);
      --text: var(--text-light, #0f172a);
      --muted: var(--muted-light, #475569);
      --border: var(--border-light, #cbd5e1);
      --gold: #b45309;
      --cyan: #0369a1;
      --emerald: #047857;
      --crimson: #be123c;
      --violet: #7e22ce;
    }
    html[data-theme="light"] .badge-done { background: rgba(16, 185, 129, 0.14); color: #065f46; border-color: rgba(5, 150, 105, 0.45); }
    html[data-theme="light"] .badge-active { background: rgba(14, 165, 233, 0.14); color: #075985; border-color: rgba(2, 132, 199, 0.45); }
    html[data-theme="light"] .badge-awaiting_human { background: rgba(245, 158, 11, 0.18); color: #92400e; border-color: rgba(217, 119, 6, 0.55); }
    html[data-theme="light"] .badge-reject { background: rgba(244, 63, 94, 0.14); color: #9f1239; border-color: rgba(225, 29, 72, 0.45); }
    html[data-theme="light"] .open-questions-box { background: rgba(245, 158, 11, 0.12); border-color: rgba(217, 119, 6, 0.55); color: #78350f; }
    html[data-theme="light"] details.odin-iteration-card > summary { background: var(--surface-elevated); color: var(--text); }
    html[data-theme="light"] #odin-env-bar > span { color: var(--text) !important; }
    /* Keep dark code diff surface readable in both light and dark page themes */
    html[data-theme="light"] .odin-diff-viewer {
      color-scheme: dark;
      color: #f8fafc;
    }
    html[data-theme="light"] .odin-diff-viewer .odin-diff-rationale-banner {
      color: #f1f5f9 !important;
      background: linear-gradient(90deg, rgba(245, 158, 11, 0.18), rgba(56, 189, 248, 0.12)) !important;
    }
    html[data-theme="light"] .odin-diff-viewer .odin-diff-rationale-banner strong {
      color: #fbbf24 !important;
    }
    html[data-theme="light"] .odin-diff-viewer .odin-diff-rationale-banner code {
      color: #38bdf8 !important;
      background: rgba(15, 23, 42, 0.8) !important;
    }
    html[data-theme="light"] .odin-diff-viewer td {
      border-color: rgba(30, 41, 59, 0.55) !important;
    }
    html[data-theme="light"] .odin-pre-pr-gate-box {
      background: #fffbeb !important;
      border-color: #f59e0b !important;
      border-left: 4px solid #d97706 !important;
      color: #0f172a !important;
    }
    html[data-theme="light"] .odin-pre-pr-gate-box strong,
    html[data-theme="light"] .odin-pre-pr-gate-box .odin-pre-pr-gate-title {
      color: #92400e !important;
    }
    html[data-theme="light"] .odin-pre-pr-gate-box div,
    html[data-theme="light"] .odin-pre-pr-gate-box .odin-pre-pr-gate-desc {
      color: #1e293b !important;
    }
  </style>"""

ODIN_THEME_HEAD_BOOTSTRAP = """  <meta name="color-scheme" content="dark light" />
  <script id="odin-theme-init">
    try {
      const s = localStorage.getItem("odin_ui_theme");
      const t = s === "light" || s === "dark" ? s : (document.documentElement.getAttribute("data-theme") || "dark");
      document.documentElement.setAttribute("data-theme", t);
      const m = document.querySelector('meta[name="color-scheme"]');
      if (m) m.content = t;
    } catch {}
  </script>"""


def _ensure_light_mode_styles(content: str) -> str:
    style_pat = re.compile(
        r'\s*<style[^>]*id=["\']odin-light-theme-fixes["\'][^>]*>[\s\S]*?</style>',
        re.IGNORECASE,
    )
    if style_pat.search(content):
        content = style_pat.sub(lambda _m: "\n  " + ODIN_LIGHT_THEME_STYLES, content, count=1)
    else:
        head_close = re.search(r"</head>", content, re.IGNORECASE)
        if head_close:
            idx = head_close.start()
            content = content[:idx] + "  " + ODIN_LIGHT_THEME_STYLES + "\n" + content[idx:]
    if 'id="odin-theme-init"' not in content:
        head_close = re.search(r"</head>", content, re.IGNORECASE)
        if head_close:
            idx = head_close.start()
            content = content[:idx] + ODIN_THEME_HEAD_BOOTSTRAP + "\n" + content[idx:]
    if "odin-diff-viewer" in content or "odin-diff-styles" in content:
        content = ensure_diff_styles(content)
    return content


def _upgrade_poller_in_html(content: str) -> str:
    main_close = re.search(r"</main\s*>", content, re.IGNORECASE)
    if not main_close:
        return content
    split_idx = main_close.end()
    prefix = content[:split_idx]
    suffix = content[split_idx:]
    poller_re = re.compile(
        r"(?:\s*const\s+lastServerSnapshots\s*=\s*\{\};(?:(?!</script>|<script\b|</table>|</section>)[\s\S])*?)?\s*async\s+function\s+pollLatestState\s*\(\)\s*\{(?:(?!</script>|<script\b|</table>|</section>)[\s\S])*?(?=\n\s*(?://\s*Controls\s+wiring\s*\n\s*)?const\s+muteBtn\s*=)",
        re.IGNORECASE,
    )
    if poller_re.search(suffix):
        suffix = poller_re.sub(lambda _m: "\n" + ODIN_SAFE_POLLER_FN, suffix, count=1)
    return prefix + suffix


def _normalize_all_projects_link_in_html(content: str) -> str:
    # Also repair any previously corrupted nested onclick attributes on the All Projects button
    content = re.sub(
        r'<a\s+class="btn"\s+href="/"\s+onclick="if\(location\.protocol===[^\n>]*?>🏛️ All Projects</a>',
        '''<a class="btn" href="/" onclick="if(location.protocol==='file:'){location.href='http://127.0.0.1:8788/';return false;}" style="text-decoration:none">🏛️ All Projects</a>''',
        content,
    )
    return re.sub(
        r'(<a\b[^>]*\bclass=["\'][^"\']*\bbtn\b[^"\']*["\'][^>]*)(?<!\.)\bhref=["\']http://127\.0\.0\.1:8788/?["\'](?![^>]*\bonclick=)',
        r'''\1href="/" onclick="if(location.protocol==='file:'){location.href='http://127.0.0.1:8788/';return false;}"''',
        content,
        flags=re.IGNORECASE,
    )


def _find_balanced_tag_span(text: str, open_match: re.Match, tag_name: str) -> tuple[int, int] | None:
    """Given an opening <tag_name ...> match, return (start, end) covering its balanced closing </tag_name>."""
    depth = 1
    tag_re = re.compile(rf"<(/?){re.escape(tag_name)}\b[^>]*>", re.IGNORECASE)
    for m in tag_re.finditer(text, open_match.end()):
        if m.group(1) == "/":
            depth -= 1
            if depth == 0:
                return (open_match.start(), m.end())
        else:
            # Ignore self-closing tags like <div ... />
            if not m.group(0).rstrip().endswith("/>"):
                depth += 1
    return None


def _replace_or_strip_top_command_banners(content: str, replacement_html: str) -> tuple[str, bool]:
    """
    Find all existing <div id="odin-top-command-banners"...>...</div> blocks (including any preceding
    <script data-odin-v2-guard>...</script>) using balanced <div> tag matching, replace the first one
    in-place with replacement_html, and strip any duplicate copies.
    """
    open_re = re.compile(
        r'(?:<script[^>]*data-odin-v2-guard[^>]*>[\s\S]*?</script>\s*)*<div\b[^>]*\bid=["\']odin-top-command-banners["\'][^>]*>',
        re.IGNORECASE,
    )
    div_only_re = re.compile(r'<div\b[^>]*\bid=["\']odin-top-command-banners["\'][^>]*>', re.IGNORECASE)
    spans: list[tuple[int, int]] = []
    search_pos = 0
    while True:
        m = open_re.search(content, search_pos)
        if not m:
            break
        div_m = div_only_re.search(content, m.start())
        if not div_m:
            break
        bal = _find_balanced_tag_span(content, div_m, "div")
        if not bal:
            break
        span_start = m.start()
        # Include leading horizontal whitespace/newline if present
        while span_start > 0 and content[span_start - 1] in (" ", "\t"):
            span_start -= 1
        spans.append((span_start, bal[1]))
        search_pos = bal[1]

    if not spans:
        return content, False

    out_parts: list[str] = []
    cursor = 0
    for idx, (s_start, s_end) in enumerate(spans):
        out_parts.append(content[cursor:s_start])
        if idx == 0:
            out_parts.append(replacement_html)
        else:
            # Strip trailing newline after duplicate banner block
            if s_end < len(content) and content[s_end] == "\n":
                s_end += 1
        cursor = s_end
    out_parts.append(content[cursor:])
    return "".join(out_parts), True


def sync_top_banners_in_html(content: str, ledger: dict, telemetry: dict | None = None) -> str:
    content = _ensure_light_mode_styles(content)
    content = _ensure_huginn_phase_body(content)
    content = _upgrade_poller_in_html(content)
    content = _normalize_all_projects_link_in_html(content)

    statuses = [_get_section_status(content, k) for k in AI_PHASE_ORDER]
    huginn_status = _get_section_status(content, "huginn")
    heimdall_status = _get_section_status(content, "heimdall")
    human_status = _get_section_status(content, "human")
    all_ai_done = all(s == "done" for s in statuses)
    awaiting_signoff = all_ai_done and human_status in ("awaiting_human", "pending", "active")

    deltas = _collect_all_deltas(ledger)
    pending_deltas = [d for d in deltas if (d.get("status") or "pending") == "pending"]
    selected_skills = ledger.get("selected_skills") if isinstance(ledger.get("selected_skills"), list) else []
    partner_notes_saved = str(ledger.get("partner_notes") or "")

    # Pre-compute initial live telemetry badge & detail so poll/SSR responses never flash "CONNECTING..."
    t_info = telemetry or {}
    t_badge_cls = "badge-active"
    t_badge_style = "font-size:0.74rem;"
    t_pause_label = "⏸️ Pause Watcher"
    if t_info.get("paused"):
        t_badge_cls = "badge-pending"
        t_badge_text = "⏸️ WATCHERS PAUSED"
        t_pause_label = "▶️ Resume Watcher"
    elif t_info.get("watcherArmed"):
        t_badge_cls = "badge-done"
        t_badge_style = "font-size:0.74rem;background:rgba(16,185,129,0.22);color:var(--emerald);border-color:rgba(16,185,129,0.65);"
        t_badge_text = "🟢 PARTNER TURN (Armed &amp; Listening in Odin)"
    elif t_info.get("agentState") == "executing":
        t_badge_cls = "badge-active"
        t_badge_text = "⚡ AGENT EXECUTING" + (f" ({html_mod.escape(str(t_info['lastToolName']))})" if t_info.get("lastToolName") else "")
    elif t_info.get("agentState") == "awaiting_human" or "awaiting_human" in statuses:
        t_badge_cls = "badge-awaiting_human"
        t_badge_text = "🟡 YOUR TURN IN ODIN (Click Save/Approve to Wake)"
    elif t_info.get("agentState") == "done" or (all_ai_done and human_status == "done"):
        t_badge_cls = "badge-done"
        t_badge_text = "✅ ALL PHASES COMPLETE"
    else:
        t_badge_cls = "badge-active"
        t_badge_text = html_mod.escape(str(t_info.get("statusLabel") or "🛰️ TELEMETRY ONLINE"))

    t_parts = []
    if t_info.get("sessionIdShort"):
        t_parts.append(f"Session <code>{html_mod.escape(str(t_info['sessionIdShort']))}</code>")
    if t_info.get("lastToolName"):
        t_parts.append(f"Tool: <code>{html_mod.escape(str(t_info['lastToolName']))}</code>")
    if t_info.get("lastActivityAgo"):
        t_parts.append(f"Last activity: {html_mod.escape(str(t_info['lastActivityAgo']))}")
    t_detail_html = " &bull; ".join(t_parts) if t_parts else "Connected to local Odin Reactive Partner Bridge"

    banner_parts = []

    # 1. Always-on Live Transcript Telemetry & Staging (:8789) Control Bar + Modal
    banner_parts.append(
        '      <div id="odin-live-telemetry-bar" style="background:var(--surface-elevated);border:1px solid rgba(56,189,248,0.38);'
        'border-left:4px solid var(--cyan);border-radius:12px;padding:0.65rem 1rem;display:flex;align-items:center;'
        'justify-content:space-between;flex-wrap:wrap;gap:0.75rem;box-shadow:0 8px 20px rgba(0,0,0,0.12);">\n'
        '        <div style="display:flex;align-items:center;gap:0.65rem;flex-wrap:wrap;">\n'
        f'          <span id="odin-telemetry-badge" class="{t_badge_cls}" style="{t_badge_style}">{t_badge_text}</span>\n'
        f'          <span id="odin-telemetry-detail" style="font-size:0.78rem;color:var(--muted);">{t_detail_html}</span>\n'
        '        </div>\n'
        '        <div style="display:flex;align-items:center;gap:0.45rem;flex-wrap:wrap;">\n'
        f'          <button type="button" class="btn" data-odin-top-action="toggle-pause" style="font-size:0.75rem;padding:0.3rem 0.65rem;">{t_pause_label}</button>\n'
        + ('          <button type="button" class="btn" data-odin-top-action="open-staging-modal" style="font-size:0.75rem;padding:0.3rem 0.75rem;border-color:rgba(168,85,247,0.55);background:rgba(168,85,247,0.16);color:var(--violet);font-weight:700;">🧪 Pause &amp; Snapshot to Staging (:8789)</button>\n' if CONFIG["staging"]["available"] else '')
        + '        </div>\n'
        '      </div>\n'
        '      <dialog id="odin-staging-dialog" closedby="any" aria-labelledby="odin-staging-dialog-title" '
        'style="max-width:680px;width:92vw;background:var(--surface);color:var(--text);border:1px solid rgba(168,85,247,0.6);border-radius:16px;padding:1.35rem;box-shadow:0 25px 50px rgba(0,0,0,0.45);">\n'
        '        <style>#odin-staging-dialog::backdrop { background: rgba(2, 6, 23, 0.78); backdrop-filter: blur(4px); }</style>\n'
        '        <div style="display:flex;justify-content:space-between;align-items:center;gap:0.75rem;margin-bottom:0.75rem;">\n'
        '          <h3 id="odin-staging-dialog-title" style="margin:0;font-size:1.05rem;color:var(--violet);">🧪 Odin Staging Environment (:8789) Snapshot Ready</h3>\n'
        '          <button type="button" class="btn" data-odin-top-action="close-staging-modal" style="padding:0.25rem 0.6rem;">✕ Close</button>\n'
        '        </div>\n'
        '        <p style="font-size:0.84rem;color:var(--muted);margin:0 0 0.75rem 0;line-height:1.5;">Live watchers on <code>:8788</code> are paused and your current Odin repo + <code>odin-states/</code> have been snapshotted into the staging worktree on port <code>8789</code>. Paste the prompt below into a <strong>new Claude Code session</strong> to debug or upgrade Odin safely:</p>\n'
        '        <textarea id="odin-staging-prompt-box" readonly style="width:100%;min-height:170px;background:#070b14;color:#38bdf8;border:1px solid #334155;border-radius:10px;padding:0.75rem;font-family:monospace;font-size:0.78rem;line-height:1.45;margin-bottom:0.85rem;"></textarea>\n'
        '        <div style="display:flex;justify-content:flex-start;align-items:center;flex-wrap:wrap;gap:0.5rem;">\n'
        '          <button type="button" class="btn" data-odin-top-action="copy-staging-prompt" style="border-color:var(--cyan);background:rgba(56,189,248,0.18);color:var(--cyan);font-weight:700;">📋 Copy Debug Prompt</button>\n'
        '          <a id="odin-staging-open-link" href="http://127.0.0.1:8789/" target="_blank" rel="noopener" class="btn" style="text-decoration:none;border-color:rgba(168,85,247,0.55);color:var(--violet);">🌐 Open Staging (:8789) ↗</a>\n'
        '        </div>\n'
        '      </dialog>'
    )

    # 2. Phase 1 Huginn Co-Editable Partner Blueprint Banner
    huginn_is_awaiting = huginn_status in ("awaiting_human", "active")
    huginn_open_attr = " open" if huginn_is_awaiting else ""
    huginn_badge = (
        '<span class="badge-awaiting_human" style="font-size:0.75rem;">🪶 PARTNER CO-EDIT GATE — YOUR TURN</span>'
        if huginn_status == "awaiting_human"
        else '<span class="badge-done" style="font-size:0.74rem;">✏️ CO-EDITABLE BLUEPRINT</span>'
    )
    banner_parts.append(
        f'      <details id="odin-huginn-partner-banner"{huginn_open_attr} style="background:linear-gradient(90deg,rgba(245,158,11,0.14),rgba(56,189,248,0.10));'
        f'border:1px solid rgba(245,158,11,0.55);border-left:5px solid var(--gold);border-radius:14px;padding:0.95rem 1.25rem;box-shadow:0 10px 25px rgba(0,0,0,0.12);">\n'
        f'        <summary style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.6rem;font-weight:700;font-size:0.95rem;list-style:none;">\n'
        f'          <span style="display:flex;align-items:center;gap:0.55rem;flex-wrap:wrap;">\n'
        f'            <span>🪶 Phase 1: Huginn Partner Co-Editor &amp; Skill Launcher</span>\n'
        f'            {huginn_badge}\n'
        f'          </span>\n'
        f'          <span style="font-size:0.76rem;color:var(--gold);font-family:monospace;">Edit blueprint inline, quote text, attach /skills &amp; wake Thor ▾</span>\n'
        f'        </summary>\n'
        f'        <div style="margin-top:0.85rem;display:flex;flex-direction:column;gap:0.75rem;">\n'
        f'          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;">\n'
        f'            <div style="display:flex;gap:0.45rem;flex-wrap:wrap;">\n'
        f'              <button type="button" class="btn btn-gold" data-odin-partner-action="toggle_edit" style="font-weight:700;">✏️ Co-Edit Blueprint Inline</button>\n'
        f'              <button type="button" class="btn" data-odin-partner-action="quote_selection">💬 Quote Highlighted Text</button>\n'
        f'              <button type="button" class="btn" data-odin-partner-action="toggle_raw">&lt;/&gt; HTML Source</button>\n'
        f'            </div>\n'
        f'            <span style="font-size:0.76rem;color:var(--muted);">Tip: Toggle checkboxes or edit any section inside <code>#huginn-plan</code> below, then click Save &amp; Launch.</span>\n'
        f'          </div>\n'
        f'          <div>\n'
        f'            <div style="font-size:0.75rem;color:var(--cyan);font-weight:700;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:0.35rem;">⚡ Attach Skills for Next Agent Turn (Optional):</div>\n'
        f'            <div style="display:flex;flex-wrap:wrap;gap:0.35rem;align-items:center;">\n'
        f'            {_render_skill_pills_html(selected_skills)}\n'
        f'            </div>\n'
        f'          </div>\n'
        f'          <div>\n'
        f'            <label for="odin-partner-notes-input" style="display:block;font-size:0.76rem;color:var(--gold);font-weight:700;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:0.3rem;">💬 Partner Decisions, Open Question Answers &amp; Revision Notes:</label>\n'
        f'            <textarea id="odin-partner-notes-input" placeholder="Answer open questions, paste quoted sections, or add architectural constraints for Thor..." '
        f'style="width:100%;min-height:78px;background:var(--surface-elevated);color:var(--text);border:1px solid rgba(245,158,11,0.45);border-radius:10px;padding:0.65rem 0.85rem;font-size:0.84rem;line-height:1.45;">{html_mod.escape(partner_notes_saved)}</textarea>\n'
        f'          </div>\n'
        f'          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;">\n'
        f'            <button type="button" class="btn" data-odin-partner-action="save_draft" style="font-size:0.8rem;">💾 Save Draft Only (No Wake)</button>\n'
        f'            <div style="display:flex;gap:0.55rem;flex-wrap:wrap;">\n'
        f'              <button type="button" class="btn btn-gold" data-odin-partner-action="revise_huginn" style="font-weight:700;padding:0.48rem 0.9rem;">🪶 Ask Huginn to Revise Plan (Wake Agent)</button>\n'
        f'              <button type="button" class="btn" data-odin-partner-action="approve_huginn" style="border-color:rgba(16,185,129,0.65);background:rgba(16,185,129,0.22);color:var(--emerald);font-weight:700;padding:0.48rem 1rem;">⚡ Save Edits &amp; Launch Phase 2: Thor (Wake Agent)</button>\n'
        f'            </div>\n'
        f'          </div>\n'
        f'        </div>\n'
        f'      </details>'
    )

    # 3. Phase 3 Heimdall Pre-PR Code Review Gate Banner
    if heimdall_status == "awaiting_human":
        banner_parts.append(
            '      <div id="odin-heimdall-gate-banner" style="background:linear-gradient(90deg,rgba(245,158,11,0.18),rgba(56,189,248,0.12));'
            'border:1px solid rgba(245,158,11,0.6);border-left:5px solid var(--gold);border-radius:14px;padding:1rem 1.25rem;'
            'display:flex;flex-direction:column;gap:0.75rem;box-shadow:0 10px 25px rgba(0,0,0,0.12);">\n'
            '        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.75rem;">\n'
            '          <div>\n'
            '            <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;">\n'
            '              <span class="badge-awaiting_human" style="font-size:0.76rem;">🛡️ HEIMDALL PRE-PR CODE REVIEW GATE</span>\n'
            '              <span class="badge-done" style="font-size:0.74rem;">✅ AUTOMATED TESTS &amp; LINTERS PASSED</span>\n'
            '            </div>\n'
            '            <div style="font-size:0.86rem;margin-top:0.35rem;color:var(--text);">Review the Side-by-Side contextual code diff in <code>#heimdall-validation</code> below. Click <strong>Approve Code Diff &amp; Open PR</strong> to wake the agent and advance to Phase 4, or request code changes to step back to Thor.</div>\n'
            '          </div>\n'
            '          <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">\n'
            '            <button type="button" class="btn" data-odin-top-action="approve-heimdall-pr" style="border-color:rgba(16,185,129,0.65);background:rgba(16,185,129,0.22);color:var(--emerald);font-weight:700;padding:0.5rem 0.95rem;">✅ Approve Code Diff &amp; Open PR (Wake Agent)</button>\n'
            '            <button type="button" class="btn" data-odin-top-action="reject-heimdall-pr" style="border-color:rgba(244,63,94,0.55);background:rgba(244,63,94,0.16);color:var(--crimson);font-weight:700;padding:0.5rem 0.95rem;">🚨 Request Code Changes (Step Back to Thor)</button>\n'
            '          </div>\n'
            '        </div>\n'
            '        <div style="display:flex;gap:0.65rem;align-items:center;flex-wrap:wrap;">\n'
            '          <input id="odin-heimdall-note-input" type="text" placeholder="Optional PR review note or requested code change reason..." '
            'style="flex:1;min-width:260px;background:var(--surface-elevated);color:var(--text);border:1px solid rgba(148,163,184,0.38);border-radius:8px;padding:0.45rem 0.75rem;font-size:0.82rem;" />\n'
            '        </div>\n'
            '      </div>'
        )

    # 4. Phase 5 Final Human Sign-Off Banner
    if awaiting_signoff:
        banner_parts.append(
            '      <div id="odin-top-signoff-banner" style="background:linear-gradient(90deg,rgba(245,158,11,0.16),rgba(16,185,129,0.12));'
            'border:1px solid rgba(245,158,11,0.55);border-left:5px solid var(--gold);border-radius:14px;padding:1rem 1.25rem;'
            'display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.85rem;box-shadow:0 10px 25px rgba(0,0,0,0.12);">\n'
            '        <div>\n'
            '          <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;">\n'
            '            <span class="badge-awaiting_human" style="font-size:0.76rem;">👑 AWAITING FINAL HUMAN SIGN-OFF</span>\n'
            '            <span class="badge-done" style="font-size:0.74rem;">✅ 4/4 AI PHASES COMPLETE</span>\n'
            '          </div>\n'
            '          <div style="font-size:0.88rem;margin-top:0.35rem;color:var(--text);">All 4 AI phases (Huginn, Thor, Heimdall, Hlidskjalf) are complete. Review the deliverables below and sign off to mark the project <strong>DONE</strong>, or reject with feedback to step back.</div>\n'
            '        </div>\n'
            '        <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">\n'
            '          <button type="button" class="btn" data-odin-top-action="approve-signoff" style="border-color:rgba(16,185,129,0.6);background:rgba(16,185,129,0.2);color:var(--emerald);font-weight:700;padding:0.5rem 0.95rem;">✅ Sign Off &amp; Mark Done</button>\n'
            '          <button type="button" class="btn" data-odin-top-action="reject-signoff" style="border-color:rgba(244,63,94,0.55);background:rgba(244,63,94,0.16);color:var(--crimson);font-weight:700;padding:0.5rem 0.95rem;">🚨 Reject &amp; Step Back</button>\n'
            '        </div>\n'
            '      </div>'
        )

    parent_delta_id = str(ledger.get("parent_delta_id") or "").strip()
    parent_ticket = str(ledger.get("parent_ticket") or "").strip()
    if parent_delta_id:
        parent_href = "/" + parent_ticket.lstrip("/") if parent_ticket else "/"
        parent_label = html_mod.escape(parent_ticket or "Parent Odin Ticket")
        banner_parts.append(
            f'      <div id="odin-child-delta-origin-banner" style="background:linear-gradient(90deg,rgba(168,85,247,0.18),rgba(56,189,248,0.12));'
            f'border:1px solid rgba(168,85,247,0.58);border-left:5px solid var(--violet);border-radius:14px;padding:0.85rem 1.2rem;'
            f'display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.75rem;box-shadow:0 8px 20px rgba(0,0,0,0.12);">\n'
            f'        <div style="display:flex;align-items:center;gap:0.55rem;flex-wrap:wrap;">\n'
            f'          <span class="badge-active" style="border-color:rgba(168,85,247,0.65);color:var(--violet);">🧠 MÍMIR HARNESS DELTA TICKET</span>\n'
            f'          <code style="font-weight:700;color:var(--cyan);">{html_mod.escape(parent_delta_id)}</code>\n'
            f'          <span style="font-size:0.83rem;color:var(--text);">Spawned from <code>{parent_label}</code> (parent ticket left untouched in place). Completing this ticket auto-marks the parent delta <strong>APPLIED</strong>.</span>\n'
            f'        </div>\n'
            f'        <a href="{html_mod.escape(parent_href)}" class="btn" style="text-decoration:none;border-color:rgba(56,189,248,0.55);color:var(--cyan);font-weight:700;">🔙 Open Parent Ticket ↗</a>\n'
            f'      </div>'
        )

    if deltas:
        in_progress_deltas = [d for d in deltas if str(d.get("status") or "").lower() == "in_progress"]
        is_open = " open"
        if len(pending_deltas) > 0:
            header_badge = f'<span class="badge-awaiting_human">⏳ {len(pending_deltas)} PENDING APPROVAL</span>'
        elif len(in_progress_deltas) > 0:
            header_badge = f'<span class="badge-active">⚡ {len(in_progress_deltas)} IN PROGRESS (TICKET SPAWNED)</span>'
        else:
            header_badge = f'<span class="badge-done">✅ {len(deltas)} APPROVED &amp; APPLIED</span>'
        items_html = []
        for d in deltas:
            d_id = html_mod.escape(str(d.get("id") or "delta"))
            d_prio = html_mod.escape(str(d.get("priority") or "p1").upper())
            d_repo = html_mod.escape(str(d.get("repo") or "odin"))
            d_target = html_mod.escape(str(d.get("target_file") or ""))
            d_cat = html_mod.escape(str(d.get("category") or "harness"))
            d_iter = html_mod.escape(str(d.get("iteration") or 1))
            d_root = html_mod.escape(str(d.get("root_cause") or ""))
            d_diff = html_mod.escape(str(d.get("proposed_diff") or ""))
            d_status = str(d.get("status") or "pending").lower()
            spawned_ticket_id = str(d.get("spawned_ticket_id") or "").strip()
            spawned_ticket_url = str(d.get("spawned_ticket_url") or (f"/{spawned_ticket_id.lstrip('/')}" if spawned_ticket_id else "")).strip()

            if d_status == "applied":
                st_badge = '<span class="badge-done">✅ APPROVED &amp; APPLIED</span>'
            elif d_status == "in_progress":
                st_badge = '<span class="badge-active">⚡ IN PROGRESS (TICKET SPAWNED)</span>'
            elif d_status == "dismissed":
                st_badge = '<span class="badge-pending">🚫 DISMISSED</span>'
            else:
                st_badge = '<span class="badge-awaiting_human">⏳ PENDING APPROVAL</span>'

            prio_badge = (
                f'<span class="badge-reject">🚨 {d_prio}</span>'
                if d_prio == "P0"
                else f'<span class="badge-awaiting_human">⚡ {d_prio}</span>'
            )

            ticket_link_btn = (
                f'<a href="{html_mod.escape(spawned_ticket_url)}" class="btn" '
                f'style="text-decoration:none;border-color:rgba(56,189,248,0.6);background:rgba(56,189,248,0.16);color:var(--cyan);font-weight:700;">'
                f'🎫 Open Delta Ticket ({html_mod.escape(os.path.basename(spawned_ticket_id or spawned_ticket_url))}) ↗</a>'
                if spawned_ticket_url
                else ""
            )

            spawn_btn_label = (
                "⚡ Re-Wake Agent / Sync Delta Ticket"
                if spawned_ticket_url and d_status == "in_progress"
                else "⚡ Approve &amp; Spawn Delta Ticket"
            )
            spawn_btn = (
                f'<button type="button" class="btn" data-odin-delta-spawn="{d_id}" '
                f'style="border-color:rgba(16,185,129,0.65);background:rgba(16,185,129,0.22);color:var(--emerald);font-weight:700;">'
                f'{spawn_btn_label}</button>'
                if d_status != "applied"
                else ""
            )
            approve_btn = (
                f'<button type="button" class="btn" data-odin-delta-btn="applied" data-delta-id="{d_id}" '
                f'title="Mark this delta as already applied in the codebase without spawning a worker task" '
                f'style="border-color:rgba(148,163,184,0.45);color:var(--text);">'
                f'☑️ Mark Already Applied</button>'
                if d_status != "applied"
                else ""
            )
            dismiss_btn = (
                f'<button type="button" class="btn" data-odin-delta-btn="dismissed" data-delta-id="{d_id}" '
                f'style="border-color:rgba(244,63,94,0.45);background:rgba(244,63,94,0.12);color:var(--crimson);">'
                f'🚫 Dismiss</button>'
                if d_status != "dismissed"
                else ""
            )
            reopen_btn = (
                f'<button type="button" class="btn btn-gold" data-odin-delta-btn="pending" data-delta-id="{d_id}">'
                f'↩️ Reopen as Pending</button>'
                if d_status != "pending"
                else ""
            )

            items_html.append(
                f'          <div class="odin-top-delta-item" data-delta-id="{d_id}" data-status="{html_mod.escape(d_status)}" '
                f'style="margin-top:0.75rem;padding:0.95rem 1.1rem;border-radius:12px;background:var(--surface-elevated);border:1px solid rgba(168,85,247,0.38);">\n'
                f'            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.55rem;">\n'
                f'              <div style="display:flex;align-items:center;gap:0.45rem;flex-wrap:wrap;">\n'
                f'                {prio_badge}\n'
                f'                {st_badge}\n'
                f'                <code style="font-weight:700;color:var(--violet);">{d_id}</code>\n'
                f'                <span style="font-size:0.78rem;color:var(--muted);">📦 <strong>{d_repo}</strong> &rarr; <code>{d_target}</code> ({d_cat} &bull; Iter {d_iter})</span>\n'
                f'              </div>\n'
                f'              <div style="display:flex;gap:0.4rem;flex-wrap:wrap;">\n'
                f'                {ticket_link_btn}\n'
                f'                {spawn_btn}\n'
                f'                {approve_btn}\n'
                f'                {dismiss_btn}\n'
                f'                {reopen_btn}\n'
                f'                <button type="button" class="btn" data-odin-delta-copy="{d_id}">📋 Copy Diff</button>\n'
                f'              </div>\n'
                f'            </div>\n'
                f'            <div style="font-size:0.86rem;line-height:1.5;margin-bottom:0.45rem;">\n'
                f'              <strong style="color:var(--gold);">What this Delta is about (Root Cause):</strong> {d_root}\n'
                f'            </div>\n'
                f'            <div style="font-size:0.76rem;color:var(--muted);margin-bottom:0.25rem;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;">Proposed Harness Rule / Script Diff:</div>\n'
                f'            <pre style="margin:0;padding:0.75rem 0.95rem;background:#070b14;border:1px solid #1e293b;border-radius:8px;overflow-x:auto;font-size:0.8rem;"><code>{d_diff}</code></pre>\n'
                f'          </div>'
            )

        banner_parts.append(
            f'      <details id="odin-top-mimir-banner" class="odin-top-delta-box"{is_open} '
            f'style="background:linear-gradient(90deg,rgba(168,85,247,0.14),rgba(56,189,248,0.08));'
            f'border:1px solid rgba(168,85,247,0.5);border-left:5px solid var(--violet);border-radius:14px;padding:0.95rem 1.25rem;'
            f'box-shadow:0 10px 25px rgba(0,0,0,0.12);">\n'
            f'        <summary style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.6rem;font-weight:700;font-size:0.95rem;list-style:none;">\n'
            f'          <span style="display:flex;align-items:center;gap:0.55rem;flex-wrap:wrap;">\n'
            f'            <span>🧠 Mímir Harness Delta Proposal{"s" if len(deltas) != 1 else ""} ({len(deltas)})</span>\n'
            f'            {header_badge}\n'
            f'          </span>\n'
            f'          <span style="font-size:0.76rem;color:var(--cyan);font-family:monospace;">Click ⚡ Approve &amp; Spawn Delta Ticket to execute in a dedicated ticket ▾</span>\n'
            f'        </summary>\n'
            + "\n".join(items_html)
            + "\n      </details>"
        )

    top_wrapper_html = (
        '    <script data-odin-v2-guard>window.__odinV2CoEditPatched = true;</script>\n'
        '    <div id="odin-top-command-banners" style="margin-bottom:1.5rem;display:flex;flex-direction:column;gap:0.9rem;">\n'
        + render_ports("\n".join(banner_parts), CONFIG)
        + "\n    </div>"
        if banner_parts
        else '    <div id="odin-top-command-banners" style="display:none;"></div>'
    )

    content, replaced_existing = _replace_or_strip_top_command_banners(content, top_wrapper_html)
    if not replaced_existing:
        if '<nav class="bifrost-stepper"' in content:
            content = content.replace(
                '<nav class="bifrost-stepper"',
                top_wrapper_html + '\n    <nav class="bifrost-stepper"',
                1,
            )
        elif '<main class="container">' in content:
            content = content.replace(
                '<main class="container">',
                '<main class="container">\n' + top_wrapper_html,
                1,
            )
        elif '</header>' in content:
            content = content.replace(
                '</header>',
                '</header>\n' + top_wrapper_html,
                1,
            )
        elif '<section id=' in content:
            content = content.replace(
                '<section id=',
                top_wrapper_html + '\n  <section id=',
                1,
            )

    script_pat = re.compile(
        r'\s*<script[^>]*id=["\']odin-top-banner-actions["\'][^>]*>[\s\S]*?</script>',
        re.IGNORECASE,
    )
    if script_pat.search(content):
        content = script_pat.sub(lambda _m: "\n" + ODIN_TOP_BANNER_SCRIPT, content, count=1)
    else:
        body_close = re.search(r"</body>", content, re.IGNORECASE)
        if body_close:
            idx = body_close.start()
            content = content[:idx] + ODIN_TOP_BANNER_SCRIPT + "\n" + content[idx:]
        else:
            content = content + "\n" + ODIN_TOP_BANNER_SCRIPT

    if "odin-diff-viewer" in content or "odin-diff-styles" in content:
        content = ensure_diff_styles(content)

    return content


def refresh_state_diff_viewers(content: str, filepath: str = "") -> str:
    viewer_re = re.compile(
        r'<div\b[^>]*\bclass=["\'][^"\']*odin-diff-viewer[^"\']*["\'][^>]*>',
        re.IGNORECASE,
    )
    matches = []
    for vm in viewer_re.finditer(content):
        sp = _find_balanced_tag_span(content, vm, "div")
        if sp:
            matches.append((sp, vm.group(0)))

    for (s_idx, e_idx), tag_str in reversed(matches):
        wt_m = re.search(r'\bdata-worktree=["\']([^"\']+)["\']', tag_str, re.IGNORECASE)
        wt_dir = html_mod.unescape(wt_m.group(1)).strip() if wt_m else _resolve_worktree_from_html(content, None)
        if not wt_dir or not os.path.isdir(wt_dir):
            continue
        ctx_m = re.search(r'\bdata-context-lines=["\'](\d+)["\']', tag_str, re.IGNORECASE)
        ctx_lines = int(ctx_m.group(1)) if ctx_m else 12
        rat_m = re.search(r'\bdata-rationale=["\']([^"\']*)["\']', tag_str, re.IGNORECASE)
        rationale_val = html_mod.unescape(rat_m.group(1)) if rat_m else ""
        fn_m = re.search(r'\bdata-file-notes=["\']([^"\']*)["\']', tag_str, re.IGNORECASE)
        file_notes_val = {}
        if fn_m:
            try:
                parsed_fn = json.loads(html_mod.unescape(fn_m.group(1)))
                if isinstance(parsed_fn, dict):
                    file_notes_val = parsed_fn
            except Exception:
                pass
        new_viewer_html = generate_contextual_diff_html(
            worktree_dir=wt_dir,
            context_lines=ctx_lines,
            rationale=rationale_val,
            file_notes=file_notes_val,
            show_pre_pr_banner=False,
        )
        content = content[:s_idx] + new_viewer_html + content[e_idx:]

    content = ensure_diff_styles(content)
    ledger = extract_ledger(content, filepath or "state.html")
    content = save_ledger_into_html(content, ledger)
    return content


def cmd_refresh_diff(args: argparse.Namespace) -> int:
    try:
        html_path = _resolve_html_path(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not html_path.exists():
        print(f"Error: File not found: {html_path}", file=sys.stderr)
        return 1
    content = html_path.read_text(encoding="utf-8")
    updated = refresh_state_diff_viewers(content, str(html_path))
    html_path.write_text(updated, encoding="utf-8")
    print(f"Refreshed contextual diff viewers and sticky review controls in {html_path.name}.")
    return 0


def save_ledger_into_html(content: str, ledger: dict) -> str:
    content = sync_top_banners_in_html(content, ledger)
    json_str = json.dumps(ledger, indent=2, ensure_ascii=False)
    script_block = f'  <script type="application/json" id="odin-iteration-ledger">\n{json_str}\n  </script>'
    pattern = re.compile(
        r'\s*<script[^>]*id=["\']odin-iteration-ledger["\'][^>]*>.*?</script>',
        re.DOTALL | re.IGNORECASE,
    )
    if pattern.search(content):
        return pattern.sub(lambda _m: "\n" + script_block, content, count=1)
    body_close = re.search(r"</body>", content, re.IGNORECASE)
    if body_close:
        idx = body_close.start()
        return content[:idx] + script_block + "\n" + content[idx:]
    return content + "\n" + script_block + "\n"


AI_PHASE_ORDER = ["huginn", "thor", "heimdall", "hlidskjalf"]


def _get_section_status(content: str, phase_key: str) -> str:
    sec_id, _, _ = PHASE_MAP[phase_key]
    sec_pat = SECTION_REGEX_MAP.get(sec_id, re.escape(sec_id))
    m = re.search(
        rf'<section[^>]*id=["\']{sec_pat}["\'][^>]*data-status=["\']([^"\']+)["\']',
        content,
        re.IGNORECASE,
    )
    return m.group(1).strip().lower() if m else "pending"


def _mark_open_questions_resolved(content: str) -> str:
    return re.sub(
        r'(<div\b(?![^>]*\bdata-resolved=)[^>]*\bclass=["\'][^"\']*open-questions-box[^"\']*["\'])',
        r'\1 data-resolved="true"',
        content,
        flags=re.IGNORECASE,
    )


def update_document_title(content: str) -> str:
    statuses = [_get_section_status(content, k) for k in AI_PHASE_ORDER]
    human_status = _get_section_status(content, "human")
    if "reject" in statuses or human_status == "reject":
        prefix = "🚨 [REJECT] "
        badge_cls, badge_txt = "badge-reject", "🚨 REJECT"
    elif "awaiting_human" in statuses:
        prefix = "❓ [OPEN QUESTIONS] "
        badge_cls, badge_txt = "badge-awaiting_human", "❓ OPEN QUESTIONS"
    elif human_status == "done":
        prefix = "✅ [DONE] "
        badge_cls, badge_txt = "badge-done", "✅ DONE"
    elif all(s == "done" for s in statuses):
        prefix = "👑 [AWAITING SIGN-OFF] "
        badge_cls, badge_txt = "badge-awaiting_human", "👑 AWAITING SIGN-OFF"
    elif "active" in statuses or "done" in statuses:
        prefix = "⚡ [ACTIVE] "
        badge_cls, badge_txt = "badge-active", "⚡ ACTIVE"
    else:
        prefix = "⏳ "
        badge_cls, badge_txt = "badge-pending", "⏳ PENDING"

    def _repl(m: re.Match) -> str:
        open_tag, raw_title, close_tag = m.group(1), m.group(2), m.group(3)
        cleaned = re.sub(
            r"^(?:✅\s*\[DONE\]\s*|👑\s*\[AWAITING SIGN-OFF\]\s*|⚡\s*\[ACTIVE\]\s*|❓\s*\[OPEN QUESTIONS\]\s*|🚨\s*\[REJECT\]\s*|⏳\s*)+",
            "",
            raw_title.strip(),
            flags=re.IGNORECASE,
        )
        return f"{open_tag}{prefix}{cleaned}{close_tag}"

    content = re.sub(r"(<title[^>]*>)([\s\S]*?)(</title>)", _repl, content, count=1, flags=re.IGNORECASE)
    content = re.sub(
        r'(<span\b[^>]*\bid=["\']odin-overall-badge["\'][^>]*\bclass=["\'])[^"\']*(["\'][^>]*>)(?:(?!</span>)[\s\S])*?(</span>)',
        rf"\g<1>{badge_cls}\g<2>{badge_txt}\g<3>",
        content,
        count=1,
        flags=re.IGNORECASE,
    )
    return content


def set_step_and_section_status(content: str, phase_key: str, new_status: str) -> str:
    sec_id, step_id, _ = PHASE_MAP[phase_key]
    sec_pat = SECTION_REGEX_MAP.get(sec_id, re.escape(sec_id))
    status_upper = "👑 AWAITING SIGN-OFF" if (phase_key == "human" and new_status == "awaiting_human") else new_status.upper()

    # Update stepper node data-status / data-step-status and .step-state / .pill-state text
    # Supports both id="step-<phase>" and id="pill-<sec_id>" / id="pill-<phase>"
    step_id_pat = rf"(?:{re.escape(step_id)}|pill-{sec_pat}|pill-{re.escape(phase_key)})"
    step_pattern = re.compile(
        rf'(<(?:div|a)\b[^>]*\bid=["\']{step_id_pat}["\'][^>]*\bdata-(?:step-)?status=["\'])([^"\']+)(["\'][^>]*>(?:(?!</(?:div|a)>)[\s\S])*?<(?:div|span)\s+class=["\'](?:step-state|pill-state)["\'][^>]*>)((?:(?!</(?:div|span)>)[\s\S])*?)(</(?:div|span)>)',
        re.IGNORECASE,
    )
    if step_pattern.search(content):
        content = step_pattern.sub(rf"\g<1>{new_status}\g<3>{status_upper}\g<5>", content, count=1)

    # Update section data-status and header .tag or .badge-* (bounded within <h2>...</h2> or <div class="phase-header">...</div>)
    sec_h2_tag_pattern = re.compile(
        rf'(<section\b[^>]*\bid=["\']{sec_pat}["\'][^>]*\bdata-status=["\'])([^"\']+)(["\'][^>]*>\s*<h2\b[^>]*>(?:(?!</h2>)[\s\S])*?<span\s+class=["\']tag["\'][^>]*>)((?:(?!</span>)[\s\S])*?)(</span>\s*</h2>)',
        re.IGNORECASE,
    )
    sec_h2_badge_pattern = re.compile(
        rf'(<section\b[^>]*\bid=["\']{sec_pat}["\'][^>]*\bdata-status=["\'])([^"\']+)(["\'][^>]*>\s*<h2\b[^>]*>(?:(?!</h2>)[\s\S])*?<span\s+class=["\'])badge-[^"\']+(["\'][^>]*>)((?:(?!</span>)[\s\S])*?)(</span>\s*</h2>)',
        re.IGNORECASE,
    )
    sec_phase_header_pattern = re.compile(
        rf'(<section\b[^>]*\bid=["\']{sec_pat}["\'][^>]*\bdata-status=["\'])([^"\']+)(["\'][^>]*>\s*<div\b[^>]*\bclass=["\'][^"\']*phase-header[^"\']*["\'][^>]*>(?:(?!</div>)[\s\S])*?<span\s+class=["\'])badge-[^"\']+(["\'][^>]*>)((?:(?!</span>)[\s\S])*?)(</span>\s*</div>)',
        re.IGNORECASE,
    )
    if sec_h2_tag_pattern.search(content):
        content = sec_h2_tag_pattern.sub(rf"\g<1>{new_status}\g<3>{status_upper}\g<5>", content, count=1)
    elif sec_h2_badge_pattern.search(content):
        content = sec_h2_badge_pattern.sub(
            rf"\g<1>{new_status}\g<3>badge-{new_status}\g<4>{status_upper}\g<6>", content, count=1
        )
    elif sec_phase_header_pattern.search(content):
        content = sec_phase_header_pattern.sub(
            rf"\g<1>{new_status}\g<3>badge-{new_status}\g<4>{status_upper}\g<6>", content, count=1
        )
    else:
        sec_tag_pattern = re.compile(
            rf'(<section\b[^>]*\bid=["\']{sec_pat}["\'][^>]*\bdata-status=["\'])([^"\']+)(["\'])',
            re.IGNORECASE,
        )
        content = sec_tag_pattern.sub(rf"\g<1>{new_status}\g<3>", content, count=1)

    return content


def update_global_banner(content: str, text: str, status: str) -> str:
    color_map = {
        "active": ("var(--cyan)", "rgba(6, 182, 212, 0.14)"),
        "awaiting_human": ("var(--purple)", "rgba(168, 85, 247, 0.14)"),
        "done": ("var(--green)", "rgba(16, 185, 129, 0.14)"),
        "reject": ("var(--red)", "rgba(239, 68, 68, 0.16)"),
    }
    fg, bg = color_map.get(status, ("var(--gold)", "rgba(245, 158, 11, 0.14)"))
    pill_html = (
        f'<div class="status-pill" id="global-status" '
        f'style="border-color:{fg}; background:{bg}; color:{fg};">'
        f"{html_mod.escape(text)}</div>"
    )
    return re.sub(
        r'<div[^>]*id=["\']global-status["\'][^>]*>.*?</div>',
        pill_html,
        content,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )


def collapse_prior_details_in_section(sec_body: str, prev_iter: int, phase_label: str) -> str:
    """
    Ensures existing content in sec_body is wrapped in <details class="odin-iteration-card">
    if legacy, and strips 'open' from all prior <details class="odin-iteration-card">.
    """
    if 'class="odin-iteration-card"' not in sec_body and "class='odin-iteration-card'" not in sec_body:
        stripped = sec_body.strip()
        if stripped and not stripped.startswith("<!--"):
            wrapped = (
                f'\n      <details class="odin-iteration-card" data-iteration="{prev_iter}">\n'
                f"        <summary>\n"
                f"          <span>Iteration {prev_iter} — {html_mod.escape(phase_label)} (Archived)</span>\n"
                f'          <span class="tag">DONE</span>\n'
                f"        </summary>\n"
                f'        <div style="margin-top: 12px;">\n{stripped}\n        </div>\n'
                f"      </details>\n"
            )
            return wrapped

    # Remove ` open` attribute from existing odin-iteration-card details tags
    return re.sub(
        r'(<details\s+[^>]*class=["\'][^"\']*odin-iteration-card[^"\']*["\'][^>]*?)\s+open(\s*>)',
        r"\1\2",
        sec_body,
        flags=re.IGNORECASE,
    )


def mutate_section_body(content: str, sec_id: str, mutator_fn) -> str:
    sec_pat = SECTION_REGEX_MAP.get(sec_id, re.escape(sec_id))
    pattern = re.compile(
        rf'(<section\b[^>]*\bid=["\']{sec_pat}["\'][^>]*>\s*(?:<div\b[^>]*\bclass=["\'][^"\']*phase-header[^"\']*["\'][^>]*>(?:(?!</div>)[\s\S])*?</div>|<h2\b[^>]*>(?:(?!</h2>)[\s\S])*?</h2>))((?:(?!</section>)[\s\S])*?)(</section>)',
        re.IGNORECASE,
    )
    match = pattern.search(content)
    if not match:
        return content
    prefix, body, suffix = match.group(1), match.group(2), match.group(3)
    new_body = mutator_fn(body)
    return content[: match.start()] + prefix + new_body + suffix + content[match.end() :]


def _find_iteration_card(body: str, iteration: int) -> tuple[int, int] | None:
    open_card_re = re.compile(
        rf'<details\b[^>]*\bclass=["\'][^"\']*odin-iteration-card[^"\']*["\'][^>]*\bdata-iteration=["\']{iteration}["\'][^>]*>',
        re.IGNORECASE,
    )
    open_m = open_card_re.search(body)
    return _find_balanced_tag_span(body, open_m, "details") if open_m else None


def _extract_balanced(html: str, open_re: str, tag: str) -> str:
    m = re.search(open_re, html, re.IGNORECASE)
    if not m:
        return ""
    span = _find_balanced_tag_span(html, m, tag)
    return html[span[0] : span[1]] if span else ""


def _extract_progress_log(card_html: str) -> str:
    return _extract_balanced(card_html, r'<ol\b[^>]*\bclass=["\'][^"\']*odin-progress-log[^"\']*["\'][^>]*>', "ol")


def _extract_card_body(card_html: str) -> str:
    # Matches both odin-iteration-body (written here) and iteration-body (template placeholder cards).
    return _extract_balanced(card_html, r'<div\b[^>]*\bclass=["\'][^"\']*\biteration-body\b[^"\']*["\'][^>]*>', "div")


def _build_iteration_card(iteration: int, summary_title: str, status: str, body_div: str, log_html: str = "") -> str:
    log_part = f"\n        {log_html}" if log_html else ""
    return (
        f'\n      <details class="odin-iteration-card" data-iteration="{iteration}" open>\n'
        f"        <summary>\n"
        f"          <span>{summary_title}</span>\n"
        f'          <span class="tag">{status.upper()}</span>\n'
        f"        </summary>\n"
        f"        {body_div}{log_part}\n"
        f"      </details>\n    "
    )


def upsert_iteration_card_in_body(
    body: str, iteration: int, summary_title: str, status: str, inner_html: str, keep_body: bool = False
) -> str:
    """Create or update the card for `iteration`.

    The card's progress log (see `cmd_log`) always survives an update. With
    keep_body, an existing card keeps its body and only the title/status change.
    """
    new_body_div = (
        f'<div class="odin-iteration-body" style="margin-top: 12px;">\n'
        f"          {inner_html}\n"
        f"        </div>"
    )
    bal = _find_iteration_card(body, iteration)
    if bal:
        s_start, s_end = bal
        old_card = body[s_start:s_end]
        body_div = (_extract_card_body(old_card) if keep_body else "") or new_body_div
        card_html = _build_iteration_card(iteration, summary_title, status, body_div, _extract_progress_log(old_card))
        while s_start > 0 and body[s_start - 1] in (" ", "\t", "\n", "\r"):
            s_start -= 1
        return body[:s_start] + card_html.rstrip() + body[s_end:].rstrip() + "\n    "
    card_html = _build_iteration_card(iteration, summary_title, status, new_body_div)

    # Collapse prior cards and append new card
    collapsed = re.sub(
        r'(<details\s+[^>]*class=["\'][^"\']*odin-iteration-card[^"\']*["\'][^>]*?)\s+open(\s*>)',
        r"\1\2",
        body,
        flags=re.IGNORECASE,
    )
    # Remove only placeholder "Awaiting ..." waiting paragraphs if no prior iteration cards exist
    if 'class="odin-iteration-card"' not in collapsed and "class='odin-iteration-card'" not in collapsed:
        collapsed = re.sub(
            r"\s*<p\b[^>]*>\s*Awaiting\b(?:(?!</p>)[\s\S])*?</p>\s*",
            "",
            collapsed,
            flags=re.IGNORECASE,
        )
    return collapsed.rstrip() + card_html


# Progress log: an append-only timeline inside each iteration card, so work is
# recorded as it happens instead of in one fragment at the end of a phase.
PROGRESS_LOG_KINDS = {
    "step": "▸",
    "phase": "🚦",
    "commit": "🔨",
    "test": "🧪",
    "finding": "🔎",
    "decision": "⚖️",
    "plan-change": "🗺️",
    "valkyrie": "⚔️",
    "incident": "🚨",
    "env": "🌿",
}

ODIN_PROGRESS_LOG_STYLES = """<style id="odin-progress-log-styles">
    .odin-progress-log { list-style: none; margin: 14px 0 0; padding: 10px 0 0; border-top: 1px dashed var(--border, #334155); font-size: 0.84rem; }
    .odin-progress-log > li { display: grid; grid-template-columns: 1.6em 3.4em 1fr; gap: 0.4rem; align-items: baseline; padding: 3px 0; }
    .odin-progress-log time { color: var(--muted, #94a3b8); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.75rem; }
    .odin-progress-log .odin-log-text { overflow-wrap: anywhere; }
    .odin-progress-log li[data-kind="incident"] .odin-log-text,
    .odin-progress-log li[data-outcome="fail"] .odin-log-text { color: var(--crimson, #f43f5e); }
    .odin-progress-log details { grid-column: 3; }
    .odin-progress-log details > summary { font-size: 0.72rem; color: var(--muted, #94a3b8); cursor: pointer; width: fit-content; }
    .odin-progress-log details pre { white-space: pre-wrap; font-size: 0.75rem; margin: 4px 0 0; color: var(--muted, #94a3b8); }
  </style>"""


def ensure_progress_log_styles(content: str) -> str:
    style_pat = re.compile(r'<style[^>]*id=["\']odin-progress-log-styles["\'][^>]*>[\s\S]*?</style>', re.IGNORECASE)
    if style_pat.search(content):
        return style_pat.sub(lambda _m: ODIN_PROGRESS_LOG_STYLES, content, count=1)
    head_close = re.search(r"</head>", content, re.IGNORECASE)
    if not head_close:
        return content
    idx = head_close.start()
    return content[:idx] + "  " + ODIN_PROGRESS_LOG_STYLES + "\n" + content[idx:]


def render_progress_log_entry(kind: str, text: str, detail: str = "", ts: str = "", outcome: str = "") -> str:
    ts = ts or now_iso()
    try:
        hhmm = datetime.datetime.fromisoformat(ts).strftime("%H:%M")
    except ValueError:
        hhmm = ""
    icon = PROGRESS_LOG_KINDS.get(kind, PROGRESS_LOG_KINDS["step"])
    outcome_attr = f' data-outcome="{html_mod.escape(outcome)}"' if outcome else ""
    detail_html = (
        f"<details><summary>details</summary><pre>{html_mod.escape(detail)}</pre></details>" if detail else ""
    )
    return (
        f'<li data-kind="{html_mod.escape(kind)}"{outcome_attr}>'
        f'<span class="odin-log-icon">{icon}</span>'
        f'<time datetime="{html_mod.escape(ts)}">{hhmm}</time>'
        f'<span class="odin-log-text">{html_mod.escape(text)}</span>{detail_html}</li>'
    )


def append_progress_log(
    content: str, phase_key: str, iteration: int, kind: str, text: str, detail: str = "", ts: str = "", outcome: str = ""
) -> tuple[str, bool]:
    """Append an entry to a phase's iteration card, creating the card if needed.

    Returns (content, appended). An entry identical to the previous one is skipped.
    """
    sec_id, _, phase_title = PHASE_MAP[phase_key]
    entry = render_progress_log_entry(kind, text, detail, ts, outcome)
    appended = False

    def mutate(body: str) -> str:
        nonlocal appended
        bal = _find_iteration_card(body, iteration)
        if not bal:
            status = _get_section_status(content, phase_key)
            body = upsert_iteration_card_in_body(
                body, iteration, f"Iteration {iteration} — {phase_title}", "active" if status == "pending" else status, ""
            )
            bal = _find_iteration_card(body, iteration)
            if not bal:
                return body
        s_start, s_end = bal
        card = body[s_start:s_end]
        log_html = _extract_progress_log(card)
        if log_html:
            entries = re.findall(
                r'<li\b[^>]*\bdata-kind="([^"]*)"[^>]*>[\s\S]*?<span class="odin-log-text">([\s\S]*?)</span>', log_html
            )
            if entries and entries[-1] == (html_mod.escape(kind), html_mod.escape(text)):
                return body
            close_idx = log_html.rfind("</ol>")
            new_log = log_html[:close_idx].rstrip() + "\n          " + entry + "\n        </ol>"
            new_card = card.replace(log_html, new_log, 1)
        else:
            new_log = f'<ol class="odin-progress-log">\n          {entry}\n        </ol>'
            close_idx = card.rfind("</details>")
            new_card = card[:close_idx].rstrip() + "\n        " + new_log + "\n      " + card[close_idx:]
        appended = True
        return body[:s_start] + new_card + body[s_end:]

    content = mutate_section_body(content, sec_id, mutate)
    if appended:
        content = ensure_progress_log_styles(content)
    return content, appended


def record_ledger_event(ledger: dict, iteration: int, event: dict) -> None:
    for it_obj in ledger.setdefault("iterations", []):
        if int(it_obj.get("iteration", 0)) == iteration:
            it_obj.setdefault("events", []).append(event)
            return
    ledger["iterations"].append(
        {"iteration": iteration, "started_at": now_iso(), "status": "active", "phases": {}, "events": [event]}
    )


def current_phase_key(content: str) -> str:
    """The phase work is happening in: the furthest active/awaiting phase, else Thor.

    Furthest, because an earlier phase can be left "active" when a later one pauses.
    """
    for key in reversed((*AI_PHASE_ORDER, "human")):
        if _get_section_status(content, key) in ("active", "awaiting_human"):
            return key
    return "thor"


_held_locks: set[str] = set()


@contextlib.contextmanager
def state_file_lock(html_path: Path):
    """Serialize read-modify-write of a state file across processes (hooks, agents, CLI).

    Re-entrant within a process, so a locked command can call log_progress.
    """
    key = str(Path(html_path).resolve())
    if key in _held_locks:
        yield
        return
    lock_path = Path(html_path).with_name(f".{Path(html_path).name}.lock")
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        _held_locks.add(key)
        try:
            yield
        finally:
            _held_locks.discard(key)
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def log_progress(
    html_path: Path,
    kind: str,
    text: str,
    phase_key: str = "",
    detail: str = "",
    outcome: str = "",
    extra: dict | None = None,
    ts: str = "",
) -> bool:
    """Append a progress entry to a state file and its ledger. Returns False for a skipped duplicate.

    ts backdates the entry (ISO 8601); it defaults to now.
    """
    with state_file_lock(html_path):
        return _log_progress_locked(html_path, kind, text, phase_key, detail, outcome, extra, ts)


def _log_progress_locked(
    html_path: Path, kind: str, text: str, phase_key: str, detail: str, outcome: str, extra: dict | None, ts: str
) -> bool:
    content = html_path.read_text(encoding="utf-8")
    ledger = extract_ledger(content, str(html_path))
    iteration = int(ledger.get("current_iteration", 1))
    phase_key = phase_key or current_phase_key(content)
    ts = ts or now_iso()
    content, appended = append_progress_log(content, phase_key, iteration, kind, text, detail, ts, outcome)
    if not appended:
        return False
    event = {"ts": ts, "phase": phase_key, "kind": kind, "text": text}
    if detail:
        event["detail"] = detail
    if outcome:
        event["outcome"] = outcome
    event.update(extra or {})
    record_ledger_event(ledger, iteration, event)
    content = save_ledger_into_html(content, ledger)
    html_path.write_text(content, encoding="utf-8")
    return True


def _resolve_html_path(args: argparse.Namespace) -> Path:
    raw = getattr(args, "file", None) or getattr(args, "html_file", None)
    if not raw:
        raise ValueError("Missing HTML state file path (pass positionally or via --file)")
    return Path(raw).resolve()


def cmd_step_back(args: argparse.Namespace) -> int:
    try:
        html_path = _resolve_html_path(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not html_path.exists():
        print(f"Error: File not found: {html_path}", file=sys.stderr)
        return 1

    content = html_path.read_text(encoding="utf-8")
    ledger = extract_ledger(content, str(html_path))

    prev_iter = int(ledger.get("current_iteration", 1))
    next_iter = prev_iter + 1
    max_iter = max(int(ledger.get("max_iterations", 5)), next_iter)
    ledger["current_iteration"] = next_iter
    ledger["max_iterations"] = max_iter

    # Mark previous iteration status as stepped_back if still active/awaiting_human
    for it_obj in ledger.get("iterations", []):
        if it_obj.get("iteration") == prev_iter:
            it_obj["ended_at"] = now_iso()
            it_obj["step_back_reason"] = args.reason
            it_obj["step_back_source"] = args.source
            if getattr(args, "root_cause", None):
                it_obj["root_cause"] = args.root_cause

    ledger.setdefault("iterations", []).append(
        {
            "iteration": next_iter,
            "started_at": now_iso(),
            "trigger": args.reason,
            "trigger_source": args.source,
            "root_cause": getattr(args, "root_cause", "") or "",
            "status": "active",
            "phases": {
                "huginn": {"status": "done"},
                "thor": {"status": "active", "summary": f"Iteration {next_iter} started: {args.reason}"},
                "heimdall": {"status": "pending"},
                "hlidskjalf": {"status": "pending"},
                "human": {"status": "active"},
            },
            "harness_delta_proposals": [],
        }
    )

    # Update #iteration-counter
    content = re.sub(
        r'(<span[^>]*id=["\']iteration-counter["\'][^>]*>)\s*\d+\s*/\s*\d+\s*(</span>)',
        rf"\g<1>{next_iter} / {max_iter}\g<2>",
        content,
        flags=re.IGNORECASE,
    )

    reason_esc = html_mod.escape(args.reason)
    ts = now_human()

    # 1. Collapse prior cards in #thor-execution and append Iteration N active card
    def mutate_thor(body: str) -> str:
        collapsed = collapse_prior_details_in_section(body, prev_iter, "Thor & Valkyries Execution")
        new_card = (
            f'\n      <details class="odin-iteration-card" data-iteration="{next_iter}" open>\n'
            f"        <summary>\n"
            f"          <span>⚡ Iteration {next_iter} — Addressing {args.source.upper()} Feedback ({ts})</span>\n"
            f'          <span class="tag">ACTIVE</span>\n'
            f"        </summary>\n"
            f'        <div class="odin-iteration-body" style="margin-top: 12px;">\n'
            f'          <p><strong>Step-Back Trigger ({html_mod.escape(args.source.upper())}):</strong> {reason_esc}</p>\n'
            f"        </div>\n"
            f"      </details>\n    "
        )
        return collapsed.rstrip() + new_card

    # 2. Collapse prior cards in #heimdall-validation and append Iteration N pending card
    def mutate_heimdall(body: str) -> str:
        collapsed = collapse_prior_details_in_section(body, prev_iter, "Heimdall Verification")
        new_card = (
            f'\n      <details class="odin-iteration-card" data-iteration="{next_iter}" open>\n'
            f"        <summary>\n"
            f"          <span>🛡️ Iteration {next_iter} — Heimdall Verification (Pending Phase 2)</span>\n"
            f'          <span class="tag">PENDING</span>\n'
            f"        </summary>\n"
            f'        <div class="odin-iteration-body" style="margin-top: 12px;">\n'
            f'          <p style="color:var(--muted);">Awaiting Iteration {next_iter} Thor execution completion.</p>\n'
            f"        </div>\n"
            f"      </details>\n    "
        )
        return collapsed.rstrip() + new_card

    # 3. Collapse prior cards in #hlidskjalf-judge and append Iteration N pending card
    def mutate_hlidskjalf(body: str) -> str:
        collapsed = collapse_prior_details_in_section(body, prev_iter, "Hlidskjalf Judgment & Retro")
        new_card = (
            f'\n      <details class="odin-iteration-card" data-iteration="{next_iter}" open>\n'
            f"        <summary>\n"
            f"          <span>👑 Iteration {next_iter} — Hlidskjalf Judgment &amp; Harness Audit (Pending Phase 3)</span>\n"
            f'          <span class="tag">PENDING</span>\n'
            f"        </summary>\n"
            f'        <div class="odin-iteration-body" style="margin-top: 12px;">\n'
            f'          <p style="color:var(--muted);">Awaiting Iteration {next_iter} Heimdall verification.</p>\n'
            f"        </div>\n"
            f"      </details>\n    "
        )
        return collapsed.rstrip() + new_card

    # 4. Prepend step-back log box in #human-validation without deleting prior boxes
    def mutate_human(body: str) -> str:
        box = (
            f'\n      <div class="human-verdict-box" data-verdict="active" data-iteration="{next_iter}" '
            f'style="margin: 14px 0; padding: 14px 16px; border-radius: 10px; border: 1px solid var(--cyan); background: rgba(6, 182, 212, 0.12);">\n'
            f'        <div style="font-family: monospace; font-size: 0.78rem; font-weight: 700; color: var(--cyan); margin-bottom: 4px;">'
            f"🔄 ITERATION {next_iter} STEP-BACK ({html_mod.escape(args.source.upper())}) &bull; {ts}</div>\n"
            f'        <div style="color: var(--text); font-size: 0.92rem;">{reason_esc}</div>\n'
            f"      </div>\n"
        )
        return box + body.lstrip()

    content = mutate_section_body(content, "thor-execution", mutate_thor)
    content = mutate_section_body(content, "heimdall-validation", mutate_heimdall)
    content = mutate_section_body(content, "hlidskjalf-judge", mutate_hlidskjalf)
    content = mutate_section_body(content, "human-validation", mutate_human)

    content = set_step_and_section_status(content, "huginn", "done")
    content = set_step_and_section_status(content, "thor", "active")
    content = set_step_and_section_status(content, "heimdall", "pending")
    content = set_step_and_section_status(content, "hlidskjalf", "pending")
    content = set_step_and_section_status(content, "human", "active")
    content = _mark_open_questions_resolved(content)
    content = update_global_banner(
        content, f"PHASE 2: THOR & VALKYRIES — ITERATION {next_iter} (ACTIVE)", "active"
    )
    content = update_document_title(content)
    content = save_ledger_into_html(content, ledger)

    html_path.write_text(content, encoding="utf-8")
    log_progress(
        html_path,
        "phase",
        f"Iteration {next_iter} started ({args.source}): {args.reason}",
        phase_key="thor",
        detail=f"Root cause: {args.root_cause}" if args.root_cause else "",
    )
    trigger_notify(
        f"Phase 2: Thor — Iteration {next_iter}",
        f"Stepped back ({args.source}): {args.reason[:80]}",
        "phase",
        html_path,
    )
    print(f"Stepped back {html_path.name} to Iteration {next_iter} (prior iterations 1..{prev_iter} preserved).")
    return 0


ODIN_DIFF_STYLES = """<style id="odin-diff-styles">
  /* Odin Contextual Code Diff, Inline Review & File Editor */
  .odin-diff-viewer {
    margin: 1rem 0 0.5rem;
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 14px;
    background: #070b14;
    color: #f8fafc;
    color-scheme: dark;
    overflow: clip;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
  }
  .odin-diff-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.75rem 1.1rem;
    background: linear-gradient(90deg, rgba(15, 23, 42, 0.98), rgba(22, 32, 56, 0.98));
    border-bottom: 1px solid #1e293b;
  }
  .odin-diff-toolbar-title {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.55rem;
    font-weight: 800;
    font-size: 0.92rem;
    color: #f8fafc !important;
  }
  .odin-diff-toolbar-title code {
    color: #38bdf8 !important;
    background: rgba(15, 23, 42, 0.85) !important;
    padding: 0.12rem 0.4rem;
    border-radius: 5px;
    border: 1px solid rgba(56, 189, 248, 0.3);
  }
  .odin-diff-stat-add {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.76rem;
    font-weight: 700;
    color: #34d399 !important;
    background: rgba(16, 185, 129, 0.16);
    border: 1px solid rgba(16, 185, 129, 0.4);
    padding: 0.14rem 0.5rem;
    border-radius: 999px;
  }
  .odin-diff-stat-del {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.76rem;
    font-weight: 700;
    color: #fb7185 !important;
    background: rgba(244, 63, 94, 0.16);
    border: 1px solid rgba(244, 63, 94, 0.4);
    padding: 0.14rem 0.5rem;
    border-radius: 999px;
  }
  .odin-diff-approved-pill {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.75rem;
    font-weight: 700;
    color: #cbd5e1 !important;
    background: rgba(30, 41, 59, 0.75);
    border: 1px solid rgba(148, 163, 184, 0.35);
    padding: 0.16rem 0.6rem;
    border-radius: 999px;
  }
  .odin-diff-approved-pill.all-approved {
    color: #34d399 !important;
    background: rgba(16, 185, 129, 0.22);
    border-color: rgba(16, 185, 129, 0.55);
  }
  .odin-diff-mode-group {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
  }
  .odin-diff-btn {
    appearance: none;
    border: 1px solid #334155;
    background: #0f172a;
    color: #e2e8f0 !important;
    font-size: 0.74rem;
    font-weight: 700;
    padding: 0.32rem 0.7rem;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .odin-diff-btn:hover {
    border-color: #38bdf8;
    color: #f8fafc !important;
  }
  .odin-diff-btn.active {
    background: rgba(56, 189, 248, 0.2);
    border-color: #38bdf8;
    color: #38bdf8 !important;
  }
  .odin-diff-rationale-banner {
    padding: 0.9rem 1.15rem;
    background: linear-gradient(90deg, rgba(245, 158, 11, 0.16), rgba(56, 189, 248, 0.10)) !important;
    border-bottom: 1px solid rgba(245, 158, 11, 0.35);
    color: #f1f5f9 !important;
    font-size: 0.88rem;
    line-height: 1.55;
  }
  .odin-diff-rationale-banner strong {
    color: #fbbf24 !important;
  }
  .odin-diff-rationale-banner code {
    color: #38bdf8 !important;
    background: rgba(15, 23, 42, 0.8) !important;
    padding: 0.1rem 0.35rem;
    border-radius: 4px;
  }
  .odin-diff-file-card {
    border-bottom: 1px solid #1e293b;
    margin: 0;
    position: relative;
    overflow-anchor: none;
  }
  .odin-diff-file-card:last-child {
    border-bottom: none;
  }
  .odin-diff-file-card[data-file-approved="true"] {
    border-left: 3px solid #10b981;
  }
  .odin-diff-file-card > summary {
    cursor: pointer;
    list-style: none;
    position: sticky;
    top: var(--odin-sticky-top-offset, 48px);
    z-index: 35;
    container-type: scroll-state;
    container-name: odin-diff-file-header;
    padding: 0;
    margin: 0;
    background: transparent !important;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.81rem;
    font-weight: 700;
    color: #f1f5f9 !important;
    user-select: none;
  }
  .odin-diff-file-card > summary::-webkit-details-marker { display: none; }
  .odin-diff-file-summary-inner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.65rem;
    width: 100%;
    padding: 0.65rem 1rem;
    background: rgba(15, 23, 42, 0.96) !important;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid #1e293b;
    transition: background 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
  }
  .odin-diff-file-card[data-file-approved="true"] > summary .odin-diff-file-summary-inner {
    background: linear-gradient(90deg, rgba(16, 185, 129, 0.18), rgba(15, 23, 42, 0.98)) !important;
  }
  @container odin-diff-file-header scroll-state(stuck: top) {
    .odin-diff-file-summary-inner {
      background: rgba(11, 17, 32, 0.98) !important;
      border-bottom-color: rgba(56, 189, 248, 0.55);
      box-shadow: 0 10px 24px -6px rgba(0, 0, 0, 0.72), 0 2px 8px rgba(56, 189, 248, 0.18);
    }
  }
  .odin-diff-file-card > summary.is-stuck .odin-diff-file-summary-inner {
    background: rgba(11, 17, 32, 0.98) !important;
    border-bottom-color: rgba(56, 189, 248, 0.55);
    box-shadow: 0 10px 24px -6px rgba(0, 0, 0, 0.72), 0 2px 8px rgba(56, 189, 248, 0.18);
  }
  .odin-diff-file-meta {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .odin-diff-file-path-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: #f8fafc !important;
    font-weight: 800;
  }
  .odin-diff-line-hint {
    font-size: 0.7rem;
    font-weight: 600;
    color: #94a3b8;
    background: rgba(30, 41, 59, 0.7);
    border: 1px solid rgba(148, 163, 184, 0.25);
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
  }
  .odin-diff-action-btn {
    appearance: none;
    cursor: pointer;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.73rem;
    font-weight: 700;
    padding: 0.22rem 0.6rem;
    border-radius: 6px;
    transition: all 0.15s ease;
  }
  .odin-diff-approve-btn {
    color: #cbd5e1 !important;
    background: rgba(30, 41, 59, 0.75);
    border: 1px solid rgba(148, 163, 184, 0.4);
  }
  .odin-diff-approve-btn:hover {
    border-color: #34d399;
    color: #ecfdf5 !important;
    background: rgba(16, 185, 129, 0.18);
  }
  .odin-diff-approve-btn.approved,
  .odin-diff-approve-btn.is-approved {
    color: #34d399 !important;
    background: rgba(16, 185, 129, 0.22);
    border: 1px solid rgba(16, 185, 129, 0.65);
  }
  .odin-diff-edit-btn {
    color: #fbbf24 !important;
    background: rgba(245, 158, 11, 0.16);
    border: 1px solid rgba(245, 158, 11, 0.48);
  }
  .odin-diff-edit-btn:hover,
  .odin-diff-edit-btn.active {
    background: rgba(245, 158, 11, 0.3);
    border-color: #fbbf24;
  }
  .odin-diff-comment-btn {
    color: #c084fc !important;
    background: rgba(168, 85, 247, 0.16);
    border: 1px solid rgba(168, 85, 247, 0.45);
  }
  .odin-diff-comment-btn:hover,
  .odin-diff-comment-btn.active {
    background: rgba(168, 85, 247, 0.28);
    border-color: #c084fc;
  }
  .odin-diff-editor-link {
    color: #38bdf8 !important;
    text-decoration: none;
    font-size: 0.73rem;
    padding: 0.18rem 0.5rem;
    border-radius: 6px;
    border: 1px solid rgba(56, 189, 248, 0.35);
    background: rgba(56, 189, 248, 0.1);
  }
  .odin-diff-editor-link:hover {
    background: rgba(56, 189, 248, 0.22);
  }
  .odin-file-comment-drawer {
    position: sticky;
    top: calc(var(--odin-sticky-top-offset, 48px) + 44px);
    z-index: 34;
    padding: 0.75rem 1.05rem;
    background: rgba(15, 23, 42, 0.98) !important;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 2px solid #a855f7;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.65);
    color: #f8fafc !important;
  }
  .odin-diff-file-note {
    padding: 0.7rem 1.05rem;
    background: rgba(56, 189, 248, 0.1) !important;
    border-bottom: 1px solid rgba(56, 189, 248, 0.25);
    border-left: 3px solid #38bdf8;
    font-size: 0.84rem;
    color: #f1f5f9 !important;
  }
  .odin-diff-file-note strong {
    color: #38bdf8 !important;
  }
  .odin-diff-hunk-header {
    padding: 0.35rem 0.9rem;
    background: rgba(30, 41, 59, 0.85) !important;
    border-top: 1px solid #1e293b;
    border-bottom: 1px solid #1e293b;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.75rem;
    color: #cbd5e1 !important;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .odin-diff-hunk-scope {
    color: #c084fc !important;
    font-weight: 700;
  }
  .odin-diff-table-wrap {
    overflow-x: auto;
    background: #070b14;
  }
  .odin-diff-viewer table.odin-diff-split-table,
  .odin-diff-viewer table.odin-diff-unified-table {
    width: 100%;
    border-collapse: collapse;
    margin: 0;
    border-radius: 0;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.79rem;
    line-height: 1.5;
    table-layout: fixed;
    background: #070b14 !important;
  }
  .odin-diff-viewer[data-view-mode="split"] .odin-diff-unified-table { display: none; }
  .odin-diff-viewer[data-view-mode="unified"] .odin-diff-split-table { display: none; }
  .odin-diff-viewer table.odin-diff-split-table td,
  .odin-diff-viewer table.odin-diff-unified-table td {
    border: none !important;
    border-bottom: 1px solid rgba(30, 41, 59, 0.5) !important;
    padding: 0.14rem 0.55rem;
    vertical-align: top;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .odin-diff-viewer tr.odin-diff-row {
    cursor: pointer;
    transition: background 0.1s ease;
  }
  .odin-diff-viewer tr.odin-diff-row:hover > td.odin-ln {
    color: #38bdf8 !important;
    background: rgba(56, 189, 248, 0.22) !important;
  }
  .odin-diff-viewer tr.odin-diff-row:hover > td.odin-ln::before {
    content: "💬";
    font-size: 0.62rem;
    margin-right: 2px;
  }
  .odin-diff-viewer td.odin-ln {
    width: 52px;
    min-width: 52px;
    max-width: 52px;
    text-align: right;
    color: #94a3b8 !important;
    background: rgba(15, 23, 42, 0.85) !important;
    border-right: 1px solid #1e293b !important;
    user-select: none;
    font-size: 0.72rem;
    padding-right: 0.45rem !important;
  }
  .odin-diff-viewer td.odin-code {
    width: calc(50% - 52px);
    color: #e2e8f0 !important;
    background: #070b14;
  }
  .odin-diff-viewer table.odin-diff-unified-table td.odin-code {
    width: calc(100% - 104px);
  }
  .odin-diff-viewer td.odin-del {
    background: rgba(244, 63, 94, 0.15) !important;
    color: #fecdd3 !important;
  }
  .odin-diff-viewer td.odin-ln-del {
    background: rgba(244, 63, 94, 0.25) !important;
    color: #fda4af !important;
  }
  .odin-diff-viewer td.odin-add {
    background: rgba(16, 185, 129, 0.15) !important;
    color: #d1fae5 !important;
  }
  .odin-diff-viewer td.odin-ln-add {
    background: rgba(16, 185, 129, 0.25) !important;
    color: #6ee7b7 !important;
  }
  .odin-diff-viewer td.odin-empty {
    background: rgba(15, 23, 42, 0.5) !important;
  }
  .odin-diff-viewer mark.odin-word-del {
    background: rgba(244, 63, 94, 0.45) !important;
    color: #fff1f2 !important;
    border-radius: 3px;
    padding: 0 2px;
  }
  .odin-diff-viewer mark.odin-word-add {
    background: rgba(16, 185, 129, 0.45) !important;
    color: #ecfdf5 !important;
    border-radius: 3px;
    padding: 0 2px;
  }
  /* Inline Code Review Comment Rows & Editor */
  .odin-diff-viewer tr.odin-diff-row.is-selected {
    background: rgba(168, 85, 247, 0.22) !important;
    outline: 1px solid rgba(168, 85, 247, 0.55);
  }
  .odin-diff-viewer tr.odin-diff-row.is-selected > td.odin-ln {
    background: rgba(168, 85, 247, 0.35) !important;
    color: #f3e8ff !important;
    font-weight: 800;
  }
  .odin-diff-viewer tr.odin-diff-row.odin-diff-row-has-comment {
    border-left: 3px solid #c084fc !important;
    background: rgba(192, 132, 252, 0.08);
  }
  .odin-diff-viewer tr.odin-diff-row.odin-diff-row-has-comment > td.odin-ln {
    border-left: 3px solid #c084fc !important;
    color: #c084fc !important;
    font-weight: 800;
  }
  .odin-comment-quote-preview {
    background: #090e1a;
    border: 1px solid rgba(168, 85, 247, 0.35);
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    margin-bottom: 0.5rem;
  }
  .odin-comment-quoted-code {
    display: block;
    color: #38bdf8 !important;
    background: rgba(15, 23, 42, 0.95);
    border: 1px solid rgba(56, 189, 248, 0.3);
    padding: 0.35rem 0.6rem;
    border-radius: 6px;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.78rem;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .odin-diff-line-range-picker input::-webkit-inner-spin-button,
  .odin-diff-line-range-picker input::-webkit-outer-spin-button {
    opacity: 1;
  }
  .odin-diff-viewer tr.odin-diff-comment-editor-row td,
  .odin-diff-viewer tr.odin-diff-saved-comment-row td {
    background: #0f172a !important;
    border-top: 1px solid rgba(168, 85, 247, 0.45) !important;
    border-bottom: 1px solid rgba(168, 85, 247, 0.45) !important;
    padding: 0.7rem 1rem !important;
    white-space: normal !important;
    color: #f1f5f9 !important;
  }
  .odin-inline-file-editor {
    padding: 0.9rem 1.05rem;
    background: #0b1120;
    border-bottom: 2px solid #f59e0b;
    color: #f8fafc;
  }
  .odin-inline-file-editor textarea {
    width: 100%;
    min-height: 360px;
    background: #050811;
    color: #f1f5f9;
    border: 1px solid #38bdf8;
    border-radius: 8px;
    padding: 0.75rem 0.9rem;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.8rem;
    line-height: 1.55;
    tab-size: 2;
    resize: vertical;
  }
  /* Pre-PR Code Review Gate Box (adapts cleanly to Light & Dark theme) */
  .odin-pre-pr-gate-box {
    margin: 0.75rem 0 1rem;
    padding: 1rem 1.2rem;
    border-radius: 12px;
    border: 1px solid rgba(245, 158, 11, 0.65);
    border-left: 5px solid #f59e0b;
    background: rgba(245, 158, 11, 0.14);
    color: #f8fafc;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .odin-pre-pr-gate-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .odin-pre-pr-gate-title {
    color: #fbbf24;
    font-size: 0.96rem;
    font-weight: 800;
  }
  .odin-pre-pr-gate-desc {
    font-size: 0.85rem;
    color: #f1f5f9;
    margin-top: 0.22rem;
    line-height: 1.45;
  }
  .odin-pre-pr-gate-review-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
    padding-top: 0.55rem;
    border-top: 1px solid rgba(245, 158, 11, 0.3);
  }
  .odin-inline-gate-note-input {
    flex: 1;
    min-width: 260px;
    background: var(--surface-elevated, #0f172a);
    color: var(--text, #f8fafc);
    border: 1px solid rgba(148, 163, 184, 0.45);
    border-radius: 8px;
    padding: 0.45rem 0.75rem;
    font-size: 0.82rem;
  }
</style>"""


def ensure_diff_styles(content: str) -> str:
    style_pat = re.compile(
        r'\s*<style[^>]*id=["\']odin-diff-styles["\'][^>]*>[\s\S]*?</style>',
        re.IGNORECASE,
    )
    if style_pat.search(content):
        return style_pat.sub(lambda _m: "\n  " + ODIN_DIFF_STYLES, content, count=1)
    head_close = re.search(r"</head>", content, re.IGNORECASE)
    if head_close:
        idx = head_close.start()
        return content[:idx] + "  " + ODIN_DIFF_STYLES + "\n" + content[idx:]
    return ODIN_DIFF_STYLES + "\n" + content


def stamp_pr_url_into_html(content: str, pr_url: str) -> str:
    if not pr_url:
        return content
    pr_esc = html_mod.escape(pr_url.strip())
    pr_num_m = re.search(r"/pull/(\d+)", pr_url)
    pr_label = f"🔗 PR #{pr_num_m.group(1)}" if pr_num_m else "🔗 Pull Request"

    # Stamp data-pr-url on <body>
    if re.search(r'<body[^>]*data-pr-url=["\'][^"\']*["\']', content, re.IGNORECASE):
        content = re.sub(
            r'(<body[^>]*data-pr-url=["\'])[^"\']*(["\'])',
            rf"\g<1>{pr_esc}\g<2>",
            content,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        content = re.sub(
            r"(<body\b)",
            rf'\1 data-pr-url="{pr_esc}"',
            content,
            count=1,
            flags=re.IGNORECASE,
        )

    badge_html = (
        f'<a id="odin-pr-badge" href="{pr_esc}" target="_blank" rel="noopener" '
        f'style="padding:0.15rem 0.55rem;border-radius:6px;background:rgba(16,185,129,0.16);'
        f'border:1px solid rgba(16,185,129,0.45);color:#34d399;text-decoration:none;font-weight:700;">'
        f"{html_mod.escape(pr_label)} ↗</a>"
    )
    if 'id="odin-pr-badge"' in content:
        content = re.sub(
            r'<a[^>]*id=["\']odin-pr-badge["\'][^>]*>.*?</a>',
            badge_html,
            content,
            count=1,
            flags=re.DOTALL | re.IGNORECASE,
        )
    elif 'id="odin-env-bar"' in content:
        content = re.sub(
            r'(<div[^>]*id=["\']odin-env-bar["\'][^>]*>)',
            rf"\g<1>{badge_html}",
            content,
            count=1,
            flags=re.IGNORECASE,
        )
    return content


def _word_level_diff_pair(old_text: str, new_text: str) -> tuple[str, str]:
    old_tokens = re.findall(r"\w+|\s+|[^\w\s]+", old_text)
    new_tokens = re.findall(r"\w+|\s+|[^\w\s]+", new_text)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens)
    old_out = []
    new_out = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        o_seg = html_mod.escape("".join(old_tokens[i1:i2]))
        n_seg = html_mod.escape("".join(new_tokens[j1:j2]))
        if tag == "equal":
            old_out.append(o_seg)
            new_out.append(n_seg)
        elif tag == "delete":
            old_out.append(f'<mark class="odin-word-del">{o_seg}</mark>')
        elif tag == "insert":
            new_out.append(f'<mark class="odin-word-add">{n_seg}</mark>')
        elif tag == "replace":
            old_out.append(f'<mark class="odin-word-del">{o_seg}</mark>')
            new_out.append(f'<mark class="odin-word-add">{n_seg}</mark>')
    return "".join(old_out), "".join(new_out)


def _resolve_worktree_from_html(content: str, explicit_repo: str | None = None) -> str:
    if explicit_repo and os.path.isdir(explicit_repo):
        return os.path.abspath(explicit_repo)
    m = re.search(r'<body[^>]*data-worktree=["\']([^"\']+)["\']', content, re.IGNORECASE)
    if m and os.path.isdir(m.group(1).strip()):
        return os.path.abspath(m.group(1).strip())
    return os.getcwd()


def _parse_file_notes(raw_notes: list[str] | None) -> dict[str, str]:
    notes: dict[str, str] = {}
    if not raw_notes:
        return notes
    for item in raw_notes:
        item = item.strip()
        if not item:
            continue
        if item.startswith("{"):
            try:
                parsed = json.loads(item)
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        notes[str(k).strip()] = str(v).strip()
                    continue
            except Exception:
                pass
        if "=" in item:
            k, v = item.split("=", 1)
            notes[k.strip()] = v.strip()
        elif ":" in item:
            k, v = item.split(":", 1)
            notes[k.strip()] = v.strip()
    return notes


def generate_contextual_diff_html(
    worktree_dir: str,
    context_lines: int = 12,
    rationale: str = "",
    file_notes: dict[str, str] | None = None,
    files_filter: list[str] | None = None,
    show_pre_pr_banner: bool = False,
    pr_url: str = "",
) -> str:
    file_notes = file_notes or {}
    cmd_base = ["git", "-C", worktree_dir, "diff", f"-U{context_lines}", "HEAD"]
    if files_filter:
        cmd_base.append("--")
        cmd_base.extend(files_filter)

    res = subprocess.run(cmd_base, capture_output=True, text=True)
    raw_diff = res.stdout or ""
    diff_source_label = "Working Tree vs. HEAD"

    # Also include untracked working-tree files (e.g. newly added unit test files)
    untracked_cmd = ["git", "-C", worktree_dir, "ls-files", "--others", "--exclude-standard"]
    if files_filter:
        untracked_cmd.append("--")
        untracked_cmd.extend(files_filter)
    untracked_res = subprocess.run(untracked_cmd, capture_output=True, text=True)
    for untracked_rel in (untracked_res.stdout or "").splitlines():
        untracked_rel = untracked_rel.strip()
        if not untracked_rel:
            continue
        no_idx_res = subprocess.run(
            ["git", "-C", worktree_dir, "diff", "--no-index", f"-U{context_lines}", "--", "/dev/null", untracked_rel],
            capture_output=True,
            text=True,
        )
        if (no_idx_res.stdout or "").strip():
            raw_diff = (raw_diff.rstrip("\n") + "\n" + no_idx_res.stdout) if raw_diff.strip() else no_idx_res.stdout

    # Fallback if changes were already committed on the branch
    if not raw_diff.strip():
        configured_bases = [
            r["base_branch"] for r in (CONFIG.get("repos") or {}).values()
            if r.get("base_branch") and os.path.realpath(r.get("path", "")) == os.path.realpath(worktree_dir)
        ]
        for candidate in (*configured_bases, "origin/HEAD", "origin/main", "origin/master", "main", "HEAD~1"):
            chk = subprocess.run(
                ["git", "-C", worktree_dir, "rev-parse", "--verify", candidate],
                capture_output=True,
                text=True,
            )
            if chk.returncode == 0:
                ref_spec = f"{candidate}...HEAD" if candidate != "HEAD~1" else "HEAD~1..HEAD"
                fb_cmd = ["git", "-C", worktree_dir, "diff", f"-U{context_lines}", ref_spec]
                if files_filter:
                    fb_cmd.append("--")
                    fb_cmd.extend(files_filter)
                fb_res = subprocess.run(fb_cmd, capture_output=True, text=True)
                if (fb_res.stdout or "").strip():
                    raw_diff = fb_res.stdout
                    diff_source_label = f"Branch Diff ({ref_spec})"
                    break

    if not raw_diff.strip():
        return (
            '<div class="odin-diff-viewer" data-view-mode="split">'
            '<div class="odin-diff-toolbar"><span class="odin-diff-toolbar-title">🧬 Odin Contextual Code Diff — No uncommitted or branch diff found</span></div>'
            "</div>"
        )

    # Parse unified diff into files and hunks
    file_blocks = re.split(r"(?=^diff --git )", raw_diff, flags=re.MULTILINE)
    parsed_files = []
    total_adds = 0
    total_dels = 0

    for block in file_blocks:
        if not block.strip().startswith("diff --git"):
            continue
        lines = block.splitlines()
        m_header = re.match(r"^diff --git a/(.*?) b/(.*?)$", lines[0])
        file_path = m_header.group(2) if m_header else "unknown"

        hunks = []
        cur_hunk = None
        for line in lines[1:]:
            if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("index ") or line.startswith("new file") or line.startswith("deleted file"):
                continue
            m_hunk = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", line)
            if m_hunk:
                if cur_hunk:
                    hunks.append(cur_hunk)
                old_start = int(m_hunk.group(1))
                new_start = int(m_hunk.group(3))
                scope = m_hunk.group(5).strip()
                cur_hunk = {
                    "header": line,
                    "old_start": old_start,
                    "new_start": new_start,
                    "scope": scope,
                    "raw_lines": [],
                }
            elif cur_hunk is not None and (line.startswith(" ") or line.startswith("+") or line.startswith("-")):
                cur_hunk["raw_lines"].append(line)
        if cur_hunk:
            hunks.append(cur_hunk)

        file_adds = sum(1 for h in hunks for l in h["raw_lines"] if l.startswith("+"))
        file_dels = sum(1 for h in hunks for l in h["raw_lines"] if l.startswith("-"))
        total_adds += file_adds
        total_dels += file_dels
        parsed_files.append(
            {
                "path": file_path,
                "adds": file_adds,
                "dels": file_dels,
                "hunks": hunks,
            }
        )

    parts = []
    if show_pre_pr_banner and not pr_url:
        parts.append(
            '<div class="odin-pre-pr-gate-box">'
            '<div class="odin-pre-pr-gate-header">'
            '<div><strong class="odin-pre-pr-gate-title">🛡️ Heimdall Pre-PR Code Review Gate — Ready for Pull Request?</strong>'
            '<div class="odin-pre-pr-gate-desc">All automated tests &amp; linters passed. Click any diff line below to leave inline review comments, click <strong>✏️ Edit File Inline</strong> to modify code directly in the browser, or approve Pull Request creation.</div></div>'
            '<div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;">'
            '<span class="badge-awaiting_human">AWAITING PR APPROVAL</span>'
            '<button type="button" class="btn" data-odin-top-action="approve-heimdall-pr" style="border-color:rgba(16,185,129,0.65);background:rgba(16,185,129,0.22);color:var(--emerald, var(--green, #34d399));font-weight:700;padding:0.38rem 0.8rem;">✅ Approve Diff &amp; Open PR</button>'
            '<button type="button" class="btn" data-odin-top-action="reject-heimdall-pr" style="border-color:rgba(244,63,94,0.55);background:rgba(244,63,94,0.16);color:var(--crimson, var(--red, #fb7185));font-weight:700;padding:0.38rem 0.8rem;">🚨 Request Code Changes</button>'
            "</div></div>"
            '<div class="odin-pre-pr-gate-review-bar">'
            '<span class="badge-active" data-odin-review-counter style="font-size:0.74rem;">💬 0 Inline Review Comments</span>'
            '<input type="text" class="odin-inline-gate-note-input" data-odin-gate-note-input placeholder="Overall code review summary or requested revision note (inline line comments attach automatically)..." />'
            '<button type="button" class="btn" data-odin-clear-comments style="display:none;font-size:0.74rem;padding:0.32rem 0.65rem;border-color:rgba(244,63,94,0.45);color:var(--crimson, #fb7185);">🗑️ Clear Inline Comments</button>'
            "</div>"
            "</div>"
        )
    elif pr_url:
        pr_esc = html_mod.escape(pr_url)
        parts.append(
            '<div class="odin-pre-pr-gate-box" style="border-color:rgba(16,185,129,0.55);border-left-color:var(--emerald, #34d399);background:rgba(16,185,129,0.12);">'
            '<div class="odin-pre-pr-gate-header">'
            f'<div><strong class="odin-pre-pr-gate-title" style="color:var(--emerald, #34d399);">✅ Pull Request Open &amp; Linked</strong>'
            f'<div class="odin-pre-pr-gate-desc"><a href="{pr_esc}" target="_blank" rel="noopener" style="color:var(--cyan, #38bdf8);font-weight:700;">{pr_esc} ↗</a></div></div>'
            f'<a href="{pr_esc}" target="_blank" rel="noopener" class="odin-diff-editor-link" style="font-weight:700;padding:0.35rem 0.75rem;">🔗 View Pull Request on GitHub ↗</a>'
            "</div></div>"
        )

    repo_name = os.path.basename(os.path.normpath(worktree_dir))
    worktree_esc = html_mod.escape(worktree_dir)
    rationale_esc = html_mod.escape(rationale or "")
    notes_json_esc = html_mod.escape(json.dumps(file_notes or {}))
    parts.append(
        f'<div class="odin-diff-viewer" data-view-mode="split" data-worktree="{worktree_esc}" '
        f'data-repo="{html_mod.escape(repo_name)}" data-context-lines="{context_lines}" '
        f'data-rationale="{rationale_esc}" data-file-notes="{notes_json_esc}">'
    )
    parts.append(
        '<div class="odin-diff-toolbar">'
        '<div class="odin-diff-toolbar-title">'
        f'<span>🧬 Odin Contextual Code Review — <code>{html_mod.escape(repo_name)}</code> ({len(parsed_files)} {"file" if len(parsed_files) == 1 else "files"})</span>'
        f'<span class="odin-diff-stat-add">+{total_adds}</span>'
        f'<span class="odin-diff-stat-del">-{total_dels}</span>'
        f'<span class="odin-diff-approved-pill" data-odin-approved-counter>✅ 0 / {len(parsed_files)} {"File" if len(parsed_files) == 1 else "Files"} Approved</span>'
        f'<span style="font-size:0.74rem;color:#94a3b8;font-weight:500;">• {html_mod.escape(diff_source_label)} • {context_lines}-line deep context • Sticky file header &amp; click any line to comment</span>'
        "</div>"
        '<div class="odin-diff-mode-group">'
        '<button type="button" class="odin-diff-btn active" data-mode-btn="split" '
        'onclick="const v=this.closest(\'.odin-diff-viewer\');v.setAttribute(\'data-view-mode\',\'split\');v.querySelectorAll(\'[data-mode-btn]\').forEach(b=>b.classList.toggle(\'active\',b.dataset.modeBtn===\'split\'));">'
        "◫ Side-by-Side (Split)</button>"
        '<button type="button" class="odin-diff-btn" data-mode-btn="unified" '
        'onclick="const v=this.closest(\'.odin-diff-viewer\');v.setAttribute(\'data-view-mode\',\'unified\');v.querySelectorAll(\'[data-mode-btn]\').forEach(b=>b.classList.toggle(\'active\',b.dataset.modeBtn===\'unified\'));">'
        "☰ Unified Diff</button>"
        '<button type="button" class="odin-diff-btn" '
        'onclick="const v=this.closest(\'.odin-diff-viewer\');const cards=v.querySelectorAll(\'details.odin-diff-file-card\');const anyOpen=Array.from(cards).some(c=>c.open);cards.forEach(c=>c.open=!anyOpen);">'
        "↕ Collapse / Expand Files</button>"
        "</div>"
        "</div>"
    )

    if rationale:
        parts.append(
            '<div class="odin-diff-rationale-banner">'
            f"<strong>🧠 What Changed &amp; Why (Architectural Context):</strong> {rationale}"
            "</div>"
        )

    for fobj in parsed_files:
        rel_path = fobj["path"]
        rel_path_esc = html_mod.escape(rel_path)
        abs_path = os.path.join(worktree_dir, rel_path)
        first_line = fobj["hunks"][0]["new_start"] if fobj["hunks"] else 1
        vscode_url = f"vscode://file/{abs_path}:{first_line}"
        file_url = f"file://{abs_path}"
        repo_rel_key = f"{repo_name}/{rel_path}"

        # Match file note by repo/rel_path, exact rel_path, or basename
        note_html = ""
        for k, v in file_notes.items():
            if k == repo_rel_key or repo_rel_key.endswith(k) or k == rel_path or rel_path.endswith(k) or os.path.basename(rel_path) == k:
                note_html = v
                break

        parts.append(
            f'<details class="odin-diff-file-card" open data-worktree="{worktree_esc}" data-repo="{html_mod.escape(repo_name)}" data-file-path="{rel_path_esc}" data-first-line="{first_line}">'
            f"<summary>"
            f'<div class="odin-diff-file-summary-inner">'
            f'<div class="odin-diff-file-meta">'
            f'<span class="odin-diff-file-path-badge">📄 {rel_path_esc}</span>'
            f'<span class="odin-diff-stat-add">+{fobj["adds"]}</span>'
            f'<span class="odin-diff-stat-del">-{fobj["dels"]}</span>'
            f'<span class="odin-diff-line-hint">📌 Sticky Header • 💬 Click any line to comment</span>'
            f"</div>"
            f'<div class="odin-diff-file-meta">'
            f'<button type="button" class="odin-diff-action-btn odin-diff-approve-btn" data-odin-approve-file="{rel_path_esc}" data-repo="{html_mod.escape(repo_name)}" aria-pressed="false">☑️ Approve File</button>'
            f'<button type="button" class="odin-diff-action-btn odin-diff-comment-btn" data-odin-comment-file="{rel_path_esc}" data-repo="{html_mod.escape(repo_name)}" data-first-line="{first_line}">💬 Comment on File</button>'
            f'<a class="odin-diff-editor-link" href="{html_mod.escape(vscode_url)}" onclick="event.stopPropagation();">⚡ IDE (L{first_line})</a>'
            f"</div>"
            f"</div>"
            f"</summary>"
            f'<div class="odin-file-comment-drawer" data-file-comment-drawer="{rel_path_esc}" hidden>'
            f'<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.45rem;">'
            f'<div style="display:flex;align-items:center;gap:0.45rem;flex-wrap:wrap;font-size:0.78rem;">'
            f'<span style="color:#c084fc;font-weight:800;">💬 Comment on Entire File: <code>{rel_path_esc}</code></span>'
            f"</div>"
            f'<button type="button" class="odin-diff-btn" data-odin-close-drawer style="padding:0.18rem 0.55rem;font-size:0.72rem;">✖ Close</button>'
            f"</div>"
            f'<textarea data-odin-drawer-comment-ta rows="2" placeholder="Add a review comment for this file (⌘+Enter to save, Esc to close)..." style="width:100%;background:#090d16;color:#f8fafc;border:1px solid #a855f7;border-radius:8px;padding:0.5rem 0.7rem;font-size:0.82rem;font-family:inherit;resize:vertical;"></textarea>'
            f'<div style="display:flex;justify-content:flex-end;gap:0.45rem;margin-top:0.4rem;">'
            f'<button type="button" class="odin-diff-btn" data-odin-close-drawer>Cancel</button>'
            f'<button type="button" class="odin-diff-action-btn odin-diff-comment-btn" data-odin-save-drawer-comment style="padding:0.28rem 0.75rem;">💾 Save Comment (⌘+Enter)</button>'
            f"</div>"
            f"</div>"
            f'<div class="odin-inline-file-editor-slot" data-editor-slot="{rel_path_esc}"></div>'
        )

        if note_html:
            parts.append(
                f'<div class="odin-diff-file-note"><strong>💡 Why This File Changed:</strong> {html_mod.escape(note_html)}</div>'
            )

        total_file_lines = sum(len(h["raw_lines"]) for h in fobj["hunks"])
        total_file_chars = sum(len(l) for h in fobj["hunks"] for l in h["raw_lines"])
        if total_file_lines > 1500 or total_file_chars > 180000:
            parts.append(
                f'<div class="odin-diff-table-wrap" style="padding:0.85rem 1.1rem;color:#94a3b8;font-size:0.8rem;">'
                f'⚡ Large file diff omitted inline (+{fobj["adds"]} / -{fobj["dels"]} across {total_file_lines:,} lines, {total_file_chars // 1024:,} KB) '
                f'to keep Odin dashboard &amp; state file fast — use <strong>⚡ IDE</strong> or <strong>✏️ Edit File Inline</strong> above to inspect.'
                f"</div>"
            )
            parts.append("</details>")
            continue

        for h in fobj["hunks"]:
            scope_txt = h["scope"] or f"Lines {h['new_start']}+"
            parts.append(
                f'<div class="odin-diff-hunk-header">'
                f'<span>📍 Enclosing Scope: <span class="odin-diff-hunk-scope">{html_mod.escape(scope_txt)}</span></span>'
                f'<span>Old L{h["old_start"]} → New L{h["new_start"]}</span>'
                f"</div>"
            )

            # Build Unified & Split rows for this hunk
            unified_rows = []
            split_rows = []
            old_ln = h["old_start"]
            new_ln = h["new_start"]
            raw_lines = h["raw_lines"]
            idx = 0
            n_lines = len(raw_lines)

            while idx < n_lines:
                line = raw_lines[idx]
                if line.startswith(" "):
                    code_esc = html_mod.escape(line[1:])
                    row_attrs = f'class="odin-diff-row" data-repo="{html_mod.escape(repo_name)}" data-file-path="{rel_path_esc}" data-line="{new_ln}" title="Click to add review comment on L{new_ln}"'
                    unified_rows.append(
                        f"<tr {row_attrs}><td class=\"odin-ln\">{old_ln}</td><td class=\"odin-ln\">{new_ln}</td>"
                        f"<td class=\"odin-code\"> {code_esc}</td></tr>"
                    )
                    split_rows.append(
                        f"<tr {row_attrs}><td class=\"odin-ln\">{old_ln}</td><td class=\"odin-code\">{code_esc}</td>"
                        f"<td class=\"odin-ln\">{new_ln}</td><td class=\"odin-code\">{code_esc}</td></tr>"
                    )
                    old_ln += 1
                    new_ln += 1
                    idx += 1
                else:
                    dels = []
                    adds = []
                    while idx < n_lines and raw_lines[idx].startswith("-"):
                        dels.append((old_ln, raw_lines[idx][1:]))
                        old_ln += 1
                        idx += 1
                    while idx < n_lines and raw_lines[idx].startswith("+"):
                        adds.append((new_ln, raw_lines[idx][1:]))
                        new_ln += 1
                        idx += 1

                    max_len = max(len(dels), len(adds))
                    paired_html = []
                    for pair_i in range(max_len):
                        d_item = dels[pair_i] if pair_i < len(dels) else None
                        a_item = adds[pair_i] if pair_i < len(adds) else None
                        if d_item and a_item:
                            d_html, a_html = _word_level_diff_pair(d_item[1], a_item[1])
                        else:
                            d_html = html_mod.escape(d_item[1]) if d_item else ""
                            a_html = html_mod.escape(a_item[1]) if a_item else ""
                        paired_html.append((d_item, d_html, a_item, a_html))

                    for d_item, d_html, _, _ in paired_html:
                        if d_item:
                            row_attrs = f'class="odin-diff-row" data-repo="{html_mod.escape(repo_name)}" data-file-path="{rel_path_esc}" data-line="{d_item[0]}" data-side="old" title="Click to add review comment on removed L{d_item[0]}"'
                            unified_rows.append(
                                f"<tr {row_attrs}><td class=\"odin-ln odin-ln-del\">{d_item[0]}</td><td class=\"odin-ln odin-ln-del\"></td>"
                                f"<td class=\"odin-code odin-del\">-{d_html}</td></tr>"
                            )
                    for _, _, a_item, a_html in paired_html:
                        if a_item:
                            row_attrs = f'class="odin-diff-row" data-repo="{html_mod.escape(repo_name)}" data-file-path="{rel_path_esc}" data-line="{a_item[0]}" data-side="new" title="Click to add review comment on added L{a_item[0]}"'
                            unified_rows.append(
                                f"<tr {row_attrs}><td class=\"odin-ln odin-ln-add\"></td><td class=\"odin-ln odin-ln-add\">{a_item[0]}</td>"
                                f"<td class=\"odin-code odin-add\">+{a_html}</td></tr>"
                            )

                    for d_item, d_html, a_item, a_html in paired_html:
                        target_ln = a_item[0] if a_item else (d_item[0] if d_item else 1)
                        side_attr = "new" if a_item else "old"
                        row_attrs = f'class="odin-diff-row" data-repo="{html_mod.escape(repo_name)}" data-file-path="{rel_path_esc}" data-line="{target_ln}" data-side="{side_attr}" title="Click to add review comment on L{target_ln}"'
                        left_cells = (
                            f"<td class=\"odin-ln odin-ln-del\">{d_item[0]}</td><td class=\"odin-code odin-del\">-{d_html}</td>"
                            if d_item
                            else "<td class=\"odin-ln odin-empty\"></td><td class=\"odin-code odin-empty\"></td>"
                        )
                        right_cells = (
                            f"<td class=\"odin-ln odin-ln-add\">{a_item[0]}</td><td class=\"odin-code odin-add\">+{a_html}</td>"
                            if a_item
                            else "<td class=\"odin-ln odin-empty\"></td><td class=\"odin-code odin-empty\"></td>"
                        )
                        split_rows.append(f"<tr {row_attrs}>{left_cells}{right_cells}</tr>")

            parts.append('<div class="odin-diff-table-wrap">')
            parts.append('<table class="odin-diff-split-table"><tbody>' + "".join(split_rows) + "</tbody></table>")
            parts.append('<table class="odin-diff-unified-table"><tbody>' + "".join(unified_rows) + "</tbody></table>")
            parts.append("</div>")

        parts.append("</details>")

    parts.append("</div>")
    return "\n".join(parts)


def cmd_update_phase(args: argparse.Namespace) -> int:
    try:
        html_path = _resolve_html_path(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not html_path.exists():
        print(f"Error: File not found: {html_path}", file=sys.stderr)
        return 1

    content = html_path.read_text(encoding="utf-8")
    ledger = extract_ledger(content, str(html_path))
    target_iter = int(args.iteration) if args.iteration else int(ledger.get("current_iteration", 1))
    phase_key = PHASE_ALIASES.get(args.phase.lower())
    if not phase_key or phase_key not in PHASE_MAP:
        print(f"Error: Invalid phase {args.phase}", file=sys.stderr)
        return 1

    sec_id, _, phase_title = PHASE_MAP[phase_key]
    prev_status = _get_section_status(content, phase_key)
    status_changed = prev_status != args.status
    inner_html = ""
    # A summary alone only fills a card that has no body yet; it never replaces a richer one.
    keep_body = False
    if args.html_file_fragment:
        inner_html = Path(args.html_file_fragment).read_text(encoding="utf-8")
    elif args.html:
        inner_html = args.html
    elif args.summary:
        inner_html = f"<p>{html_mod.escape(args.summary)}</p>"
        keep_body = not getattr(args, "with_diff", False)

    pr_url = (getattr(args, "pr_url", "") or "").strip()
    if pr_url:
        content = stamp_pr_url_into_html(content, pr_url)

    files_list = [f.strip() for f in (getattr(args, "files", "") or "").split(",") if f.strip()]

    if getattr(args, "with_diff", False):
        content = ensure_diff_styles(content)
        raw_repo_dirs = getattr(args, "repo_dir", None)
        if isinstance(raw_repo_dirs, list) and raw_repo_dirs:
            repo_dirs = [os.path.abspath(r) for r in raw_repo_dirs if r and os.path.isdir(r)]
        elif isinstance(raw_repo_dirs, str) and raw_repo_dirs and os.path.isdir(raw_repo_dirs):
            repo_dirs = [os.path.abspath(raw_repo_dirs)]
        else:
            repo_dirs = [_resolve_worktree_from_html(content, None)]

        file_notes = _parse_file_notes(getattr(args, "file_note", None))
        diff_blocks = []
        for idx, worktree_dir in enumerate(repo_dirs):
            diff_html = generate_contextual_diff_html(
                worktree_dir=worktree_dir,
                context_lines=int(getattr(args, "diff_context", 12) or 12),
                rationale=(getattr(args, "diff_rationale", "") or "") if idx == 0 else "",
                file_notes=file_notes,
                files_filter=files_list or None,
                show_pre_pr_banner=(phase_key == "heimdall" and args.status == "awaiting_human" and idx == 0),
                pr_url=pr_url if idx == 0 else "",
            )
            diff_blocks.append(diff_html)
        combined_diff_html = "\n".join(diff_blocks)
        inner_html = (inner_html + "\n" + combined_diff_html) if inner_html else combined_diff_html

    summary_title = args.title or f"Iteration {target_iter} — {phase_title}"
    if inner_html:
        content = mutate_section_body(
            content,
            sec_id,
            lambda body: upsert_iteration_card_in_body(
                body, target_iter, summary_title, args.status, inner_html, keep_body=keep_body
            ),
        )

    content = set_step_and_section_status(content, phase_key, args.status)

    transition_event = None
    if status_changed:
        short_title = phase_title.split(": ", 1)[-1]
        transition_text = f"{short_title} → {args.status}" + (f": {args.summary}" if args.summary else "")
        ts = now_iso()
        content, logged = append_progress_log(content, phase_key, target_iter, "phase", transition_text, ts=ts)
        if logged:
            transition_event = {"ts": ts, "phase": phase_key, "kind": "phase", "text": transition_text}

    # Reconcile upstream/downstream AI phase statuses so stale awaiting_human states are automatically closed
    auto_reconciled = {}
    if phase_key in AI_PHASE_ORDER and args.status in ("active", "done"):
        p_idx = AI_PHASE_ORDER.index(phase_key)
        for prev_key in AI_PHASE_ORDER[:p_idx]:
            if _get_section_status(content, prev_key) != "done":
                content = set_step_and_section_status(content, prev_key, "done")
                auto_reconciled[prev_key] = "done"
        if args.status == "active":
            for next_key in AI_PHASE_ORDER[p_idx + 1 :]:
                if _get_section_status(content, next_key) in ("awaiting_human", "active", "done", "reject"):
                    content = set_step_and_section_status(content, next_key, "pending")
                    auto_reconciled[next_key] = "pending"
        content = _mark_open_questions_resolved(content)

    content = update_global_banner(
        content,
        f"{phase_title.upper()} — ITERATION {target_iter} ({args.status.upper()})",
        args.status,
    )
    content = update_document_title(content)

    # Update JSON ledger
    found_iter = False
    for it_obj in ledger.get("iterations", []):
        if int(it_obj.get("iteration", 0)) == target_iter:
            found_iter = True
            phases_dict = it_obj.setdefault("phases", {})
            for rec_k, rec_status in auto_reconciled.items():
                phases_dict.setdefault(rec_k, {})["status"] = rec_status
                phases_dict[rec_k]["updated_at"] = now_iso()
            phases_dict[phase_key] = {
                "status": args.status,
                "updated_at": now_iso(),
                "summary": args.summary or summary_title,
            }
            if files_list:
                it_obj["files_touched"] = files_list
            if pr_url:
                it_obj["pr_url"] = pr_url
            if phase_key == "human" and args.status in ("done", "awaiting_human", "reject"):
                it_obj["status"] = args.status
    if not found_iter:
        new_it = {
            "iteration": target_iter,
            "started_at": now_iso(),
            "status": args.status,
            "phases": {
                phase_key: {
                    "status": args.status,
                    "updated_at": now_iso(),
                    "summary": args.summary or summary_title,
                }
            },
            "harness_delta_proposals": [],
        }
        if files_list:
            new_it["files_touched"] = files_list
        if pr_url:
            new_it["pr_url"] = pr_url
        ledger.setdefault("iterations", []).append(new_it)
    if transition_event:
        record_ledger_event(ledger, target_iter, transition_event)

    content = save_ledger_into_html(content, ledger)
    html_path.write_text(content, encoding="utf-8")

    if (phase_key == "hlidskjalf" and args.status == "done") or (
        phase_key == "human" and args.status in ("done", "awaiting_human")
    ):
        sync_parent_mimir_delta_on_completion(html_path, content, ledger)

    # Chime only when the phase actually moved; same-status progress updates stay quiet.
    if status_changed:
        sound_mode = (
            "awaiting_human"
            if args.status == "awaiting_human"
            else ("complete" if phase_key == "human" and args.status == "done" else "phase")
        )
        trigger_notify(
            f"{phase_title} (Iter {target_iter})",
            args.summary or f"Status transitioned to {args.status.upper()}",
            sound_mode,
            html_path,
        )
    print(f"Updated {html_path.name} [{phase_key}] Iteration {target_iter} -> {args.status.upper()}.")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    try:
        html_path = _resolve_html_path(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not html_path.exists():
        print(f"Error: File not found: {html_path}", file=sys.stderr)
        return 1
    phase_key = PHASE_ALIASES.get(args.phase.lower(), "") if args.phase else ""
    if args.phase and not phase_key:
        print(f"Error: Invalid phase {args.phase}", file=sys.stderr)
        return 1
    ts = ""
    if args.at:
        try:
            parsed = datetime.datetime.fromisoformat(args.at)
        except ValueError:
            print(f"Error: --at must be ISO 8601, got {args.at}", file=sys.stderr)
            return 1
        ts = (parsed if parsed.tzinfo else parsed.astimezone()).isoformat(timespec="seconds")
    if log_progress(html_path, args.kind, args.text, phase_key, args.detail or "", args.outcome or "", ts=ts):
        print(f"Logged {html_path.name} [{phase_key or 'current phase'}] {args.kind}: {args.text}")
    else:
        print(f"Skipped duplicate entry in {html_path.name}: {args.text}")
    return 0


ENV_BAR_PATTERNS = {
    "branch": r"(>🌿 )[^<]*(</span>)",
    "repo": r"(>🗂️ )[^<]*(</span>)",
    "app_port": r"(>🌐 app :)[^<]*(</span>)",
    "backend_port": r"(>🧩 backend :)[^<]*(</span>)",
}


def set_body_env(content: str, branch=None, worktree=None, app_port=None, backend_port=None) -> str:
    body_m = re.search(r"""<body\b(?:[^>"']|"[^"]*"|'[^']*')*>""", content, re.IGNORECASE)
    if not body_m:
        return content
    body_tag = body_m.group(0)
    for attr, value in (
        ("data-branch", branch),
        ("data-worktree", worktree),
        ("data-app-port", app_port),
        ("data-backend-port", backend_port),
    ):
        if value is None:
            continue
        escaped = html_mod.escape(value)
        if re.search(rf'\b{attr}="[^"]*"', body_tag):
            body_tag = re.sub(rf'\b{attr}="[^"]*"', lambda _m: f'{attr}="{escaped}"', body_tag)
        else:
            body_tag = body_tag[:-1] + f' {attr}="{escaped}">'
    content = content[: body_m.start()] + body_tag + content[body_m.end() :]
    bar_values = {
        "branch": branch,
        "repo": os.path.basename(worktree.rstrip("/")) if worktree else None,
        "app_port": app_port or ("none" if app_port == "" else None),
        "backend_port": backend_port or ("none" if backend_port == "" else None),
    }
    for key, value in bar_values.items():
        if value is not None:
            content = re.sub(ENV_BAR_PATTERNS[key], lambda m: m.group(1) + html_mod.escape(value) + m.group(2), content, count=1)
    return content


def cmd_set_env(args: argparse.Namespace) -> int:
    try:
        html_path = _resolve_html_path(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not html_path.exists():
        print(f"Error: File not found: {html_path}", file=sys.stderr)
        return 1
    changes = {
        "branch": args.branch,
        "worktree": os.path.abspath(args.worktree) if args.worktree else None,
        "app_port": args.app_port,
        "backend_port": args.backend_port,
    }
    if all(v is None for v in changes.values()):
        print("Error: pass at least one of --branch, --worktree, --app-port, --backend-port", file=sys.stderr)
        return 1
    content = set_body_env(html_path.read_text(encoding="utf-8"), **changes)
    html_path.write_text(content, encoding="utf-8")
    summary = ", ".join(f"{k.replace('_', ' ')}={v or 'none'}" for k, v in changes.items() if v is not None)
    log_progress(html_path, "env", f"Environment: {summary}" + (f" ({args.reason})" if args.reason else ""))
    print(f"Updated environment of {html_path.name}: {summary}")
    return 0


def cmd_propose_delta(args: argparse.Namespace) -> int:
    try:
        html_path = _resolve_html_path(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not html_path.exists():
        print(f"Error: File not found: {html_path}", file=sys.stderr)
        return 1

    target_file = getattr(args, "target", None) or getattr(args, "target_path", None)
    root_cause = getattr(args, "root_cause", None) or getattr(args, "failure_pattern", None)
    diff_text = getattr(args, "diff", None) or getattr(args, "proposed_diff", None)
    if not target_file or not root_cause or not diff_text:
        print("Error: --target/--target-path, --root-cause/--failure-pattern, and --diff/--proposed-diff are required.", file=sys.stderr)
        return 1

    content = html_path.read_text(encoding="utf-8")
    ledger = extract_ledger(content, str(html_path))
    target_iter = int(args.iteration) if args.iteration else int(ledger.get("current_iteration", 1))

    delta_obj = {
        "id": args.id,
        "iteration": target_iter,
        "repo": args.repo or "odin",
        "target_file": target_file,
        "priority": args.priority.lower(),
        "category": args.category.lower(),
        "root_cause": root_cause,
        "proposed_diff": diff_text,
        "status": "pending",
        "created_at": now_iso(),
    }

    for it_obj in ledger.get("iterations", []):
        if int(it_obj.get("iteration", 0)) == target_iter:
            proposals = it_obj.setdefault("harness_delta_proposals", [])
            proposals = [p for p in proposals if p.get("id") != args.id]
            proposals.append(delta_obj)
            it_obj["harness_delta_proposals"] = proposals
            break

    # Also render a visual card inside #hlidskjalf-judge
    delta_card_html = (
        f'\n          <div class="card harness-delta-card" data-delta-id="{html_mod.escape(args.id)}" '
        f'style="margin-top: 12px; border-color: rgba(245, 158, 11, 0.45);">\n'
        f'            <h3>🧠 Harness Delta Proposal: <code>{html_mod.escape(args.id)}</code> '
        f'<span class="tag">{html_mod.escape(args.priority.upper())}</span> '
        f'<span class="tag">{html_mod.escape(args.repo or "odin")}</span></h3>\n'
        f"            <p><strong>Target Harness File:</strong> <code>{html_mod.escape(target_file)}</code> &bull; "
        f"<strong>Category:</strong> <code>{html_mod.escape(args.category)}</code></p>\n"
        f"            <p><strong>Root Cause (Iter {target_iter}):</strong> {html_mod.escape(root_cause)}</p>\n"
        f"            <pre>{html_mod.escape(diff_text)}</pre>\n"
        f"          </div>\n"
    )

    def inject_delta(body: str) -> str:
        # Remove existing card with same data-delta-id if present
        body = re.sub(
            rf'\s*<div[^>]*class=["\'][^"\']*harness-delta-card[^"\']*["\'][^>]*data-delta-id=["\']{re.escape(args.id)}["\'][^>]*>.*?</div>',
            "",
            body,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Try to insert inside <details ... data-iteration="target_iter"> using balanced <details> matching
        open_iter_re = re.compile(
            rf'<details\b[^>]*\bclass=["\'][^"\']*odin-iteration-card[^"\']*["\'][^>]*\bdata-iteration=["\']{target_iter}["\'][^>]*>',
            re.IGNORECASE,
        )
        open_m = open_iter_re.search(body)
        if open_m:
            bal = _find_balanced_tag_span(body, open_m, "details")
            if bal:
                close_tag_start = body.rfind("</details>", bal[0], bal[1])
                if close_tag_start != -1:
                    return body[:close_tag_start] + delta_card_html + "      " + body[close_tag_start:]
        return body.rstrip() + delta_card_html

    content = mutate_section_body(content, "hlidskjalf-judge", inject_delta)
    content = save_ledger_into_html(content, ledger)
    html_path.write_text(content, encoding="utf-8")
    print(f"Recorded Harness Delta Proposal '{args.id}' in {html_path.name} (Iteration {target_iter}).")
    return 0


def run_mimir_audit(scan_root: str = AI_DEV_ROOT) -> dict:
    """
    Aggregates Kvasir quantitative telemetry and Mímir harness delta proposals
    across all odin-states/*.html files under scan_root.
    """
    state_files = []
    if os.path.exists(scan_root):
        base_depth = os.path.normpath(scan_root).count(os.sep)
        for root, dirs, files in os.walk(scan_root):
            cur_depth = os.path.normpath(root).count(os.sep) - base_depth
            if cur_depth >= 4:
                dirs[:] = []
            else:
                dirs[:] = [
                    d
                    for d in dirs
                    if not d.startswith(".")
                    and d not in ("node_modules", "dist", "build", "coverage", "__pycache__", "venv", ".venv", "vendor", "target", "out")
                    and not (d == "odin-staging" and os.path.basename(os.path.normpath(scan_root)) != "odin-staging")
                ]
            if os.path.basename(root) == "odin-states":
                for fname in files:
                    if fname.endswith(".html") and fname != "index.html":
                        state_files.append(os.path.join(root, fname))

    projects_summary = []
    all_deltas = []
    total_iterations = 0
    multi_iter_projects = 0
    rejection_counts = {"human": 0, "ci": 0, "heimdall": 0, "other": 0}
    category_counts = {"testing": 0, "architecture": 0, "workflow": 0, "skill_gap": 0}

    for fpath in sorted(state_files):
        try:
            content = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        ledger = extract_ledger(content, fpath)
        slug = ledger.get("slug") or Path(fpath).stem
        title = ledger.get("title") or slug
        cur_iter = int(ledger.get("current_iteration", 1))
        iterations = ledger.get("iterations", [])
        iter_count = max(cur_iter, len(iterations), 1)
        total_iterations += iter_count
        if iter_count > 1:
            multi_iter_projects += 1

        try:
            rel_path = os.path.relpath(fpath, scan_root).replace(os.sep, "/")
            url_path = "/" + rel_path
        except ValueError:
            url_path = "/odin-states/" + os.path.basename(fpath)

        project_deltas = []
        for it_obj in iterations:
            src = (it_obj.get("trigger_source") or "").lower()
            if src in rejection_counts:
                rejection_counts[src] += 1
            elif int(it_obj.get("iteration", 1)) > 1:
                rejection_counts["human"] += 1

            for d in it_obj.get("harness_delta_proposals", []):
                cat = (d.get("category") or "workflow").lower()
                category_counts[cat] = category_counts.get(cat, 0) + 1
                enriched = dict(d)
                enriched["project_slug"] = slug
                enriched["source_project"] = slug
                enriched["project_title"] = title
                enriched["source_title"] = title
                enriched["state_file"] = fpath
                enriched["source_file"] = fpath
                enriched["state_url"] = url_path
                enriched["source_url"] = url_path
                project_deltas.append(enriched)
                all_deltas.append(enriched)

        # Also check top-level harness_delta_proposals if any
        for d in ledger.get("harness_delta_proposals", []):
            if not any(existing.get("id") == d.get("id") for existing in project_deltas):
                cat = (d.get("category") or "workflow").lower()
                category_counts[cat] = category_counts.get(cat, 0) + 1
                enriched = dict(d)
                enriched["project_slug"] = slug
                enriched["source_project"] = slug
                enriched["project_title"] = title
                enriched["source_title"] = title
                enriched["state_file"] = fpath
                enriched["source_file"] = fpath
                enriched["state_url"] = url_path
                enriched["source_url"] = url_path
                project_deltas.append(enriched)
                all_deltas.append(enriched)

        projects_summary.append(
            {
                "slug": slug,
                "title": title,
                "state_file": fpath,
                "state_url": url_path,
                "session_id": ledger.get("session_id", ""),
                "current_iteration": iter_count,
                "max_iterations": int(ledger.get("max_iterations", 5)),
                "delta_count": len(project_deltas),
                "pending_delta_count": sum(
                    1 for d in project_deltas if d.get("status", "pending") == "pending"
                ),
            }
        )

    # Sort deltas by priority (p0 -> p1 -> p2)
    priority_rank = {"p0": 0, "p1": 1, "p2": 2}
    all_deltas.sort(key=lambda d: (0 if d.get("status", "pending") == "pending" else 1, priority_rank.get(d.get("priority", "p1"), 9)))

    total_projects = len(projects_summary)
    pending_count = sum(1 for d in all_deltas if d.get("status", "pending") == "pending")
    in_progress_count = sum(1 for d in all_deltas if d.get("status") == "in_progress")
    applied_count = sum(1 for d in all_deltas if d.get("status") == "applied")
    mimir_block = {
        "total_deltas": len(all_deltas),
        "pending_deltas": pending_count,
        "pending_proposals": pending_count,
        "in_progress_deltas": in_progress_count,
        "applied_deltas": applied_count,
        "proposals": all_deltas,
    }
    return {
        "generated_at": now_iso(),
        "kvasir_telemetry": {
            "total_projects": total_projects,
            "projects_scanned": total_projects,
            "multi_iteration_projects": multi_iter_projects,
            "total_iterations": total_iterations,
            "avg_iterations_per_project": round(total_iterations / max(total_projects, 1), 2),
            "step_back_rate_pct": round((multi_iter_projects / max(total_projects, 1)) * 100, 1),
            "rejection_breakdown": rejection_counts,
            "weakness_categories": category_counts,
        },
        "mimir_synthesis": mimir_block,
        "mimir_proposals": mimir_block,
        "projects": projects_summary,
    }


def mark_deltas_status(scan_root: str, delta_ids: list[str], new_status: str = "applied") -> int:
    updated_count = 0
    audit = run_mimir_audit(scan_root)
    files_to_update = {
        d["state_file"] for d in audit["mimir_synthesis"]["proposals"] if d.get("id") in delta_ids
    }
    for fpath in files_to_update:
        path_obj = Path(fpath)
        if not path_obj.exists():
            continue
        content = path_obj.read_text(encoding="utf-8")
        ledger = extract_ledger(content, fpath)
        changed = False
        for it_obj in ledger.get("iterations", []):
            for d in it_obj.get("harness_delta_proposals", []):
                if d.get("id") in delta_ids:
                    d["status"] = new_status
                    d["updated_at"] = now_iso()
                    updated_count += 1
                    changed = True
        for d in ledger.get("harness_delta_proposals", []):
            if d.get("id") in delta_ids:
                d["status"] = new_status
                d["updated_at"] = now_iso()
                changed = True
        if changed:
            content = save_ledger_into_html(content, ledger)
            path_obj.write_text(content, encoding="utf-8")
    return updated_count


def _slugify_delta_id(delta_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (delta_id or "delta").strip().lower()).strip("-")
    if cleaned.startswith("delta-"):
        cleaned = cleaned[len("delta-"):]
    return f"mimir-delta-{cleaned or 'proposal'}"


def is_watcher_armed_for_file(html_path: str | Path) -> bool:
    p = Path(html_path).resolve()
    states_dir = _get_odin_states_dir_for_html(p)
    pause_info = get_pause_state(states_dir)
    if pause_info.get("paused"):
        return False
    watcher_file = states_dir / f".odin-watcher-{p.stem}.json"
    if not watcher_file.exists():
        return False
    try:
        w_data = json.loads(watcher_file.read_text(encoding="utf-8"))
        pid = w_data.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                pass
    except Exception:
        pass
    return False


def sync_parent_mimir_delta_on_completion(
    child_html_path: str | Path,
    child_content: str,
    child_ledger: dict | None = None,
) -> dict | None:
    """When a spawned child delta ticket reaches hlidskjalf-judge=done or human-validation=done/awaiting_human,
    automatically mark the parent ticket's Mímir delta proposal as applied."""
    ledger = child_ledger if isinstance(child_ledger, dict) else extract_ledger(child_content, str(child_html_path))
    origin = ledger.get("mimir_delta_origin")
    if not isinstance(origin, dict):
        m_id = re.search(r'data-parent-delta-id=["\']([^"\']+)["\']', child_content)
        m_file = re.search(r'data-parent-state-file=["\']([^"\']+)["\']', child_content)
        if not (m_id and m_file):
            return None
        origin = {
            "delta_id": m_id.group(1),
            "parent_state_file": m_file.group(1),
        }

    delta_id = (origin.get("delta_id") or "").strip()
    parent_file_str = (origin.get("parent_state_file") or "").strip()
    if not delta_id or not parent_file_str:
        return None

    parent_path = Path(parent_file_str)
    if not parent_path.is_absolute():
        parent_path = (Path(AI_DEV_ROOT) / parent_file_str).resolve()
    if not parent_path.exists():
        alt = Path(child_html_path).resolve().parent / Path(parent_file_str).name
        if alt.exists():
            parent_path = alt
        else:
            return None

    p_content = parent_path.read_text(encoding="utf-8")
    p_ledger = extract_ledger(p_content, str(parent_path))
    try:
        child_rel = str(Path(child_html_path).resolve().relative_to(Path(AI_DEV_ROOT).resolve())).replace("\\", "/")
    except ValueError:
        child_rel = f"odin-states/{Path(child_html_path).name}"
    changed = False
    for it_obj in p_ledger.get("iterations", []):
        for d in it_obj.get("harness_delta_proposals", []):
            if d.get("id") == delta_id:
                if d.get("status") != "applied" or d.get("spawned_ticket_id") != child_rel:
                    d["status"] = "applied"
                    d["spawned_ticket_id"] = child_rel
                    d["spawned_ticket_url"] = f"{base_url(CONFIG)}/{child_rel}"
                    d["applied_at"] = now_iso()
                    d["updated_at"] = now_iso()
                    changed = True
    for d in p_ledger.get("harness_delta_proposals", []):
        if d.get("id") == delta_id:
            if d.get("status") != "applied" or d.get("spawned_ticket_id") != child_rel:
                d["status"] = "applied"
                d["spawned_ticket_id"] = child_rel
                d["spawned_ticket_url"] = f"{base_url(CONFIG)}/{child_rel}"
                d["applied_at"] = now_iso()
                d["updated_at"] = now_iso()
                changed = True

    if changed:
        p_content = save_ledger_into_html(p_content, p_ledger)
        parent_path.write_text(p_content, encoding="utf-8")
    return {
        "deltaId": delta_id,
        "parentStateFile": str(parent_path),
        "childTicketId": child_rel,
        "status": "applied",
        "updatedParent": changed,
    }


def _build_child_delta_ticket_html(
    child_slug: str,
    title: str,
    worktree_dir: str,
    session_id: str,
    delta_id: str,
    parent_rel: str,
    plan_html: str,
) -> str:
    title_esc = html_mod.escape(title)
    wt_esc = html_mod.escape(worktree_dir, quote=True)
    sid_esc = html_mod.escape(session_id, quote=True)
    did_esc = html_mod.escape(delta_id, quote=True)
    prel_esc = html_mod.escape(parent_rel, quote=True)
    slug_esc = html_mod.escape(child_slug)
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>⚡ [ACTIVE] ⚡ Odin Command Center — {title_esc}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg-light: #f8fafc; --bg-dark: #070b14;
      --surface-light: #ffffff; --surface-dark: #0f172a;
      --surface-elevated-light: #f1f5f9; --surface-elevated-dark: #162038;
      --text-light: #0f172a; --text-dark: #e2e8f0;
      --muted-light: #475569; --muted-dark: #94a3b8;
      --border-light: #cbd5e1; --border-dark: #1e293b;
      --gold: #f59e0b; --cyan: #38bdf8; --emerald: #10b981; --crimson: #f43f5e; --violet: #a855f7;
      --bg: var(--bg-dark); --surface: var(--surface-dark); --surface-elevated: var(--surface-elevated-dark);
      --text: var(--text-dark); --muted: var(--muted-dark); --border: var(--border-dark);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.65; min-height: 100vh; padding-bottom: 4rem;
    }}
    .odin-header {{
      position: sticky; top: 0; z-index: 40; backdrop-filter: blur(16px);
      background: color-mix(in srgb, var(--surface) 85%, transparent);
      border-bottom: 1px solid var(--border); padding: 0.9rem 1.5rem;
    }}
    .odin-header-inner {{
      max-width: 1120px; margin: 0 auto; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 1rem;
    }}
    .brand-title {{ font-size: 1.15rem; font-weight: 800; margin: 0; color: var(--gold); }}
    .brand-subtitle {{ font-size: 0.78rem; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .controls {{ display: flex; align-items: center; flex-wrap: wrap; gap: 0.55rem; }}
    .btn {{
      appearance: none; border: 1px solid var(--border); background: var(--surface-elevated); color: var(--text);
      font-size: 0.78rem; font-weight: 600; padding: 0.42rem 0.78rem; border-radius: 999px; cursor: pointer;
      display: inline-flex; align-items: center; gap: 0.4rem;
    }}
    .container {{ max-width: 1120px; margin: 1.5rem auto; padding: 0 1.5rem; }}
    section {{
      background: var(--surface); border: 1px solid var(--border); border-left: 5px solid var(--border);
      border-radius: 16px; padding: 1.4rem 1.6rem; margin-bottom: 1.35rem;
    }}
    section[data-status="done"] {{ border-left-color: var(--emerald); }}
    section[data-status="active"] {{ border-left-color: var(--cyan); }}
    section[data-status="awaiting_human"] {{ border-left-color: var(--gold); }}
    section[data-status="reject"] {{ border-left-color: var(--crimson); }}
    .phase-header {{
      display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.75rem;
      margin-bottom: 1rem; padding-bottom: 0.75rem; border-bottom: 1px solid var(--border);
    }}
    .phase-title {{ font-size: 1.15rem; font-weight: 700; margin: 0; }}
    .badge-done, .badge-active, .badge-pending, .badge-reject, .badge-awaiting_human {{
      display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.22rem 0.68rem;
      border-radius: 999px; font-size: 0.74rem; font-weight: 700; text-transform: uppercase;
    }}
    .badge-done {{ background: rgba(16, 185, 129, 0.16); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }}
    .badge-active {{ background: rgba(56, 189, 248, 0.16); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.45); }}
    .badge-awaiting_human {{ background: rgba(245, 158, 11, 0.18); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.55); }}
    .badge-pending {{ background: rgba(148, 163, 184, 0.14); color: var(--muted); border: 1px solid rgba(148, 163, 184, 0.28); }}
    .badge-reject {{ background: rgba(244, 63, 94, 0.16); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.45); }}
    details.odin-iteration-card {{
      background: var(--surface-elevated); border: 1px solid var(--border); border-radius: 12px; margin: 0.85rem 0; overflow: hidden;
    }}
    details.odin-iteration-card > summary {{
      cursor: pointer; padding: 0.75rem 1rem; font-weight: 700; font-size: 0.92rem; display: flex; justify-content: space-between;
    }}
    pre, code {{
      color-scheme: only dark; background: #090d16; color: #e2e8f0;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.84rem; border-radius: 6px;
    }}
    code {{ padding: 0.15rem 0.4rem; border: 1px solid #1e293b; }}
    pre {{ padding: 1rem; overflow-x: auto; border: 1px solid #1e293b; }}
  </style>
</head>
<body data-branch="main" data-worktree="{wt_esc}" data-app-port="" data-backend-port="" data-session-id="{sid_esc}" data-parent-delta-id="{did_esc}" data-parent-state-file="{prel_esc}">
  <header class="odin-header">
    <div class="odin-header-inner">
      <div>
        <h1 class="brand-title">ODIN ALLFATHER — {title_esc}</h1>
        <div class="brand-subtitle" id="odin-meta">slug: {slug_esc} • spawned from: {prel_esc}</div>
      </div>
      <div class="controls">
        <a class="btn" href="/" onclick="if(location.protocol==='file:'){{location.href='http://127.0.0.1:8788/';return false;}}" style="text-decoration:none">🏛️ All Projects</a>
        <button type="button" class="btn" id="odin-mute-btn">🔊 Sound: ON</button>
        <button type="button" class="btn" id="odin-theme-btn">☀️ Theme</button>
      </div>
    </div>
  </header>
  <main class="container">
    <section id="huginn-plan" data-status="done">
      <div class="phase-header">
        <h2 class="phase-title">🪶 Phase 1: Huginn (Spawned Mímir Delta Blueprint)</h2>
        <span class="badge-done">DONE</span>
      </div>
      <div class="phase-body">
        {plan_html}
      </div>
    </section>
    <section id="thor-execution" data-status="active">
      <div class="phase-header">
        <h2 class="phase-title">⚡ Phase 2: Thor &amp; Valkyries (Delta Implementation)</h2>
        <span class="badge-active">ACTIVE</span>
      </div>
      <div class="phase-body">
        <p>Ready for Thor execution to apply Mímir Harness Delta <code>{did_esc}</code>.</p>
      </div>
    </section>
    <section id="heimdall-validation" data-status="pending">
      <div class="phase-header">
        <h2 class="phase-title">🛡️ Phase 3: Heimdall (Verification &amp; Diff Review)</h2>
        <span class="badge-pending">PENDING</span>
      </div>
      <div class="phase-body"></div>
    </section>
    <section id="hlidskjalf-judge" data-status="pending">
      <div class="phase-header">
        <h2 class="phase-title">👁️ Phase 4: Hlidskjalf (Judgment &amp; Parent Delta Sync)</h2>
        <span class="badge-pending">PENDING</span>
      </div>
      <div class="phase-body"></div>
    </section>
    <section id="human-validation" data-status="pending">
      <div class="phase-header">
        <h2 class="phase-title">👑 Phase 5: Human Validation (Sign-Off)</h2>
        <span class="badge-pending">PENDING</span>
      </div>
      <div class="phase-body"></div>
    </section>
  </main>
</body>
</html>
"""


def spawn_mimir_delta_ticket(
    delta_id: str,
    scan_root: str = AI_DEV_ROOT,
    parent_file: str = "",
) -> dict:
    """Create a dedicated child Odin Delta Ticket (odin-states/mimir-delta-<slug>.html) from a Mímir Harness Delta Proposal,
    link it onto the parent ticket's delta entry (status='in_progress') without stepping back or altering the parent ticket's phases,
    and trigger a reactive wakeup event if a watcher is armed."""
    target_delta = None
    parent_path: Path | None = None

    if parent_file:
        cand = Path(parent_file)
        if not cand.is_absolute():
            cand = (Path(scan_root) / parent_file).resolve()
        if cand.exists():
            parent_path = cand
            p_content = cand.read_text(encoding="utf-8")
            p_ledger = extract_ledger(p_content, str(cand))
            for d in p_ledger.get("harness_delta_proposals", []):
                if d.get("id") == delta_id:
                    target_delta = dict(d)
                    break
            if not target_delta:
                for it_obj in p_ledger.get("iterations", []):
                    for d in it_obj.get("harness_delta_proposals", []):
                        if d.get("id") == delta_id:
                            target_delta = dict(d)
                            break

    if not target_delta:
        audit = run_mimir_audit(scan_root)
        for d in audit["mimir_synthesis"]["proposals"]:
            if d.get("id") == delta_id:
                target_delta = dict(d)
                parent_path = Path(d["state_file"]).resolve()
                break

    if not target_delta or not parent_path or not parent_path.exists():
        raise ValueError(f"Mímir Delta Proposal '{delta_id}' not found in {scan_root}")

    states_dir = parent_path.parent
    child_slug = _slugify_delta_id(delta_id)
    child_path = (states_dir / f"{child_slug}.html").resolve()
    try:
        child_rel = str(child_path.relative_to(Path(scan_root).resolve())).replace("\\", "/")
    except ValueError:
        child_rel = f"odin-states/{child_path.name}"
    child_url = f"{base_url(CONFIG)}/{child_rel}"
    try:
        parent_rel = str(parent_path.relative_to(Path(scan_root).resolve())).replace("\\", "/")
    except ValueError:
        parent_rel = f"odin-states/{parent_path.name}"

    repo_name = (target_delta.get("repo") or "odin").strip()
    target_file = (target_delta.get("target_file") or target_delta.get("target") or "SKILL.md").strip()
    priority = (target_delta.get("priority") or "p1").upper()
    category = (target_delta.get("category") or "harness").strip()
    root_cause = (target_delta.get("root_cause") or "Harness delta proposal").strip()
    proposed_diff = (target_delta.get("proposed_diff") or "").strip()

    worktree_dir = ODIN_REPO_DIR
    cand_repo = Path(scan_root) / repo_name
    if cand_repo.is_dir():
        worktree_dir = str(cand_repo.resolve())

    # 1. Create or refresh child Odin Delta Ticket in Phase 1 (done -> Thor Execution ready)
    parent_content = parent_path.read_text(encoding="utf-8")
    m_sid = re.search(r'data-session-id=["\']([^"\']+)["\']', parent_content)
    session_id = m_sid.group(1) if m_sid else ""
    child_title = f"Mímir Harness Delta: {delta_id} ({repo_name} → {target_file})"

    plan_html = (
        f"<h4>🧠 Spawned Mímir Harness Delta Blueprint — <code>{html_mod.escape(delta_id)}</code></h4>"
        f"<ul>"
        f"<li><strong>Parent Ticket:</strong> <a href=\"/{html_mod.escape(parent_rel)}\" target=\"_blank\"><code>{html_mod.escape(parent_rel)}</code> ↗</a></li>"
        f"<li><strong>Target Repo &amp; File:</strong> <code>{html_mod.escape(repo_name)}</code> → <code>{html_mod.escape(target_file)}</code> (Worktree: <code>{html_mod.escape(worktree_dir)}</code>)</li>"
        f"<li><strong>Priority &amp; Category:</strong> <code>[{html_mod.escape(priority)}] {html_mod.escape(category)}</code></li>"
        f"<li><strong>Root Cause:</strong> {html_mod.escape(root_cause)}</li>"
        f"</ul>"
        f"<h4>Proposed Harness Diff / Guardrail</h4>"
        f"<pre style=\"white-space:pre-wrap;font-size:11px;\"><code>{html_mod.escape(proposed_diff)}</code></pre>"
    )

    if not child_path.exists():
        c_content = _build_child_delta_ticket_html(
            child_slug=child_slug,
            title=child_title,
            worktree_dir=worktree_dir,
            session_id=session_id,
            delta_id=delta_id,
            parent_rel=parent_rel,
            plan_html=plan_html,
        )
    else:
        c_content = child_path.read_text(encoding="utf-8")

    c_ledger = extract_ledger(c_content, str(child_path))
    c_ledger["slug"] = child_slug
    c_ledger["title"] = child_title
    c_ledger["parent_delta_id"] = delta_id
    c_ledger["parent_ticket"] = parent_rel
    c_ledger["mimir_delta_origin"] = {
        "delta_id": delta_id,
        "parent_state_file": str(parent_path),
        "parent_ticket": parent_rel,
        "parent_slug": parent_path.stem,
        "repo": repo_name,
        "target_file": target_file,
        "priority": priority.lower(),
        "category": category,
        "root_cause": root_cause,
        "proposed_diff": proposed_diff,
        "spawned_at": now_iso(),
    }
    c_content = save_ledger_into_html(c_content, c_ledger)
    c_content = update_document_title(c_content)
    child_path.write_text(c_content, encoding="utf-8")

    # 2. Update parent ticket's delta status -> in_progress & attach spawned_ticket_id/url WITHOUT touching parent phases
    p_ledger = extract_ledger(parent_content, str(parent_path))
    for it_obj in p_ledger.get("iterations", []):
        for d in it_obj.get("harness_delta_proposals", []):
            if d.get("id") == delta_id:
                d["status"] = "in_progress"
                d["spawned_ticket_id"] = child_rel
                d["spawned_ticket_url"] = child_url
                d["updated_at"] = now_iso()
    for d in p_ledger.get("harness_delta_proposals", []):
        if d.get("id") == delta_id:
            d["status"] = "in_progress"
            d["spawned_ticket_id"] = child_rel
            d["spawned_ticket_url"] = child_url
            d["updated_at"] = now_iso()
    parent_content = save_ledger_into_html(parent_content, p_ledger)
    parent_path.write_text(parent_content, encoding="utf-8")

    # 3. Check if any watcher is armed on parent_path (or any active watcher in states_dir) and write wakeup event
    watcher_armed = is_watcher_armed_for_file(parent_path)
    agent_prompt = (
        f"/odin Execute spawned Mímir Harness Delta ticket `{child_rel}` (`{child_path}`) "
        f"for `{delta_id}` (`{repo_name}` -> `{target_file}`). "
        f"Leave parent ticket `{parent_rel}` untouched; when `{child_rel}` completes, parent delta status auto-syncs to APPLIED."
    )
    wakeup_notes = (
        f"Partner approved Mímir Harness Delta `{delta_id}` and spawned dedicated Delta Ticket `{child_rel}` "
        f"({child_url}). Execute `{child_path}` through Phases 2–5 to apply the harness guardrail to `{repo_name}` -> `{target_file}`."
    )
    write_wakeup_event(
        parent_path,
        decision="mimir_spawn_delta_ticket",
        phase="human-validation",
        notes=wakeup_notes,
        extra={
            "deltaId": delta_id,
            "spawnedTicketId": child_rel,
            "spawnedTicketFile": str(child_path),
            "spawnedTicketUrl": child_url,
            "agentPrompt": agent_prompt,
        },
    )
    # Also wake any other currently armed watcher in states_dir if the user clicked from Dashboard or another ticket
    if not watcher_armed:
        for wf in states_dir.glob(".odin-watcher-*.json"):
            other_slug = wf.stem[len(".odin-watcher-"):]
            other_html = states_dir / f"{other_slug}.html"
            if other_html.exists() and is_watcher_armed_for_file(other_html):
                watcher_armed = True
                write_wakeup_event(
                    other_html,
                    decision="mimir_spawn_delta_ticket",
                    phase="human-validation",
                    notes=wakeup_notes,
                    extra={
                        "deltaId": delta_id,
                        "spawnedTicketId": child_rel,
                        "spawnedTicketFile": str(child_path),
                        "spawnedTicketUrl": child_url,
                        "agentPrompt": agent_prompt,
                    },
                )
                break

    return {
        "ok": True,
        "deltaId": delta_id,
        "status": "in_progress",
        "parentStateFile": str(parent_path),
        "parentTicketId": parent_rel,
        "spawnedTicketFile": str(child_path),
        "spawnedTicketId": child_rel,
        "spawnedTicketUrl": child_url,
        "watcherArmed": watcher_armed,
        "agentPrompt": agent_prompt,
    }


def cmd_spawn_delta_ticket(args: argparse.Namespace) -> int:
    try:
        res = spawn_mimir_delta_ticket(
            delta_id=args.id,
            scan_root=getattr(args, "dir", AI_DEV_ROOT) or AI_DEV_ROOT,
            parent_file=getattr(args, "file", "") or getattr(args, "html_file", "") or "",
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_mimir_audit(args: argparse.Namespace) -> int:
    report = run_mimir_audit(args.dir)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    kt = report["kvasir_telemetry"]
    ms = report["mimir_synthesis"]
    print("# 🧠 Mímir & Kvasir Harness Audit Report")
    print(f"Generated: `{report['generated_at']}`\n")
    print("## 📊 Kvasir Quantitative Telemetry")
    print(f"- **Total Odin Projects**: {kt['total_projects']}")
    print(f"- **Multi-Iteration Projects**: {kt['multi_iteration_projects']} ({kt['step_back_rate_pct']}% step-back rate)")
    print(f"- **Total Iterations Tracked**: {kt['total_iterations']} (avg {kt['avg_iterations_per_project']} / project)")
    print(f"- **Rejection Sources**: Human={kt['rejection_breakdown']['human']}, CI/CD={kt['rejection_breakdown']['ci']}, Heimdall={kt['rejection_breakdown']['heimdall']}\n")
    print(f"## 🛠️ Mímir Harness Delta Proposals ({ms['pending_deltas']} Pending / {ms['total_deltas']} Total)")
    for d in ms["proposals"]:
        print(
            f"\n### [{d.get('priority', 'p1').upper()}] `{d.get('id')}` — `{d.get('repo')}` -> `{d.get('target_file')}` ({d.get('status', 'pending').upper()})"
        )
        print(f"- **Source Project**: `{d.get('project_slug')}` (Iteration {d.get('iteration')})")
        print(f"- **Category**: `{d.get('category')}`")
        print(f"- **Root Cause**: {d.get('root_cause')}")
        print(f"```markdown\n{d.get('proposed_diff')}\n```")
    return 0


def _get_odin_states_dir_for_html(html_path: Path) -> Path:
    return html_path.resolve().parent


def write_wakeup_event(
    html_path: str | Path,
    decision: str,
    phase: str = "",
    notes: str = "",
    selected_skills: list | None = None,
    extra: dict | None = None,
) -> Path:
    p = Path(html_path).resolve()
    slug = p.stem
    states_dir = _get_odin_states_dir_for_html(p)
    wakeup_file = states_dir / f".odin-wakeup-{slug}.json"
    payload = {
        "slug": slug,
        "file": str(p),
        "decision": decision,
        "phase": phase,
        "notes": notes,
        "selectedSkills": list(selected_skills or []),
        "timestamp": now_iso(),
    }
    if extra:
        payload.update(extra)
    wakeup_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return wakeup_file


def get_pause_state(states_dir: str | Path | None = None) -> dict:
    d = Path(states_dir) if states_dir else Path(AI_DEV_ROOT) / "odin-states"
    pause_file = d / ".odin-paused.json"
    if pause_file.exists():
        try:
            data = json.loads(pause_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"paused": False, "updatedAt": now_iso()}


def set_pause_state(paused: bool, reason: str = "", states_dir: str | Path | None = None) -> dict:
    d = Path(states_dir) if states_dir else Path(AI_DEV_ROOT) / "odin-states"
    d.mkdir(parents=True, exist_ok=True)
    pause_file = d / ".odin-paused.json"
    data = {
        "paused": bool(paused),
        "reason": reason,
        "updatedAt": now_iso(),
    }
    pause_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def cmd_wait_for_human(args: argparse.Namespace) -> int:
    try:
        html_path = _resolve_html_path(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not html_path.exists():
        print(f"Error: File not found: {html_path}", file=sys.stderr)
        return 1

    slug = html_path.stem
    states_dir = _get_odin_states_dir_for_html(html_path)
    wakeup_file = states_dir / f".odin-wakeup-{slug}.json"
    watcher_file = states_dir / f".odin-watcher-{slug}.json"

    # Clear any stale wakeup event from before this wait started unless --consume-existing is passed
    if wakeup_file.exists() and not getattr(args, "consume_existing", False):
        try:
            wakeup_file.unlink()
        except OSError:
            pass

    timeout_sec = float(getattr(args, "timeout", 0) or 0)
    poll_interval = float(getattr(args, "poll_interval", 1.0) or 1.0)
    started_at = time.time()
    started_at_iso = now_iso()

    def write_heartbeat(status: str = "armed") -> None:
        hb = {
            "slug": slug,
            "file": str(html_path),
            "pid": os.getpid(),
            "status": status,
            "phase": getattr(args, "phase", "") or "",
            "sessionId": getattr(args, "session_id", "") or "",
            "startedAt": started_at_iso,
            "heartbeatAt": now_iso(),
        }
        try:
            watcher_file.write_text(json.dumps(hb, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def cleanup_heartbeat(*_sig) -> None:
        try:
            if watcher_file.exists():
                watcher_file.unlink()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, lambda s, f: (cleanup_heartbeat(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda s, f: (cleanup_heartbeat(), sys.exit(0)))

    write_heartbeat("armed")
    try:
        last_hb = 0.0
        while True:
            now_ts = time.time()
            if now_ts - last_hb >= 2.0:
                pause_info = get_pause_state(states_dir)
                write_heartbeat("paused" if pause_info.get("paused") else "armed")
                last_hb = now_ts

            pause_info = get_pause_state(states_dir)
            if not pause_info.get("paused") and wakeup_file.exists():
                try:
                    raw = wakeup_file.read_text(encoding="utf-8")
                    event = json.loads(raw)
                except Exception:
                    event = {"slug": slug, "file": str(html_path), "decision": "human_action", "timestamp": now_iso()}
                try:
                    wakeup_file.unlink()
                except OSError:
                    pass
                cleanup_heartbeat()
                print("\n=== 🔔 ODIN REACTIVE PARTNER WAKEUP EVENT ===")
                print(json.dumps(event, indent=2, ensure_ascii=False))
                skills = event.get("selectedSkills") or []
                if skills:
                    print(f"\n⚡ Partner Attached Skills to Activate: {' '.join(skills)}")
                if event.get("notes"):
                    print(f"\n💬 Partner Notes / Instructions:\n{event['notes']}")
                return 0

            if timeout_sec > 0 and (now_ts - started_at) >= timeout_sec:
                cleanup_heartbeat()
                print(
                    json.dumps(
                        {
                            "slug": slug,
                            "file": str(html_path),
                            "decision": "timeout",
                            "timeoutSeconds": timeout_sec,
                            "timestamp": now_iso(),
                        },
                        indent=2,
                    )
                )
                return 0

            time.sleep(poll_interval)
    finally:
        cleanup_heartbeat()


def _is_git_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def _git_head_sha(path: Path) -> str:
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _normalize_state_file_links(html_file: Path) -> None:
    if not html_file.is_file() or html_file.name == "index.html":
        return
    try:
        raw = html_file.read_text(encoding="utf-8", errors="replace")
        updated = _normalize_all_projects_link_in_html(raw)
        if updated != raw:
            html_file.write_text(updated, encoding="utf-8")
    except OSError:
        pass


def snapshot_staging(
    active_state_rel: str = "",
    repo_dir: str = ODIN_REPO_DIR,
    staging_dir: str = ODIN_STAGING_DIR,
    states_src_dir: str | None = None,
    start_server: bool = True,
) -> dict:
    repo_path = Path(repo_dir).resolve()
    staging_path = Path(staging_dir).resolve()
    src_states = Path(states_src_dir).resolve() if states_src_dir else Path(AI_DEV_ROOT) / "odin-states"

    # 1. Pause live watchers on :8788
    set_pause_state(True, reason="Paused for Odin Staging (:8789) snapshot", states_dir=src_states)

    # 2. Auto-commit any dirty Odin work on main and hard-sync staging worktree (staging/odin-8789) to HEAD
    branch_name = STAGING_BRANCH
    head_sha = ""
    if _is_git_checkout(repo_path):
        st_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if st_res.stdout.strip():
            subprocess.run(["git", "add", "-A"], cwd=str(repo_path), capture_output=True, text=True, check=False)
            subprocess.run(
                ["git", "reset", "-q", "--", "odin-states"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Odin Allfather",
                    "-c",
                    "user.email=odin@local",
                    "commit",
                    "-m",
                    "chore(odin): auto-commit working tree before staging (:8789) snapshot",
                ],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
        head_sha = _git_head_sha(repo_path)

        if not _is_git_checkout(staging_path):
            if staging_path.exists():
                shutil.rmtree(staging_path)
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                ["git", "worktree", "add", "-B", branch_name, str(staging_path), head_sha or "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            subprocess.run(
                ["git", "checkout", "-B", branch_name, head_sha or "HEAD"],
                cwd=str(staging_path),
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                ["git", "reset", "--hard", head_sha or "HEAD"],
                cwd=str(staging_path),
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                ["git", "clean", "-fd", "-e", "odin-states"],
                cwd=str(staging_path),
                capture_output=True,
                text=True,
                check=False,
            )

    if not _is_git_checkout(staging_path):
        staging_path.mkdir(parents=True, exist_ok=True)
        for sub_name in ("SKILL.md", "scripts", "resources", "examples", "tests", ".gitignore"):
            src_item = repo_path / sub_name
            dst_item = staging_path / sub_name
            if src_item.is_dir():
                shutil.copytree(src_item, dst_item, dirs_exist_ok=True)
            elif src_item.is_file():
                dst_item.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_item, dst_item)

    # 3. Mirror odin-states/ (including archive/ and sub-workspace states) into staging_path / odin-states
    staging_states = staging_path / "odin-states"
    if staging_states.exists():
        shutil.rmtree(staging_states)
    staging_states.mkdir(parents=True, exist_ok=True)
    staging_archive = staging_states / "archive"
    staging_archive.mkdir(parents=True, exist_ok=True)

    if src_states.exists():
        for item in src_states.iterdir():
            if item.is_file() and (item.suffix == ".html" or item.name.startswith(".odin-")):
                dst_file = staging_states / item.name
                shutil.copy2(item, dst_file)
                _normalize_state_file_links(dst_file)
        src_archive = src_states / "archive"
        if src_archive.is_dir():
            for item in src_archive.iterdir():
                if item.is_file() and item.suffix == ".html":
                    dst_file = staging_archive / item.name
                    shutil.copy2(item, dst_file)
                    _normalize_state_file_links(dst_file)

    # Also copy sub-workspace state files if present
    rel_raw = (active_state_rel or "").lstrip("/")
    if rel_raw:
        for base_root in (src_states.parent, Path(AI_DEV_ROOT)):
            candidate_src = (base_root / rel_raw).resolve()
            if candidate_src.is_file() and candidate_src.suffix == ".html":
                dst_file = staging_states / candidate_src.name
                shutil.copy2(candidate_src, dst_file)
                _normalize_state_file_links(dst_file)
                break

    # Keep odin-states/index.html synced with resources/dashboard.html
    dashboard_tpl = repo_path / "resources" / "dashboard.html"
    if dashboard_tpl.is_file():
        shutil.copy2(dashboard_tpl, staging_states / "index.html")
        if src_states.exists():
            shutil.copy2(dashboard_tpl, src_states / "index.html")

    # 4. Ensure staging server on :8789 is running
    staging_server_script = staging_path / "scripts" / "odin-server.py"
    staging_port = STAGING_PORT
    staging_ready = False
    if start_server and staging_server_script.exists():
        current_pids = []
        try:
            lsof_res = subprocess.run(
                ["lsof", f"-tiTCP:{staging_port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                check=False,
            )
            current_pids = [int(p.strip()) for p in lsof_res.stdout.strip().splitlines() if p.strip().isdigit()]
        except Exception:
            pass

        if os.getpid() in current_pids or os.getppid() in current_pids:
            # Called from inside the :8789 server itself; schedule a detached restart so HTTP response completes first
            trigger_post_promote_servers(
                repo_dir=str(repo_path),
                staging_dir=str(staging_path),
                live_root=str(src_states.parent),
                staging_port=staging_port,
                stop_staging=False,
                restart_staging=True,
                restart_live=False,
            )
            staging_ready = True
        else:
            for pid_val in current_pids:
                try:
                    os.kill(pid_val, signal.SIGTERM)
                except Exception:
                    pass
            if current_pids:
                time.sleep(0.25)

            child_env = os.environ.copy()
            child_env["ODIN_PORT"] = str(staging_port)
            child_env["ODIN_ROOT"] = str(staging_path)
            child_env["ODIN_ROOT"] = str(staging_path)
            child_env["ODIN_PRIMARY_REPO_DIR"] = str(repo_path)
            child_env["ODIN_STAGING_DIR"] = str(staging_path)

            subprocess.Popen(
                [sys.executable, str(staging_server_script), "--port", str(staging_port), "--dir", str(staging_path)],
                cwd=str(staging_path),
                env=child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.time() + 2.5
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", staging_port), timeout=0.15):
                        staging_ready = True
                        break
                except OSError:
                    time.sleep(0.1)

    rel_clean = (active_state_rel or "").lstrip("/")
    if rel_clean and not rel_clean.startswith("odin-states/"):
        rel_clean = f"odin-states/{os.path.basename(rel_clean)}"
    staging_url = f"{base_url(CONFIG, staging_port)}/"
    staging_state_url = f"{staging_url}{rel_clean}" if rel_clean else staging_url

    debug_prompt = (
        f"/odin We are debugging/enhancing Odin in the isolated Staging Environment (:8789).\n"
        f"- Staging Repo Worktree: `{staging_path}` (branch `{branch_name}` @ `{head_sha[:7] if head_sha else 'HEAD'}`)\n"
        f"- Staging Dashboard: {staging_url}\n"
        f"- Snapshotted State File: `{staging_path / rel_clean if rel_clean else staging_states}` ({staging_state_url})\n"
        f"- Live `:8788` watchers are currently PAUSED so our active feature job is untouched.\n\n"
        f"Task: Inspect and fix the issue in `{staging_path}` and verify on `{staging_url}`. "
        f"When verified, we will promote the fix back to Live (`:8788`) via `odin-state.py promote-staging`."
    )

    return {
        "ok": True,
        "stagingDir": str(staging_path),
        "stagingBranch": branch_name,
        "stagingCommit": head_sha,
        "stagingPort": staging_port,
        "stagingUrl": staging_url,
        "stagingStateUrl": staging_state_url,
        "stagingServerReady": staging_ready if start_server else True,
        "pausedLive": True,
        "debugPrompt": debug_prompt,
    }


def cmd_snapshot_staging(args: argparse.Namespace) -> int:
    res = snapshot_staging(
        active_state_rel=getattr(args, "file", "") or "",
        repo_dir=getattr(args, "repo_dir", ODIN_REPO_DIR) or ODIN_REPO_DIR,
        staging_dir=getattr(args, "staging_dir", ODIN_STAGING_DIR) or ODIN_STAGING_DIR,
        start_server=not getattr(args, "no_server", False),
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def _get_listening_pids_for_port(port: int) -> list[int]:
    try:
        res = subprocess.run(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
        return [int(p.strip()) for p in res.stdout.strip().splitlines() if p.strip().isdigit()]
    except Exception:
        return []


def trigger_post_promote_servers(
    repo_dir: str = ODIN_REPO_DIR,
    staging_dir: str = ODIN_STAGING_DIR,
    live_root: str = AI_DEV_ROOT,
    staging_port: int = STAGING_PORT,
    live_port: int = LIVE_PORT,
    stop_staging: bool = False,
    restart_staging: bool = True,
    restart_live: bool = True,
) -> None:
    repo_path = Path(repo_dir).resolve()
    staging_path = Path(staging_dir).resolve()
    live_server_script = repo_path / "scripts" / "odin-server.py"
    staging_server_script = staging_path / "scripts" / "odin-server.py"

    active_server_pids = set(_get_listening_pids_for_port(live_port) + _get_listening_pids_for_port(staging_port))
    called_from_server = os.getpid() in active_server_pids or os.getppid() in active_server_pids

    if not called_from_server:
        # Synchronous restart when invoked from CLI so processes are up before CLI exits
        if restart_live and live_server_script.exists():
            for pid_val in _get_listening_pids_for_port(live_port):
                try:
                    os.kill(pid_val, signal.SIGTERM)
                except Exception:
                    pass
            time.sleep(0.25)
            live_env = os.environ.copy()
            live_env["ODIN_PORT"] = str(live_port)
            live_env["ODIN_ROOT"] = str(live_root)
            live_env["ODIN_ROOT"] = str(live_root)
            live_env["ODIN_PRIMARY_REPO_DIR"] = str(repo_path)
            live_env["ODIN_STAGING_DIR"] = str(staging_path)
            subprocess.Popen(
                [sys.executable, str(live_server_script), "--port", str(live_port), "--dir", str(live_root)],
                cwd=str(live_root),
                env=live_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        if stop_staging:
            for pid_val in _get_listening_pids_for_port(staging_port):
                try:
                    os.kill(pid_val, signal.SIGTERM)
                except Exception:
                    pass
        elif restart_staging and staging_server_script.exists():
            for pid_val in _get_listening_pids_for_port(staging_port):
                try:
                    os.kill(pid_val, signal.SIGTERM)
                except Exception:
                    pass
            time.sleep(0.25)
            stag_env = os.environ.copy()
            stag_env["ODIN_PORT"] = str(staging_port)
            stag_env["ODIN_ROOT"] = str(staging_path)
            stag_env["ODIN_ROOT"] = str(staging_path)
            stag_env["ODIN_PRIMARY_REPO_DIR"] = str(repo_path)
            stag_env["ODIN_STAGING_DIR"] = str(staging_path)
            subprocess.Popen(
                [sys.executable, str(staging_server_script), "--port", str(staging_port), "--dir", str(staging_path)],
                cwd=str(staging_path),
                env=stag_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        time.sleep(0.25)
        return

    # Spawn a detached restarter process when invoked from inside :8788 or :8789 HTTP handler
    helper_code = """
import os, signal, subprocess, sys, time
cfg = __import__("json").loads(sys.argv[1])
time.sleep(0.25)

def kill_port(port):
    try:
        res = subprocess.run(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, check=False)
        for p in res.stdout.strip().splitlines():
            if p.strip().isdigit():
                try:
                    os.kill(int(p.strip()), signal.SIGTERM)
                except Exception:
                    pass
    except Exception:
        pass

if cfg.get("restart_live") and os.path.isfile(cfg["live_script"]):
    kill_port(cfg["live_port"])
    time.sleep(0.25)
    env = os.environ.copy()
    env["ODIN_PORT"] = str(cfg["live_port"])
    env["ODIN_ROOT"] = cfg["live_root"]
    env["ODIN_ROOT"] = cfg["live_root"]
    env["ODIN_PRIMARY_REPO_DIR"] = cfg["repo_dir"]
    env["ODIN_STAGING_DIR"] = cfg["staging_dir"]
    subprocess.Popen(
        [sys.executable, cfg["live_script"], "--port", str(cfg["live_port"]), "--dir", cfg["live_root"]],
        cwd=cfg["live_root"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

if cfg.get("stop_staging"):
    kill_port(cfg["staging_port"])
elif cfg.get("restart_staging") and os.path.isfile(cfg["staging_script"]):
    kill_port(cfg["staging_port"])
    time.sleep(0.25)
    env = os.environ.copy()
    env["ODIN_PORT"] = str(cfg["staging_port"])
    env["ODIN_ROOT"] = cfg["staging_dir"]
    env["ODIN_ROOT"] = cfg["staging_dir"]
    env["ODIN_PRIMARY_REPO_DIR"] = cfg["repo_dir"]
    env["ODIN_STAGING_DIR"] = cfg["staging_dir"]
    subprocess.Popen(
        [sys.executable, cfg["staging_script"], "--port", str(cfg["staging_port"]), "--dir", cfg["staging_dir"]],
        cwd=cfg["staging_dir"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
"""
    cfg_payload = json.dumps(
        {
            "repo_dir": str(repo_path),
            "staging_dir": str(staging_path),
            "live_root": str(live_root),
            "live_script": str(live_server_script),
            "staging_script": str(staging_server_script),
            "live_port": int(live_port),
            "staging_port": int(staging_port),
            "stop_staging": bool(stop_staging),
            "restart_staging": bool(restart_staging),
            "restart_live": bool(restart_live),
        }
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", helper_code, cfg_payload],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def promote_staging(
    repo_dir: str = ODIN_REPO_DIR,
    staging_dir: str = ODIN_STAGING_DIR,
    states_src_dir: str | None = None,
    stop_staging_server: bool = False,
    restart_staging_server: bool = False,
    restart_live_server: bool = False,
) -> dict:
    repo_path = Path(repo_dir).resolve()
    staging_path = Path(staging_dir).resolve()
    src_states = Path(states_src_dir).resolve() if states_src_dir else Path(AI_DEV_ROOT) / "odin-states"

    if not staging_path.exists():
        return {"ok": False, "error": f"Staging worktree not found at {staging_path}"}

    promoted_files: list[str] = []
    promoted_commit = ""
    git_merged = False

    if _is_git_checkout(staging_path) and _is_git_checkout(repo_path):
        # 1. Auto-commit any modified Odin files on staging/odin-8789 (excluding odin-states/)
        st_staging = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(staging_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if st_staging.stdout.strip():
            subprocess.run(["git", "add", "-A"], cwd=str(staging_path), capture_output=True, text=True, check=False)
            subprocess.run(
                ["git", "reset", "-q", "--", "odin-states"],
                cwd=str(staging_path),
                capture_output=True,
                text=True,
                check=False,
            )
            staged_names = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=str(staging_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if staged_names.stdout.strip():
                promoted_files.extend([ln.strip() for ln in staged_names.stdout.strip().splitlines() if ln.strip()])
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Odin Allfather",
                        "-c",
                        "user.email=odin@local",
                        "commit",
                        "-m",
                        "feat(odin): promote verified staging (:8789) changes to main",
                    ],
                    cwd=str(staging_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )

        staging_sha = _git_head_sha(staging_path)
        main_sha = _git_head_sha(repo_path)

        if staging_sha and staging_sha != main_sha:
            if not promoted_files and main_sha:
                diff_names = subprocess.run(
                    ["git", "diff", "--name-only", f"{main_sha}..{staging_sha}"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                promoted_files.extend([ln.strip() for ln in diff_names.stdout.strip().splitlines() if ln.strip()])

            # Ensure repo_path working tree is clean before fast-forward merge
            st_live = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if st_live.stdout.strip():
                subprocess.run(["git", "add", "-A"], cwd=str(repo_path), capture_output=True, text=True, check=False)
                subprocess.run(
                    ["git", "reset", "-q", "--", "odin-states"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Odin Allfather",
                        "-c",
                        "user.email=odin@local",
                        "commit",
                        "-m",
                        "chore(odin): auto-commit live working tree before staging promotion",
                    ],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )

            ff_res = subprocess.run(
                ["git", "merge", "--ff-only", staging_sha],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if ff_res.returncode != 0:
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Odin Allfather",
                        "-c",
                        "user.email=odin@local",
                        "merge",
                        "--no-edit",
                        staging_sha,
                    ],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                merged_sha = _git_head_sha(repo_path)
                if merged_sha:
                    subprocess.run(
                        ["git", "checkout", "-B", STAGING_BRANCH, merged_sha],
                        cwd=str(staging_path),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    subprocess.run(
                        ["git", "reset", "--hard", merged_sha],
                        cwd=str(staging_path),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
            git_merged = True

        promoted_commit = _git_head_sha(repo_path)
        if not promoted_files:
            promoted_files = ["SKILL.md", "scripts/", "resources/", "examples/", "tests/"]
    else:
        # Fallback for non-git directories (e.g. unit test temp dirs)
        for sub_name in ("SKILL.md", "scripts", "resources", "examples", "tests", ".gitignore"):
            src_item = staging_path / sub_name
            dst_item = repo_path / sub_name
            if src_item.is_dir():
                shutil.copytree(src_item, dst_item, dirs_exist_ok=True)
                promoted_files.append(sub_name + "/")
            elif src_item.is_file():
                dst_item.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_item, dst_item)
                promoted_files.append(sub_name)

    # Sync odin-states/index.html from resources/dashboard.html so static index never drifts
    dashboard_tpl = repo_path / "resources" / "dashboard.html"
    staging_states = staging_path / "odin-states"
    if dashboard_tpl.is_file():
        if src_states.exists():
            shutil.copy2(dashboard_tpl, src_states / "index.html")
        if staging_states.exists():
            shutil.copy2(dashboard_tpl, staging_states / "index.html")

    # Resume live watchers on :8788 (and clear staging pause state if present)
    set_pause_state(False, reason="Promoted staging fix to Live (:8788)", states_dir=src_states)
    if staging_states.exists():
        set_pause_state(False, reason="Promoted staging fix to Live (:8788)", states_dir=staging_states)

    if stop_staging_server or restart_staging_server or restart_live_server:
        trigger_post_promote_servers(
            repo_dir=str(repo_path),
            staging_dir=str(staging_path),
            live_root=str(src_states.parent),
            stop_staging=stop_staging_server,
            restart_staging=restart_staging_server,
            restart_live=restart_live_server,
        )

    return {
        "ok": True,
        "promotedFrom": str(staging_path),
        "promotedTo": str(repo_path),
        "promotedItems": promoted_files,
        "promotedCommit": promoted_commit,
        "gitMerged": git_merged,
        "pausedLive": False,
        "stagingStopped": bool(stop_staging_server),
        "stagingRestarted": bool(restart_staging_server),
        "liveRestarted": bool(restart_live_server),
        "timestamp": now_iso(),
    }


def cmd_promote_staging(args: argparse.Namespace) -> int:
    res = promote_staging(
        repo_dir=getattr(args, "repo_dir", ODIN_REPO_DIR) or ODIN_REPO_DIR,
        staging_dir=getattr(args, "staging_dir", ODIN_STAGING_DIR) or ODIN_STAGING_DIR,
        stop_staging_server=False,
        restart_staging_server=True,
        restart_live_server=True,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Odin v3 Reactive Partner Bridge, Staging (:8789) & Mímir/Kvasir Ledger CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sb = sub.add_parser("step-back", help="Step back an Odin state file to a new iteration without losing prior history")
    sb.add_argument("html_file", nargs="?", help="Path to odin-states/<slug>.html")
    sb.add_argument("--file", help="Path to odin-states/<slug>.html")
    sb.add_argument("--reason", required=True, help="Human/CI/Heimdall rejection or follow-up reason")
    sb.add_argument("--root-cause", help="Optional root cause of why prior iteration failed")
    sb.add_argument("--source", default="human", choices=["human", "ci", "heimdall"], help="Source of step-back trigger")
    sb.set_defaults(func=cmd_step_back)

    up = sub.add_parser("update-phase", help="Atomically update a phase card for the current/target iteration")
    up.add_argument("html_file", nargs="?", help="Path to odin-states/<slug>.html")
    up.add_argument("--file", help="Path to odin-states/<slug>.html")
    up.add_argument("--phase", required=True, choices=list(PHASE_ALIASES.keys()))
    up.add_argument("--status", required=True, choices=["pending", "active", "done", "reject", "awaiting_human"])
    up.add_argument("--iteration", type=int, help="Target iteration number (defaults to current_iteration in ledger)")
    up.add_argument("--title", help="Custom summary title for the <details> iteration card")
    up.add_argument("--summary", help="Plain-text summary for the ledger and notification")
    up.add_argument("--files", help="Comma-separated list of files touched in this iteration")
    up.add_argument("--html", "--body-html", dest="html", help="Inline HTML fragment for the iteration card body")
    up.add_argument("--html-file-fragment", help="Path to file containing HTML fragment for the iteration card body")
    up.add_argument("--with-diff", action="store_true", help="Render an interactive Side-by-Side + Unified contextual git diff viewer inside the iteration card")
    up.add_argument("--diff-context", type=int, default=12, help="Number of surrounding context lines for git diff (default: 12, vs GitHub's 3)")
    up.add_argument("--diff-rationale", help="Architectural 'What Changed & Why' summary banner rendered above the code diff")
    up.add_argument("--file-note", action="append", help="Per-file 'Why This File Changed' explanation (format: 'path/to/file.js=Explanation' or JSON dict; repeatable)")
    up.add_argument("--repo-dir", action="append", help="Explicit git repository/worktree directory (repeatable for multi-repo diffs; defaults to body[data-worktree])")
    up.add_argument("--pr-url", help="GitHub Pull Request URL to stamp on <body> and render in the header & phase card")
    up.set_defaults(func=cmd_update_phase)

    lg = sub.add_parser("log", help="Append a progress entry to a phase's iteration card (no status change, no notification)")
    lg.add_argument("html_file", nargs="?", help="Path to odin-states/<slug>.html")
    lg.add_argument("--file", help="Path to odin-states/<slug>.html")
    lg.add_argument("--phase", help="Phase to log under (defaults to the current active phase)")
    lg.add_argument("--kind", default="step", choices=list(PROGRESS_LOG_KINDS.keys()))
    lg.add_argument("--text", required=True, help="One-line entry")
    lg.add_argument("--detail", help="Optional longer text, shown collapsed under the entry")
    lg.add_argument("--outcome", choices=["pass", "fail"], help="For test entries: pass or fail")
    lg.add_argument("--at", help="When it happened (ISO 8601), for recording events after the fact; defaults to now")
    lg.set_defaults(func=cmd_log)

    env = sub.add_parser("set-env", help="Restamp branch/worktree/ports on a state file and log the change")
    env.add_argument("html_file", nargs="?", help="Path to odin-states/<slug>.html")
    env.add_argument("--file", help="Path to odin-states/<slug>.html")
    env.add_argument("--branch")
    env.add_argument("--worktree")
    env.add_argument("--app-port")
    env.add_argument("--backend-port")
    env.add_argument("--reason", help="Why the environment changed")
    env.set_defaults(func=cmd_set_env)

    pd = sub.add_parser("propose-delta", help="Record a Phase 4 Hlidskjalf Harness Delta Proposal for Mímir/Kvasir")
    pd.add_argument("html_file", nargs="?", help="Path to odin-states/<slug>.html")
    pd.add_argument("--file", help="Path to odin-states/<slug>.html")
    pd.add_argument("--id", required=True, help="Unique delta ID (e.g. delta-glidepath-01)")
    pd.add_argument("--repo", default="odin", help="Target repository or skill (e.g. my-app, odin)")
    pd.add_argument("--target", help="Target harness file path relative to repo (e.g. docs/TESTING.md)")
    pd.add_argument("--target-path", dest="target_path", help="Alias for --target")
    pd.add_argument("--priority", default="p1", choices=["p0", "p1", "p2"])
    pd.add_argument("--category", default="testing", help="Category (e.g. testing, architecture, workflow, skill_gap, skill, rule, script)")
    pd.add_argument("--root-cause", help="Root cause from iteration retry or human rejection")
    pd.add_argument("--failure-pattern", dest="failure_pattern", help="Alias for --root-cause")
    pd.add_argument("--diff", help="Proposed markdown rule/skill addition or diff")
    pd.add_argument("--proposed-diff", dest="proposed_diff", help="Alias for --diff")
    pd.add_argument("--iteration", type=int, help="Source iteration number")
    pd.set_defaults(func=cmd_propose_delta)

    ma = sub.add_parser("mimir-audit", help="Run Kvasir telemetry & Mímir harness delta synthesis across all Odin states")
    ma.add_argument("--dir", default=AI_DEV_ROOT, help="Root directory to scan for odin-states/*.html")
    ma.add_argument("--format", default="markdown", choices=["markdown", "json"])
    ma.set_defaults(func=cmd_mimir_audit)

    sdt = sub.add_parser("spawn-delta-ticket", help="Spawn a dedicated child Odin Delta Ticket from a Mímir Harness Delta Proposal")
    sdt.add_argument("html_file", nargs="?", help="Optional parent path to odin-states/<slug>.html")
    sdt.add_argument("--file", help="Optional parent path to odin-states/<slug>.html")
    sdt.add_argument("--id", required=True, help="Mímir delta proposal ID (e.g. DELTA-STAGING-CLI-CONTRACT)")
    sdt.add_argument("--dir", default=AI_DEV_ROOT, help="Root directory to scan for odin-states/*.html")
    sdt.set_defaults(func=cmd_spawn_delta_ticket)

    wfh = sub.add_parser("wait-for-human", help="Block as a background task until the human partner clicks Save/Approve/Reject in Odin")
    wfh.add_argument("html_file", nargs="?", help="Path to odin-states/<slug>.html")
    wfh.add_argument("--file", help="Path to odin-states/<slug>.html")
    wfh.add_argument("--phase", default="", help="Current phase awaiting human (huginn-plan, heimdall-validation, human-validation)")
    wfh.add_argument("--session-id", dest="session_id", default="", help="Optional Claude Code session ID")
    wfh.add_argument("--timeout", type=float, default=0, help="Optional timeout in seconds (0 = wait indefinitely)")
    wfh.add_argument("--poll-interval", dest="poll_interval", type=float, default=1.0, help="Polling interval in seconds")
    wfh.add_argument("--consume-existing", dest="consume_existing", action="store_true", help="Consume an existing wakeup file immediately if present")
    wfh.set_defaults(func=cmd_wait_for_human)

    ss = sub.add_parser("snapshot-staging", help="Pause live watchers, snapshot the Odin repo & odin-states/ into the staging worktree, and launch :8789")
    ss.add_argument("--file", default="", help="Active state file relative path (e.g. odin-states/<slug>.html)")
    ss.add_argument("--repo-dir", dest="repo_dir", default=ODIN_REPO_DIR, help="Source Odin repo directory")
    ss.add_argument("--staging-dir", dest="staging_dir", default=ODIN_STAGING_DIR, help="Target Odin staging worktree directory")
    ss.add_argument("--no-server", dest="no_server", action="store_true", help="Skip launching the :8789 server daemon")
    ss.set_defaults(func=cmd_snapshot_staging)

    ps = sub.add_parser("promote-staging", help="Promote verified Odin changes from the staging worktree back to Live (:8788) and resume watchers")
    ps.add_argument("--repo-dir", dest="repo_dir", default=ODIN_REPO_DIR, help="Target Live Odin repo directory")
    ps.add_argument("--staging-dir", dest="staging_dir", default=ODIN_STAGING_DIR, help="Source Odin staging worktree directory")
    ps.set_defaults(func=cmd_promote_staging)

    rd = sub.add_parser("refresh-diff", help="Refresh all contextual diff viewers, sticky headers, and inline review scripts in an Odin state file")
    rd.add_argument("html_file", nargs="?", help="Path to odin-states/<slug>.html")
    rd.add_argument("--file", help="Path to odin-states/<slug>.html")
    rd.set_defaults(func=cmd_refresh_diff)

    return p


# Commands that rewrite a state file hold its lock, so concurrent hook writes aren't lost.
LOCKED_COMMANDS = {"update-phase", "log", "set-env", "step-back", "propose-delta", "refresh-diff"}


def run_command(parsed_args: argparse.Namespace) -> int:
    if parsed_args.command in LOCKED_COMMANDS:
        try:
            html_path = _resolve_html_path(parsed_args)
        except ValueError:
            return parsed_args.func(parsed_args)
        if html_path.exists():
            with state_file_lock(html_path):
                return parsed_args.func(parsed_args)
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    parser = build_parser()
    parsed_args = parser.parse_args()
    sys.exit(run_command(parsed_args))
