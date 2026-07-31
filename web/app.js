const AUTO_REFRESH_INTERVAL_MS = 10 * 60 * 1000;

const state = {
  view: "live",
  valuations: [],
  snapshots: [],
  snapshotRows: [],
  selectedSnapshotKey: null,
};

const liveView = document.querySelector("#liveView");
const snapshotsView = document.querySelector("#snapshotsView");
const fundList = document.querySelector("#fundList");
const fundForm = document.querySelector("#fundForm");
const fundCode = document.querySelector("#fundCode");
const fundAlias = document.querySelector("#fundAlias");
const healthLine = document.querySelector("#healthLine");
const refreshButton = document.querySelector("#refreshButton");
const snapshotButton = document.querySelector("#snapshotButton");
const fundCount = document.querySelector("#fundCount");
const estimatedCount = document.querySelector("#estimatedCount");
const updatedAt = document.querySelector("#updatedAt");
const snapshotCount = document.querySelector("#snapshotCount");
const selectedSnapshot = document.querySelector("#selectedSnapshot");
const snapshotDate = document.querySelector("#snapshotDate");
const snapshotFundCount = document.querySelector("#snapshotFundCount");
const snapshotList = document.querySelector("#snapshotList");
const snapshotDetail = document.querySelector("#snapshotDetail");

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.view = button.dataset.view;
    document.querySelectorAll("[data-view]").forEach((tab) => tab.classList.toggle("active", tab === button));
    liveView.classList.toggle("hidden", state.view !== "live");
    snapshotsView.classList.toggle("hidden", state.view !== "snapshots");
    if (state.view === "snapshots") {
      await loadSnapshots();
    }
  });
});

fundForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    code: fundCode.value.trim(),
    alias: fundAlias.value.trim() || null,
  };
  const response = await fetch("/api/funds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    showError(fundList, "基金代码格式不正确");
    return;
  }
  fundCode.value = "";
  fundAlias.value = "";
  await loadLive();
});

refreshButton.addEventListener("click", async () => {
  if (state.view === "snapshots") {
    await loadSnapshots();
  } else {
    await loadLive();
  }
});

snapshotButton.addEventListener("click", async () => {
  snapshotButton.disabled = true;
  try {
    const response = await fetch("/api/snapshots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const snapshot = await response.json();
    state.selectedSnapshotKey = snapshot.snapshot_key;
    document.querySelector('[data-view="snapshots"]').click();
  } finally {
    snapshotButton.disabled = false;
  }
});

async function loadLive() {
  refreshButton.disabled = true;
  try {
    const [healthResponse, valuationResponse] = await Promise.all([
      fetch("/api/health"),
      fetch("/api/valuations"),
    ]);
    const health = await healthResponse.json();
    state.valuations = await valuationResponse.json();
    renderHealth(health);
    renderLive();
  } catch (error) {
    showError(fundList, "无法连接本地服务");
  } finally {
    refreshButton.disabled = false;
  }
}

async function loadSnapshots() {
  const response = await fetch("/api/snapshots");
  state.snapshots = await response.json();
  if (!state.selectedSnapshotKey && state.snapshots.length) {
    state.selectedSnapshotKey = state.snapshots[0].snapshot_key;
  }
  if (state.selectedSnapshotKey) {
    const detailResponse = await fetch(`/api/snapshots/${encodeURIComponent(state.selectedSnapshotKey)}`);
    state.snapshotRows = await detailResponse.json();
  } else {
    state.snapshotRows = [];
  }
  renderSnapshots();
}

function isTradingRefreshWindow(date) {
  const day = date.getDay();
  const minutes = date.getHours() * 60 + date.getMinutes();
  return day >= 1 && day <= 5 && minutes >= 9 * 60 && minutes <= 15 * 60;
}

function startAutoRefresh() {
  window.setInterval(async () => {
    if (state.view !== "live" || document.hidden || !(await shouldAutoRefreshNow(new Date()))) {
      return;
    }
    loadLive();
  }, AUTO_REFRESH_INTERVAL_MS);
}

async function shouldAutoRefreshNow(date) {
  if (!isTradingRefreshWindow(date)) {
    return false;
  }
  try {
    const response = await fetch("/api/trading-status");
    const status = await response.json();
    return Boolean(status.is_refresh_window);
  } catch (error) {
    return true;
  }
}

function renderHealth(health) {
  const entries = Object.entries(health || {});
  healthLine.textContent = entries.length
    ? entries.map(([key, value]) => `${key}: ${value}`).join(" / ")
    : "数据源状态未知";
}

function renderLive() {
  fundCount.textContent = String(state.valuations.length);
  estimatedCount.textContent = String(state.valuations.filter((item) => item.status === "estimated").length);
  updatedAt.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });

  if (!state.valuations.length) {
    fundList.innerHTML = `<div class="empty">暂无自选基金</div>`;
    return;
  }

  fundList.innerHTML = renderFundTable(state.valuations, true);

  document.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      await fetch(`/api/funds/${button.dataset.delete}`, { method: "DELETE" });
      await loadLive();
    });
  });
}

function renderSnapshots() {
  const selected = state.snapshots.find((item) => item.snapshot_key === state.selectedSnapshotKey);
  snapshotCount.textContent = String(state.snapshots.length);
  selectedSnapshot.textContent = state.selectedSnapshotKey || "--";
  snapshotDate.textContent = selected?.snapshot_date || state.snapshotRows[0]?.snapshot_date || "--";
  snapshotFundCount.textContent = String(state.snapshotRows.length);

  if (!state.snapshots.length) {
    snapshotList.innerHTML = "";
    snapshotDetail.innerHTML = `<div class="empty">暂无快照数据</div>`;
    return;
  }

  snapshotList.innerHTML = state.snapshots
    .map((item) => `
      <button class="snapshot-chip ${item.snapshot_key === state.selectedSnapshotKey ? "active" : ""}" data-snapshot="${escapeHtml(item.snapshot_key)}">
        <strong>${escapeHtml(item.snapshot_date || item.snapshot_key)}</strong>
        <span>${item.count} 只基金 · ${escapeHtml(item.snapshot_key)}</span>
      </button>
    `)
    .join("");

  document.querySelectorAll("[data-snapshot]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.selectedSnapshotKey = button.dataset.snapshot;
      await loadSnapshots();
    });
  });

  snapshotDetail.innerHTML = renderFundTable(state.snapshotRows, false);
}

function renderFundTable(items, withDelete) {
  return `
    <div class="fund-card">
      <div class="fund-head">
        <span>基金</span>
        <span>估算净值</span>
        <span>估算涨跌</span>
        <span>覆盖率</span>
        <span>置信度</span>
        <span>来源</span>
        <span></span>
      </div>
      ${items.map((item) => renderFund(item, withDelete)).join("")}
    </div>
  `;
}

function renderFund(item, withDelete) {
  const growthClass = item.estimate_growth_pct > 0 ? "up" : item.estimate_growth_pct < 0 ? "down" : "neutral";
  const sourceClass = item.status === "estimated" ? "" : "warn";
  return `
    <div class="fund-row">
      <div class="fund-title">
        <strong>${escapeHtml(item.name || item.code)}</strong>
        <span>${escapeHtml(item.code)}${item.reason ? ` · ${escapeHtml(item.reason)}` : ""}</span>
      </div>
      <div>
        <span class="cell-label">估算净值</span>
        <div class="value">${formatNumber(item.estimate_nav, 4)}</div>
      </div>
      <div>
        <span class="cell-label">估算涨跌</span>
        <div class="value ${growthClass}">${formatPercent(item.estimate_growth_pct)}</div>
      </div>
      <div>
        <span class="cell-label">覆盖率</span>
        <div class="value">${formatPercent(item.coverage_pct)}</div>
      </div>
      <div>
        <span class="cell-label">置信度</span>
        <div class="value">${formatPercent(item.confidence)}</div>
      </div>
      <div>
        <span class="source-tag ${sourceClass}">${escapeHtml(item.source || item.status)}</span>
      </div>
      ${withDelete ? `<button class="delete-button" data-delete="${escapeHtml(item.code)}">删除</button>` : "<span></span>"}
    </div>
  `;
}

function showError(target, message) {
  target.innerHTML = `<div class="error">${escapeHtml(message)}</div>`;
}

function formatNumber(value, digits) {
  return typeof value === "number" ? value.toFixed(digits) : "--";
}

function formatPercent(value) {
  return typeof value === "number" ? `${value.toFixed(2)}%` : "--";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

loadLive();
startAutoRefresh();
