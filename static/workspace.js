"use strict";
// Presentation and navigation only. All mutations use the existing protected API.
const documentStages = {
  all: {title: "Все", matches: () => true},
  preparation: {title: "Подготовка", matches: d => ["draft", "repack"].includes(d.status)},
  processing: {title: "В обработке", matches: d => ["ordering", "receiving", "reporting", "reported", "aggregating"].includes(d.display_status || d.status)},
  labels_ready: {title: "Этикетки готовы", matches: d => !d.stopped && d.status === "ready"},
  supplier: {title: "Поставщик", matches: d => !d.stopped && d.status === "transferred"},
  attention: {title: "Требуют проверки", matches: d => ["attention", "label_error", "report_error", "preflight_blocked", "unknown", "blocked"].includes(d.display_status || d.status)},
  logistics: {title: "Комплект сформирован", matches: d => !d.stopped && d.status === "done"},
  archived: {title: "Закрыты", matches: d => !d.stopped && d.status === "archive"},
};
const adminSections = {
  overview: ["Обзор", "Состояние системы и задачи, требующие внимания"],
  operations: ["Операции", "Очередь подписи, отправка и проверка результатов ЧЗ"],
  approvals: ["Согласования", "Решения по добавлению и изъятию КМ из АТК"],
  users: ["Пользователи", "Учётные записи, роли и доступ сотрудников"],
  documents: ["Документы", "Назначение ответственных за накладные и передача между сотрудниками"],
  organization: ["Организация", "Реквизиты, место деятельности и параметры эмиссии"],
  connections: ["Подключения", "Доступ к ГИС МТ, СУЗ и Windows-подписант"],
  suppliers: ["Поставщики", "Выгрузка КМ для поставщика, наименования и объединение записей"],
  catalog: ["Коды и справочник", "Общий пул КМ, привязки GTIN и повторный подбор"],
  audit: ["Журнал действий", "История действий сотрудников и администратора"],
};
const adminAttentionStates = new Set(["unknown", "blocked", "preflight_blocked", "rejected", "partial"]);
function adminOperationNeedsAttention(op) {
  return adminAttentionStates.has(op.state) && !op.attention_resolved_at;
}
function adminOperationPending(op) {
  return !op.stopped && !adminAttentionStates.has(op.state)
    && !["done", "cancelled"].includes(op.state);
}
function organizeAdmin(hb) {
  const main = $("main");
  const stats = main.querySelector(".stats");
  const notice = main.querySelector(":scope > .notice");
  const attention = overview.operations.filter(adminOperationNeedsAttention);
  const approvals = overview.transformations.filter(t => t.state === "approval");
  const pending = overview.operations.filter(adminOperationPending);
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
    done: ["АТК и RESULT сформированы", "Комплект документов готов для работы логиста. Накладная ещё открыта: получение логистом, формирование физического груза, перевозка и доставка в системе не подтверждены. Закрытие выполняется отдельно в реестре АТК."],
    archive: ["Накладная в архиве", "Рабочая обработка завершена либо остановлена. Состав и сохранённые версии файлов доступны для просмотра."],
    label_error: ["Исправьте состав для этикеток", "Проверьте сообщение об ошибке и сохраните исправленный состав. Полученные коды сохраняются."],
    report_error: ["Ошибка отчёта о нанесении", "СУЗ отклонила отчёт или обработала его частично. После сверки карточек НК и статусов КМ используйте действие «Повторить отчёт о нанесении». Подтверждённо нанесённые КМ не будут отправлены повторно."],
  };
  const [title, description] = (doc.stopped ? ["Обработка остановлена", "Документ в архиве. Возврат доступен только администратору; отправленные операции требуют сверки."] : guidance[doc.display_status || doc.status]) || ["Документ в обработке", "Проверьте состояние на вкладке «Обработка и файлы». Дополнительные действия выполняются после завершения текущего этапа."];
  const next = document.createElement("section");
  next.className = "next-step";
  next.innerHTML = `<div><span class="eyebrow">${["done", "archive"].includes(doc.status) ? "Состояние накладной" : "Следующий шаг"}</span><h2>${title}</h2><p>${description}</p><span id="document-save-state" class="muted" role="status">${editable ? "Все изменения состава сохранены" : "Состав доступен для просмотра"}</span></div>`;
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

  if (["ready", "transferred"].includes(doc.status) && !composition.querySelector('[data-action="move-lines"]')) {
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

const adminTableState = {
  users: {page: 0, size: 25, sort: "name"},
  operations: {page: 0, size: 50, sort: "newest"},
  audit: {page: 0, size: 25, sort: "newest"},
  suppliers: {page: 0, size: 25, sort: "name"},
};

function setupAdminFilters() {
  for (const key of ["users", "operations", "audit", "suppliers"]) {
    const panel = `[data-admin-panel="${key}"]`;
    const section = document.querySelector(panel);
    const isOperations = key === "operations";
    const state = adminTableState[key];
    const prefix = isOperations ? "operation" : `admin-${key}`;
    const toolbar = document.createElement("div");
    toolbar.className = "admin-filter-bar";
    const sortOptions = key === "operations"
      ? '<option value="newest">Сначала новые</option><option value="oldest">Сначала старые</option><option value="status">По статусу</option>'
      : key === "audit"
      ? '<option value="newest">Сначала новые</option><option value="oldest">Сначала старые</option><option value="name">По сотруднику</option>'
      : '<option value="name">По названию</option><option value="reverse">По названию, Я–А</option>';
    toolbar.innerHTML = `<label>Поиск в разделе<input data-section-search="${key}" placeholder="${key === "users" ? "Username или роль" : key === "suppliers" ? "Наименование поставщика" : isOperations ? "Операция, ID или пояснение" : "Сотрудник или действие"}"></label>${isOperations ? '<label>Показать<select id="operation-filter"><option value="all">Все операции</option><option value="attention">Требуют проверки</option><option value="pending">В очереди и обработке</option><option value="done">Завершённые</option><option value="stopped">Остановленные</option><option value="resolved">Скрытые предупреждения</option></select></label>' : ""}<label>Сортировка<select id="${prefix}-sort">${sortOptions}</select></label><label>Строк на странице<select id="${prefix}-page-size"><option value="25">25</option><option value="50">50</option><option value="100">100</option></select></label><span class="muted" data-filter-count></span>`;
    section.querySelector("h2").after(toolbar);
    const rows = key === "suppliers"
      ? [...section.querySelectorAll(":scope > .toolbar")]
      : [...section.querySelectorAll("table tbody tr")];
    const dataRows = rows.filter(row => !row.querySelector("th"));
    const entries = dataRows.map((row, index) => ({
      row,
      index,
      op: isOperations ? overview.operations[index] : null,
      audit: key === "audit" ? overview.audit[index] : null,
      name: key === "users" ? (overview.users[index]?.username || row.cells?.[0]?.textContent || "")
        : key === "suppliers" ? row.querySelector("span")?.textContent || ""
        : "",
    }));
    const pager = document.createElement("div");
    pager.className = "admin-pager";
    pager.innerHTML = `<span id="${prefix}-page-label" role="status"></span><div class="actions"><button type="button" id="${prefix}-previous">Предыдущая</button><button type="button" id="${prefix}-next">Следующая</button></div>`;
    const tableWrap = section.querySelector(".table-wrap");
    if (tableWrap) tableWrap.after(pager);
    else section.append(pager);
    toolbar.querySelector(`#${prefix}-sort`).value = state.sort;
    toolbar.querySelector(`#${prefix}-page-size`).value = String(state.size);
    const update = () => {
      const query = toolbar.querySelector("input").value.trim().toLocaleLowerCase("ru-RU");
      const selection = isOperations ? toolbar.querySelector("#operation-filter").value : "all";
      const filtered = entries.filter(({row, op}) => {
        const matchesState = !isOperations || selection === "all" ||
          (selection === "attention" && adminOperationNeedsAttention(op)) ||
          (selection === "pending" && adminOperationPending(op)) ||
          (selection === "stopped" && (op?.stopped || op?.state === "cancelled")) ||
          (selection === "resolved" && Boolean(op?.attention_resolved_at)) ||
          (selection === "done" && op?.state === "done");
        return matchesState && row.textContent.toLocaleLowerCase("ru-RU").includes(query);
      });
      toolbar.querySelector("[data-filter-count]").textContent =
        `Найдено ${filtered.length} из ${dataRows.length}`;
      filtered.sort((a, b) => {
        if (key === "operations" && state.sort === "status")
          return (states[a.op?.state] || a.op?.state || "").localeCompare(
            states[b.op?.state] || b.op?.state || "", "ru") || a.index - b.index;
        if (key === "audit" && state.sort === "name")
          return (a.audit?.actor || "").localeCompare(b.audit?.actor || "", "ru") || a.index - b.index;
        if (["operations", "audit"].includes(key)) {
          const lhs = key === "operations" ? a.op?.created : a.audit?.created;
          const rhs = key === "operations" ? b.op?.created : b.audit?.created;
          const cmp = String(lhs || "").localeCompare(String(rhs || ""));
          return (state.sort === "oldest" ? cmp : -cmp) || a.index - b.index;
        }
        const cmp = a.name.localeCompare(b.name, "ru", {numeric: true});
        return (state.sort === "reverse" ? -cmp : cmp) || a.index - b.index;
      });
      const pages = Math.max(1, Math.ceil(filtered.length / state.size));
      state.page = Math.max(0, Math.min(state.page, pages - 1));
      const from = state.page * state.size;
      const selected = filtered.slice(from, from + state.size);
      entries.forEach(({row}) => { row.hidden = true; });
      const parent = dataRows[0]?.parentElement;
      selected.forEach(({row}) => {
        if (parent) parent.insertBefore(row, parent === section ? pager : null);
        row.hidden = false;
      });
      pager.querySelector(`#${prefix}-page-label`).textContent = filtered.length
        ? `Строки ${from + 1}–${from + selected.length} из ${filtered.length} · страница ${state.page + 1} из ${pages}`
        : "Записей по выбранным фильтрам нет";
      pager.querySelector(`#${prefix}-previous`).disabled = state.page === 0;
      pager.querySelector(`#${prefix}-next`).disabled = state.page + 1 >= pages;
    };
    toolbar.querySelector("input").oninput = () => {
      state.page = 0;
      update();
    };
    toolbar.querySelector("#operation-filter")?.addEventListener("change", () => {
      state.page = 0;
      update();
    });
    toolbar.querySelector(`#${prefix}-sort`).onchange = event => {
      state.sort = event.target.value;
      state.page = 0;
      update();
    };
    toolbar.querySelector(`#${prefix}-page-size`).onchange = event => {
      state.size = Number(event.target.value);
      state.page = 0;
      update();
    };
    pager.querySelector(`#${prefix}-previous`).onclick = () => {state.page--; update();};
    pager.querySelector(`#${prefix}-next`).onclick = () => {state.page++; update();};
    update();
  }
}
