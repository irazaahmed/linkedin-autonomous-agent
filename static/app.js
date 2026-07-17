(() => {
  const POLL_MS = 1200;

  // Categorical steps from the validated reference palette (dataviz skill),
  // light/dark columns — picked at render time so charts match the OS theme
  // the same way the CSS custom properties do.
  const PALETTE = {
    dark:  { blue: "#3987e5", aqua: "#199e70", yellow: "#c98500", green: "#008300",
              violet: "#9085e9", red: "#e66767", magenta: "#d55181", orange: "#d95926",
              muted: "#898781", grid: "#2c2c2a", text: "#c3c2b7", surface: "#1a1a19" },
    light: { blue: "#2a78d6", aqua: "#1baf7a", yellow: "#eda100", green: "#008300",
              violet: "#4a3aa7", red: "#e34948", magenta: "#e87ba4", orange: "#eb6834",
              muted: "#898781", grid: "#e1e0d9", text: "#52514e", surface: "#fcfcfb" },
  };
  const REACTION_COLOR_KEY = {
    like: "blue", love: "magenta", celebrate: "orange", support: "aqua",
    insightful: "violet", funny: "yellow", unknown: "muted",
  };

  const mql = window.matchMedia("(prefers-color-scheme: light)");
  const theme = () => (mql.matches ? PALETTE.light : PALETTE.dark);

  const $ = (id) => document.getElementById(id);
  const consoleEl = $("console");
  const statusPill = $("statusPill");
  const runningBar = $("runningBar");
  const runningLabel = $("runningLabel");
  const continueBtn = $("continueBtn");
  const stopBtn = $("stopBtn");
  const runBtns = { personal: $("runPersonal"), cybrum: $("runCybrum") };
  const AGENT_LABEL = { personal: "Personal Profile", cybrum: "Cybrum Solutions Page" };

  let logsSince = 0;
  let lastStatus = "idle";
  let dailyChart = null;
  let reactionChart = null;

  function classifyLine(line) {
    if (/\[OK\]/.test(line)) return "log-ok";
    if (/\[FAIL\]|error|Error/.test(line)) return "log-fail";
    if (/\[!\]|waiting|Enter/.test(line)) return "log-warn";
    if (/^={5,}/.test(line.trim())) return "log-dim";
    return "";
  }

  function appendLogs(lines) {
    if (!lines.length) return;
    const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;
    for (const line of lines) {
      const span = document.createElement("div");
      const cls = classifyLine(line);
      if (cls) span.className = cls;
      span.textContent = line;
      consoleEl.appendChild(span);
    }
    if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  function setStatus(status, agent) {
    statusPill.className = "pill pill-" + status;
    const label = {
      idle: "Idle",
      running: "Running",
      waiting_login: "Waiting for login",
      completed: "Completed",
      failed: "Failed",
      stopped: "Stopped",
    }[status] || status;
    statusPill.textContent = agent ? `${label} — ${AGENT_LABEL[agent] || agent}` : label;

    const isActive = status === "running" || status === "waiting_login";
    runningBar.classList.toggle("hidden", !isActive && status !== "completed" && status !== "failed" && status !== "stopped");
    if (isActive) {
      runningBar.classList.remove("hidden");
      runningLabel.textContent = status === "waiting_login"
        ? "Waiting for you to finish logging in in the browser window…"
        : `Running ${AGENT_LABEL[agent] || agent}…`;
    } else if (status === "completed" || status === "failed" || status === "stopped") {
      runningLabel.textContent = status === "completed" ? "Run finished." : status === "failed" ? "Run failed — check the console." : "Run stopped.";
    }
    continueBtn.classList.toggle("hidden", status !== "waiting_login");
    stopBtn.classList.toggle("hidden", !isActive);

    for (const btn of Object.values(runBtns)) btn.disabled = isActive;
  }

  async function pollLogs() {
    try {
      const res = await fetch(`/api/logs?since=${logsSince}`);
      const data = await res.json();
      appendLogs(data.lines || []);
      logsSince = data.next ?? logsSince;
      setStatus(data.status, data.agent);

      if (lastStatus !== data.status && ["completed", "failed", "stopped"].includes(data.status)) {
        loadStats();
      }
      lastStatus = data.status;
    } catch (e) {
      // transient network hiccup — next poll will retry
    }
  }

  async function runAgent(agent) {
    const maxPostsEl = $(agent === "personal" ? "maxPostsPersonal" : "maxPostsCybrum");
    const max_posts = parseInt(maxPostsEl.value, 10) || undefined;
    for (const btn of Object.values(runBtns)) btn.disabled = true;
    consoleEl.textContent = "";
    logsSince = 0;
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent, max_posts }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.error || "Could not start the run.");
      for (const btn of Object.values(runBtns)) btn.disabled = false;
    }
  }

  runBtns.personal.addEventListener("click", () => runAgent("personal"));
  runBtns.cybrum.addEventListener("click", () => runAgent("cybrum"));
  stopBtn.addEventListener("click", () => fetch("/api/stop", { method: "POST" }));
  continueBtn.addEventListener("click", () => fetch("/api/continue", { method: "POST" }));

  function fmtPct(n) { return `${n}%`; }

  function renderCharts(stats) {
    const t = theme();
    const dailyCtx = $("dailyChart");
    const reactionCtx = $("reactionChart");

    if (dailyChart) dailyChart.destroy();
    dailyChart = new Chart(dailyCtx, {
      type: "bar",
      data: {
        labels: stats.days,
        datasets: [
          { label: "Processed", data: stats.daily_total, backgroundColor: t.blue, borderRadius: 4, maxBarThickness: 24 },
          { label: "Successful", data: stats.daily_success, backgroundColor: t.aqua, borderRadius: 4, maxBarThickness: 24 },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { color: t.text } } },
        scales: {
          x: { ticks: { color: t.text }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { color: t.text, precision: 0 }, grid: { color: t.grid } },
        },
      },
    });

    if (reactionChart) reactionChart.destroy();
    const labels = stats.reaction_labels.length ? stats.reaction_labels : ["No data"];
    const values = stats.reaction_values.length ? stats.reaction_values : [1];
    const colors = stats.reaction_labels.length
      ? stats.reaction_labels.map((l) => t[REACTION_COLOR_KEY[l] || "muted"])
      : [t.grid];
    reactionChart = new Chart(reactionCtx, {
      type: "doughnut",
      data: { labels, datasets: [{ data: values, backgroundColor: colors, borderColor: t.surface, borderWidth: 2 }] },
      options: { responsive: true, plugins: { legend: { position: "bottom", labels: { color: t.text } } } },
    });
  }

  function renderRecent(recent) {
    const body = $("recentBody");
    if (!recent.length) {
      body.innerHTML = "<tr><td colspan='5'>No data yet.</td></tr>";
      return;
    }
    body.innerHTML = recent.map((r) => {
      const statusCls = r.success ? "ok" : "fail";
      const statusTxt = r.success ? "Posted" : "Failed";
      const comment = (r.comment || "").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      const ts = (r.timestamp || "").replace("T", " ").slice(0, 16);
      return `<tr><td class="ts">${ts}</td><td>${r.agent}</td><td>${r.reaction}</td>` +
             `<td class="${statusCls}">${statusTxt}</td><td>${comment}</td></tr>`;
    }).join("\n");
  }

  async function loadStats() {
    const res = await fetch("/api/stats");
    const stats = await res.json();
    $("statTotal").textContent = stats.total;
    $("statSuccess").textContent = stats.successful;
    $("statRate").textContent = fmtPct(stats.success_rate);
    $("statReacted").textContent = stats.reacted;
    $("agentBreakdown").textContent =
      `Personal: ${stats.personal_total} posts   ·   Cybrum: ${stats.cybrum_total} posts`;
    renderCharts(stats);
    renderRecent(stats.recent);
  }

  mql.addEventListener("change", () => loadStats());

  async function init() {
    const res = await fetch("/api/status");
    const status = await res.json();
    lastStatus = status.status;
    logsSince = 0;
    setStatus(status.status, status.agent);
    if (status.status !== "idle") await pollLogs();
    await loadStats();
    setInterval(pollLogs, POLL_MS);
  }

  init();
})();
