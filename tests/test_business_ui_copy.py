"""Stable contracts for the meaning of business buttons and document states.

This catches accidental reintroduction of misleading copy. Chromium smoke tests
exercise representative mutating operations through the actual FastAPI backend.
"""
import re
from pathlib import Path


APP = Path("static/app.js").read_text(encoding="utf-8")
WORKSPACE = Path("static/workspace.js").read_text(encoding="utf-8")


def test_all_static_action_buttons_have_a_dispatch_handler():
    # Dynamic actions (ternaries) are separately exercised by browser smoke.
    presented = set(re.findall(r'data-action="([a-z][a-z-]+)"', APP + WORKSPACE))
    handled = set(re.findall(r'case "([a-z][a-z-]+)":', APP))
    assert presented, "No wired business-action buttons were found"
    assert not presented - handled, "Unwired buttons: " + ", ".join(sorted(presented - handled))


def test_formatted_completion_is_not_physical_freight_or_logistics_acceptance():
    assert 'if (d.status === "done") return "Комплект сформирован";' in APP
    assert 'logistics: {title: "Комплект сформирован"' in WORKSPACE
    assert 'done: ["АТК и RESULT сформированы"' in WORKSPACE
    assert "физического груза" in WORKSPACE
    assert 'if (d.status === "archive") return "В архиве";' in APP
    assert 'done: "Завершён"' in APP  # External operation only.
    assert 'done: "АТК сформирован"' in APP  # ATK only.


def test_buttons_name_actions_instead_of_promising_completed_external_effect():
    for copy in (
        "Подтвердить нанесение и создать отчёт",
        "Подтвердить нанесение и создать отчёты",
        "Создать повторный отчёт",
        "Возобновить локальное задание",
        "Повторить предпроверку",
        "Запросить проверку статуса",
        "Запросить обновление доступа",
        "Сменить ответственного",
        "Завершить работу",
        "Закрыть накладную в архив",
    ):
        assert copy in APP, copy
    assert 'a.documents.length === 1 ? "Завершить накладную"' not in APP
    assert ">Повторить отправку</button>" not in APP
    assert "не подтверждает фактическую доставку" in APP


def test_dangerous_admin_actions_require_explicit_confirmation():
    assert 'case "user-toggle":' in APP
    assert '"Отключить доступ сотрудника?"' in APP
    assert '"Объединить записи поставщиков?"' in APP
    assert '"Сбросить пароль сотрудника?"' in APP
    assert "attentionReasonDialog()" in APP


def test_document_excel_and_transfer_controls_share_composition_toolbar():
    # Transfer remains gated, but is no longer mixed with workflow actions.
    toolbar = APP.split('const excelActions = editable ?', 1)[1].split('  shell(', 1)[0]
    assert 'class="button" href="/api/template"' in toolbar
    assert 'data-action="import"' in toolbar
    assert 'canMoveLines ? \'<button data-action="move-lines"' in toolbar
    assert '${excelActions}</div><div class="table-wrap">' in APP
    assert '<div class="actions">${canMoveLines ?' not in APP
    assert '!composition.querySelector(\'[data-action="move-lines"]\')' in WORKSPACE
