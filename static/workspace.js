"use strict";
// Presentation and navigation only. All mutations use the existing protected API.
const documentStages = {
  all: {title: "Все", matches: () => true},
  preparation: {title: "Подготовка", matches: d => ["draft", "repack"].includes(d.status)},
  processing: {title: "В обработке", matches: d => ["ordering", "receiving", "reporting", "reported", "aggregating"].includes(d.display_status || d.status)},
  supplier: {title: "Поставщик", matches: d => ["ready", "transferred"].includes(d.status)},
  attention: {title: "Требуют проверки", matches: d => ["attention", "label_error", "report_error", "preflight_blocked", "unknown", "blocked"].includes(d.display_status || d.status)},
  done: {title: "Завершены", matches: d => !d.stopped && ["done", "archive"].includes(d.status)},
  stopped: {title: "Остановлены", matches: d => !!d.stopped},
};
const adminSections = {
  overview: ["Обзор", "Состояние системы и задачи, требующие внимания"],
  operations: ["Операции", "Очередь подписи, отправка и проверка результатов ЧЗ"],
  approvals: ["Согласования", "Решения по добавлению и изъятию КМ из АТК"],
  users: ["Пользователи", "Учётные записи, роли и доступ сотрудников"],
  documents: ["Документы", "Назначение ответственных за накладные и передача между сотрудниками"],
  organization: ["Организация", "Реквизиты, место деятельности и параметры эмиссии"],
  connections: ["Подключения", "Доступ к ГИС МТ, СУЗ и Windows-подписант"],
  suppliers: ["Поставщики", "Наименования и объединение записей справочника"],
  catalog: ["Коды и справочник", "Общий пул КМ, привязки GTIN и повторный подбор"],
  audit: ["Журнал действий", "История действий сотрудников и администратора"],
};
function organizeAdmin(hb) {
  const main = $("main");
  const stats = main.querySelector(".stats");
  const notice = main.querySelector(":scope > .notice");
  const attention = overview.operations.filter(o => ["unknown", "blocked", "preflight_blocked", "rejected", "partial"].includes(o.state) && !o.attention_resolved_at);
  const approvals = overview.transformations.filter(t => t.state === "approval");
  const pending = overview.operations.filter(o => !["done", "rejected", "cancelled"].includes(o.state) && !o.stopped);
  const overviewPanel = document.createElement("section");
  overviewPanel.dataset.adminPanel = "overview";
  overviewPanel.innerHTML = `<div class="section-heading"><h2>Состояние системы</h2><p class="muted">Начните с задач, требующих решения. Настройки и история доступны в разделах слева.</p></div><div class="overview-tasks">${[
    ["operations", "Требуют проверки", attention.length, "Проверить результаты внешних операций", attention.length > 0],
    ["approvals", "На согласовании", approvals.length, "Рассмотреть изменения состава АТК", approvals.length > 0],
    ["operations", "В очереди и обработке", pending.length, "Посмотреть подпись и отправку", false],
  ].map(([id,title,count,description,warn]) => `<button class="task-card ${warn ? "needs-attention" : ""}" data-action="admin-section" data-id="${id}" data-operation-filter="${warn ? "attention" : "pending"}"><span>${title}</span><b>${fmt(count)}</b><span class="muted">${description} →</span></button>`).join("")}</div>`;
  if (notice) overviewPanel.append(notice);
  overviewPanel.append(stats);
  const connected = hb && Date.now() - new Date(hb.time) < 90000;
  overviewPanel.insertAdjacentHTML("beforeend", `<section class="card"><h3>Подключения</h3><div class="health-row"><span>Windows-подписант</span><b class="${connected ? "status-ok" : "status-warning"}">${connected ? "На связи" : "Нет связи"}</b></div>${["true", "oms"].map(k => {
    const token = overview.tokens[k], expired = token?.expires && new Date(token.expires).getTime() <= Date.now();
    return `<div class="health-row"><span>${k === "true" ? "ГИС МТ" : "СУЗ"}</span><b class="${token && !expired ? "status-ok" : "status-warning"}">${!token ? "Требуется авторизация" : expired ? "Срок доступа истёк" : "Токен получен"}</b></div>`;
  }).join("")}<div class="actions"><button data-action="admin-section" data-id="connections">Управление подключениями</button></div></section>`);
  const layout = document.createElement("div");
  layout.className = "admin-workspace";
  layout.innerHTML = `<nav class="section-nav" aria-label="Разделы администрирования">${Object.entries(adminSections).map(([key,[title]]) => `<button data-action="admin-section" data-id="${key}">${title}${key === "operations" && attention.length ? `<span class="nav-count">${attention.length}</span>` : key === "approvals" && approvals.length ? `<span class="nav-count">${approvals.length}</span>` : ""}</button>`).join("")}</nav><div class="section-content"><div class="section-heading" id="admin-section-heading"></div></div>`;
  layout.insertAdjacentHTML("afterbegin", `<label class="mobile-section-select">Раздел администрирования<select id="admin-mobile-section">${Object.entries(adminSections).map(([key,[title]]) => `<option value="${key}">${title}</option>`).join("")}</select></label>`);
  layout.querySelector("#admin-mobile-section").onchange = event => selectAdminSection(event.target.value);
  const content = layout.querySelector(".section-content");
  content.append(overviewPanel);
  $$('[data-admin-panel]').filter(p => p !== overviewPanel).forEach(panel => content.append(panel));
  // Old two-column wrappers are empty after their sections have been moved.
  main.querySelectorAll(":scope > .split").forEach(wrapper => wrapper.remove());
  main.append(layout);
  const config = $("#config-form .fields");
  const groups = [
    ["Реквизиты организации", ["organization", "inn"]],
    ["Место деятельности", ["kpp", "fias_id"]],
    ["Эмиссия в СУЗ", ["oms_id", "oms_connection", "template_id"]],
  ];
  groups.forEach(([title, names]) => {
    const group = document.createElement("fieldset");
    group.className = "settings-group";
    group.innerHTML = `<legend>${title}</legend><div class="fields"></div>`;
    names.forEach(name => group.querySelector(".fields").append(config.querySelector(`[name="${name}"]`).closest("label")));
    config.before(group);
  });
  config.remove();
  // Explicit empty states and readable table headers on dedicated pages.
  const usersTable = $('[data-admin-panel="users"] table');
  usersTable.insertAdjacentHTML("afterbegin", "<thead><tr><th>Username</th><th>Роль</th><th>Доступ</th><th>Действие</th></tr></thead>");
  $("#username").setAttribute("aria-label", "Новый username");
  $("#user-form").insertAdjacentHTML("afterbegin", '<h3>Добавить сотрудника</h3>');
  for (const [key, count, message] of [
    ["operations", overview.operations.length, "Внешние операции ещё не запускались"],
    ["suppliers", overview.suppliers.filter(s => !s.merged_into).length, "Поставщики появятся при создании накладных"],
    ["catalog", overview.pool.length, "В общем пуле пока нет кодов"],
    ["audit", overview.audit.length, "Действий пока нет"],
  ]) if (!count) $(`[data-admin-panel="${key}"]`).insertAdjacentHTML("beforeend", `<p class="empty">${message}</p>`);
  setupAdminFilters();
  selectAdminSection(adminSection);
}
function selectAdminSection(key, operationFilter) {
  adminSection = adminSections[key] ? key : "overview";
  $$("[data-admin-panel]").forEach(panel => panel.hidden = panel.dataset.adminPanel !== adminSection);
  $$(".section-nav [data-id]").forEach(button => {
    button.classList.toggle("active", button.dataset.id === adminSection);
    if (button.dataset.id === adminSection) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("#admin-mobile-section").value = adminSection;
  const [title, description] = adminSections[adminSection];
  $("#admin-section-heading").innerHTML = `<p class="eyebrow">Администрирование / ${title}</p><p class="muted">${description}</p>`;
  if (adminSection === "operations" && operationFilter) {
    $("#operation-filter").value = operationFilter;
    $("#operation-filter").dispatchEvent(new Event("change"));
  }
  history.replaceState(null, "", location.pathname + "#admin/" + adminSection);
  if (adminSection === "documents") void loadAdminDocuments();
}
function organizeDocument(editable) {
  const main = $("main"), composition = $("#edit-rows").closest(".card");
  const flowActions = composition.querySelector(":scope > .actions:last-child");
  const guidance = {
    draft: ["Подготовьте состав накладной", "Загрузите Excel или добавьте строки, проверьте и сохраните состав. Затем закажите коды маркировки."],
    ordering: ["Заказ КМ обрабатывается", "Дождитесь подписи и получения полного комплекта кодов. Состояние операций — на вкладке «Обработка и файлы»."],
    receiving: ["Ожидаем полный комплект КМ", "Этикетки станут доступны после получения всех кодов. Обновите состояние через некоторое время."],
    ready: ["Передайте этикетки поставщику", "Скачайте актуальную версию этикеток и отметьте передачу поставщику."],
    transferred: ["Дождитесь подтверждения от поставщика", "После фактического нанесения этикеток подтвердите нанесение. Система запустит отчёт в СУЗ, после его принятия начнёт формирование АТК."],
    reporting: ["Отчёт о нанесении отправляется или обрабатывается", "Это длительный процесс. Статусы проверяются автоматически раз в 30 секунд, когда вы не редактируете состав. При отказе появится причина и действие для повторной отправки."],
    reported: ["Отчёт о нанесении принят", "Следующий этап — формирование АТК и подготовка итоговых файлов. Дождитесь завершения обработки."],
    aggregating: ["Формируется АТК", "Дождитесь завершения обработки в ЧЗ. Когда комплект будет готов, он появится в рабочем месте логиста."],
    repack: ["Формируем новые этикетки", "Старые PDF больше неактуальны. Новая версия появится после завершения подготовки. Повторно заказывать КМ не требуется."],
    done: ["Документ завершён", "АТК и итоговые файлы доступны ниже. Дополнительный товар оформите отдельным дополнением."],
    archive: ["Документ в архиве", "Состав и сохранённые версии файлов доступны для просмотра."],
    label_error: ["Исправьте состав для этикеток", "Проверьте сообщение об ошибке и сохраните исправленный состав. Полученные коды сохраняются."],
    report_error: ["Ошибка отчёта о нанесении", "СУЗ отклонила отчёт или обработала его частично. После проверки карточек в НК нажмите «Отправить повторно». Уже нанесённые КМ повторно отправлены не будут."],
  };
  const [title, description] = (doc.stopped ? ["Обработка остановлена", "Документ в архиве. Возврат доступен только администратору; отправленные операции требуют сверки."] : guidance[doc.display_status || doc.status]) || ["Документ в обработке", "Проверьте состояние на вкладке «Обработка и файлы». Дополнительные действия выполняются после завершения текущего этапа."];
  const next = document.createElement("section");
  next.className = "next-step";
  next.innerHTML = `<div><span class="eyebrow">Следующий шаг</span><h2>${title}</h2><p>${description}</p><span id="document-save-state" class="muted" role="status">${editable ? "Все изменения состава сохранены" : "Состав доступен для просмотра"}</span></div>`;
  flowActions.classList.add("workflow-actions");
  next.append(flowActions);
  composition.before(next);
  const nav = document.createElement("nav");
  nav.className = "workspace-tabs";
  nav.setAttribute("aria-label", "Разделы накладной");
  nav.innerHTML = [["content", "Состав"], ["processing", "Обработка и файлы"], ["history", "История"]].map(([key,label]) => `<button data-action="document-tab" data-id="${key}">${label}</button>`).join("");
  composition.before(nav);
  composition.dataset.documentPanel = "content";
  const split = main.querySelector(":scope > .split");
  const [processing, historyPanel] = split.children;
  processing.dataset.documentPanel = "processing";
  historyPanel.dataset.documentPanel = "history";
  // ATK results belong with operations and downloadable files.
  const result = composition.nextElementSibling;
  if (result !== split) {
    result.dataset.documentPanel = "processing";
  }
  split.replaceWith(processing, historyPanel);
  if (editable) {
    const save = $('[data-action="save-lines"]');
    save.classList.add("primary");
    if (!doc.lines.length) $("#edit-rows").insertAdjacentHTML("afterend", '<tbody id="composition-empty"><tr><td colspan="6"><div class="empty"><b>Добавьте товары</b>Загрузите Excel или добавьте строку вручную. При необходимости приложение предложит сопоставить столбцы.</div></td></tr></tbody>');
  }
  const order = $('[data-action="order"]');
  if (order && !doc.lines.length) {
    order.disabled = true;
    order.title = "Сначала добавьте товары и сохраните состав";
    order.setAttribute("aria-describedby", "order-block-reason");
    order.insertAdjacentHTML("afterend",
      '<span id="order-block-reason" class="muted">Чтобы заказать КМ, сначала добавьте и сохраните строки.</span>');
  }
  // Files retained for audit are accessible, but old label sets must never
  // look interchangeable with the current printable version.
  const historyLinks = [...processing.querySelectorAll("details .files a")];
  doc.artifacts.forEach(file => {
    if (file.kind !== "labels" || file.version === doc.current_version) return;
    const link = historyLinks.find(a => a.getAttribute("href") === "/api/files/" + file.id);
    if (!link) return;
    link.classList.add("outdated-artifact");
    link.insertAdjacentHTML("beforeend",
      '<span class="file-warning">Устаревшая версия · не передавать поставщику</span>');
  });

  if (["ready", "transferred"].includes(doc.status) && !flowActions.querySelector('[data-action="move-lines"]')) {
    const why = doc.stopped ? "обработка накладной остановлена" :
      doc.group_stop ? "обработка общей группы остановлена" :
      doc.parent_id ? "для накладной-дополнения перенос не предусмотрен" :
      doc.operations.some(op => ["report", "atk", "transform"].includes(op.kind)) ?
        "уже создан отчёт, АТК или трансформация" :
      "условия безопасного переноса не выполнены";
    next.firstElementChild.insertAdjacentHTML("beforeend",
      `<p class="workflow-block-reason">Перенос строк и КМ недоступен: ${why}.</p>`);
  }
  selectDocumentTab(documentTab);
}
function selectDocumentTab(key) {
  documentTab = ["content", "processing", "history"].includes(key) ? key : "content";
  $$("[data-document-panel]").forEach(panel => panel.hidden = panel.dataset.documentPanel !== documentTab);
  $$(".workspace-tabs [data-id]").forEach(button => {
    button.classList.toggle("active", button.dataset.id === documentTab);
    button.setAttribute("aria-pressed", button.dataset.id === documentTab);
  });
}
function markDocumentDirty() {
  documentDirty = true;
  $("#composition-empty")?.remove();
  if ($("#document-save-state")) {
    $("#document-save-state").textContent = "Есть несохранённые изменения состава";
    $("#document-save-state").classList.add("status-warning");
  }
  const order = $('[data-action="order"]');
  if (order) {
    order.disabled = true;
    order.title = "Сначала проверьте и сохраните состав";
  }
}
window.addEventListener("beforeunload", event => {
  if (documentDirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

function setupAdminFilters() {
  for (const key of ["users", "operations", "audit", "suppliers"]) {
    const panel = $(`[data-admin-panel="${key}"]`);
    const toolbar = document.createElement("div");
    toolbar.className = "admin-filter-bar";
    toolbar.innerHTML = `<label>Поиск в разделе<input data-section-search="${key}" placeholder="${key === "users" ? "Username или роль" : key === "suppliers" ? "Наименование поставщика" : key === "operations" ? "Операция, ID или пояснение" : "Сотрудник или действие"}"></label>${key === "operations" ? '<label>Показать<select id="operation-filter"><option value="all">Все операции</option><option value="attention">Требуют проверки</option><option value="pending">В очереди и обработке</option><option value="done">Завершённые</option><option value="stopped">Остановленные</option><option value="resolved">Скрытые предупреждения</option></select></label>' : ""}<span class="muted" data-filter-count></span>`;
    panel.querySelector("h2").after(toolbar);
    const rows = key === "suppliers" ? [...panel.querySelectorAll(":scope > .toolbar")] : [...panel.querySelectorAll("table tbody tr")];
    // Legacy operation/audit tables have their header in tbody.
    const dataRows = rows.filter(row => !row.querySelector("th"));
    const update = () => {
      const query = toolbar.querySelector("input").value.trim().toLocaleLowerCase("ru-RU");
      const state = $("#operation-filter")?.value || "all";
      let visible = 0;
      dataRows.forEach(row => {
        const op = key === "operations" ? overview.operations[dataRows.indexOf(row)] : null;
        const stateMatches = key !== "operations" || state === "all" ||
          (state === "attention" && ["unknown", "blocked", "preflight_blocked", "rejected", "partial"].includes(op?.state) && !op?.attention_resolved_at) ||
          (state === "pending" && !op?.stopped && !["done", "rejected", "cancelled"].includes(op?.state)) ||
          (state === "stopped" && (op?.stopped || op?.state === "cancelled")) ||
          (state === "resolved" && Boolean(op?.attention_resolved_at)) ||
          (state === "done" && op?.state === "done");
        row.hidden = !stateMatches || !row.textContent.toLocaleLowerCase("ru-RU").includes(query);
        if (!row.hidden) visible++;
      });
      toolbar.querySelector("[data-filter-count]").textContent = `Показано ${visible} из ${dataRows.length}`;
    };
    toolbar.querySelector("input").oninput = update;
    toolbar.querySelector("select")?.addEventListener("change", update);
    update();
  }
}
