const state = {
  bootstrap: null,
  matches: [],
  alerts: [],
  subscriptions: [],
  adminState: null,
  selectedFixtureIds: new Set(),
  focusedMatchId: null,
  matchFilter: "all",
  currentTheme: localStorage.getItem("pulseTheme") || "dark",
};

const els = {
  loginView: document.getElementById("loginView"),
  loginMessage: document.getElementById("loginMessage"),
  dashboard: document.getElementById("dashboard"),
  appNameEyebrow: document.getElementById("appNameEyebrow"),
  statusLine: document.getElementById("statusLine"),
  sportSelect: document.getElementById("sportSelect"),
  dayOffsetSelect: document.getElementById("dayOffsetSelect"),
  statSelect: document.getElementById("statSelect"),
  operatorSelect: document.getElementById("operatorSelect"),
  thresholdInput: document.getElementById("thresholdInput"),
  teamSelect: document.getElementById("teamSelect"),
  teamWrap: document.getElementById("teamWrap"),
  operatorWrap: document.getElementById("operatorWrap"),
  thresholdWrap: document.getElementById("thresholdWrap"),
  conditionHint: document.getElementById("conditionHint"),
  matchesMeta: document.getElementById("matchesMeta"),
  matchesList: document.getElementById("matchesList"),
  matchDetail: document.getElementById("matchDetail"),
  selectedCount: document.getElementById("selectedCount"),
  alertsList: document.getElementById("alertsList"),
  subscriptionBadge: document.getElementById("subscriptionBadge"),
  enablePush: document.getElementById("enablePush"),
  testPush: document.getElementById("testPush"),
  refreshAlerts: document.getElementById("refreshAlerts"),
  createCurrentAlert: document.getElementById("createCurrentAlert"),
  createSelectedAlerts: document.getElementById("createSelectedAlerts"),
  createAllVisibleAlerts: document.getElementById("createAllVisibleAlerts"),
  selectAllMatches: document.getElementById("selectAllMatches"),
  clearMatchSelection: document.getElementById("clearMatchSelection"),
  matchFilterSelect: document.getElementById("matchFilterSelect"),
  themeToggle: document.getElementById("themeToggle"),
  logoutButton: document.getElementById("logoutButton"),
  revokeSessionsButton: document.getElementById("revokeSessionsButton"),
  adminPanel: document.getElementById("adminPanel"),
  adminState: document.getElementById("adminState"),
  refreshAdminState: document.getElementById("refreshAdminState"),
};

function setTheme(theme) {
  state.currentTheme = theme;
  document.body.dataset.theme = theme;
  localStorage.setItem("pulseTheme", theme);
  els.themeToggle.textContent = theme === "dark" ? "Светлая тема" : "Тёмная тема";
}

function getStoredToken() {
  const params = new URLSearchParams(window.location.search);
  const queryToken = params.get("token");
  if (queryToken) {
    localStorage.setItem("pulseAccessToken", queryToken);
    window.history.replaceState({}, "", window.location.pathname);
    return queryToken;
  }
  return localStorage.getItem("pulseAccessToken") || "";
}

function currentToken() {
  return localStorage.getItem("pulseAccessToken") || "";
}

async function jsonFetch(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text() || `${response.status}`);
  }
  return await response.json();
}

function updateStatus(text) {
  els.statusLine.textContent = text;
}

function showLogin(message) {
  els.loginView.classList.remove("hidden");
  els.dashboard.classList.add("hidden");
  els.loginMessage.textContent = message;
}

function showDashboard() {
  els.loginView.classList.add("hidden");
  els.dashboard.classList.remove("hidden");
}

function teamLogo(team) {
  const name = team?.name || "";
  const initials = name.split(/\s+/).slice(0, 2).map((x) => x[0] || "").join("").toUpperCase() || "TM";
  if (team?.id) {
    return `<img class="team-logo" src="https://img.sofascore.com/api/v1/team/${team.id}/image" alt="${name}" loading="lazy">`;
  }
  return `<span class="team-logo">${initials}</span>`;
}

function currentSportCatalog() {
  return state.bootstrap.stats_catalog[els.sportSelect.value];
}

function currentStatMeta() {
  return currentSportCatalog()[els.statSelect.value];
}

function syncConditionInputs() {
  const meta = currentStatMeta();
  const booleanMode = meta.mode === "boolean";
  els.operatorWrap.classList.toggle("hidden", booleanMode);
  els.thresholdWrap.classList.toggle("hidden", booleanMode);
  els.teamWrap.classList.toggle("hidden", !meta.supports_team);
  els.conditionHint.textContent = booleanMode
    ? "Для этого условия не нужны оператор и порог."
    : "Выбери условие и значение, при котором придёт уведомление.";
}

function renderSports() {
  els.sportSelect.innerHTML = Object.entries(state.bootstrap.sports)
    .map(([key, value]) => `<option value="${key}">${value.emoji} ${value.label}</option>`)
    .join("");
}

function renderStats() {
  els.statSelect.innerHTML = Object.entries(currentSportCatalog())
    .map(([key, meta]) => `<option value="${key}">${meta.label}</option>`)
    .join("");
  syncConditionInputs();
}

function renderOperators() {
  els.operatorSelect.innerHTML = state.bootstrap.operators
    .map((operator) => `<option value="${operator}">${operator}</option>`)
    .join("");
}

function getFilteredMatches() {
  return state.matches.filter((match) => {
    if (state.matchFilter === "live") return match.status.type === "inprogress";
    if (state.matchFilter === "upcoming") return match.status.type === "notstarted";
    if (state.matchFilter === "hide_finished") return match.status.type !== "finished";
    return true;
  });
}

function renderMatches() {
  const matches = getFilteredMatches();
  els.matchesMeta.textContent = `Показано матчей: ${matches.length} из ${state.matches.length}`;
  if (!matches.length) {
    els.matchesList.innerHTML = `<div class="detail empty">Ничего не найдено под текущий фильтр.</div>`;
    return;
  }

  els.matchesList.innerHTML = matches.map((match) => {
    const selected = state.selectedFixtureIds.has(match.fixture_id);
    const active = state.focusedMatchId === match.fixture_id;
    const kickoff = match.kickoff_at ? new Date(match.kickoff_at * 1000).toLocaleString("ru-RU", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    }) : "Время уточняется";
    const progress = match.status.type === "finished" ? 100 : match.status.type === "inprogress" ? 60 : 20;
    return `
      <article class="match-card ${active ? "active" : ""}">
        <input type="checkbox" data-role="toggle" data-fixture-id="${match.fixture_id}" ${selected ? "checked" : ""}>
        <div>
          <div class="team-line">
            ${teamLogo(match.home_team)}
            <div>
              <h3>${match.match_label}</h3>
              <div class="meta">${match.summary}</div>
            </div>
          </div>
          <div class="pill-row">
            <span class="pill">${kickoff}</span>
            <span class="pill">${match.status.description || match.status.type}</span>
          </div>
          <div class="status-track"><span style="width:${progress}%"></span></div>
        </div>
        <div class="quick">
          <button class="ghost" data-role="focus" data-fixture-id="${match.fixture_id}">Открыть</button>
          <button class="secondary" data-role="single" data-fixture-id="${match.fixture_id}">Поставить</button>
        </div>
      </article>
    `;
  }).join("");
  els.selectedCount.textContent = `${state.selectedFixtureIds.size} выбрано`;
}

function formatAlertText(alert) {
  const meta = (state.bootstrap.stats_catalog[alert.sport] || {})[alert.stat_key];
  const label = meta ? meta.label : alert.stat_key;
  if (meta && meta.mode === "boolean") return label;
  const teamMap = { total: "общий", home: "хозяева", away: "гости" };
  return `${label} · ${teamMap[alert.team] || alert.team} · ${alert.operator} ${alert.threshold}`;
}

function renderAlerts() {
  if (!state.alerts.length) {
    els.alertsList.className = "alerts empty";
    els.alertsList.textContent = "Алертов пока нет.";
    return;
  }
  els.alertsList.className = "alerts";
  els.alertsList.innerHTML = state.alerts.map((alert) => `
    <article class="alert-card">
      <h3>${alert.match_label || `Матч #${alert.fixture_id}`}</h3>
      <div class="meta">${formatAlertText(alert)}</div>
      <div class="pill-row"><span class="pill">${alert.sport === "football" ? "Футбол" : "Баскетбол"}</span></div>
      <div class="inline-actions" style="margin-top:12px;">
        <button class="danger" data-role="delete-alert" data-alert-id="${alert.id}">Удалить</button>
      </div>
    </article>
  `).join("");
}

function renderAdminState() {
  if (!state.bootstrap.is_admin) {
    els.adminPanel.classList.add("hidden");
    return;
  }
  els.adminPanel.classList.remove("hidden");
  const data = state.adminState;
  if (!data) {
    els.adminState.className = "detail empty";
    els.adminState.textContent = "Нет данных.";
    return;
  }
  els.adminState.className = "admin-grid";
  els.adminState.innerHTML = `
    <div class="detail-box"><strong>Админы</strong><div class="meta">${(data.admins || []).join(", ") || "—"}</div></div>
    <div class="detail-box"><strong>Whitelist</strong><div class="meta">${(data.authorized || []).join(", ") || "—"}</div></div>
    <div class="detail-box"><strong>Одобрены</strong><div class="meta">${(data.approved || []).join(", ") || "—"}</div></div>
    <div class="detail-box"><strong>Ожидают подтверждения</strong><div class="meta">${(data.pending || []).map((item) => `${item.user_id} — ${item.name}`).join("<br>") || "—"}</div></div>
  `;
}

async function loadBootstrap() {
  const token = getStoredToken();
  if (!token) {
    showLogin("Открой персональную ссылку, полученную в Telegram после подтверждения доступа.");
    return false;
  }
  try {
    state.bootstrap = await jsonFetch(`/api/bootstrap?token=${encodeURIComponent(token)}`);
    if (state.bootstrap.app_name) {
      document.title = state.bootstrap.app_name;
      els.appNameEyebrow.textContent = state.bootstrap.app_name;
    }
    showDashboard();
    return true;
  } catch {
    localStorage.removeItem("pulseAccessToken");
    showLogin("Ссылка недействительна или истекла. Запроси новую командой /web.");
    return false;
  }
}

async function loadMatches() {
  updateStatus("Загружаю матчи...");
  const data = await jsonFetch(`/api/matches?token=${encodeURIComponent(currentToken())}&sport=${encodeURIComponent(els.sportSelect.value)}&day_offset=${encodeURIComponent(els.dayOffsetSelect.value)}`);
  state.matches = data.events;
  state.selectedFixtureIds.clear();
  state.focusedMatchId = getFilteredMatches()[0]?.fixture_id || data.events[0]?.fixture_id || null;
  renderMatches();
  if (state.focusedMatchId) await loadMatchDetail(state.focusedMatchId);
  else {
    els.matchDetail.className = "detail empty";
    els.matchDetail.textContent = "Выбери матч из списка.";
  }
  updateStatus(`Матчи на ${data.date} загружены`);
}

async function loadMatchDetail(fixtureId) {
  state.focusedMatchId = fixtureId;
  renderMatches();
  const data = await jsonFetch(`/api/match-detail?token=${encodeURIComponent(currentToken())}&sport=${encodeURIComponent(els.sportSelect.value)}&fixture_id=${fixtureId}`);
  const event = data.event;
  if (!event || !event.title) {
    els.matchDetail.className = "detail empty";
    els.matchDetail.textContent = "Детали матча недоступны.";
    return;
  }
  if (els.sportSelect.value === "football") {
    els.matchDetail.className = "detail";
    els.matchDetail.innerHTML = `
      <h3>${event.title}</h3>
      <p class="muted">${event.tournament || ""}</p>
      <div class="pill-row">
        <span class="pill">${event.status.description || event.status.type}</span>
        <span class="pill">${event.score.home ?? "?"}:${event.score.away ?? "?"}</span>
        <span class="pill">${event.score.minute || "без минуты"}</span>
      </div>
      <div class="detail-grid" style="margin-top:14px;">
        <div class="detail-box"><strong>Хозяева</strong><div class="meta">${(event.stats.home || []).map((row) => `${row.label}: ${row.value ?? "-"}`).join("<br>") || "Статистика появится по ходу матча."}</div></div>
        <div class="detail-box"><strong>Гости</strong><div class="meta">${(event.stats.away || []).map((row) => `${row.label}: ${row.value ?? "-"}`).join("<br>") || "Статистика появится по ходу матча."}</div></div>
      </div>
    `;
  } else {
    els.matchDetail.className = "detail";
    els.matchDetail.innerHTML = `
      <h3>${event.title}</h3>
      <p class="muted">${event.tournament || ""}</p>
      <div class="pill-row">
        <span class="pill">${event.status.description || event.status.type}</span>
        <span class="pill">${event.score.home ?? "?"}:${event.score.away ?? "?"}</span>
        <span class="pill">${event.score.status_text || ""}</span>
      </div>
      <div class="detail-box" style="margin-top:14px;">
        <strong>По четвертям</strong>
        <div class="meta" style="margin-top:8px;">${(event.score.quarters || []).map((value, idx) => `Q${idx + 1}: ${value ?? "-"}`).join(" · ")}</div>
      </div>
    `;
  }
}

async function loadAlerts() {
  const data = await jsonFetch(`/api/alerts?token=${encodeURIComponent(currentToken())}`);
  state.alerts = data.alerts;
  state.subscriptions = data.subscriptions;
  els.subscriptionBadge.textContent = data.subscriptions.length ? "push on" : "push off";
  renderAlerts();
}

async function loadAdminState() {
  if (!state.bootstrap.is_admin) return renderAdminState();
  state.adminState = await jsonFetch(`/api/admin/access-state?token=${encodeURIComponent(currentToken())}`);
  renderAdminState();
}

function buildConditionPayload() {
  const meta = currentStatMeta();
  return {
    sport: els.sportSelect.value,
    stat_key: els.statSelect.value,
    operator: meta.mode === "compare" ? els.operatorSelect.value : "==",
    threshold: meta.mode === "compare" ? Number(els.thresholdInput.value) : 1,
    team: meta.supports_team ? els.teamSelect.value : "total",
  };
}

function visibleSelectedMatches() {
  return getFilteredMatches().filter((match) => state.selectedFixtureIds.has(match.fixture_id));
}

async function createSingleAlert(fixtureId) {
  const match = state.matches.find((item) => item.fixture_id === fixtureId);
  if (!match) return;
  await jsonFetch("/api/alerts", {
    method: "POST",
    body: JSON.stringify({ token: currentToken(), ...buildConditionPayload(), fixture_id: match.fixture_id, match_label: match.match_label, kickoff_at: match.kickoff_at }),
  });
  updateStatus("Алерт создан");
  await loadAlerts();
}

async function createBulkAlerts(matches) {
  if (!matches.length) return updateStatus("Сначала выбери матчи");
  const result = await jsonFetch("/api/alerts/bulk", {
    method: "POST",
    body: JSON.stringify({ token: currentToken(), ...buildConditionPayload(), matches }),
  });
  updateStatus(`Создано алертов: ${result.count}`);
  await loadAlerts();
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

async function subscribePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return updateStatus("Браузер не поддерживает push");
  if (!state.bootstrap.vapid_public_key) return updateStatus("Push не настроен");
  const registration = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(state.bootstrap.vapid_public_key),
    });
  }
  await jsonFetch("/api/subscribe", {
    method: "POST",
    body: JSON.stringify({ token: currentToken(), subscription: subscription.toJSON() }),
  });
  updateStatus("Push включён");
  await loadAlerts();
}

async function sendTestPush() {
  const result = await jsonFetch("/api/push/test", {
    method: "POST",
    body: JSON.stringify({ token: currentToken() }),
  });
  updateStatus(result.sent ? "Тестовое уведомление отправлено" : "Нет активной push-подписки");
}

async function logout() {
  await jsonFetch("/api/auth/logout", { method: "POST", body: JSON.stringify({ token: currentToken() }) });
  localStorage.removeItem("pulseAccessToken");
  location.reload();
}

async function revokeSessions() {
  await jsonFetch("/api/auth/revoke-sessions", { method: "POST", body: JSON.stringify({ token: currentToken() }) });
  localStorage.removeItem("pulseAccessToken");
  showLogin("Все web-сессии сброшены. Запроси новую ссылку командой /web.");
}

async function onMatchesClick(event) {
  const toggle = event.target.closest("[data-role='toggle']");
  if (toggle) {
    const fixtureId = Number(toggle.dataset.fixtureId);
    if (toggle.checked) state.selectedFixtureIds.add(fixtureId);
    else state.selectedFixtureIds.delete(fixtureId);
    els.selectedCount.textContent = `${state.selectedFixtureIds.size} выбрано`;
    return;
  }
  const focus = event.target.closest("[data-role='focus']");
  if (focus) return loadMatchDetail(Number(focus.dataset.fixtureId));
  const single = event.target.closest("[data-role='single']");
  if (single) return createSingleAlert(Number(single.dataset.fixtureId));
}

async function onAlertsClick(event) {
  const button = event.target.closest("[data-role='delete-alert']");
  if (!button) return;
  await jsonFetch("/api/alerts", {
    method: "DELETE",
    body: JSON.stringify({ token: currentToken(), alert_id: Number(button.dataset.alertId) }),
  });
  updateStatus("Алерт удалён");
  await loadAlerts();
}

async function bootstrap() {
  setTheme(state.currentTheme);
  const ok = await loadBootstrap();
  if (!ok) return;
  renderSports();
  renderOperators();
  renderStats();
  await loadMatches();
  await loadAlerts();
  await loadAdminState();
  updateStatus("Готово");
}

els.sportSelect.addEventListener("change", async () => { renderStats(); await loadMatches(); });
els.dayOffsetSelect.addEventListener("change", loadMatches);
els.matchFilterSelect.addEventListener("change", async (event) => {
  state.matchFilter = event.target.value;
  renderMatches();
  const first = getFilteredMatches()[0];
  if (first) await loadMatchDetail(first.fixture_id);
});
els.statSelect.addEventListener("change", syncConditionInputs);
els.matchesList.addEventListener("click", onMatchesClick);
els.alertsList.addEventListener("click", onAlertsClick);
els.enablePush.addEventListener("click", subscribePush);
els.testPush.addEventListener("click", sendTestPush);
els.refreshAlerts.addEventListener("click", loadAlerts);
els.createCurrentAlert.addEventListener("click", async () => {
  if (!state.focusedMatchId) return updateStatus("Сначала выбери матч");
  await createSingleAlert(state.focusedMatchId);
});
els.createSelectedAlerts.addEventListener("click", async () => createBulkAlerts(visibleSelectedMatches()));
els.createAllVisibleAlerts.addEventListener("click", async () => createBulkAlerts(getFilteredMatches()));
els.selectAllMatches.addEventListener("click", () => {
  state.selectedFixtureIds = new Set(getFilteredMatches().map((match) => match.fixture_id));
  renderMatches();
});
els.clearMatchSelection.addEventListener("click", () => {
  state.selectedFixtureIds.clear();
  renderMatches();
});
els.themeToggle.addEventListener("click", () => setTheme(state.currentTheme === "dark" ? "light" : "dark"));
els.logoutButton.addEventListener("click", logout);
els.revokeSessionsButton.addEventListener("click", revokeSessions);
els.refreshAdminState.addEventListener("click", loadAdminState);

bootstrap().catch((error) => {
  console.error(error);
  showLogin(`Ошибка входа: ${error.message}`);
});
