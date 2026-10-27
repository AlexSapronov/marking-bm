"use strict";
// Desktop-only interface helpers. Business permissions remain server-side.
(function () {
  let noticeTimer = null;
  let noticeRevision = 0;

  function notify(message, kind = "error") {
    const modal = document.querySelector("#modal");
    const inDialog = modal?.open && kind === "error";
    let node;
    if (inDialog) {
      node = modal.querySelector(".dialog-error");
      if (!node) {
        node = document.createElement("div");
        node.className = "dialog-error";
        node.setAttribute("role", "alert");
        modal.prepend(node);
      }
    } else {
      node = document.querySelector("#toast");
      if (!node) return;
      node.className = "app-toast " + (kind === "error" ? "is-error" : "is-success");
      node.setAttribute("role", kind === "error" ? "alert" : "status");
    }
    node.replaceChildren();
    const body = document.createElement("span");
    body.textContent = String(message || "Неизвестная ошибка");
    const close = document.createElement("button");
    close.type = "button";
    close.className = "notice-close";
    close.setAttribute("aria-label", "Закрыть уведомление");
    close.textContent = "×";
    close.onclick = () => {
      if (inDialog) node.remove();
      else node.style.display = "none";
    };
    node.append(body, close);
    if (!inDialog) {
      node.style.display = "flex";
      clearTimeout(noticeTimer);
      const id = ++noticeRevision;
      if (kind !== "error") {
        noticeTimer = setTimeout(() => {
          if (noticeRevision === id) node.style.display = "none";
        }, 6500);
      }
    }
  }

  function supplierPicker(input, suppliers) {
    if (!input || input.dataset.pickerReady) return;
    input.dataset.pickerReady = "true";
    input.removeAttribute("list"); // native datalist cannot be positioned or styled
    const names = [...new Set((suppliers || []).map(s => typeof s === "string" ? s : s.name)
      .filter(Boolean))].sort((a, b) => a.localeCompare(b, "ru"));
    const host = input.closest("dialog") || document.body;
    const menu = document.createElement("div");
    menu.className = "supplier-picker";
    menu.id = "supplier-picker-" + Math.random().toString(36).slice(2);
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    host.append(menu);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", menu.id);
    input.setAttribute("autocomplete", "off");
    let active = -1;
    let visibleNames = [];

    function close() {
      menu.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }
    function position() {
      if (menu.hidden) return;
      const rect = input.getBoundingClientRect();
      const roomBelow = window.innerHeight - rect.bottom - 12;
      const above = roomBelow < 180 && rect.top > roomBelow;
      const available = Math.max(110, above ? rect.top - 16 : roomBelow);
      menu.style.left = Math.max(8, rect.left) + "px";
      menu.style.width = rect.width + "px";
      menu.style.maxHeight = Math.min(320, available) + "px";
      menu.style.top = (above
        ? Math.max(8, rect.top - Math.min(320, available))
        : rect.bottom + 3) + "px";
    }
    function setActive(index) {
      const options = [...menu.querySelectorAll('[role="option"]')];
      active = Math.max(-1, Math.min(options.length - 1, index));
      options.forEach((node, i) => node.classList.toggle("active", i === active));
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", options[active].id);
        options[active].scrollIntoView({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    }
    function select(name) {
      input.value = name;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      close();
      input.focus();
    }
    function render() {
      const value = input.value.trim().toLocaleLowerCase("ru");
      visibleNames = names.filter(name =>
        name.toLocaleLowerCase("ru").includes(value)).slice(0, 50);
      menu.replaceChildren();
      for (let i = 0; i < visibleNames.length; i++) {
        const option = document.createElement("div");
        option.setAttribute("role", "option");
        option.id = menu.id + "-option-" + i;
        option.className = "supplier-picker-option";
        option.textContent = visibleNames[i];
        option.addEventListener("pointerdown", event => {
          event.preventDefault();
          select(visibleNames[i]);
        });
        menu.append(option);
      }
      const footer = document.createElement("div");
      footer.className = "supplier-picker-hint";
      footer.textContent = value && !names.some(n => n.toLocaleLowerCase("ru") === value)
        ? "Нового поставщика создадим при сохранении"
        : visibleNames.length ? "↑ ↓ выбор · Enter подтвердить · Esc закрыть"
          : "Ничего не найдено";
      menu.append(footer);
      menu.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
      position();
    }
    input.addEventListener("focus", render);
    input.addEventListener("input", render);
    input.addEventListener("keydown", event => {
      if (event.key === "Escape" && !menu.hidden) {
        event.preventDefault();
        close();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (menu.hidden) render();
        const diff = event.key === "ArrowDown" ? 1 : -1;
        setActive(active < 0 ? (diff > 0 ? 0 : visibleNames.length - 1) : active + diff);
      } else if (event.key === "Enter" && !menu.hidden && active >= 0) {
        event.preventDefault();
        select(visibleNames[active]);
      }
    });
    input.addEventListener("blur", () => setTimeout(() => {
      if (document.activeElement !== input) close();
    }, 100));
    const controller = new AbortController();
    document.addEventListener("pointerdown", event => {
      if (!input.isConnected) {
        close();
        menu.remove();
        controller.abort();
      } else if (!event.composedPath().includes(input) &&
                 !event.composedPath().includes(menu)) close();
    }, { signal: controller.signal });
    window.addEventListener("resize", position, { signal: controller.signal });
    window.addEventListener("scroll", position, {
      capture: true, passive: true, signal: controller.signal,
    });
  }
  window.BmUX = { notify, supplierPicker };
})();
