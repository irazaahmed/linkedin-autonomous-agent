"""Reads logs/*.json and writes a self-contained dashboard.html (Chart.js via CDN,
no extra Python dependency) summarizing the agent's activity."""

import json
from collections import defaultdict
from pathlib import Path

LOGS_DIR = Path("logs")
OUTPUT_FILE = Path("dashboard.html")


def load_all_entries() -> list[dict]:
    entries = []
    for log_file in sorted(LOGS_DIR.glob("*.json")):
        if log_file.name == "engaged.json":
            continue
        try:
            with open(log_file, encoding="utf-8") as f:
                entries.extend(json.load(f))
        except Exception:
            continue
    return entries


def build_stats(entries: list[dict]) -> dict:
    total = len(entries)
    successful = sum(1 for e in entries if e.get("success"))
    reacted = sum(1 for e in entries if e.get("reacted"))

    reaction_counts = defaultdict(int)
    for e in entries:
        reaction_counts[e.get("reaction") or "unknown"] += 1

    daily = defaultdict(lambda: {"total": 0, "success": 0})
    for e in entries:
        day = (e.get("timestamp") or "")[:10] or "unknown"
        daily[day]["total"] += 1
        if e.get("success"):
            daily[day]["success"] += 1
    days = sorted(daily.keys())

    recent = sorted(entries, key=lambda e: e.get("timestamp", ""), reverse=True)[:10]

    return {
        "total": total,
        "successful": successful,
        "success_rate": round(successful / total * 100, 1) if total else 0,
        "reacted": reacted,
        "reaction_labels": list(reaction_counts.keys()),
        "reaction_values": list(reaction_counts.values()),
        "days": days,
        "daily_total": [daily[d]["total"] for d in days],
        "daily_success": [daily[d]["success"] for d in days],
        "recent": [
            {
                "timestamp": e.get("timestamp", ""),
                "reaction": e.get("reaction") or "-",
                "success": bool(e.get("success")),
                "comment": (e.get("comment") or "")[:160],
            }
            for e in recent
        ],
    }


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>LinkedIn Agent — Activity Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    background: #0f1115; color: #e6e6e6;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0; padding: 32px;
  }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  p.sub {{ color: #9aa0a6; margin-top: 0; margin-bottom: 28px; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
  .card {{
    background: #1a1d23; border: 1px solid #2a2e37; border-radius: 10px;
    padding: 18px 24px; min-width: 150px;
  }}
  .card .value {{ font-size: 28px; font-weight: 600; }}
  .card .label {{ color: #9aa0a6; font-size: 13px; margin-top: 4px; }}
  .charts {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 32px; }}
  .chart-box {{
    background: #1a1d23; border: 1px solid #2a2e37; border-radius: 10px;
    padding: 18px; flex: 1; min-width: 320px;
  }}
  .chart-box h3 {{ margin-top: 0; font-size: 15px; color: #c9ccd1; }}
  table {{ width: 100%; border-collapse: collapse; background: #1a1d23; border-radius: 10px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #2a2e37; }}
  th {{ color: #9aa0a6; font-weight: 500; }}
  .ok {{ color: #4ade80; }}
  .fail {{ color: #f87171; }}
</style>
</head>
<body>
  <h1>LinkedIn Agent — Activity Dashboard</h1>
  <p class="sub">Generated from logs/*.json</p>

  <div class="cards">
    <div class="card"><div class="value">{total}</div><div class="label">Posts processed</div></div>
    <div class="card"><div class="value">{successful}</div><div class="label">Comments posted</div></div>
    <div class="card"><div class="value">{success_rate}%</div><div class="label">Success rate</div></div>
    <div class="card"><div class="value">{reacted}</div><div class="label">Reactions given</div></div>
  </div>

  <div class="charts">
    <div class="chart-box">
      <h3>Daily activity</h3>
      <canvas id="dailyChart"></canvas>
    </div>
    <div class="chart-box">
      <h3>Reaction breakdown</h3>
      <canvas id="reactionChart"></canvas>
    </div>
  </div>

  <h3>Recent comments</h3>
  <table>
    <tr><th>Timestamp</th><th>Reaction</th><th>Status</th><th>Comment</th></tr>
    {recent_rows}
  </table>

<script>
new Chart(document.getElementById('dailyChart'), {{
  type: 'bar',
  data: {{
    labels: {days},
    datasets: [
      {{ label: 'Processed', data: {daily_total}, backgroundColor: '#3b82f6' }},
      {{ label: 'Successful', data: {daily_success}, backgroundColor: '#4ade80' }}
    ]
  }},
  options: {{ responsive: true, scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }} }}
}});

new Chart(document.getElementById('reactionChart'), {{
  type: 'doughnut',
  data: {{
    labels: {reaction_labels},
    datasets: [{{ data: {reaction_values}, backgroundColor: ['#3b82f6','#a855f7','#f59e0b','#ef4444','#10b981','#6366f1','#64748b'] }}]
  }},
  options: {{ responsive: true }}
}});
</script>
</body>
</html>
"""


def render_recent_rows(recent: list[dict]) -> str:
    rows = []
    for r in recent:
        status_cls = "ok" if r["success"] else "fail"
        status_txt = "Posted" if r["success"] else "Failed"
        comment = (r["comment"] or "").replace("<", "&lt;").replace(">", "&gt;")
        rows.append(
            f"<tr><td>{r['timestamp']}</td><td>{r['reaction']}</td>"
            f"<td class='{status_cls}'>{status_txt}</td><td>{comment}</td></tr>"
        )
    return "\n    ".join(rows) if rows else "<tr><td colspan='4'>No data yet.</td></tr>"


def main():
    entries = load_all_entries()
    stats = build_stats(entries)

    html = PAGE_TEMPLATE.format(
        total=stats["total"],
        successful=stats["successful"],
        success_rate=stats["success_rate"],
        reacted=stats["reacted"],
        days=json.dumps(stats["days"]),
        daily_total=json.dumps(stats["daily_total"]),
        daily_success=json.dumps(stats["daily_success"]),
        reaction_labels=json.dumps(stats["reaction_labels"]),
        reaction_values=json.dumps(stats["reaction_values"]),
        recent_rows=render_recent_rows(stats["recent"]),
    )

    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {OUTPUT_FILE.resolve()}")
    print(f"  {stats['total']} entries, {stats['successful']} successful ({stats['success_rate']}%)")


if __name__ == "__main__":
    main()
