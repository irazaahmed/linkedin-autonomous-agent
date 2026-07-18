"""Local web dashboard: start/stop the LinkedIn agents from a browser instead
of a terminal, watch their console output live, and see activity stats.
Binds to 127.0.0.1 only — this is a single-user local control panel, not a
hosted service, so it intentionally has no auth."""

import json
import os
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
CYBRUM_LOGS_DIR = LOGS_DIR / "cybrum"

AGENTS = {
    "personal": {"script": "linkedin_watcher.py", "label": "Personal Profile"},
    "cybrum": {"script": "linkedin_watcher_cybrum.py", "label": "Cybrum Solutions Page"},
}

# The login input() prompt in linkedin_watcher.py blocks the subprocess on
# stdin — matched as a substring against the not-yet-newline-terminated tail
# of stdout so we can react before a full line is even flushed. (The old
# end-of-run close prompt is skipped entirely when stdin is a pipe, so only
# the login prompt needs handling here.)
LOGIN_PROMPT_MARKER = "jab feed pe ho"

app = Flask(__name__)


class JobManager:
    """Owns at most one running agent subprocess at a time (both agents share
    the same LinkedIn browser session, so concurrent runs would fight over
    it). Reads child stdout char-by-char, not line-by-line, so we can detect
    an input() prompt even though it has no trailing newline."""

    def __init__(self):
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.agent: str | None = None
        self.status = "idle"  # idle | running | waiting_login | completed | failed | stopped
        self.log_lines: list[str] = []
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.returncode: int | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "agent": self.agent,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "returncode": self.returncode,
                "log_count": len(self.log_lines),
            }

    def get_logs(self, since: int) -> list[str]:
        with self.lock:
            return list(self.log_lines[since:])

    def _push_line(self, line: str) -> None:
        with self.lock:
            self.log_lines.append(line)

    def start(self, agent: str, max_posts: int | None) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("A run is already in progress.")

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            if max_posts:
                env["MAX_POSTS"] = str(max_posts)

            script = AGENTS[agent]["script"]
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self.process = subprocess.Popen(
                [sys.executable, "-u", script],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
            )
            self.agent = agent
            self.status = "running"
            self.log_lines = []
            self.started_at = datetime.now().isoformat()
            self.ended_at = None
            self.returncode = None

        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        proc = self.process
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if ch == "":
                break
            buf += ch
            if ch == "\n":
                self._push_line(buf.rstrip("\n"))
                buf = ""
                continue

            if LOGIN_PROMPT_MARKER in buf:
                self._push_line(buf)
                buf = ""
                with self.lock:
                    self.status = "waiting_login"

        if buf:
            self._push_line(buf)

        proc.wait()
        with self.lock:
            self.returncode = proc.returncode
            if self.status != "stopped":
                self.status = "completed" if proc.returncode == 0 else "failed"
            self.ended_at = datetime.now().isoformat()

    def stop(self) -> None:
        with self.lock:
            proc = self.process
            if not proc or proc.poll() is not None:
                return
            pid = proc.pid
            self.status = "stopped"

        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            proc.terminate()

    def send_continue(self) -> bool:
        with self.lock:
            if not (self.process and self.status == "waiting_login"):
                return False
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            except Exception:
                return False
            self.status = "running"
            return True


job = JobManager()


# --- Stats -------------------------------------------------------------

REACTION_ORDER = ["like", "love", "celebrate", "support", "insightful", "funny"]


def _load_entries(log_dir: Path, agent: str) -> list[dict]:
    entries = []
    if not log_dir.is_dir():
        return entries
    for f in sorted(log_dir.glob("*.json")):
        if f.name == "engaged.json" or f.name.startswith("switch_debug_"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in data:
            e["_agent"] = agent
        entries.extend(data)
    return entries


def build_stats() -> dict:
    entries = _load_entries(LOGS_DIR, "personal") + _load_entries(CYBRUM_LOGS_DIR, "cybrum")

    total = len(entries)
    successful = sum(1 for e in entries if e.get("success"))
    reacted = sum(1 for e in entries if e.get("reacted"))
    per_agent = defaultdict(int)
    for e in entries:
        per_agent[e["_agent"]] += 1

    reaction_counts: dict[str, int] = defaultdict(int)
    for e in entries:
        label = e.get("reaction") or "unknown"
        reaction_counts[label if label in REACTION_ORDER else "unknown"] += 1
    reaction_labels = [r for r in REACTION_ORDER if reaction_counts.get(r)]
    if reaction_counts.get("unknown"):
        reaction_labels.append("unknown")

    daily = defaultdict(lambda: {"total": 0, "success": 0})
    for e in entries:
        day = (e.get("timestamp") or "")[:10] or "unknown"
        daily[day]["total"] += 1
        if e.get("success"):
            daily[day]["success"] += 1
    days = sorted(daily.keys())

    recent = sorted(entries, key=lambda e: e.get("timestamp", ""), reverse=True)[:12]

    return {
        "total": total,
        "successful": successful,
        "success_rate": round(successful / total * 100, 1) if total else 0,
        "reacted": reacted,
        "personal_total": per_agent.get("personal", 0),
        "cybrum_total": per_agent.get("cybrum", 0),
        "reaction_labels": reaction_labels,
        "reaction_values": [reaction_counts[r] for r in reaction_labels],
        "days": days,
        "daily_total": [daily[d]["total"] for d in days],
        "daily_success": [daily[d]["success"] for d in days],
        "recent": [
            {
                "timestamp": e.get("timestamp", ""),
                "agent": e["_agent"],
                "reaction": e.get("reaction") or "-",
                "success": bool(e.get("success")),
                "comment": (e.get("comment") or "")[:160],
            }
            for e in recent
        ],
    }


# --- Routes --------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", agents=AGENTS)


@app.route("/api/status")
def api_status():
    return jsonify(job.snapshot())


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    agent = data.get("agent")
    if agent not in AGENTS:
        return jsonify({"error": "Unknown agent."}), 400

    max_posts = data.get("max_posts")
    try:
        max_posts = int(max_posts) if max_posts else None
        if max_posts is not None:
            max_posts = max(1, min(15, max_posts))
    except (TypeError, ValueError):
        max_posts = None

    try:
        job.start(agent, max_posts)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    job.stop()
    return jsonify({"ok": True})


@app.route("/api/continue", methods=["POST"])
def api_continue():
    ok = job.send_continue()
    return jsonify({"ok": ok})


@app.route("/api/logs")
def api_logs():
    since = request.args.get("since", 0, type=int)
    lines = job.get_logs(since)
    snap = job.snapshot()
    snap["lines"] = lines
    snap["next"] = since + len(lines)
    return jsonify(snap)


@app.route("/api/stats")
def api_stats():
    return jsonify(build_stats())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
