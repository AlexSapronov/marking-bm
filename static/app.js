"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)],
  e = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const states = {
  attention: "Требует проверки",
  cancelled: "Остановлена",
  checking: "Проверяется в ЧЗ",
  stopped: "Обработка остановлена",
  transforming: "Трансформация",
  draft: "Черновик",
  label_error: "Ошибка формирования PDF",
  report_error: "Ошибка",
  preflight_blocked: "Проверка перед отправкой не пройдена",
  repack: "Подготовка новой разбивки",
  receiving: "Получение КМ",
  ordering: "Заказ КМ",
  ready: "Этикетки готовы",
  transferred: "Ожидает нанесения",
  reporting: "Отчёт о нанесении",
  reported: "Отчёт принят",
  aggregating: "Формирование АТК",
  done: "Завершён",
  archive: "Архив",
  signing: "Ожидает подписи",
  leased: "Подписывается",
  queued: "Ожидает отправки",
  dispatching: "Отправляется",
  submitted: "Обрабатывается в ЧЗ",
  unknown: "Неизвестный результат",
  blocked: "Требует проверки",
  approval: "На согласовании",
  approved: "Согласована",
  rejected: "Отклонена",
  partial: "Обработан частично",
  processing: "Обрабатывается",
  free: "Свободен",
  reserved: "Зарезервирован",
  applied: "Нанесён",
  aggregated: "В АТК",
  removed_review: "Изъят из АТК. Требует проверки",
};
const kinds = {
  order: "Заказ КМ",
  fetch: "Получение КМ",
  report: "Отчёт о нанесении",
  atk: "Создание АТК",
  transform: "Трансформация АТК",
  auth_true: "Авторизация ГИС МТ",
  auth_oms: "Авторизация СУЗ",
};
const badge = (s) => `<span class="badge ${e(s)}">${e(states[s] || s)}</span>`,
  fmt = (n) => Number(n || 0).toLocaleString("ru-RU"),
  date = (s) => (s ? new Date(s).toLocaleString("ru-RU") : "—");
let user,
  docs = [],
  doc,
  archive = false,
  documentListArchive = false,
  adminMode = location.pathname === "/admin",
  overview,
  refreshTimer,
  adminSection = "overview",
  documentTab = "content",
  documentDirty = false,
  documentFilters = {search: "", status: "", supplier: "", from: "", to: "", stage: "all"};
let statusLastChecked = null;
let statusPollBusy = false;
const STATUS_POLL_MS = 30000;
function documentStatusSignature(d) {
  return JSON.stringify([
    d.status, d.display_status, d.revision, d.current_version,
    d.transferred_version, d.last_error, d.report_issue,
    d.owner, d.supplier, d.history?.length,
    d.codes?.map(c => [c.id, c.external_status, c.local_state]),
    d.atks?.map(a => [a.id, a.state, a.code]),
    d.operations?.map(o => [o.id, o.state, o.updated, o.error]),
    d.exports?.map(a => a.id), d.artifacts?.map(a => a.id),
    d.stopped, d.group_stop,
  ]);
}
function documentListSignature(items) {
  return JSON.stringify(items.map(d =>
    [d.id, d.status, d.display_status, d.updated, d.units, d.stopped]));
}
function logisticsStatusSignature(items) {
  return JSON.stringify(items.map(a => [
    a.id, a.status, a.error, a.download_ready, a.archived,
    a.files?.map(f => [f.id, f.version]),
    a.documents?.map(d => [d.id, d.status, d.can_complete]),
  ]));
}
function adminStatusSignature(items) {
  return JSON.stringify([
    items.operations.map(o => [o.id, o.state, o.updated, o.attention_resolved_at]),
    items.transformations.map(t => [t.id, t.state]),
  ]);
}
async function pollStatuses() {
  if (statusPollBusy || !user || document.hidden || documentDirty ||
      $("#modal")?.open || $("#app").querySelector("form:focus-within") ||
      $$(".doc-select:checked, .atk-select:checked, .move-line:checked").length) return;
  statusPollBusy = true;
  const activeDocId = doc?.id;
  const wasArchive = archive;
  const wasAdmin = adminMode;
  try {
    if (activeDocId && !wasAdmin && !registryMode) {
      const fresh = await api("/documents/" + encodeURIComponent(activeDocId));
      if (doc?.id !== activeDocId || documentDirty || $("#modal").open) return;
      statusLastChecked = new Date();
      if (documentStatusSignature(fresh) !== documentStatusSignature(doc)) {
        const scroll = window.scrollY;
        doc = fresh;
        archive = doc.status === "archive";
        renderDoc();
        window.scrollTo(0, scroll);
      }
      const mark = $("#status-checked");
      if (mark) {
        mark.textContent = "Проверено: " + statusLastChecked.toLocaleTimeString("ru-RU");
        mark.title = "Статус обновляется автоматически каждые 30 секунд";
      }
    } else if (!wasAdmin && !registryMode && $(".document-list")) {
      const incoming = await api("/documents?archive=" + wasArchive);
      if (doc || archive !== wasArchive || documentDirty || $("#modal").open ||
          $("#app").querySelector("form:focus-within")) return;
      if (documentListSignature(incoming) !== documentListSignature(docs)) {
        const scroll = window.scrollY;
        docs = incoming;
        renderList();
        window.scrollTo(0, scroll);
      }
    } else if (!wasAdmin && registryMode && $("#atk-table")) {
      const incoming = await api("/logistics/atks?" + new URLSearchParams(registryFilters));
      if (!registryMode || $("#modal").open ||
          $("#app").querySelector("form:focus-within") ||
          $$(".atk-select:checked").length) return;
      if (logisticsStatusSignature(incoming) !== logisticsStatusSignature(registryRows)) {
        const scroll = window.scrollY;
        await logisticsPage();
        window.scrollTo(0, scroll);
      }
    } else if (wasAdmin && adminSection === "overview") {
      const incoming = await api("/admin/overview");
      if (!adminMode || adminSection !== "overview" || $("#modal").open ||
          $("#app").querySelector("form:focus-within")) return;
      if (adminStatusSignature(incoming) !== adminStatusSignature(overview))
        await adminPage(incoming);
    }
  } catch (error) {
    const mark = $("#status-checked");
    if (mark) {
      mark.textContent = "Нет связи для проверки";
      mark.title = String(error.message || error);
    }
  } finally {
    statusPollBusy = false;
  }
}
async function api(url, body, method) {
  const headers = { "X-Requested-With": "marking-bm" };
  if (user?.csrf) headers["X-CSRF-Token"] = user.csrf;
  if (body && !(body instanceof FormData))
    headers["Content-Type"] = "application/json";
  const r = await fetch("/api" + url, {
    method: method || (body ? "POST" : "GET"),
    headers,
    body: body
      ? body instanceof FormData
        ? body
        : JSON.stringify(body)
      : undefined,
  });
  let data = r.status === 204 ? {} : await r.json();
  if (!r.ok) {
    let ex = new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
    ex.data = data;
    throw ex;
  }
  return data;
}
function toast(message, kind = "error") {
  window.BmUX.notify(message, kind);
}
function bind() {
  document.onclick = async (ev) => {
    const b = ev.target.closest("[data-action]");
    if (!b) return;
    try {
      if (documentDirty && ["list", "archive", "logistics", "admin", "refresh-page", "refresh-doc", "logout"].includes(b.dataset.action) && !confirm("В составе есть несохранённые изменения. Покинуть документ?")) return;
      b.disabled = true;
      await action(b.dataset.action, b.dataset.id, b);
    } catch (err) {
      toast(err.message);
    } finally {
      b.disabled = false;
    }
  };
}
const roleNames = {
  manager: "Бренд-менеджер",
  logistics: "Логист",
  admin: "Администратор",
};
let registryRequest = 0;
let registryMode = false,
  registryRows = [],
  registryFilters = {
    q: "",
    supplier: "",
    status: "",
    tnved: "",
    date_from: "",
    date_to: "",
    archive: false,
  };
function shell(content) {
  const canManage = user.admin || user.role === "manager";
  const canReadRegistry = user.admin || user.role === "logistics";
  $("#app").innerHTML =
    `<div class="layout"><header class="app-header"><div class="header-inner"><div><div class="eyebrow">Честный ЗНАК · Радиоэлектроника</div><div class="app-title">Маркировка импортных товаров</div><div class="header-meta">${e(roleNames[user.role] || roleNames.manager)} · ${e(user.username)}</div></div><div class="header-actions"><span class="muted user-label">${e(user.username)}</span>${!user.admin ? '<button data-action="change-password">Сменить пароль</button>' : ""}<button class="icon-btn" data-action="refresh-page" aria-label="Обновить страницу" title="Обновить">↻</button><button class="icon-btn" data-theme-toggle aria-label="Тема">☾</button><button data-action="logout">Выйти</button></div></div></header><nav class="app-nav" aria-label="Разделы приложения"><div class="nav-inner">${canManage ? `<button data-action="list" class="${!adminMode && !archive && !registryMode ? "active" : ""}">Накладные</button><button data-action="archive" class="${archive && !registryMode && !adminMode ? "active" : ""}">Архив</button>` : ""}${canReadRegistry ? `<button data-action="logistics" class="${registryMode ? "active" : ""}">Реестр АТК</button>` : ""}${user.admin ? `<button data-action="admin" class="${adminMode ? "active" : ""}">Администрирование</button>` : ""}</div></nav><main>${content}</main><footer class="app-footer">Честный ЗНАК · ${user.role === "logistics" ? "АТК, итоговые файлы и завершение накладных" : "PDF 58 × 40 мм · печатайте в масштабе 100%"}</footer></div>`;
  window.GisTheme.refresh();
}
function login(admin = false, setup = false) {
  documentDirty = false;
  registryMode = false;
  const userFields = setup
    ? `<label>Username<input id="login-value" autocomplete="username" required autofocus></label>
       <label>Одноразовый код от администратора<input id="setup-code" autocomplete="off" required></label>
       <label>Новый пароль, минимум 8 символов<input id="password-new" type="password" autocomplete="new-password" minlength="8" required></label>
       <label>Повторите пароль<input id="password-repeat" type="password" autocomplete="new-password" minlength="8" required></label>`
    : `<label>Username<input id="login-value" autocomplete="username" required autofocus></label>
       <label>Пароль<input id="login-password" type="password" autocomplete="current-password" required></label>`;
  $("#app").innerHTML =
    `<div class="login"><div class="login-heading"><span class="eyebrow">Честный ЗНАК · Радиоэлектроника</span><button class="icon-btn" data-theme-toggle aria-label="Тема">☾</button></div>
      <h1>${admin ? "Вход администратора" : setup ? "Первый вход или сброс пароля" : "Вход сотрудника"}</h1>
      <p class="muted">${admin ? "Отдельный пароль для управления системой" : setup ? "Введите одноразовый код, полученный от администратора, и придумайте свой пароль." : "Введите username и пароль."}</p>
      <form id="login-form">${admin
        ? '<label>Пароль администратора<input id="login-value" type="password" autocomplete="current-password" required autofocus></label>'
        : userFields}
        <button class="primary">${setup ? "Установить пароль" : "Войти"}</button></form>
      <small>${setup ? "Не менее 8 символов. Требований к регистру и специальным символам нет." : admin ? "Подпись и настройки доступны только администратору." : "При первом входе запросите одноразовый код у администратора."}</small>
      <p>${admin
        ? '<button class="linkbutton" data-action="user-login">Вход сотрудника</button>'
        : `<button class="linkbutton" data-action="${setup ? "user-login" : "setup-login"}">${setup ? "Вернуться ко входу" : "Первый вход / сброс пароля"}</button> · <button class="linkbutton" data-action="admin-login">Вход администратора</button>`}</p></div>`;
  window.GisTheme.refresh();
  $("#login-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      if (setup) {
        const password = $("#password-new").value;
        const confirmation = $("#password-repeat").value;
        if (password.length < 8) throw new Error("Пароль должен содержать минимум 8 символов");
        if (password !== confirmation) throw new Error("Пароли не совпадают");
        const username = $("#login-value").value;
        await api("/users/set-password", {
          username,
          code: $("#setup-code").value,
          password,
          confirmation
        });
        login();
        $("#login-value").value = username;
        toast("Пароль установлен. Теперь войдите с новым паролем", "success");
        return;
      }
      user = await api("/session", admin
        ? { admin: true, password: $("#login-value").value }
        : { username: $("#login-value").value, password: $("#login-password").value }
      );
      adminMode = admin;
      admin ? await adminPage()
        : user.role === "logistics" ? await logisticsPage() : await list();
    } catch (err) {
      toast(err.message);
    }
  };
}
function showSetupCode(username, result) {
  if (!result?.code) return;
  modal(`<h2>Одноразовый код для ${e(username)}</h2>
    <p class="muted">Передайте код сотруднику лично. Он действует 7 дней, после использования недействителен. Старые сессии сотрудника закрыты. Этот код показывается только один раз.</p>
    <p class="mono" id="setup-code-issued" style="overflow-wrap:anywhere;user-select:all">${e(result.code)}</p>
    <div class="actions"><button data-action="copy-setup-code">Скопировать код</button><button class="primary" data-action="close">Закрыть</button></div>`);
}
async function changeOwnPassword() {
  modal(`<h2>Сменить пароль</h2><form id="change-password-form">
    <label>Текущий пароль<input name="current_password" type="password" autocomplete="current-password" required></label>
    <label>Новый пароль, минимум 8 символов<input name="password" type="password" minlength="8" autocomplete="new-password" required></label>
    <label>Повторите новый пароль<input name="confirmation" type="password" minlength="8" autocomplete="new-password" required></label>
    <div class="actions"><button class="primary">Изменить пароль</button><button type="button" data-action="close">Отмена</button></div></form>`);
  $("#change-password-form").onsubmit = async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.target));
    if (values.password !== values.confirmation) return toast("Пароли не совпадают");
    try {
      await api("/users/change-password", values);
      $("#modal").close();
      toast("Пароль изменён", "success");
    } catch (err) { toast(err.message); }
  };
}
async function list() {
  if (documentListArchive !== archive) {
    documentFilters.stage = "all";
    documentFilters.status = "";
    documentListArchive = archive;
  }
  if (user.role === "logistics" && !user.admin) return logisticsPage();
  registryMode = false;
  history.replaceState(null, "", location.pathname + (archive ? "#archive" : "#documents"));
  adminMode = false;
  doc = null;
  documentDirty = false;
  docs = await api("/documents?archive=" + archive);
  renderList();
}
function renderList() {
  const stages = documentStages;
  shell(`<div class="topbar"><div><div class="eyebrow">${user.admin ? "Все сотрудники" : "Мои документы"}</div><h1>${archive ? "Архив накладных" : "Накладные"}</h1><p class="muted">${archive ? "Завершённые документы и сохранённые файлы" : "Подготовьте этикетки, передайте поставщику и подтвердите нанесение"}</p></div>${archive ? "" : '<button class="primary" data-action="new">+ Новая накладная</button>'}</div>
  <div class="stage-filters" aria-label="Этапы накладных">${Object.entries(stages).map(([key, stage]) => `<button data-stage="${key}" aria-pressed="${documentFilters.stage === key}">${stage.title}<b>${docs.filter(d => stage.matches(d)).length}</b></button>`).join("")}</div>
  <section class="card document-list"><h2>${archive ? "Архивные документы" : "Рабочие документы"}</h2><div class="document-filters"><label>Номер накладной<input id="search" placeholder="Поиск по номеру" value="${e(documentFilters.search)}"></label><label>Поставщик<select id="supplier"><option value="">Все поставщики</option>${[...new Set(docs.map(d => d.supplier))].sort().map(v => `<option ${v === documentFilters.supplier ? "selected" : ""}>${e(v)}</option>`).join("")}</select></label><label>Статус<select id="status"><option value="">Все статусы</option>${[...new Set(docs.map(d => d.display_status || d.status))].map(v => `<option value="${e(v)}" ${v === documentFilters.status ? "selected" : ""}>${e(states[v] || v)}</option>`).join("")}</select></label><label>Создана с<input type="date" id="from" value="${e(documentFilters.from)}"></label><label>Создана по<input type="date" id="to" value="${e(documentFilters.to)}"></label></div>
  <div class="list-caption"><span id="document-count" class="muted" role="status"></span><button class="linkbutton" data-action="reset-doc-filters">Сбросить фильтры</button></div>
  <div class="table-wrap"><table><thead><tr><th><input id="select-all" type="checkbox" aria-label="Выбрать все доступные для нанесения"></th><th>Накладная</th><th>Поставщик</th><th>Статус</th><th>Штук</th><th>Создана</th>${user.admin ? "<th>Владелец</th>" : ""}</tr></thead><tbody id="doc-rows"></tbody></table></div>
  ${archive ? "" : '<div class="selection-bar"><div><b id="selected-count">Накладные не выбраны</b><span class="cell-sub">Выберите документы со статусом «Ожидает нанесения».</span></div><button class="primary" data-action="batch" disabled>Подтвердить нанесение и сформировать АТК</button></div>'}</section>`);
  filterRows();
  for (const key of ["search", "status", "supplier", "from", "to"])
    $("#" + key).oninput = () => {
      documentFilters[key] = $("#" + key).value;
      filterRows();
    };
  $$("[data-stage]").forEach(button => button.onclick = () => {
    documentFilters.stage = button.dataset.stage;
    $$("[data-stage]").forEach(b => b.setAttribute("aria-pressed", b === button));
    filterRows();
  });
  $("#select-all").onchange = () => {
    $$(".doc-select:not(:disabled)").forEach(x => x.checked = $("#select-all").checked);
    updateDocumentSelection();
  };
}
function filterRows() {
  const f = documentFilters;
  const ds = docs.filter(d => documentStages[f.stage].matches(d) &&
    (!f.search || d.number.toLowerCase().includes(f.search.toLowerCase())) &&
    (!f.status || (d.display_status || d.status) === f.status) && (!f.supplier || d.supplier === f.supplier) &&
    (!f.from || d.created.slice(0, 10) >= f.from) && (!f.to || d.created.slice(0, 10) <= f.to));
  $("#document-count").textContent = `Показано ${ds.length} из ${docs.length}`;
  $("#doc-rows").innerHTML = ds.length ? ds.map(d => `<tr><td><input type="checkbox" class="doc-select" value="${e(d.id)}" ${d.status === "transferred" && !archive ? "" : 'disabled title="Доступно после передачи этикеток поставщику"'} aria-label="Выбрать ${e(d.number)}"></td><td><button class="linkbutton" data-action="open" data-id="${e(d.id)}"><b>${e(d.number)}</b></button></td><td>${e(d.supplier)}</td><td>${d.stopped ? badge("cancelled") : badge(d.display_status || d.status)}</td><td>${fmt(d.units)}</td><td>${date(d.created)}</td>${user.admin ? `<td>${e(d.owner)}</td>` : ""}</tr>`).join("") : `<tr><td colspan="${user.admin ? 7 : 6}"><div class="empty"><b>${docs.length ? "По выбранным условиям ничего не найдено" : archive ? "Архив пока пуст" : "Накладных пока нет"}</b>${docs.length ? "Сбросьте фильтры или выберите другой этап." : archive ? "Здесь появятся завершённые документы." : "Создайте накладную и загрузите состав из Excel."}</div></td></tr>`;
  $$(".doc-select").forEach(x => x.onchange = updateDocumentSelection);
  updateDocumentSelection();
}
function updateDocumentSelection() {
  const eligible = $$(".doc-select:not(:disabled)"), count = $$(".doc-select:checked").length;
  $("#select-all").disabled = !eligible.length;
  $("#select-all").checked = !!eligible.length && count === eligible.length;
  $("#select-all").indeterminate = count > 0 && count < eligible.length;
  if ($("#selected-count")) $("#selected-count").textContent = count ? `Выбрано накладных: ${count}` : "Накладные не выбраны";
  if ($('[data-action="batch"]')) $('[data-action="batch"]').disabled = !count;
}
function modal(content) {
  const dialog = $("#modal");
  dialog.innerHTML = content;
  dialog.showModal();
}
async function newDoc(parent) {
  const suppliers = await api("/suppliers");
  modal(
    `<h2>${parent ? "Дополнение к накладной " + e(doc.number) : "Новая накладная"}</h2><form id="new-form"><div class="fields"><label>Номер накладной<input id="number" required maxlength="120"></label><label>Поставщик<input id="new-supplier" list="supplier-list" value="${parent ? e(doc.supplier) : ""}" required maxlength="160" ${parent ? "readonly" : ""}><datalist id="supplier-list">${suppliers.map((s) => `<option value="${e(s.name)}">`).join("")}</datalist></label></div><div class="actions"><button class="primary">Создать черновик</button><button type="button" data-action="close">Отмена</button></div></form>`,
  );
  window.BmUX.supplierPicker($("#new-supplier"), suppliers);
  $("#new-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const r = await api("/documents", {
        number: $("#number").value,
        supplier: $("#new-supplier").value,
        parent_id: parent || null,
      });
      $("#modal").close();
      await open(r.id);
    } catch (err) {
      toast(err.message);
    }
  };
}
function lineRow(l = {}, move = false) {
  return `<tr>${move ? `<td>${l.id ? `<input type="checkbox" class="move-line" value="${e(l.id)}" aria-label="Перенести строку ${e(l.pn)}">` : ""}</td>` : ""}<td class="pn"><input class="pn-input" aria-label="PN" value="${e(l.pn)}" maxlength="120"></td><td class="gtin"><input class="gtin-input" aria-label="GTIN" value="${e(l.gtin)}" maxlength="14"></td><td><input class="qty-input" aria-label="Штук в этикетке" type="number" min="1" max="99999999" value="${e(l.quantity || 1)}"></td><td><input class="labels-input" aria-label="Этикеток" type="number" min="1" value="${e(l.labels || 1)}"></td><td class="tnved"><input class="tnved-input" aria-label="ТН ВЭД" value="${e(l.tnved)}" maxlength="10"></td><td><button data-action="remove-line" class="danger" aria-label="Удалить строку">×</button></td></tr>`;
}
function gatherRows() {
  return $$("#edit-rows tr").map((tr) => {
    const values = [...tr.querySelectorAll("input")].map((x) => x.value);
    return [
      values[0],
      values[1],
      Number(values[2]),
      Number(values[3]),
      values[4],
    ];
  });
}
async function open(id) {
  registryMode = false;
  history.replaceState(
    null,
    "",
    location.pathname + "#document/" + encodeURIComponent(id),
  );
  if (doc?.id !== id) documentTab = "content";
  documentDirty = false;
  doc = await api("/documents/" + id);
  statusLastChecked = new Date();
  archive = doc.status === "archive";
  adminMode = false;
  renderDoc();
}
function renderDoc() {
  const editable = ["draft", "ready", "transferred", "label_error"].includes(
    doc.status,
  );
  const canMoveLines = !doc.stopped && !doc.group_stop && !doc.parent_id &&
    ["ready", "transferred"].includes(doc.status) &&
    !doc.operations.some(o => ["report", "atk", "transform"].includes(o.kind));
  const steps = [
    ["draft", "Подготовка"],
    ["receiving", "Коды"],
    ["ready", "Этикетки"],
    ["transferred", "Нанесение"],
    ["reporting", "Отчёт"],
    ["aggregating", "АТК"],
    ["done", "Готово"],
  ];
  const current =
    doc.status === "reported"
      ? "reporting"
      : doc.status === "archive"
        ? "done"
        : doc.status;
  const latestReport = [...doc.operations].reverse().find(o => o.kind === "report");
  const reportIssue = doc.report_issue;
  const currentFile = doc.artifacts.find(
    (a) => a.kind === "labels" && a.version === doc.current_version,
  );
  shell(
    `<div class="topbar"><div><button class="linkbutton" data-action="${archive ? "archive" : "list"}">${archive ? "← Архив накладных" : "← Накладные"}</button><h1>Накладная ${e(doc.number)}</h1><span class="muted">${e(doc.supplier)} · ${e(doc.owner)} · ${date(doc.created)}</span></div><div class="doc-status-meta">${badge(doc.display_status || doc.status)}<span id="status-checked" class="status-checked" title="Проверка каждые 30 секунд">Проверено: ${statusLastChecked ? statusLastChecked.toLocaleTimeString("ru-RU") : "—"}</span></div></div>${doc.stopped ? `<div class="notice"><b>Обработка остановлена пользователем ${e(doc.stop.actor)}</b><br>Новые операции и подпись заблокированы. Уже отправленные действия ЧЗ не отозваны; их результаты сохранены для сверки. Вернуть накладную из архива может только администратор.</div>` : doc.group_stop ? `<div class="notice">Общая обработка приостановлена: накладная ${e(doc.group_stop.number)} остановлена. Обратитесь к администратору.</div>` : ""}<div class="steps">${steps.map(([s, l]) => `<span class="step ${s === current ? "current" : ""}">${l}</span>`).join("")}</div><div class="stats"><div class="stat"><span class="muted">Единиц товара</span><b>${fmt(doc.units)}</b></div><div class="stat"><span class="muted">Получено КМ</span><b>${fmt(doc.codes.length)} / ${fmt(doc.required)}</b></div><div class="stat"><span class="muted">Версия этикеток</span><b>${doc.current_version || "—"}</b></div></div>${doc.last_error ? `<div class="error">Этикетки не сформированы: ${e(doc.last_error)}. Полученные КМ сохранены. Исправьте состав накладной.</div>` : ""}${doc.operations
      .filter((o) => o.error && (o.kind !== "report" || o.id === latestReport?.id))
      .map(
        (o) =>
          `<div class="error">${e(kinds[o.kind])}: ${e(o.error)} · ${e(states[o.state] || o.state)}</div>`,
      )
      .join(
        "",
      )}${doc.status === "receiving" ? '<div class="notice">PDF станет доступен после получения полного комплекта КМ. Повторный заказ не требуется.</div>' : ""}<div class="card"><div class="topbar"><h2>Состав накладной</h2>${editable ? '<div class="actions"><a href="/api/template">Скачать шаблон Excel</a><button data-action="import">Загрузить Excel</button><input type="file" id="excel-file" accept=".xlsx" hidden></div>' : ""}</div><div class="table-wrap"><table><thead><tr>${canMoveLines ? "<th>Перенести</th>" : ""}<th>PN</th><th>GTIN</th><th>Штук в этикетке</th><th>Этикеток</th><th>ТН ВЭД</th>${editable ? "<th></th>" : ""}</tr></thead><tbody id="edit-rows">${editable ? doc.lines.map(l => lineRow(l, canMoveLines)).join("") : doc.lines.map((l) => `<tr><td>${e(l.pn)}</td><td>${e(l.gtin)}</td><td>${fmt(l.quantity)}</td><td>${fmt(l.labels)}</td><td>${e(l.tnved)}</td></tr>`).join("")}</tbody></table></div>${editable ? '<div class="actions"><button data-action="add-line">+ Добавить строку</button><button data-action="save-lines">Проверить и сохранить состав</button></div>' : ""}<div class="actions">${canMoveLines ? '<button data-action="move-lines">Перенести строки и КМ</button>' : ""}${!doc.stopped && !doc.group_stop && reportIssue ? `<button class="primary" data-action="retry-report-doc" data-id="${e(reportIssue.id)}">Отправить повторно</button>` : ""}${user.admin && doc.status === "transferred" && doc.current_version === 0 ? '<button data-action="upload-legacy-pdf">Загрузить PDF</button><input type="file" id="legacy-pdf-file" accept=".pdf,application/pdf" hidden>' : ""}${doc.status === "draft" ? '<button class="primary" data-action="order">Заказать КМ</button>' : ""}${currentFile && doc.required > 0 && doc.codes.length === doc.required && !["draft", "repack", "receiving"].includes(doc.status) ? `<a class="button" href="/api/files/${e(currentFile.id)}">↓ Скачать ZIP с этикетками · v${doc.current_version}</a>` : ""}${["ready", "transferred"].includes(doc.status) ? `<button class="${doc.status === "ready" ? "primary" : ""}" data-action="transfer">${doc.status === "transferred" ? "Повторно подтвердить передачу" : "Подтвердить передачу этикеток"}</button>` : ""}${doc.status === "transferred" ? '<button class="primary" data-action="report">Подтвердить нанесение</button>' : ""}${!doc.stopped && ["done", "archive", "reported"].includes(doc.status) ? '<button data-action="supplement">Создать дополнение</button>' : ""}${!doc.stopped ? '<button class="danger" data-action="stop-document">Остановить и архивировать</button>' : ""}${user.admin && doc.status === "archive" ? '<button data-action="restore-document">Вернуть из архива</button>' : ""}<button data-action="refresh-doc">Обновить состояние</button></div></div>${doc.atks.length ? `<div class="card"><h2>Таможенные коды</h2>${doc.atks.map((a) => `<div class="toolbar"><span>ТН ВЭД <b>${e(a.tnved)}</b> · ${a.code ? e(a.code) : "Ожидает формирования"}</span>${a.state === "done" ? `<button data-action="atk" data-id="${e(a.id)}">Состав и трансформации</button>` : badge(a.state)}</div>`).join("")}<div class="files">${doc.exports.map((f) => `<a href="/api/files/${e(f.id)}">${e(f.name)} · v${f.version}</a>`).join("")}</div></div>` : ""}<div class="split"><div class="card"><h2>Обработка</h2>${doc.operations.length ? doc.operations.map((o) => `<div class="history"><b>${e(kinds[o.kind])}</b>${o.stopped && o.state !== "cancelled" ? badge("cancelled") + " " : ""}${badge(o.state)} <span class="muted">${date(o.updated)}</span>${o.error ? `<p class="muted">${e(o.error)}</p>` : ""}${!o.stopped && o.id === reportIssue?.id && o.kind === "report" && ["rejected", "partial"].includes(o.state) ? `<div class="actions"><button class="primary" data-action="retry-report-doc" data-id="${e(o.id)}">Повторить отправку отчёта</button></div>` : ""}</div>`).join("") : '<p class="muted">Внешние операции ещё не запускались.</p>'}<details><summary>История файлов и прежние версии</summary><p class="muted">Скачанные поставщиком старые этикетки технически отозвать нельзя.</p><div class="files">${doc.artifacts.map((f) => `<a href="/api/files/${e(f.id)}">${e(f.name)} · ${e(f.kind === "source" ? "Исходный Excel" : "v" + f.version)}</a>`).join("")}</div></details></div><div class="card"><h2>История документа</h2>${doc.history.map((h) => `<div class="history"><b>${e(h.action)}</b><span class="muted">${date(h.created)} · ${e(h.actor)}</span></div>`).join("")}</div></div>`,
  );
  organizeDocument(editable);
  if ($("#legacy-pdf-file")) $("#legacy-pdf-file").onchange = async ev => {
    const file = ev.target.files[0];
    if (!file) return;
    ev.target.value = "";
    const form = new FormData();
    form.append("file", file);
    try {
      await api("/admin/legacy/" + encodeURIComponent(doc.id) + "/pdf", form);
      await open(doc.id);
      toast("Исходный PDF сохранён в истории файлов", "success");
    } catch(err) { toast(err.message); }
  };
  if (editable) {
    $("#excel-file").onchange = importExcel;
    $("#edit-rows").oninput = ev => {
      if (!ev.target.matches(".move-line")) markDocumentDirty();
    };
  }
}
async function saveRows(raw) {
  const preview = await api(`/documents/${doc.id}/preview`, {
    rows: raw,
    revision: doc.revision,
  });
  if (preview.errors.length) {
    modal(
      `<h2>Исправьте состав</h2>${preview.errors.map((x) => `<p class="error">Строка ${x.row}: ${e(x.message)}</p>`).join("")}<button data-action="close">Закрыть</button>`,
    );
    return;
  }
  if (preview.changes.length || preview.warnings.length) {
    modal(
      `<h2>Проверьте изменения</h2>${preview.changes.length ? `<table><tr><th>PN</th><th>Было</th><th>Стало</th><th>Разница</th></tr>${preview.changes.map((c) => `<tr><td>${e(c.pn)}</td><td>${fmt(c.before)}</td><td>${fmt(c.after)}</td><td>${fmt(c.difference)}</td></tr>`).join("")}</table>` : ""}${preview.warnings.map((w) => `<p class="notice">${e(w)}</p>`).join("")}<div class="actions"><button id="confirm-save" class="primary">Подтвердить и сохранить</button><button data-action="close">Отмена</button></div>`,
    );
    $("#confirm-save").onclick = () => doSave(raw, preview.fingerprint);
  } else await doSave(raw);
}
async function doSave(raw, confirmation) {
  try {
    doc = await api(`/documents/${doc.id}/lines`, {
      rows: raw,
      revision: doc.revision,
      confirmation,
    });
    $("#modal").close();
    documentDirty = false;
    renderDoc();
    toast("Состав сохранён", "success");
  } catch (err) {
    toast(err.message);
  }
}
let excelImportVersion = 0;
async function importExcel() {
  const input = $("#excel-file");
  const file = input.files[0];
  if (!file) return;
  input.value = ""; // Selecting the same file again must fire change as well.
  const context = { file, docId: doc.id, version: ++excelImportVersion };
  try {
    await runExcelImport(context);
  } catch (err) {
    toast(err.message);
  }
}
async function runExcelImport(context, options = {}) {
  const form = new FormData();
  form.append("file", context.file);
  if (options.mapping) form.append("mapping", JSON.stringify(options.mapping));
  if (options.sheet !== undefined) form.append("sheet", options.sheet);
  if (options.headerRow !== undefined)
    form.append("header_row", String(options.headerRow));
  const result = await api(`/documents/${context.docId}/import`, form);
  if (
    !doc ||
    doc.id !== context.docId ||
    context.version !== excelImportVersion
  )
    return;
  if (result.requires_mapping) {
    excelMapping(context, result);
    return;
  }
  if (result.errors.length) {
    modal(
      `<h2>Ошибки Excel</h2>${result.errors.map((x) => `<p class="error">Строка ${x.row || "—"}: ${e(x.message)}</p>`).join("")}<div class="actions">${options.mapping ? '<button id="excel-correct-mapping">Исправить сопоставление</button>' : ""}<button data-action="close">Закрыть</button></div>`,
    );
    if (options.mapping)
      $("#excel-correct-mapping").onclick = () =>
        excelMapping(context, context.inspection, options);
    return;
  }
  if ($("#modal").open) $("#modal").close();
  $("#edit-rows").innerHTML = result.lines.map(lineRow).join("");
  markDocumentDirty();
  if (result.warnings.length) {
    modal(
      `<h2>Excel загружен</h2>${result.warnings.map((w) => `<p class="notice">${e(w)}</p>`).join("")}<p>Проверьте значения в таблице накладной и сохраните состав.</p><button data-action="close">Продолжить</button>`,
    );
  }
  toast("Excel загружен в предпросмотр. Проверьте строки и сохраните состав.", "success");
}
function excelMapping(context, inspection, previous = {}) {
  context.inspection = inspection;
  let sheet =
    inspection.sheets.find(
      (s) => s.name === (previous.sheet || inspection.selected_sheet),
    ) || inspection.sheets[0];
  let headerRow = previous.headerRow ?? sheet.suggested_header_row;
  let chosen = { ...(previous.mapping || sheet.suggested_mapping) };
  function valueAt(row, index) {
    return row?.values[index] ?? "";
  }
  function draw() {
    const header = sheet.preview.find((r) => r.row === Number(headerRow));
    const sampleRows = sheet.preview
      .filter((r) => r.row > Number(headerRow))
      .slice(0, 3);
    const content = `<div class="eyebrow">Импорт Excel</div><h2>Сопоставление столбцов</h2><p class="muted">Не удалось однозначно определить все поля. Проверьте лист и укажите, какой столбец содержит каждое значение. Формат ячеек менять не нужно.</p><form id="excel-mapping-form"><div class="fields"><label>Лист<select id="excel-sheet">${inspection.sheets.map((s) => `<option value="${e(s.name)}" ${s.name === sheet.name ? "selected" : ""}>${e(s.name)}</option>`).join("")}</select></label><label>Строка заголовков<input id="excel-header-row" type="number" min="0" max="${sheet.row_count}" value="${headerRow}" required><span class="muted">0 — файл без заголовков</span></label></div><div class="table-wrap mapping-table"><table><thead><tr><th>Поле в приложении</th><th>Столбец Excel</th><th>Примеры значений</th></tr></thead><tbody>${Object.entries(
      inspection.fields,
    )
      .map(
        ([field, title]) =>
          `<tr><td>${e(title)}</td><td><select data-excel-field="${e(field)}" aria-label="Столбец для ${e(title)}" required><option value="">Выберите столбец</option>${sheet.columns.map((c) => `<option value="${c.index}" ${chosen[field] === c.index ? "selected" : ""}>${c.letter}${valueAt(header, c.index) ? " — " + e(valueAt(header, c.index)) : ""}</option>`).join("")}</select></td><td class="mapping-sample" data-sample="${e(field)}">${
            chosen[field] !== null && chosen[field] !== undefined
              ? e(
                  sampleRows
                    .map((r) => valueAt(r, chosen[field]))
                    .filter(Boolean)
                    .join(" · "),
                )
              : "—"
          }</td></tr>`,
      )
      .join(
        "",
      )}</tbody></table></div><div id="excel-mapping-error" class="error" role="alert" hidden></div><details><summary>Фрагмент файла</summary><p class="muted">Предпросмотр первых ${sheet.preview.length} строк листа. Номера ошибок соответствуют исходному Excel.</p><div class="table-wrap"><table><thead><tr><th>Строка</th>${sheet.columns.map((c) => `<th>${c.letter}</th>`).join("")}</tr></thead><tbody>${sheet.preview
      .filter((r) => r.row >= Math.max(1, Number(headerRow)))
      .slice(0, 8)
      .map(
        (r) =>
          `<tr><th>${r.row}</th>${sheet.columns.map((c) => `<td class="mapping-cell">${e(valueAt(r, c.index))}</td>`).join("")}</tr>`,
      )
      .join(
        "",
      )}</tbody></table></div></details><div class="actions mapping-actions"><button class="primary" id="excel-mapping-apply" type="submit">Применить сопоставление</button><button type="button" data-action="close">Отмена</button></div></form>`;
    if ($("#modal").open) $("#modal").innerHTML = content;
    else modal(content);
    $("#excel-sheet").onchange = () => {
      sheet = inspection.sheets.find((s) => s.name === $("#excel-sheet").value);
      headerRow = sheet.suggested_header_row;
      chosen = { ...sheet.suggested_mapping };
      draw();
    };
    $("#excel-header-row").onchange = () => {
      headerRow = Number($("#excel-header-row").value);
      const newHeader = sheet.preview.find((r) => r.row === headerRow);
      chosen = {
        ...(newHeader?.mapping ||
          Object.fromEntries(
            Object.keys(inspection.fields).map((k) => [k, null]),
          )),
      };
      draw();
    };
    $$("[data-excel-field]").forEach(
      (select) =>
        (select.onchange = () => {
          const field = select.dataset.excelField;
          chosen[field] = select.value === "" ? null : Number(select.value);
          $(`[data-sample="${field}"]`).textContent =
            chosen[field] === null
              ? "—"
              : sampleRows
                  .map((r) => valueAt(r, chosen[field]))
                  .filter(Boolean)
                  .join(" · ") || "—";
        }),
    );
    $("#excel-mapping-form").onsubmit = async (event) => {
      event.preventDefault();
      const error = $("#excel-mapping-error");
      const values = Object.values(chosen);
      if (
        values.some((v) => v === null || v === undefined) ||
        new Set(values).size !== Object.keys(inspection.fields).length
      ) {
        error.hidden = false;
        error.textContent =
          "Выберите пять разных столбцов: отдельный для каждого поля.";
        return;
      }
      const button = $("#excel-mapping-apply");
      button.disabled = true;
      try {
        await runExcelImport(context, {
          sheet: sheet.name,
          headerRow: Number(headerRow),
          mapping: chosen,
        });
      } catch (err) {
        if ($("#excel-mapping-error")) {
          error.hidden = false;
          error.textContent = err.message;
        } else toast(err.message);
      } finally {
        button.disabled = false;
      }
    };
  }
  draw();
}

async function moveLinesModal(lineIds) {
  const sourceId = doc.id;
  const targets = await api("/documents/" + sourceId + "/move-targets");
  if (!targets.length) {
    throw new Error("Нет доступной накладной этого поставщика. Создайте пустую накладную или обратитесь к администратору");
  }
  const selected = doc.lines.filter(l => lineIds.includes(l.id));
  modal(`<h2>Перенести строки и КМ</h2>
    <p>Из накладной <b>${e(doc.number)}</b> к другому документу поставщика <b>${e(doc.supplier)}</b>.</p>
    <p class="muted">Переносятся целые строки и все закреплённые за ними КМ. Новые КМ не заказываются.</p>
    <div class="table-wrap"><table><thead><tr><th>PN</th><th>GTIN</th><th>Штук</th><th>КМ</th></tr></thead>
    <tbody>${selected.map(l=>`<tr><td>${e(l.pn)}</td><td>${e(l.gtin)}</td><td>${fmt(l.quantity)}</td><td>${fmt(l.labels)}</td></tr>`).join("")}</tbody></table></div>
    <form id="move-lines-form">
      <label>Накладная-получатель<select name="target" required>
        <option value="">Выберите накладную</option>
        ${targets.map(t=>`<option value="${e(t.id)}">${e(t.number)} · ${e(states[t.status] || t.status)}${user.admin ? " · " + e(t.owner) : ""}</option>`).join("")}
      </select></label>
      <div class="actions"><button class="primary" type="submit">Проверить перенос</button>
      <button type="button" data-action="close">Отмена</button></div>
    </form>`);
  $("#move-lines-form").onsubmit = async event => {
    event.preventDefault();
    const targetId = new FormData(event.target).get("target");
    if (!targetId) return toast("Выберите накладную-получатель");
    try {
      const preview = await api("/documents/" + sourceId + "/move-lines/preview", {
        target_id: targetId, line_ids: lineIds,
      });
      $("#modal").close();
      modal(`<h2>Подтвердить перенос</h2>
        <p><b>${e(preview.source.number)}</b> → <b>${e(preview.target.number)}</b></p>
        <p>${fmt(preview.lines.length)} строк · ${fmt(preview.codes)} КМ · ${fmt(preview.units)} единиц товара</p>
        <p class="notice">${e(preview.warning)}</p>
        <div class="actions">
          <button class="primary" id="confirm-move-lines">Перенести строки и КМ</button>
          <button data-action="close">Отмена</button>
        </div>`);
      $("#confirm-move-lines").onclick = async () => {
        const button = $("#confirm-move-lines");
        button.disabled = true;
        try {
          await api("/documents/" + sourceId + "/move-lines", {
            target_id: targetId, line_ids: lineIds,
            fingerprint: preview.fingerprint,
          });
          $("#modal").close();
          await open(sourceId);
          toast("Строки и КМ перенесены. Отправьте новые версии этикеток поставщику", "success");
        } catch(error) {
          button.disabled = false;
          toast(error.message);
        }
      };
    } catch(error) { toast(error.message); }
  };
}
async function batchModal(ids) {
  if (!ids.length) throw new Error("Выберите накладные");
  modal(
    `<h2>Подтверждение нанесения</h2><p>Поставщик подтвердил нанесение актуальных этикеток по выбранным накладным (${ids.length}). По каждой накладной будет отправлен отдельный отчёт, затем сформированы АТК.</p><p class="muted">КПП и ФИАС места деятельности будут взяты из настроек организации в админке.</p><form id="report-form"><label>Дата производства<input id="production-date" type="date" required></label><div class="actions"><button class="primary">Подтвердить и начать обработку</button><button type="button" data-action="close">Отмена</button></div></form>`,
  );
  $("#report-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api("/batches", {
        documents: ids,
        production_date: $("#production-date").value,
      });
      $("#modal").close();
      doc ? await open(doc.id) : await list();
      toast("Отчёты поставлены в очередь подписи", "success");
    } catch (err) {
      toast(err.message);
    }
  };
}
async function atkModal(id) {
  const a = await api("/atks/" + id);
  modal(
    `<h2>АТК ${e(a.code)}</h2><p class="muted">ТН ВЭД ${e(a.tnved)} · версия ${a.version}</p>${a.stopped ? '<p class="notice">Общая обработка остановлена. Трансформации доступны после возврата накладной администратором.</p>' : `<div class="toolbar"><button data-action="transform-add" data-id="${e(id)}">Добавить КМ</button><button data-action="transform-remove" data-id="${e(id)}">Изъять КМ</button></div>`}<p>Вложено КМ: ${a.codes.length}, штук: ${fmt(a.codes.reduce((n, c) => n + c.quantity, 0))}</p><div class="table-wrap"><table><tr><th>КИ</th><th>PN</th><th>Штук</th></tr>${a.codes.map((c) => `<tr><td class="mono">${e(c.cis)}</td><td>${e(c.pn)}</td><td>${fmt(c.quantity)}</td></tr>`).join("")}</table></div><h3>Заявки на трансформацию</h3>${a.transformations.map((t) => `<p>${t.kind === "ADDING" ? "Добавление" : "Изъятие"} · ${badge(t.state)} · ${date(t.created)} ${e(t.reason || "")}</p>`).join("")}<button data-action="close">Закрыть</button>`,
  );
}
async function transformModal(id, kind) {
  const codes =
    kind === "ADDING"
      ? await api("/atks/" + id + "/candidates")
      : (await api("/atks/" + id)).codes;
  $("#modal").innerHTML =
    `<h2>${kind === "ADDING" ? "Добавление" : "Изъятие"} КМ</h2><p class="muted">Заявка будет отправлена на согласование администратору.</p><div class="table-wrap"><table><tr><th></th><th>КИ</th><th>PN</th><th>Штук</th></tr>${codes.map((c) => `<tr><td><input class="code-select" type="checkbox" value="${e(c.id)}"></td><td class="mono">${e(c.cis)}</td><td>${e(c.pn)}</td><td>${c.quantity}</td></tr>`).join("")}</table></div>${codes.length ? "" : '<p class="notice">Подходящих КМ нет. Изъятые ранее КМ требуют отдельного разрешения администратора.</p>'}<div class="actions"><button id="transform-submit" class="primary">Отправить на согласование</button><button data-action="close">Отмена</button></div>`;
  $("#transform-submit").onclick = async () => {
    try {
      await api("/atks/" + id + "/transform", {
        kind,
        codes: $$(".code-select:checked").map((x) => x.value),
      });
      $("#modal").close();
      toast("Заявка отправлена администратору", "success");
    } catch (err) {
      toast(err.message);
    }
  };
}
function renderOperationRow(o) {
  const attention = ["unknown", "blocked", "preflight_blocked", "rejected", "partial"].includes(o.state);
  const resolved = Boolean(o.attention_resolved_at);
  const buttons = [];
  if (!o.stopped && o.kind === "report" && ["rejected", "partial"].includes(o.state) && o.external_id) {
    buttons.push(`<button data-action="retry-report" data-id="${e(o.id)}">Повторить отправку</button>`);
  }
  if (!o.stopped && o.state === "blocked" && !o.external_id &&
      ["Обновите доступ к ГИС МТ", "Обновите доступ к СУЗ", "Отправка операций отключена администратором сервера"]
        .some(text => (o.error || "").startsWith(text))) {
    buttons.push(`<button data-action="retry-local" data-id="${e(o.id)}">Повторить отправку</button>`);
  }
  if (!o.stopped && ((o.external_id && ["unknown", "blocked", "submitted"].includes(o.state)) || o.state === "preflight_blocked")) {
    buttons.push(`<button data-action="check-op" data-id="${e(o.id)}">${o.state === "preflight_blocked" ? "Повторить проверку и отправку" : "Проверить состояние"}</button>`);
  }
  if (!o.stopped && o.state === "unknown" && o.kind === "fetch") {
    buttons.push(`<button data-action="recover-block" data-id="${e(o.id)}">Восстановить блок</button>`);
  }
  if (!o.stopped && !o.external_id && o.state === "unknown" && o.kind !== "fetch") {
    buttons.push(`<button data-action="reconcile-op" data-id="${e(o.id)}">Указать ID после сверки ЛК</button>`);
  }
  if (attention) {
    buttons.push(`<button data-action="${resolved ? "unresolve-op" : "resolve-op"}" data-id="${e(o.id)}">${resolved ? "Вернуть предупреждение" : "Скрыть предупреждение"}</button>`);
  }
  const invoice = o.doc_id && o.doc_number
    ? `<span class="cell-sub"><button class="linkbutton" data-action="open" data-id="${e(o.doc_id)}">Накладная ${e(o.doc_number)}</button></span>`
    : "";
  return `<tr><td>${e(kinds[o.kind])}<br><span class="muted">${date(o.created)}</span>${invoice}</td><td>${o.stopped && o.state !== "cancelled" ? badge("cancelled") + " " : ""}${badge(o.state)}${resolved ? '<span class="cell-sub">Предупреждение скрыто</span>' : ""}</td><td class="mono">${e(o.external_id || "—")}</td><td class="filename">${e(o.error || "")}</td><td><div class="download-actions">${buttons.join("")}</div></td></tr>`;
}
async function loadAdminDocuments() {
  const result = $("#admin-doc-results");
  if (!result || !user?.admin) return;
  const search = $("#admin-doc-q").value.trim();
  result.textContent = "Загрузка накладных…";
  try {
    const invoices = await api("/admin/documents?q=" + encodeURIComponent(search));
    if (!$("#admin-doc-results")) return;
    const managers = overview.users.filter(u => u.enabled && u.role === "manager");
    result.innerHTML = invoices.length
      ? `<div class="table-wrap"><table><thead><tr><th>Накладная</th><th>Поставщик</th><th>Статус</th><th>Текущий ответственный</th><th>Новый ответственный</th><th>Действие</th></tr></thead><tbody>${invoices.map(d => {
          const options = managers.filter(u => u.username !== d.owner);
          return `<tr data-owner="${e(d.owner)}" data-number="${e(d.number)}"><td><b>${e(d.number)}</b></td><td>${e(d.supplier)}</td><td>${badge(d.status)}</td><td>${e(d.owner)}</td><td><select class="user-role" aria-label="Назначить ответственного для ${e(d.number)}"><option value="">Выберите сотрудника</option>${options.map(u => `<option value="${e(u.username)}">${e(u.username)}</option>`).join("")}</select></td><td><button data-action="reassign-document" data-id="${e(d.id)}" ${options.length ? "" : "disabled"}>Передать</button></td></tr>`;
        }).join("")}</tbody></table></div>`
      : '<p class="empty">Накладные не найдены. Измените запрос.</p>';
    $("#admin-doc-count").textContent = `Показано ${invoices.length} накладных${invoices.length === 100 ? " (первые 100; уточните поиск)" : ""}. Архивные документы тоже учитываются.`;
  } catch (error) {
    if ($("#admin-doc-results")) result.textContent = "Не удалось загрузить накладные";
    toast(error.message);
  }
}
function setupAdminDocumentPanel() {
  const panel = document.createElement("section");
  panel.className = "card";
  panel.dataset.adminPanel = "documents";
  panel.innerHTML = `<h2>Передать накладную другому сотруднику</h2><p class="muted">Администратор может изменить ответственного, включая архивные накладные. История и полученные КМ сохраняются. Новый сотрудник получит доступ, прежний его потеряет.</p><form id="admin-doc-search-form" class="admin-filter-bar"><label>Поиск накладной, поставщика или владельца<input id="admin-doc-q" type="search" maxlength="120" placeholder="Номер, поставщик или username"></label><button class="primary" type="submit">Найти</button></form><p id="admin-doc-count" class="muted" role="status"></p><div id="admin-doc-results"></div>`;
  // Import old KM packs without requesting or rendering new labels.
  const managers = overview.users.filter(u => u.enabled && u.role === "manager");
  const legacy = document.createElement("div");
  legacy.className = "card";
  legacy.innerHTML = `<h3>Импорт старой накладной из паков</h3>
    <p class="muted">Формат result_НОМЕР.xlsx: КМ, GTIN, PN, штук в этикетке, ТН ВЭД. До импорта каждый КМ обязательно сверяется с ЧЗ: допускается только EMITTED. Нанесённые или введённые в оборот КМ блокируют всю накладную. PDF не создаётся.</p>
    <form id="legacy-import-form" class="fields">
      <label class="full">Excel из паков<input name="file" id="legacy-file" type="file" accept=".xlsx" required></label>
      <label>Поставщик<input name="supplier" list="legacy-suppliers" maxlength="160" required placeholder="Выберите или введите поставщика">
      <datalist id="legacy-suppliers">${overview.suppliers.map(p=>`<option value="${e(p.name)}">`).join("")}</datalist></label>
      <label>Ответственный<select name="owner" id="legacy-owner" required>${managers.map(u=>`<option value="${e(u.username)}" ${u.username===user.username?"selected":""}>${e(u.username)}</option>`).join("")}</select></label>
      <div id="legacy-preview" class="full muted" role="status">Выберите Excel для проверки.</div>
      <div class="actions full"><button type="submit" class="primary" id="legacy-import-submit" disabled>Импортировать накладную</button></div>
    </form>`;
  panel.prepend(legacy);
  $('[data-admin-panel="users"]').after(panel);
  window.BmUX.supplierPicker($("#legacy-import-form [name=supplier]"), overview.suppliers.filter(s => !s.merged_into));
  let previewReady = false;
  const input = $("#legacy-file");
  const submit = $("#legacy-import-submit");
  input.onchange = async () => {
    previewReady = false;
    submit.disabled = true;
    $("#legacy-preview").textContent = "Проверяем Excel…";
    if (!input.files.length) {
      $("#legacy-preview").textContent = "Выберите Excel для проверки.";
      return;
    }
    const selected = input.files[0];
    try {
      const form = new FormData();
      form.append("file", selected);
      const result = await api("/admin/legacy/preview", form);
      if (input.files[0] !== selected) return;
      const verification = result.verification;
      const statusSummary = Object.entries(verification.status_counts)
        .map(([status, count]) => e(status) + ": " + fmt(count)).join(" · ");
      $("#legacy-preview").innerHTML = `<b>Накладная ${e(result.number)}</b> · ${fmt(result.codes)} КМ · ${fmt(result.units)} шт. · ${result.lines.length} позиций
        ${result.duplicate_invoice ? '<p class="error">Накладная с этим номером уже есть среди активных документов. Импорт запрещён.</p>' : ""}
        ${result.archived_codes ? `<p class="muted">В архиве найдено ${fmt(result.archived_codes)} КМ. При импорте они будут перенесены в новую рабочую накладную без создания дублей. Архив и история назначений сохранятся.</p>` : ""}
        ${result.blocked_codes ? `<p class="error">${fmt(result.blocked_codes)} КМ нельзя перенести из архива. Ниже указана конкретная причина блокировки.</p><div class="table-wrap"><table><thead><tr><th>Архивная накладная</th><th>PN</th><th>GTIN</th><th>CIS</th><th>Причина</th></tr></thead><tbody>${(result.blocked_details || []).map(item=>`<tr><td>${e(item.source_number)}</td><td>${e(item.pn)}</td><td>${e(item.gtin)}</td><td class="mono">${e(item.cis)}</td><td>${e(item.reason)}</td></tr>`).join("")}</tbody></table></div>` : ""}
        <p class="${verification.allowed ? "muted" : "error"}"><b>Проверка ЧЗ:</b> ${verification.allowed ? "Все " + fmt(verification.checked) + " КМ имеют статус EMITTED и принадлежат нашей организации. Сверка ЧЗ успешна; требуется также проверка ERP." : "Импорт запрещён: " + fmt(verification.errors_count) + " КМ не соответствуют требованиям."}</p>
        <p class="muted">Статусы: ${statusSummary}</p>
        ${verification.errors.length ? `<div class="table-wrap"><table><thead><tr><th>PN</th><th>GTIN</th><th>КМ (CIS)</th><th>Статус ЧЗ</th><th>Причина</th></tr></thead><tbody>${verification.errors.map(item=>`<tr><td>${e(item.pn)}</td><td>${e(item.gtin)}</td><td class="mono">${e(item.cis)}</td><td>${e(item.status)}</td><td>${e(item.reason)}</td></tr>`).join("")}</tbody></table></div>${verification.errors_count > verification.errors.length ? '<p class="muted">Показаны первые 100 проблемных КМ.</p>' : ""}` : ""}
        <div class="table-wrap"><table><thead><tr><th>PN</th><th>GTIN</th><th>В этикетке</th><th>Этикеток</th></tr></thead><tbody>${result.lines.slice(0, 20).map(line=>`<tr><td>${e(line.pn)}</td><td>${e(line.gtin)}</td><td>${fmt(line.quantity)}</td><td>${fmt(line.labels)}</td></tr>`).join("")}</tbody></table></div>
        ${result.lines.length>20 ? '<p class="muted">Показаны первые 20 позиций</p>' : ""}`;
      previewReady = !result.duplicate_invoice && !result.blocked_codes && verification.allowed;
      submit.disabled = !previewReady;
    } catch (err) {
      $("#legacy-preview").textContent = err.message;
    }
  };
  $("#legacy-import-form").onsubmit = async ev => {
    ev.preventDefault();
    if (!previewReady || !input.files.length) return;
    const form = new FormData(ev.target);
    const filename = input.files[0].name;
    const supplier = String(form.get("supplier") || "").trim();
    if (!supplier || !confirm("Импортировать " + filename + " для поставщика «" + supplier + "»? Новые КМ не заказываются, PDF не создаётся.")) return;
    submit.disabled = true;
    try {
      const result = await api("/admin/legacy/import", form);
      toast("Накладная " + result.number + " импортирована: " + result.codes + " КМ", "success");
      await open(result.id);
    } catch(err) {
      submit.disabled = false;
      toast(err.message);
    }
  };
  $("#admin-doc-search-form").onsubmit = event => {
    event.preventDefault();
    void loadAdminDocuments();
  };
}
async function adminPage(freshOverview = null) {
  registryMode = false;

  if (!user.admin) {
    login(true);
    return;
  }
  adminMode = true;
  documentDirty = false;
  doc = null;
  overview = freshOverview || await api("/admin/overview");
  const c = overview.config;
  let hb;
  try {
    hb = JSON.parse(overview.heartbeat?.value || "null");
  } catch {}
  shell(
    `<div class="topbar"><div><div class="muted">УПРАВЛЕНИЕ СИСТЕМОЙ</div><h1>Администрирование</h1></div><button data-action="admin-refresh">Обновить</button></div>${!overview.writes_enabled ? '<div class="notice">Внешняя отправка отключена в конфигурации сервера. Подготовленные задания сохраняются в очереди.</div>' : ""}<div class="stats"><div class="stat"><span class="muted">Свободно на диске</span><b>${(overview.disk.free / 1024 ** 3).toFixed(1)} ГБ</b></div><div class="stat"><span class="muted">Windows-подписант</span><b>${hb && Date.now() - new Date(hb.time) < 90000 ? "На связи" : "Нет связи"}</b><span class="muted">${hb ? e(hb.status) + " · " + date(hb.time) : "Подключите подписант"}</span></div><div class="stat"><span class="muted">Резервная копия</span><b>${overview.last_backup ? date(JSON.parse(overview.last_backup.value).created) : "Нет данных"}</b></div></div><div class="split"><div class="card" data-admin-panel="users"><h2>Пользователи</h2><div class="table-wrap"><table>${overview.users.map((u) => `<tr><td>${e(u.username)}</td><td><select class="user-role" data-user="${e(u.username)}" aria-label="Роль ${e(u.username)}"><option value="manager" ${u.role === "manager" ? "selected" : ""}>Бренд-менеджер</option><option value="logistics" ${u.role === "logistics" ? "selected" : ""}>Логист</option></select></td><td>${u.enabled ? "Активен" : "Отключён"}<span class="cell-sub">${u.password_set ? "Пароль установлен" : u.setup_pending ? "Ожидает установки пароля" : "Нужен код первого входа"}</span></td><td><button data-action="reset-password" data-id="${e(u.username)}">Сбросить пароль</button> <button data-action="user-toggle" data-id="${e(u.username)}" data-enabled="${u.enabled ? "0" : "1"}">${u.enabled ? "Отключить" : "Включить"}</button></td></tr>`).join("")}</table></div><form id="user-form"><div class="toolbar"><input id="username" placeholder="Новый username" required><select id="new-user-role" aria-label="Роль нового пользователя"><option value="manager">Бренд-менеджер</option><option value="logistics">Логист</option></select><button>Добавить</button></div></form></div><div class="card" data-admin-panel="connections"><h2>Подключения и подпись</h2><p>Сертификат: ${overview.signer_configured ? "Настроен" : "Нужны токен подписанта и отпечаток сертификата в конфигурации сервера"}</p>${["true", "oms"].map((k) => `<p>${k === "true" ? "ГИС МТ" : "СУЗ"}: ${overview.tokens[k] ? "Токен получен · срок " + e(overview.tokens[k].expires || "не указан ЧЗ") : "Требуется авторизация"} <button data-action="auth" data-id="${k}">Обновить доступ</button></p>`).join("")}<p class="muted">Физический Рутокен остаётся на Windows-ПК. При запросе PIN задания ждут его ввода на этом ПК.</p><a href="https://docs.crpt.ru/gismt/True_API/" target="_blank" rel="noreferrer">Документация True API</a></div></div><div class="card" data-admin-panel="organization"><h2>Наша организация</h2><form id="config-form"><div class="fields">${[
      ["organization", "Наименование"],
      ["inn", "ИНН"],
      ["oms_id", "OMS ID"],
      ["oms_connection", "omsConnection"],
      ["kpp", "КПП места деятельности"],
      ["fias_id", "ФИАС места деятельности"],
    ]
      .map(
        ([key, label]) =>
          `<label>${label}<input name="${key}" value="${e(c[key])}" ${["organization", "inn", "oms_id", "oms_connection"].includes(key) ? "required" : ""}></label>`,
      )
      .join(
        "",
      )}<label>Шаблон кода<select name="template_id"><option value="36" ${c.template_id === 36 ? "selected" : ""}>36 · стандартный</option><option value="37" ${c.template_id === 37 ? "selected" : ""}>37 · укороченный</option></select></label></div><div class="actions"><button class="primary">Сохранить настройки</button></div></form></div><div class="card" data-admin-panel="operations"><h2>Очередь подписи и внешних операций</h2><div class="table-wrap"><table><tr><th>Операция</th><th>Состояние</th><th>Внешний ID</th><th>Пояснение</th><th></th></tr>${overview.operations.map(renderOperationRow).join("")}</table></div></div><div class="card" data-admin-panel="approvals"><h2>Заявки на трансформацию АТК</h2>${overview.transformations.length ? overview.transformations.map((t) => `<div class="history"><b>${t.kind === "ADDING" ? "Добавить" : "Изъять"} КМ · ${e(t.actor)}</b>${t.stopped ? badge("cancelled") : badge(t.state)} · ${date(t.created)}<div class="mono">ID заявки: ${e(t.id)}<br>КМ: ${e(JSON.parse(t.code_ids).join(", "))}</div>${t.state === "approval" && !t.stopped ? `<div class="actions"><button data-action="review" data-id="${e(t.id)}">Рассмотреть состав и принять решение</button></div>` : ""}</div>`).join("") : '<p class="muted">Нет заявок.</p>'}</div><div class="split"><div class="card" data-admin-panel="catalog"><h2>Общий пул кодов</h2><table><tr><th>GTIN</th><th>Статус ЧЗ</th><th>Состояние</th><th>КМ</th></tr>${overview.pool.map((p) => `<tr><td>${e(p.gtin)}</td><td>${e(p.external_status)}</td><td>${e(states[p.local_state] || p.local_state)}</td><td>${fmt(p.count)}</td></tr>`).join("")}</table><details><summary>Исправить привязку ТН ВЭД</summary><form id="catalog-form"><div class="fields"><label>GTIN<input name="gtin" required></label><label>ТН ВЭД<input name="tnved" required></label><label class="full">Причина<input name="reason" required></label></div><div class="actions"><button>Исправить справочник</button></div></form><p class="muted">Исторические документы остаются без изменений.</p></details>${overview.removed.map((c) => `<p class="mono">${e(c.cis)} <button data-action="allow-reuse" data-id="${e(c.id)}">Разрешить подбор в новый АТК</button></p>`).join("")}</div><div class="card" data-admin-panel="suppliers"><h2>Поставщики</h2>${overview.suppliers
      .filter((s) => !s.merged_into)
      .map(
        (s) =>
          `<div class="toolbar"><span>${e(s.name)}</span><button data-action="supplier-edit" data-id="${e(s.id)}">Изменить</button></div>`,
      )
      .join(
        "",
      )}<p class="muted">Объединение сохраняет исходные партии, АТК и версии файлов. Новые документы используют единый справочник.</p></div></div><div class="card" data-admin-panel="audit"><h2>Журнал действий</h2><div class="table-wrap"><table><tr><th>Когда</th><th>Username</th><th>Действие</th></tr>${overview.audit.map((a) => `<tr><td>${date(a.created)}</td><td>${e(a.actor)}</td><td>${e(a.action)}</td></tr>`).join("")}</table></div></div>`,
  );
  setupAdminDocumentPanel();
  organizeAdmin(hb);
  $$(".user-role").forEach(
    (select) =>
      (select.onchange = async () => {
        const u = overview.users.find(
          (x) => x.username === select.dataset.user,
        );
        try {
          await api("/admin/users", {
            username: u.username,
            enabled: !!u.enabled,
            role: select.value,
          });
          await adminPage();
          toast("Роль пользователя обновлена", "success");
        } catch (err) {
          select.value = u.role;
          toast(err.message);
        }
      }),
  );
  $("#user-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const username = $("#username").value;
      const result = await api("/admin/users", {
        username,
        enabled: true,
        role: $("#new-user-role").value,
      });
      await adminPage();
      if (result.setup) showSetupCode(username, result.setup);
    } catch (err) {
      toast(err.message);
    }
  };
  $("#config-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const body = Object.fromEntries(new FormData(ev.target));
    body.template_id = Number(body.template_id);
    try {
      await api("/admin/config", body, "PUT");
      toast("Настройки сохранены", "success");
    } catch (err) {
      toast(err.message);
    }
  };
  $("#catalog-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api("/admin/catalog", Object.fromEntries(new FormData(ev.target)));
      toast("Справочник исправлен", "success");
    } catch (err) {
      toast(err.message);
    }
  };
}
function reasonModal(title, callback) {
  modal(
    `<h2>${e(title)}</h2><form id="reason-form"><label>Причина<input id="reason" required maxlength="1000"></label><div class="actions"><button class="primary">Подтвердить</button><button type="button" data-action="close">Отмена</button></div></form>`,
  );
  $("#reason-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await callback($("#reason").value);
      $("#modal").close();
      await adminPage();
    } catch (err) {
      toast(err.message);
    }
  };
}
async function action(name, id, b) {
  switch (name) {
    case "upload-legacy-pdf":
      $("#legacy-pdf-file")?.click();
      break;
    case "reassign-document": {
      const row = b.closest("tr");
      const owner = row?.querySelector("select")?.value;
      if (!owner) throw new Error("Выберите нового ответственного");
      const number = row.dataset.number;
      const previous = row.dataset.owner;
      if (!confirm("Передать накладную " + number + " от " + previous + " сотруднику " + owner + "?")) break;
      await api("/admin/documents/" + encodeURIComponent(id) + "/owner", {owner}, "PUT");
      await loadAdminDocuments();
      toast("Накладная " + number + " передана сотруднику " + owner, "success");
      break;
    }
    case "admin-section":
      selectAdminSection(id, b.dataset.operationFilter);
      break;
    case "document-tab":
      selectDocumentTab(id);
      break;
    case "reset-doc-filters":
      documentFilters = {search: "", status: "", supplier: "", from: "", to: "", stage: "all"};
      renderList();
      break;
    case "logistics":
      await logisticsPage();
      break;
    case "registry-refresh":
      await logisticsPage();
      break;
    case "logistics-complete": {
      const item = registryRows.flatMap(a => a.documents).find(d => d.id === id);
      if (!item) throw new Error("Обновите реестр перед завершением накладной");
      modal(`<h2>Завершить накладную ${e(item.number)}?</h2><p>Накладная будет закрыта и перемещена в архив. АТК и итоговые файлы останутся доступны в архивном реестре.</p><div class="actions"><button class="primary" data-action="confirm-logistics-complete" data-id="${e(id)}">Завершить накладную</button><button data-action="close">Отмена</button></div>`);
      break;
    }
    case "confirm-logistics-complete":
      await api("/logistics/documents/" + encodeURIComponent(id) + "/complete", {});
      $("#modal").close();
      await logisticsPage();
      toast("Накладная завершена и перемещена в архив", "success");
      break;
    case "registry-detail":
      await logisticsDetail(id);
      break;
    case "registry-reset":
      registryFilters = {
        q: "",
        supplier: "",
        status: "",
        tnved: "",
        date_from: "",
        date_to: "",
        archive: false,
      };
      await logisticsPage();
      break;
    case "registry-download":
      await downloadRegistry($$(".atk-select:checked").map((x) => x.value));
      break;
    case "refresh-page":
      if (registryMode) await logisticsPage();
      else if (adminMode) await adminPage();
      else if (doc) await open(doc.id);
      else await list();
      break;
    case "close":
      if ($("#excel-mapping-form")) excelImportVersion++;
      $("#modal").close();
      break;
    case "admin-login":
      login(true);
      break;
    case "user-login":
      login();
      break;
    case "setup-login":
      login(false, true);
      break;
    case "reset-password": {
      if (!confirm("Сбросить пароль пользователя " + id + "? Все его сессии завершатся.")) break;
      const setup = await api("/admin/users/" + encodeURIComponent(id) + "/reset-password", {});
      await adminPage();
      showSetupCode(id, setup);
      break;
    }
    case "copy-setup-code": {
      const code = $("#setup-code-issued")?.textContent || "";
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(code);
      } else {
        // Corporate HTTP environments may not allow Clipboard API.
        const field = document.createElement("textarea");
        field.value = code;
        field.style.position = "fixed";
        field.style.opacity = "0";
        document.body.append(field);
        field.select();
        const copied = document.execCommand("copy");
        field.remove();
        if (!copied) throw new Error("Не удалось скопировать. Выделите код вручную.");
      }
      toast("Код скопирован", "success");
      break;
    }
    case "change-password":
      await changeOwnPassword();
      break;
    case "logout":
      await api("/session", null, "DELETE");
      user = null;
      login();
      break;
    case "list":
      archive = false;
      await list();
      break;
    case "archive":
      archive = true;
      await list();
      break;
    case "refresh":
      await list();
      break;
    case "admin":
      user.admin ? await adminPage() : login(true);
      break;
    case "admin-refresh":
      await adminPage();
      break;
    case "stop-document": {
      const context = {id: doc.id, number: doc.number};
      modal(`<h2>Остановить и архивировать накладную?</h2><p>Будут остановлены очередь, подпись и дальнейшая обработка накладной <b>${e(context.number)}</b>. Общие операции АТК с её участием также будут приостановлены.</p><p class="notice">Уже отправленные операции ЧЗ не отменяются. Заказанные КМ и их оплата не откатываются. Файлы и история сохранятся. Вернуть документ сможет только администратор.</p><form id="stop-document-form"><label>Введите точный номер накладной<input id="stop-document-number" autocomplete="off" required maxlength="120"></label><div id="stop-document-error" class="error" role="alert" hidden></div><div class="actions"><button id="stop-document-submit" class="danger" disabled>Остановить и архивировать</button><button type="button" data-action="close">Отмена</button></div></form>`);
      $("#stop-document-number").oninput = () => $("#stop-document-submit").disabled = $("#stop-document-number").value !== context.number;
      $("#stop-document-form").onsubmit = async event => {
        event.preventDefault();
        const button = $("#stop-document-submit");
        button.disabled = true;
        try {
          await api("/documents/" + encodeURIComponent(context.id) + "/stop", {number: $("#stop-document-number").value});
          $("#modal").close();
          documentDirty = false;
          await open(context.id);
          toast("Обработка остановлена. Накладная перемещена в архив", "success");
        } catch (error) {
          $("#stop-document-error").hidden = false;
          $("#stop-document-error").textContent = error.message;
          button.disabled = $("#stop-document-number").value !== context.number;
        }
      };
      break;
    }
    case "restore-document": {
      const id = doc.id;
      modal(`<h2>Вернуть накладную из архива?</h2><p>Неотправленные задания потребуют новой подписи. Ранее отправленные операции и неизвестные результаты нужно проверить в админке перед продолжением.</p><form id="restore-document-form"><label>Причина возврата<input name="reason" required maxlength="1000"></label><div id="restore-document-error" class="error" role="alert" hidden></div><div class="actions"><button class="primary">Вернуть в работу</button><button type="button" data-action="close">Отмена</button></div></form>`);
      $("#restore-document-form").onsubmit = async event => {
        event.preventDefault();
        const button = event.target.querySelector('button[type="submit"], button.primary');
        button.disabled = true;
        try {
          await api("/admin/documents/" + encodeURIComponent(id) + "/restore", Object.fromEntries(new FormData(event.target)));
          $("#modal").close();
          await open(id);
          toast("Накладная возвращена из архива", "success");
        } catch (error) {
          $("#restore-document-error").hidden = false;
          $("#restore-document-error").textContent = error.message;
          button.disabled = false;
        }
      };
      break;
    }
    case "new":
      await newDoc();
      break;
    case "supplement":
      await newDoc(doc.id);
      break;
    case "open":
      await open(id);
      break;
    case "refresh-doc":
      await open(doc.id);
      break;
    case "add-line":
      $("#edit-rows").insertAdjacentHTML("beforeend", lineRow({}, !!$("#edit-rows .move-line")));
      markDocumentDirty();
      break;
    case "remove-line":
      b.closest("tr").remove();
      markDocumentDirty();
      break;
    case "save-lines":
      await saveRows(gatherRows());
      break;
    case "move-lines": {
      if (documentDirty) throw new Error("Сначала сохраните изменения строк");
      const ids = $$(".move-line:checked").map(input => Number(input.value));
      if (!ids.length) throw new Error("Отметьте галочками строки, которые нужно перенести");
      await moveLinesModal(ids);
      break;
    }
    case "import":
      $("#excel-file").click();
      break;
    case "order":
      if (documentDirty) throw new Error("Сначала проверьте и сохраните состав");
      modal(
        `<h2>Заказать коды маркировки?</h2><p>Будут использованы свободные КМ из общего пула. В СУЗ отправится заказ только на недостающее количество. Новые КМ оплачиваются по эмиссии.</p><p class="notice">Заказ использует сохранённый состав. Проверьте, что все изменения сохранены.</p><div class="actions"><button class="primary" data-action="confirm-order">Заказать КМ</button><button data-action="close">Отмена</button></div>`,
      );
      break;
    case "confirm-order":
      doc = await api("/documents/" + doc.id + "/order", {});
      $("#modal").close();
      renderDoc();
      break;
    case "transfer":
      await api("/documents/" + doc.id + "/transferred", {
        version: doc.current_version,
      });
      await open(doc.id);
      break;
    case "report":
      await batchModal([doc.id]);
      break;
    case "batch":
      await batchModal($$(".doc-select:checked").map((x) => x.value));
      break;
    case "atk":
      await atkModal(id);
      break;
    case "transform-add":
      await transformModal(id, "ADDING");
      break;
    case "transform-remove":
      await transformModal(id, "REMOVING");
      break;
    case "user-toggle":
      await api("/admin/users", {
        username: id,
        enabled: b.dataset.enabled === "1",
      });
      await adminPage();
      break;
    case "auth":
      await api("/admin/auth/" + id, {});
      await adminPage();
      toast("Авторизация ожидает подписи на Windows-ПК", "success");
      break;
    case "retry-local":
      if (!confirm("Это локальная ошибка до отправки в ЧЗ. Повторить операцию после исправления подключения?")) break;
      await api("/admin/operations/" + id + "/retry-local", {});
      await adminPage();
      toast("Операция повторно поставлена в очередь", "success");
      break;
    case "retry-report":
      if (!confirm("Отправить новый отчёт? При частичной обработке будут включены только КМ, которые ГИС МТ подтвердит как ещё не нанесённые.")) break;
      await api("/admin/operations/" + id + "/retry", {});
      await adminPage();
      toast("Новый отчёт ожидает подписи на Windows-ПК", "success");
      break;
    case "retry-report-doc":
      if (!confirm("Повторить отчёт после проверки карточек в НК? Уже нанесённые КМ не будут отправлены повторно.")) break;
      await api("/documents/" + encodeURIComponent(doc.id) + "/report/" + encodeURIComponent(id) + "/retry", {});
      await open(doc.id);
      toast("Повторная отправка ожидает подписи на Windows-ПК", "success");
      break;
    case "resolve-op": {
      const reason = prompt("Причина скрытия предупреждения (операция в ЧЗ останется без изменений):", "Рассмотрено администратором");
      if (reason === null) break;
      await api("/admin/operations/" + id + "/attention", {resolved: true, reason});
      await adminPage();
      toast("Предупреждение скрыто. История операции сохранена", "success");
      break;
    }
    case "unresolve-op":
      await api("/admin/operations/" + id + "/attention", {resolved: false});
      await adminPage();
      toast("Предупреждение возвращено в список", "success");
      break;
    case "check-op":
      await api("/admin/operations/" + id + "/check", {});
      await adminPage();
      break;
    case "recover-block": {
      const blocks = await api("/admin/operations/" + id + "/blocks");
      modal(
        `<h2>Восстановление блока СУЗ</h2><p>Выберите блок, появившийся после прерванного получения. Исходный запрос получения не повторяется. Если заказ уже закрыт СУЗ, восстановление может быть недоступно.</p><form id="block-form"><label>Блок<select name="block_id" required>${blocks.map((b) => `<option value="${e(b.blockId)}">${e(b.blockId)} · ${b.quantity} КМ</option>`).join("")}</select></label><label>Основание сверки<input name="reason" required></label><div class="actions"><button class="primary">Восстановить выбранный блок</button><button type="button" data-action="close">Отмена</button></div></form>`,
      );
      $("#block-form").onsubmit = async (ev) => {
        ev.preventDefault();
        try {
          await api(
            "/admin/operations/" + id + "/recover-block",
            Object.fromEntries(new FormData(ev.target)),
          );
          $("#modal").close();
          await adminPage();
        } catch (err) {
          toast(err.message);
        }
      };
      break;
    }
    case "reconcile-op":
      modal(
        `<h2>Сверка неизвестного результата</h2><p>Найдите операцию в ЛК ЧЗ и сверьте точный состав, количество и время. Это действие связывает запись с найденной операцией; повторная отправка не выполняется.</p><form id="reconcile-form"><label>Внешний ID<input name="external_id" required></label><label>Основание сверки<input name="reason" required></label><div class="actions"><button class="primary">Привязать и проверить</button><button type="button" data-action="close">Отмена</button></div></form>`,
      );
      $("#reconcile-form").onsubmit = async (ev) => {
        ev.preventDefault();
        try {
          await api(
            "/admin/operations/" + id + "/reconcile",
            Object.fromEntries(new FormData(ev.target)),
          );
          $("#modal").close();
          await adminPage();
        } catch (err) {
          toast(err.message);
        }
      };
      break;
    case "allow-reuse":
      reasonModal("Разрешить повторное включение изъятого КМ", (reason) =>
        api("/admin/codes/" + id + "/allow-reuse", { approve: true, reason }),
      );
      break;
    case "review": {
      const t = overview.transformations.find((t) => t.id === id);
      const a = await api("/atks/" + t.atk_id);
      const current = new Map(a.codes.map((c) => [c.id, c]));
      if (t.kind === "ADDING")
        for (const c of await api("/atks/" + t.atk_id + "/candidates"))
          current.set(c.id, c);
      modal(
        `<h2>Согласование трансформации</h2><p>АТК: ${e(a.code)} · ${t.kind === "ADDING" ? "Добавить" : "Изъять"} КМ</p><table><tr><th>КИ</th><th>PN</th><th>Штук</th></tr>${JSON.parse(
          t.code_ids,
        )
          .map((i) => {
            const c = current.get(i);
            return `<tr><td class="mono">${e(c?.cis || i)}</td><td>${e(c?.pn || "Недоступен для операции")}</td><td>${e(c?.quantity || "—")}</td></tr>`;
          })
          .join(
            "",
          )}</table><form id="decision-form"><label>Решение<select id="decision"><option value="1">Одобрить</option><option value="0">Отклонить</option></select></label><label>Причина<input id="decision-reason" required></label><div class="actions"><button class="primary">Сохранить решение</button><button type="button" data-action="close">Отмена</button></div></form>`,
      );
      $("#decision-form").onsubmit = async (ev) => {
        ev.preventDefault();
        try {
          await api("/admin/transformations/" + id, {
            approve: $("#decision").value === "1",
            reason: $("#decision-reason").value,
          });
          $("#modal").close();
          await adminPage();
        } catch (err) {
          toast(err.message);
        }
      };
      break;
    }
    case "supplier-edit": {
      const s = overview.suppliers.find((s) => s.id === id);
      modal(
        `<h2>Поставщик</h2><form id="supplier-form"><label>Наименование<input name="name" value="${e(s.name)}" required maxlength="160"></label><label>Объединить с поставщиком<select name="merge_into"><option value="">Не объединять</option>${overview.suppliers
          .filter((x) => x.id !== id && !x.merged_into)
          .map((x) => `<option value="${e(x.id)}">${e(x.name)}</option>`)
          .join(
            "",
          )}</select></label><div class="actions"><button class="primary">Сохранить</button><button type="button" data-action="close">Отмена</button></div></form>`,
      );
      $("#supplier-form").onsubmit = async (ev) => {
        ev.preventDefault();
        const body = Object.fromEntries(new FormData(ev.target));
        body.id = id;
        body.merge_into = body.merge_into || null;
        try {
          await api("/admin/suppliers", body);
          $("#modal").close();
          await adminPage();
        } catch (err) {
          toast(err.message);
        }
      };
      break;
    }
  }
}
async function logisticsPage() {
  if (!user.admin && user.role !== "logistics") return list();
  documentDirty = false;
  registryMode = true;
  adminMode = false;
  archive = false;
  doc = null;
  history.replaceState(null, "", location.pathname + "#logistics");
  const generation = ++registryRequest;
  const params = new URLSearchParams(registryFilters);
  const [items, suppliers] = await Promise.all([
    api("/logistics/atks?" + params),
    api("/suppliers"),
  ]);
  if (generation !== registryRequest || !registryMode) return;
  registryRows = items;
  const registryLabels = {
    done: "Готов",
    processing: "Формируется",
    attention: "Требует проверки",
    transforming: "Трансформация",
    stopped: "Обработка остановлена",
  };
  shell(
    `<div class="topbar"><div><div class="eyebrow">Логистика · Все сотрудники</div><h1>Реестр АТК</h1><p class="muted">Таможенные коды, состав и итоговые файлы по всем поставщикам</p></div><button data-action="registry-refresh">Обновить реестр</button></div><div class="stats"><div class="stat"><span class="muted">АТК в списке</span><b>${fmt(items.length)}</b></div><div class="stat"><span class="muted">Готовы к скачиванию</span><b>${fmt(items.filter((a) => a.download_ready).length)}</b></div><div class="stat"><span class="muted">Единиц товара</span><b>${fmt(items.reduce((n, a) => n + a.units, 0))}</b></div></div><section class="card"><form id="registry-filters" class="registry-filters"><label>Код АТК или номер накладной<input id="atk-search" name="q" placeholder="Поиск по АТК или накладной" value="${e(registryFilters.q)}"></label><label>Поставщик<select id="atk-supplier" name="supplier"><option value="">Все поставщики</option>${suppliers.map((s) => `<option value="${e(s.id)}" ${s.id === registryFilters.supplier ? "selected" : ""}>${e(s.name)}</option>`).join("")}</select></label><label>Статус<select id="atk-status" name="status"><option value="">Все статусы</option>${Object.entries(
      registryLabels,
    )
      .map(
        ([value, label]) =>
          `<option value="${value}" ${value === registryFilters.status ? "selected" : ""}>${label}</option>`,
      )
      .join(
        "",
      )}</select></label><label>ТН ВЭД<input id="atk-tnved" name="tnved" inputmode="numeric" maxlength="10" value="${e(registryFilters.tnved)}" placeholder="Полный код или часть"></label><div class="filter-row"><label>Создан с<input id="atk-from" type="date" name="date_from" value="${e(registryFilters.date_from)}"></label><label>Создан по<input id="atk-to" type="date" name="date_to" value="${e(registryFilters.date_to)}"></label><label class="check-label"><input id="atk-archive" name="archive" type="checkbox" ${registryFilters.archive ? "checked" : ""}>Архивные АТК</label><button class="primary" type="submit">Применить фильтры</button><button type="button" data-action="registry-reset">Сбросить</button></div></form><div class="table-wrap"><table id="atk-table"><thead><tr><th><input id="atk-select-all" type="checkbox" aria-label="Выбрать готовые АТК"></th><th>АТК / поставщик</th><th>Накладные / сотрудники</th><th>ТН ВЭД</th><th>Штук / КМ</th><th>Статус / создан</th><th>Файлы</th><th>Завершение</th></tr></thead><tbody>${items.length ? items.map((a) => `<tr><td><input type="checkbox" class="atk-select" value="${e(a.id)}" ${a.download_ready ? "" : "disabled"} aria-label="Выбрать АТК ${e(a.code || a.id)}"></td><td><button class="linkbutton registry-code" data-action="registry-detail" data-id="${e(a.id)}">${a.code ? e(a.code) : "АТК формируется"}</button><span class="cell-sub">${e(a.supplier)}</span></td><td>${a.invoices.map(e).join(", ")}<span class="cell-sub">${a.owners.map(e).join(", ")}</span></td><td>${e(a.tnved)}</td><td><b>${fmt(a.units)}</b><span class="cell-sub">${fmt(a.labels)} КМ</span></td><td><span class="badge ${e(a.status)}">${registryLabels[a.status]}</span><span class="cell-sub">${date(a.created)}</span>${a.error ? `<span class="cell-sub">${e(a.error)}</span>` : ""}</td><td><div class="download-actions">${a.files.map((f) => `<a href="/api/files/${e(f.id)}" class="button" title="${e(f.name)}">${f.kind === "atk" ? "АТК" : "RESULT"} · v${f.version}</a>`).join("")}</div>${a.download_ready ? "" : '<span class="cell-sub">Комплект ещё не готов</span>'}</td><td>${a.documents.some(d => d.can_complete) ? `<button data-action="registry-detail" data-id="${e(a.id)}">${a.documents.length === 1 ? "Завершить накладную" : "Завершить накладные"}</button>` : a.status === "stopped" ? '<span class="badge cancelled">Остановлена</span>' : a.archived ? '<span class="badge done">Завершены</span>' : '<span class="muted">Ожидание готовности</span>'}</td></tr>`).join("") : ""}</tbody></table></div>${items.length ? "" : '<div class="empty"><b>АТК не найдены</b>Измените фильтры или дождитесь формирования таможенных кодов.</div>'}<div class="actions"><button class="primary" data-action="registry-download" disabled>Скачать выбранные · ZIP</button><span class="muted">Комплект АТК + RESULT на поставщика. Общие файлы включаются один раз.</span></div></section>`,
  );
  $("#registry-filters").onsubmit = async (ev) => {
    ev.preventDefault();
    registryFilters = {
      q: $("#atk-search").value.trim(),
      supplier: $("#atk-supplier").value,
      status: $("#atk-status").value,
      tnved: $("#atk-tnved").value.trim(),
      date_from: $("#atk-from").value,
      date_to: $("#atk-to").value,
      archive: $("#atk-archive").checked,
    };
    try {
      await logisticsPage();
    } catch (err) {
      toast(err.message);
    }
  };
  for (const selector of ["#atk-supplier", "#atk-status", "#atk-archive"])
    $(selector).onchange = () => $("#registry-filters").requestSubmit();
  function updateSelection() {
    $('[data-action="registry-download"]').disabled = !$(".atk-select:checked");
  }
  $$(".atk-select").forEach((input) => (input.onchange = updateSelection));
  $("#atk-select-all").onchange = () => {
    $$(".atk-select:not(:disabled)").forEach(
      (input) => (input.checked = $("#atk-select-all").checked),
    );
    updateSelection();
  };
}
async function logisticsDetail(id) {
  const a = await api("/logistics/atks/" + encodeURIComponent(id));
  modal(
    `<div class="eyebrow">Логистика · Накладные и АТК</div><h2>${a.code ? "АТК " + e(a.code) : "АТК формируется"}</h2><p>${e(a.supplier)} · ТН ВЭД ${e(a.tnved)} · версия состава ${a.version}</p><p class="muted">Накладные: ${a.invoices.map(e).join(", ")}<br>Сотрудники: ${a.owners.map(e).join(", ")}</p><div class="files">${a.files.map((f) => `<a href="/api/files/${e(f.id)}">${e(f.name)} · v${f.version}</a>`).join("")}</div><h3>Накладные</h3><div class="table-wrap"><table><thead><tr><th>Номер</th><th>Сотрудник</th><th>Завершение</th></tr></thead><tbody>${a.documents.map(d => `<tr><td>${e(d.number)}</td><td>${e(d.owner)}</td><td>${d.can_complete ? `<button class="primary" data-action="logistics-complete" data-id="${e(d.id)}">Завершить накладную</button>` : d.stopped ? '<span class="badge cancelled">Остановлена · архив</span>' : d.status === "archive" ? '<span class="badge done">Завершена · архив</span>' : '<span class="muted">Ожидание готовности</span>'}</td></tr>`).join("")}</tbody></table></div><h3>Состав: ${fmt(a.labels)} КМ, ${fmt(a.units)} шт.</h3><div class="table-wrap"><table><thead><tr><th>КИ</th><th>PN</th><th>Штук</th></tr></thead><tbody>${a.codes.map((c) => `<tr><td class="mono">${e(c.cis)}</td><td>${e(c.pn)}</td><td>${fmt(c.quantity)}</td></tr>`).join("")}</tbody></table></div><details><summary>Предыдущие версии файлов</summary><div class="files">${a.history_files.map((f) => `<a href="/api/files/${e(f.id)}">${e(f.name)} · v${f.version}</a>`).join("")}</div></details><div class="actions"><button data-action="close">Закрыть</button></div>`,
  );
}
async function downloadRegistry(ids) {
  if (!ids.length) throw new Error("Выберите готовые АТК для скачивания");
  const response = await fetch("/api/logistics/download", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "marking-bm",
      "X-CSRF-Token": user.csrf,
    },
    body: JSON.stringify({ atks: ids }),
  });
  if (!response.ok) {
    const value = await response.json();
    throw new Error(value.detail || "Не удалось скачать файлы");
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const encodedFilename = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  let filename = "АТК-и-RESULT.zip";
  if (encodedFilename) {
    try {
      filename = decodeURIComponent(encodedFilename[1]);
    } catch {
      // Keep a safe generic fallback if a proxy sends a malformed header.
    }
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("#modal").addEventListener("cancel", () => {
  if ($("#excel-mapping-form")) excelImportVersion++;
});
bind();
refreshTimer = setInterval(pollStatuses, STATUS_POLL_MS);
(async () => {
  try {
    user = await api("/session");
    if (user.role === "logistics" && !user.admin) await logisticsPage();
    else if (location.hash.startsWith("#logistics")) await logisticsPage();
    else if (["#documents", "#archive"].includes(location.hash)) {
      archive = location.hash === "#archive";
      await list();
    }
    else if (location.hash.startsWith("#admin/") && user.admin) {
      adminSection = location.hash.slice(7);
      await adminPage();
    }
    else if (location.hash.startsWith("#document/"))
      await open(decodeURIComponent(location.hash.slice(10)));
    else adminMode && user.admin ? await adminPage() : await list();
  } catch {
    login(adminMode);
  }
})();
