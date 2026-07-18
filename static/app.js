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
  const REACTION_EMOJI = {
    like: "\u{1F44D}", love: "❤️", celebrate: "\u{1F389}",
    support: "\u{1F91D}", insightful: "\u{1F4A1}", funny: "\u{1F602}",
  };

  const mql = window.matchMedia("(prefers-color-scheme: light)");
  const theme = () => (mql.matches ? PALETTE.light : PALETTE.dark);

  const $ = (id) => document.getElementById(id);
  const consoleEl = $("console");
  const statusPill = $("statusPill");
  const statusText = $("statusText");
  const runningBar = $("runningBar");
  const runningLabel = $("runningLabel");
  const runSpinner = $("runSpinner");
  const continueBtn = $("continueBtn");
  const stopBtn = $("stopBtn");
  const runBtns = { personal: $("runPersonal"), cybrum: $("runCybrum"), x: $("runX") };
  const AGENT_LABEL = { personal: "Personal Profile", cybrum: "Cybrum Solutions Page", x: "X (Twitter)" };
  const MAX_POSTS_INPUT = { personal: "maxPostsPersonal", cybrum: "maxPostsCybrum", x: "maxPostsX" };
  const AGENT_CHIP = {
    personal: { cls: "chip-personal", label: "Personal" },
    cybrum: { cls: "chip-cybrum", label: "Cybrum" },
    x: { cls: "chip-x", label: "X" },
  };

  let logsSince = 0;
  let lastStatus = "idle";
  let dailyChart = null;
  let reactionChart = null;

  function classifyLine(line) {
    if (/\[OK\]/.test(line)) return "log-ok";
    if (/\[FAIL\]|\[!\]|error|Error/.test(line)) return "log-fail";
    if (/\[SKIP\]|\[WARN\]|waiting|Enter/.test(line)) return "log-warn";
    if (/^={5,}|^─{5,}/.test(line.trim())) return "log-dim";
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
    statusText.textContent = agent ? `${label} — ${AGENT_LABEL[agent] || agent}` : label;

    const isActive = status === "running" || status === "waiting_login";
    const showBar = isActive || ["completed", "failed", "stopped"].includes(status);
    runningBar.classList.toggle("hidden", !showBar);
    runningBar.classList.toggle("is-active", isActive);
    runSpinner.classList.toggle("paused", !isActive);

    if (isActive) {
      runningLabel.textContent = status === "waiting_login"
        ? "Waiting for you to finish logging in in the browser window…"
        : `Running ${AGENT_LABEL[agent] || agent}…`;
    } else if (status === "completed") {
      runningLabel.textContent = "Run finished.";
    } else if (status === "failed") {
      runningLabel.textContent = "Run failed — check the console.";
    } else if (status === "stopped") {
      runningLabel.textContent = "Run stopped.";
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
    const maxPostsEl = $(MAX_POSTS_INPUT[agent]);
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
  runBtns.x.addEventListener("click", () => runAgent("x"));
  stopBtn.addEventListener("click", () => fetch("/api/stop", { method: "POST" }));
  continueBtn.addEventListener("click", () => fetch("/api/continue", { method: "POST" }));
  $("clearConsole").addEventListener("click", () => { consoleEl.textContent = ""; });

  function renderCharts(stats) {
    const t = theme();

    // Sirf aakhri 14 din — poori history bars ko itna patla kar deti hai ke
    // chart parhna mushkil ho jata hai.
    const n = 14;
    const days = stats.days.slice(-n);
    const dailyTotal = stats.daily_total.slice(-n);
    const dailySuccess = stats.daily_success.slice(-n);
    const dayLabels = days.map((d) => d.slice(5));  // "2026-07-17" -> "07-17"

    if (dailyChart) dailyChart.destroy();
    dailyChart = new Chart($("dailyChart"), {
      type: "bar",
      data: {
        labels: dayLabels,
        datasets: [
          { label: "Processed", data: dailyTotal, backgroundColor: t.blue, borderRadius: 4, maxBarThickness: 24 },
          { label: "Successful", data: dailySuccess, backgroundColor: t.aqua, borderRadius: 4, maxBarThickness: 24 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: t.text, boxWidth: 12, boxHeight: 12 } } },
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
    reactionChart = new Chart($("reactionChart"), {
      type: "doughnut",
      data: { labels, datasets: [{ data: values, backgroundColor: colors, borderColor: t.surface, borderWidth: 2 }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "62%",
        plugins: { legend: { position: "bottom", labels: { color: t.text, boxWidth: 12, boxHeight: 12 } } },
      },
    });
  }

  function esc(s) {
    return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function renderRecent(recent) {
    const body = $("recentBody");
    if (!recent.length) {
      body.innerHTML = "<tr><td colspan='5' class='empty-row'>No activity yet — run an agent to see results here.</td></tr>";
      return;
    }
    body.innerHTML = recent.map((r) => {
      const ts = (r.timestamp || "").replace("T", " ").slice(0, 16);
      const chip = AGENT_CHIP[r.agent] || AGENT_CHIP.personal;
      const agentCls = chip.cls;
      const agentTxt = chip.label;
      const emoji = REACTION_EMOJI[r.reaction] || "";
      const statusChip = r.success
        ? "<span class='chip chip-ok'>Posted</span>"
        : "<span class='chip chip-fail'>Failed</span>";
      return `<tr>
        <td class="ts">${ts}</td>
        <td><span class="chip ${agentCls}">${agentTxt}</span></td>
        <td class="reaction-cell"><span class="reaction-emoji">${emoji}</span>${esc(r.reaction)}</td>
        <td>${statusChip}</td>
        <td class="comment-cell" title="${esc(r.comment)}">${esc(r.comment)}</td>
      </tr>`;
    }).join("\n");
  }

  const META_EL = { personal: "metaPersonal", cybrum: "metaCybrum", x: "metaX" };

  function renderAgentMeta(recent) {
    const latest = { personal: null, cybrum: null, x: null };
    for (const r of recent) {
      if (latest[r.agent] === null) latest[r.agent] = r;
    }
    for (const agent of ["personal", "cybrum", "x"]) {
      const el = $(META_EL[agent]);
      const r = latest[agent];
      if (!r) { el.textContent = "No activity yet"; continue; }
      const ts = (r.timestamp || "").replace("T", " ").slice(0, 16);
      const emoji = REACTION_EMOJI[r.reaction] || "";
      el.textContent = `Last engagement: ${ts} ${emoji}`;
    }
  }

  async function loadStats() {
    const res = await fetch("/api/stats");
    const stats = await res.json();
    $("statTotal").textContent = stats.total;
    $("statSuccess").textContent = stats.successful;
    $("statRate").textContent = `${stats.success_rate}%`;
    $("statReacted").textContent = stats.reacted;
    $("rateMeter").style.width = `${Math.min(100, stats.success_rate)}%`;
    $("agentBreakdown").textContent =
      `Personal: ${stats.personal_total} posts   ·   Cybrum: ${stats.cybrum_total} posts` +
      `   ·   X: ${stats.x_total || 0} posts`;
    renderCharts(stats);
    renderRecent(stats.recent);
    renderAgentMeta(stats.recent);
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
