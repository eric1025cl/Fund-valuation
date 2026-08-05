const AUTO_REFRESH_INTERVAL_MS = 10 * 60 * 1000;
const AUTO_REFRESH_END_MINUTE = 15 * 60 + 5;

const state = {
  view: "live",
  funds: null,
  valuations: [],
  snapshots: [],
  snapshotRows: [],
  reconciliations: [],
  selectedSnapshotKey: null,
  lastValuationUpdatedAt: null,
  liveRefreshRetryId: null,
  tradingStatus: null,
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
const reconcileButton = document.querySelector("#reconcileButton");
const fundCount = document.querySelector("#fundCount");
const estimatedCount = document.querySelector("#estimatedCount");
const updatedAt = document.querySelector("#updatedAt");
const tradeDate = document.querySelector("#tradeDate");
const snapshotCount = document.querySelector("#snapshotCount");
const selectedSnapshot = document.querySelector("#selectedSnapshot");
const snapshotDate = document.querySelector("#snapshotDate");
const snapshotFundCount = document.querySelector("#snapshotFundCount");
const snapshotList = document.querySelector("#snapshotList");
const snapshotDetail = document.querySelector("#snapshotDetail");
const reconciliationStatus = document.querySelector("#reconciliationStatus");
const reconciliationList = document.querySelector("#reconciliationList");

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
  await loadFundsOnly();
});

refreshButton.addEventListener("click", async () => {
  if (state.view === "snapshots") {
    await loadSnapshots();
  } else {
    await loadLive({ forceRefresh: true });
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

reconcileButton.addEventListener("click", async () => {
  reconcileButton.disabled = true;
  reconciliationStatus.textContent = "正在校准对账";
  try {
    const response = await fetch("/api/reconciliations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const result = await response.json();
    reconciliationStatus.textContent = `本次检查 ${result.checked || 0} 条，新增对账 ${result.reconciled || 0} 条，跳过 ${result.skipped || 0} 条`;
    if (state.view === "snapshots") {
      await loadSnapshots();
    } else {
      state.view = "snapshots";
      document.querySelector('[data-view="snapshots"]').click();
    }
  } catch (error) {
    reconciliationStatus.textContent = "对账失败";
  } finally {
    reconcileButton.disabled = false;
  }
});

async function loadLive(options = {}) {
  const forceRefresh = Boolean(options.forceRefresh);
  refreshButton.disabled = true;
  try {
    const valuationUrl = forceRefresh ? "/api/valuations?refresh=1" : "/api/valuations";
    const [healthResponse, tradingStatusResponse, fundsResponse, valuationResponse] = await Promise.all([
      fetch("/api/health"),
      fetch("/api/trading-status"),
      fetch("/api/funds"),
      fetch(valuationUrl),
    ]);
    const health = await healthResponse.json();
    state.tradingStatus = await tradingStatusResponse.json();
    state.funds = await fundsResponse.json();
    state.valuations = await valuationResponse.json();
    state.lastValuationUpdatedAt = new Date();
    renderHealth(health);
    renderLive();
    schedulePendingLiveRetry();
  } catch (error) {
    showError(fundList, "无法连接本地服务");
  } finally {
    refreshButton.disabled = false;
  }
}

function schedulePendingLiveRetry() {
  const hasFunds = Array.isArray(state.funds) && state.funds.length > 0;
  const hasValuations = Array.isArray(state.valuations) && state.valuations.length > 0;
  if (!hasFunds || hasValuations || state.view !== "live" || document.hidden) {
    clearPendingLiveRetry();
    return;
  }
  if (state.liveRefreshRetryId !== null) {
    return;
  }
  state.liveRefreshRetryId = window.setTimeout(() => {
    state.liveRefreshRetryId = null;
    if (state.view === "live" && !document.hidden) {
      loadLive();
    }
  }, 3000);
}

function clearPendingLiveRetry() {
  if (state.liveRefreshRetryId !== null) {
    window.clearTimeout(state.liveRefreshRetryId);
    state.liveRefreshRetryId = null;
  }
}

async function loadFundsOnly() {
  try {
    const response = await fetch("/api/funds");
    if (!response.ok) {
      throw new Error("failed to load funds");
    }
    state.funds = await response.json();
    renderLive();
  } catch (error) {
    showError(fundList, "无法读取自选基金");
  }
}

async function loadInitialLive() {
  await loadFundsOnly();
  loadLive();
}

async function loadSnapshots() {
  const response = await fetch("/api/snapshots");
  state.snapshots = await response.json();
  const selectedExists = state.snapshots.some((item) => item.snapshot_key === state.selectedSnapshotKey);
  if (!selectedExists) {
    state.selectedSnapshotKey = state.snapshots[0]?.snapshot_key || null;
  }
  if (state.selectedSnapshotKey) {
    const detailResponse = await fetch(`/api/snapshots/${encodeURIComponent(state.selectedSnapshotKey)}`);
    state.snapshotRows = await detailResponse.json();
  } else {
    state.snapshotRows = [];
  }
  await loadReconciliations();
  renderSnapshots();
}

async function loadReconciliations() {
  try {
    const response = await fetch("/api/reconciliations");
    if (!response.ok) {
      throw new Error("failed to load reconciliations");
    }
    state.reconciliations = await response.json();
  } catch (error) {
    state.reconciliations = [];
  }
}

function isTradingRefreshWindow(date) {
  const day = date.getDay();
  const minutes = date.getHours() * 60 + date.getMinutes();
  return day >= 1 && day <= 5 && minutes >= 9 * 60 && minutes <= AUTO_REFRESH_END_MINUTE;
}

function startAutoRefresh() {
  window.setInterval(async () => {
    if (state.view !== "live" || document.hidden || !(await shouldAutoRefreshNow(new Date()))) {
      return;
    }
    loadLive({ forceRefresh: true });
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
  const rows = liveRows();
  fundCount.textContent = String(rows.length);
  estimatedCount.textContent = String(rows.filter((item) => item.status === "estimated").length);
  updatedAt.textContent = state.lastValuationUpdatedAt
    ? state.lastValuationUpdatedAt.toLocaleTimeString("zh-CN", { hour12: false })
    : "--";
  tradeDate.textContent = liveTradeDate(rows);

  if (!rows.length) {
    fundList.innerHTML = `<div class="empty">暂无自选基金</div>`;
    return;
  }

  fundList.innerHTML = renderFundTable(rows, true);

  document.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      await fetch(`/api/funds/${button.dataset.delete}`, { method: "DELETE" });
      await loadFundsOnly();
    });
  });
}

function liveRows() {
  const funds = Array.isArray(state.funds)
    ? state.funds
    : state.valuations.map((item) => ({ code: item.code, name: item.name, alias: item.alias }));
  const valuationByCode = new Map(state.valuations.map((item) => [item.code, item]));
  return funds.map((fund) => {
    const valuation = valuationByCode.get(fund.code);
    if (valuation) {
      return {
        ...valuation,
        alias: fund.alias ?? valuation.alias,
        name: fund.name || valuation.name,
      };
    }
    return {
      code: fund.code,
      alias: fund.alias,
      name: fund.name,
      status: "pending",
      source: "待刷新",
      estimate_nav: null,
      actual_nav: null,
      actual_nav_date: null,
      estimate_growth_pct: null,
      coverage_pct: null,
      confidence: null,
      reason: "等待自动刷新估值",
      latest_nav: null,
      latest_nav_date: null,
      trade_date: state.tradingStatus?.trade_date || null,
      target_trade_date: state.tradingStatus?.trade_date || null,
      context_trade_date: state.tradingStatus?.trade_date || null,
      market_phase: state.tradingStatus?.market_phase || null,
      is_final: state.tradingStatus?.is_final ?? null,
      contributions: [],
    };
  });
}

function liveTradeDate(rows) {
  const dates = rows.map((item) => item.trade_date).filter(Boolean);
  if (dates.length) {
    return [...new Set(dates)].join("/");
  }
  return state.tradingStatus?.trade_date || "--";
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
    renderReconciliations();
    return;
  }

  snapshotList.innerHTML = state.snapshots
    .map((item) => `
      <div class="snapshot-chip-wrap">
        <button class="snapshot-chip ${item.snapshot_key === state.selectedSnapshotKey ? "active" : ""}" data-snapshot="${escapeHtml(item.snapshot_key)}">
          <strong>${escapeHtml(item.snapshot_date || item.snapshot_key)}</strong>
          <span>${item.count} 只基金 · ${escapeHtml(item.snapshot_key)}</span>
        </button>
        <button class="snapshot-delete-button" data-delete-snapshot="${escapeHtml(item.snapshot_key)}" type="button" title="删除快照" aria-label="删除快照">&times;</button>
      </div>
    `)
    .join("");

  document.querySelectorAll("[data-snapshot]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.selectedSnapshotKey = button.dataset.snapshot;
      await loadSnapshots();
    });
  });
  document.querySelectorAll("[data-delete-snapshot]").forEach((button) => {
    button.addEventListener("click", async () => {
      await deleteSnapshot(button.dataset.deleteSnapshot);
    });
  });

  snapshotDetail.innerHTML = renderFundTable(state.snapshotRows, false, true);
  renderReconciliations();
}

async function deleteSnapshot(snapshotKey) {
  if (!snapshotKey) {
    return;
  }
  if (!window.confirm(`确认物理删除快照 ${snapshotKey}？`)) {
    return;
  }
  const response = await fetch(`/api/snapshots/${encodeURIComponent(snapshotKey)}`, { method: "DELETE" });
  if (!response.ok && response.status !== 404) {
    showError(snapshotDetail, "删除快照失败");
    return;
  }
  if (state.selectedSnapshotKey === snapshotKey) {
    state.selectedSnapshotKey = null;
  }
  await loadSnapshots();
}

function renderReconciliations() {
  const rows = state.reconciliations || [];
  reconciliationStatus.textContent = rows.length ? `最近 ${rows.length} 条校准样本` : "暂无校准对账记录";
  if (!rows.length) {
    reconciliationList.innerHTML = `<div class="empty">暂无校准对账记录</div>`;
    return;
  }
  reconciliationList.innerHTML = `
    <div class="reconciliation-card">
      <div class="reconciliation-head">
        <span>基金</span>
        <span>快照日期</span>
        <span>估算净值</span>
        <span>实际净值</span>
        <span>涨跌误差</span>
        <span>净值误差</span>
        <span>对账时间</span>
      </div>
      ${rows.map(renderReconciliation).join("")}
    </div>
  `;
}

function renderReconciliation(item) {
  const name = item.name || item.code;
  const meta = [item.code, item.source].filter(Boolean).join(" · ");
  return `
    <div class="reconciliation-row">
      <div class="fund-title">
        <strong>${escapeHtml(name)}</strong>
        <span>${escapeHtml(meta)}</span>
      </div>
      <div>
        <span class="cell-label">快照日期</span>
        <div class="value">${escapeHtml(item.snapshot_date || "--")}</div>
        <span class="cell-note">${escapeHtml(item.snapshot_key || "")}</span>
      </div>
      <div>
        <span class="cell-label">估算净值</span>
        <div class="value">${formatNumber(item.estimate_nav, 4)}</div>
        <span class="cell-note">${formatPercent(item.estimate_growth_pct)}</span>
      </div>
      <div>
        <span class="cell-label">实际净值</span>
        <div class="value">${formatNumber(item.actual_nav, 4)}</div>
        <span class="cell-note">${escapeHtml(item.actual_nav_date || "")}</span>
      </div>
      <div>
        <span class="cell-label">涨跌误差</span>
        <div class="value ${percentClass(item.growth_error_pct)}">${formatPercent(item.growth_error_pct)}</div>
        <span class="cell-note">绝对 ${formatPercent(item.abs_growth_error_pct)}</span>
      </div>
      <div>
        <span class="cell-label">净值误差</span>
        <div class="value ${percentClass(item.nav_error_pct)}">${formatPercent(item.nav_error_pct)}</div>
        <span class="cell-note">绝对 ${formatPercent(item.abs_nav_error_pct)}</span>
      </div>
      <div>
        <span class="cell-label">对账时间</span>
        <div class="value small-value">${escapeHtml(item.reconciled_at || "--")}</div>
      </div>
    </div>
  `;
}

function renderFundTable(items, withDelete, showActualGrowth = false) {
  return `
    <div class="fund-card ${showActualGrowth ? "snapshot-fund-card" : ""}">
      <div class="fund-head">
        <span>基金</span>
        <span>估算净值</span>
        <span>基准/实际净值</span>
        <span>估算涨跌</span>
        ${showActualGrowth ? "<span>净值涨跌幅</span>" : ""}
        <span>覆盖率</span>
        <span>置信度</span>
        <span>说明</span>
        <span>来源</span>
        <span></span>
      </div>
      ${items.map((item) => renderFund(item, withDelete, showActualGrowth)).join("")}
    </div>
  `;
}

function renderFund(item, withDelete, showActualGrowth = false) {
  const growthClass = percentClass(item.estimate_growth_pct);
  const actualGrowthClass = percentClass(item.actual_growth_pct);
  const sourceClass = item.status === "estimated" ? "" : "warn";
  const sourceText = sourceLabel(item);
  const displayName = item.alias || item.name || item.code;
  const actualNav = actualNavValue(item);
  const actualNavDate = navDateLabel(item);
  const explanationItems = estimateExplanationItems(item);
  const meta = [
    item.code,
    item.alias && item.name ? item.name : null,
    formatTradeContext(item),
    item.reason,
  ].filter(Boolean).join(" · ");
  return `
    <div class="fund-row">
      <div class="fund-title">
        <strong>${escapeHtml(displayName)}</strong>
        <span>${escapeHtml(meta)}</span>
      </div>
      <div>
        <span class="cell-label">估算净值</span>
        <div class="value">${formatNumber(item.estimate_nav, 4)}</div>
      </div>
      <div>
        <span class="cell-label">基准/实际净值</span>
        <div class="value">${formatNumber(actualNav, 4)}</div>
        ${actualNavDate ? `<span class="cell-note">${escapeHtml(actualNavDate)}</span>` : ""}
      </div>
      <div>
        <span class="cell-label">估算涨跌</span>
        <div class="value ${growthClass}">${formatPercent(item.estimate_growth_pct)}</div>
      </div>
      ${showActualGrowth ? `
      <div>
        <span class="cell-label">净值涨跌幅</span>
        <div class="value ${actualGrowthClass}">${formatPercent(item.actual_growth_pct)}</div>
      </div>` : ""}
      <div>
        <span class="cell-label">覆盖率</span>
        <div class="value">${formatPercent(item.coverage_pct)}</div>
      </div>
      <div>
        <span class="cell-label">置信度</span>
        <div class="value">${formatPercent(item.confidence)}</div>
      </div>
      <div class="fund-explanation">
        <span class="cell-label">说明</span>
        ${explanationItems.length ? explanationItems.map((note) => `
          <span class="cell-note ${note.className}">${escapeHtml(note.text)}</span>
        `).join("") : `<span class="cell-note">--</span>`}
      </div>
      <div>
        <span class="source-tag ${sourceClass}">${escapeHtml(sourceText)}</span>
      </div>
      <div class="fund-action">
        ${withDelete ? `<button class="delete-button" data-delete="${escapeHtml(item.code)}">删除</button>` : ""}
      </div>
    </div>
  `;
}

function actualNavValue(item) {
  return typeof item.actual_nav === "number" ? item.actual_nav : item.latest_nav;
}

function navDateLabel(item) {
  if (item.actual_nav_date) {
    return item.actual_nav_date;
  }
  if (item.latest_nav_date) {
    return item.latest_nav_date;
  }
  return null;
}

function sourceLabel(item) {
  const labels = {
    nav: "正式净值",
    official: "官方估算",
    holding: "持仓估算",
    factor_fit: "指数拟合",
    qdii_benchmark: "海外基准",
    snapshot: "快照",
  };
  return labels[item.source] || item.source || item.status || "";
}

function estimateExplanationItems(item) {
  return [
    { text: estimateRiskLabel(item), className: "risk-note" },
    { text: qdiiBenchmarkLabel(item), className: "" },
    { text: factorFitLabel(item), className: "" },
    { text: styleDriftLabel(item), className: "" },
  ].filter((note) => note.text);
}

function qdiiBenchmarkLabel(item) {
  if (item.source !== "qdii_benchmark") {
    return null;
  }
  const name = item.benchmark_name || item.benchmark_symbol || "海外基准";
  const parts = [];
  if (typeof item.benchmark_growth_pct === "number") {
    parts.push(`基准 ${formatPercent(item.benchmark_growth_pct)}`);
  }
  if (typeof item.fx_growth_pct === "number") {
    parts.push(`汇率 ${formatPercent(item.fx_growth_pct)}`);
  }
  return parts.length ? `${name} / ${parts.join(" / ")}` : name;
}

function factorFitLabel(item) {
  const growth = typeof item.fit_growth_pct === "number" ? item.fit_growth_pct : (
    item.source === "factor_fit" ? item.estimate_growth_pct : null
  );
  if (typeof growth !== "number") {
    return null;
  }
  const r2 = typeof item.fit_r2 === "number" ? ` / R² ${item.fit_r2.toFixed(2)}` : "";
  return `指数拟合 ${formatPercent(growth)}${r2}`;
}

function styleDriftLabel(item) {
  if (typeof item.style_drift_score !== "number") {
    return null;
  }
  const level = {
    low: "低",
    medium: "中",
    high: "高",
  }[item.style_drift_level] || item.style_drift_level || "低";
  return `风格漂移 ${level} ${item.style_drift_score.toFixed(1)}`;
}

function estimateRiskLabel(item) {
  if (item.source !== "holding" || !item.estimate_risk_level) {
    return null;
  }
  const level = {
    low: "低",
    medium: "中",
    high: "高",
  }[item.estimate_risk_level] || item.estimate_risk_level;
  const details = [];
  if (Array.isArray(item.estimate_risk_reasons)) {
    item.estimate_risk_reasons.map(estimateRiskReasonLabel).filter(Boolean).forEach((label) => details.push(label));
  }
  const proxyLabel = uncoveredProxyLabel(item);
  if (typeof item.uncovered_weight_pct === "number" && proxyLabel) {
    details.push(`未覆盖仓位 ${formatPercent(item.uncovered_weight_pct)} ${proxyLabel}`);
  }
  return `估值风险 ${level}${details.length ? ` · ${details.join(" · ")}` : ""}`;
}

function uncoveredProxyLabel(item) {
  if (item.uncovered_proxy_source === "tracking_index") {
    return item.uncovered_proxy_name ? `用跟踪指数 ${item.uncovered_proxy_name}` : "用跟踪指数";
  }
  if (item.uncovered_proxy_source === "factor_fit") {
    return "用指数拟合";
  }
  if (item.uncovered_proxy_source === "holding_momentum_blend") {
    return "用持仓动量混合";
  }
  return null;
}

function estimateRiskReasonLabel(reason) {
  return {
    low_coverage: "覆盖率低",
    medium_coverage: "覆盖率偏低",
    volatile_holdings: "持仓波动大",
  }[reason] || "";
}

function percentClass(value) {
  if (typeof value !== "number") {
    return "neutral";
  }
  return value > 0 ? "up" : value < 0 ? "down" : "neutral";
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

function formatTradeContext(item) {
  if (!item.trade_date) {
    return null;
  }
  const targetDate = item.target_trade_date || item.trade_date;
  const phase = item.is_final ? "收盘后" : "盘中";
  const labels = [`估值日 ${targetDate} ${phase}`];
  if (item.context_trade_date && item.context_trade_date !== targetDate) {
    labels.push(`本地交易日 ${item.context_trade_date}`);
  }
  return labels.join(" / ");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

loadInitialLive();
startAutoRefresh();
