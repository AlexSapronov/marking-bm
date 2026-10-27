"""Real Chromium against the live application. No external writes or simulated UI data."""

import os
from pathlib import Path
import socket
import sqlite3
import hashlib
import json
from uuid import uuid4
from datetime import datetime, timezone
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marking_bm.auth import hash_password
from marking_bm.excel import xlsx, HEADERS
from playwright.sync_api import sync_playwright, expect
from conftest import env as workflow_env, add_doc, advance

with tempfile.TemporaryDirectory() as tmp:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "BM_DATA_DIR": tmp,
        "BM_PUBLIC_URL": url,
        "BM_COOKIE_SECURE": "0",
        "BM_ADMIN_PASSWORD_HASH": hash_password("browser-smoke-password"),
        "BM_ALLOW_EXTERNAL_WRITES": "0",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        with sync_playwright() as p:
            kwargs = {"headless": True, "args": ["--no-sandbox"]}
            if os.getenv("BM_BROWSER_EXECUTABLE"):
                kwargs["executable_path"] = os.environ["BM_BROWSER_EXECUTABLE"]
            browser = p.chromium.launch(**kwargs)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            response = page.goto(url + "/admin")
            try:
                page.locator("#login-value").wait_for(timeout=8000)
            except Exception as exc:
                raise AssertionError(
                    "Административный экран входа не загрузился: "
                    f"HTTP={response.status if response else 'no response'}, "
                    f"browser_errors={errors}, body={page.locator('body').inner_text()[:1000]}"
                ) from exc
            page.locator("#login-value").fill("browser-smoke-password")
            page.locator("#login-form button").click()
            page.get_by_role("heading", name="Администрирование", exact=True).wait_for()
            expect(page.locator('[data-admin-panel="overview"]')).to_be_visible()
            expect(page.locator("#user-form")).not_to_be_visible()
            # Escape is equivalent to cancellation in the shared confirmation UI.
            page.evaluate("document.body.dataset.confirmOutcome = 'pending'; void confirmAction('UX test', 'Check cancellation').then(value => document.body.dataset.confirmOutcome = String(value))")
            expect(page.get_by_role("heading", name="UX test")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator("body")).to_have_attribute("data-confirm-outcome", "false")
            page.evaluate("document.body.dataset.reasonOutcome = 'pending'; void attentionReasonDialog().then(value => document.body.dataset.reasonOutcome = value || 'cancelled')")
            expect(page.get_by_role("heading", name="Скрыть предупреждение?")).to_be_visible()
            page.locator("#attention-reason").fill("Сверено с результатом ЧЗ")
            page.get_by_role("button", name="Скрыть предупреждение").click()
            expect(page.locator("body")).to_have_attribute("data-reason-outcome", "Сверено с результатом ЧЗ")
            page.locator('.section-nav [data-id="users"]').click()
            page.locator("#username").fill("smoke-user")
            page.locator("#user-form button").click()
            page.get_by_role("cell", name="smoke-user", exact=True).wait_for()
            expect(page.locator("#setup-code-issued")).to_be_visible()
            manager_setup_code = page.locator("#setup-code-issued").inner_text()
            page.locator("#modal [data-action=close]").click()
            page.locator("#username").fill("smoke-logist")
            page.locator("#new-user-role").select_option("logistics")
            page.locator("#user-form button").click()
            page.get_by_role("cell", name="smoke-logist", exact=True).wait_for()
            expect(page.locator("#setup-code-issued")).to_be_visible()
            logist_setup_code = page.locator("#setup-code-issued").inner_text()
            page.locator("#modal [data-action=close]").click()
            # Administrator can invalidate the first code and issue a reset code.
            page.locator('[data-action="reset-password"][data-id="smoke-user"]').click()
            expect(page.get_by_role("heading", name="Сбросить пароль сотрудника?")).to_be_visible()
            expect(page.locator("#confirm-action")).to_have_text("Сбросить пароль")
            page.locator("#confirm-action").click()
            expect(page.locator("#setup-code-issued")).to_be_visible()
            manager_setup_code = page.locator("#setup-code-issued").inner_text()
            page.locator("#modal [data-action=close]").click()
            page.locator('[data-section-search="users"]').fill("smoke-logist")
            expect(page.get_by_role("cell", name="smoke-user", exact=True)).not_to_be_visible()
            expect(page.get_by_role("cell", name="smoke-logist", exact=True)).to_be_visible()
            page.locator('[data-section-search="users"]').fill("")
            assert (
                page.locator('.user-role[data-user="smoke-logist"]').input_value()
                == "logistics"
            )
            page.get_by_role("button", name="Накладные", exact=True).click()
            page.reload()
            page.get_by_role("heading", name="Накладные", exact=True).wait_for()
            page.get_by_role("button", name="Архив", exact=True).click()
            page.get_by_role("heading", name="Архив накладных", exact=True).wait_for()
            page.reload()
            page.get_by_role("heading", name="Архив накладных", exact=True).wait_for()
            page.get_by_role("button", name="Накладные", exact=True).click()
            page.get_by_role("button", name="+ Новая накладная").click()
            page.locator("#number").fill("SMOKE-001")
            page.locator("#new-supplier").fill("Поставщик smoke")
            page.get_by_role("button", name="Создать черновик").click()
            page.get_by_role("heading", name="Накладная SMOKE-001").wait_for()
            expect(page.locator("#document-save-state")).to_contain_text("сохранены")
            # Desktop supplier picker is app-rendered (not native datalist).
            page.get_by_role("button", name="← Накладные", exact=True).click()
            page.get_by_role("button", name="+ Новая накладная").click()
            assert page.locator("#new-supplier").get_attribute("role") == "combobox"
            assert page.locator("#new-supplier").get_attribute("list") is None
            page.locator("#new-supplier").fill("smoke")
            expect(page.locator("#modal .supplier-picker-option", has_text="Поставщик smoke")).to_be_visible()
            page.locator("#new-supplier").press("ArrowDown")
            page.locator("#new-supplier").press("Enter")
            expect(page.locator("#new-supplier")).to_have_value("Поставщик smoke")
            page.locator("#number").fill("SMOKE-ERR")
            # Exercise the error presentation with a controlled 409 response.
            # All normal API calls continue to the actual FastAPI server.
            def fail_invoice_create(route):
                if route.request.method == "POST":
                    route.fulfill(
                        status=409, content_type="application/json",
                        body=json.dumps({"detail": "Документ отклонён: исправьте данные"}),
                    )
                else:
                    route.continue_()
            page.route("**/api/documents", fail_invoice_create)
            page.get_by_role("button", name="Создать черновик").click()
            expect(page.locator("#modal .dialog-error")).to_be_visible()
            expect(page.locator("#modal .dialog-error")).to_contain_text("Документ отклонён")
            page.unroute("**/api/documents")
            page.get_by_role("button", name="Отмена", exact=True).click()
            page.get_by_role("button", name="SMOKE-001", exact=True).click()
            page.get_by_role("heading", name="Накладная SMOKE-001").wait_for()
            page.evaluate("pollStatuses()")
            expect(page.locator("#status-checked")).to_contain_text("Проверено:")
            page.get_by_role("button", name="+ Добавить строку").click()
            expect(page.get_by_role("button", name="Заказать КМ", exact=True)).to_be_disabled()
            expect(page.locator("#document-save-state")).to_contain_text("несохранённые")
            count_before = page.locator(".pn-input").count()
            page.evaluate("pollStatuses()")
            assert page.locator(".pn-input").count() == count_before
            expect(page.locator("#document-save-state")).to_contain_text("несохранённые")
            page.locator(".pn-input").fill("L-132XYD")
            page.locator(".gtin-input").fill("04640638349704")
            page.locator(".qty-input").fill("5")
            page.locator(".labels-input").fill("3")
            page.locator(".tnved-input").fill("8536699008")
            prepared_rows = page.evaluate("gatherRows()")
            assert prepared_rows == [["L-132XYD", "04640638349704", 5, 3, "8536699008"]], prepared_rows
            page.get_by_role("button", name="Проверить и сохранить состав").click()
            try:
                page.get_by_text("Состав сохранён", exact=True).wait_for(timeout=6000)
            except Exception as exc:
                raise AssertionError(
                    f"Сохранение состава: {exc}; modal={page.locator('#modal').inner_text()}; "
                    f"toast={page.locator('#toast').inner_text()}; "
                    f"dialog_open={page.locator('#modal').evaluate('(el) => el.open')}; "
                    f"page_errors={errors}; rows={prepared_rows}"
                ) from exc
            page.reload()
            page.get_by_role("heading", name="Накладная SMOKE-001").wait_for()
            assert page.locator(".pn-input").input_value() == "L-132XYD"
            assert page.locator(".qty-input").input_value() == "5"
            expect(page.get_by_role("button", name="Заказать КМ", exact=True)).to_be_enabled()
            page.get_by_role("button", name="Обработка и файлы", exact=True).click()
            expect(page.locator("#edit-rows")).not_to_be_visible()
            page.get_by_role("button", name="История", exact=True).click()
            page.get_by_role("heading", name="История документа", exact=True).wait_for()
            page.get_by_role("button", name="Состав", exact=True).click()
            expect(page.locator(".pn-input")).to_have_value("L-132XYD")
            page.evaluate("batchModal([doc.id])")
            page.get_by_role("heading", name="Подтвердить нанесение и создать отчёт", exact=True).wait_for()
            expect(page.locator("#report-form button.primary")).to_have_text(
                "Подтвердить и поставить отчёт в очередь подписи"
            )
            expect(page.locator("#report-form input")).to_have_count(1)
            expect(page.locator("#production-date")).to_be_visible()
            expect(page.locator("#report-form")).not_to_contain_text("КПП места деятельности")
            page.get_by_role("button", name="Отмена", exact=True).click()
            out = Path(os.getenv("BM_SCREENSHOT_DIR", "docs/screenshots"))
            out.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out / "invoice.png"), full_page=True)
            page.get_by_role("button", name="Тёмная тема", exact=True).click()
            assert page.locator("html").get_attribute("data-theme") == "dark"
            page.screenshot(path=str(out / "invoice-dark.png"), full_page=True)
            page.reload()
            page.get_by_role("heading", name="Накладная SMOKE-001").wait_for()
            assert page.locator("html").get_attribute("data-theme") == "dark"
            page.get_by_role("button", name="Светлая тема", exact=True).click()
            # Non-standard headers trigger explicit mapping; raw numbers/text normalize.
            unknown = xlsx(
                {
                    "Supplier": [
                        ["Export"],
                        [
                            "Model code",
                            "Product",
                            "Amount",
                            "Labels",
                            "Customs",
                            "Notes",
                        ],
                        [
                            "MAPPED-PN",
                            "04640638349704",
                            "1000",
                            "2",
                            8536699008,
                            "extra",
                        ],
                    ]
                }
            )
            page.locator("#excel-file").set_input_files(
                {
                    "name": "supplier.xlsx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "buffer": unknown,
                }
            )
            page.get_by_role(
                "heading", name="Сопоставление столбцов", exact=True
            ).wait_for()
            expect(page.locator("#excel-header-row")).to_have_value("2")
            page.screenshot(path=str(out / "excel-mapping.png"), full_page=True)
            for field, index in {
                "pn": 0,
                "gtin": 1,
                "quantity": 2,
                "labels": 2,
                "tnved": 4,
            }.items():
                page.locator(f'[data-excel-field="{field}"]').select_option(str(index))
            page.get_by_role(
                "button", name="Применить сопоставление", exact=True
            ).click()
            expect(page.locator("#excel-mapping-error")).to_contain_text(
                "пять разных столбцов"
            )
            page.locator('[data-excel-field="labels"]').select_option("3")
            page.get_by_role(
                "button", name="Применить сопоставление", exact=True
            ).click()
            page.get_by_role("heading", name="Excel загружен", exact=True).wait_for()
            page.get_by_role("button", name="Продолжить", exact=True).click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator(".pn-input")).to_have_value("MAPPED-PN")
            expect(page.locator(".qty-input")).to_have_value("1000")
            page.get_by_role(
                "button", name="Проверить и сохранить состав", exact=True
            ).click()
            page.locator("#confirm-save").click()
            expect(page.locator("#modal")).not_to_be_visible()
            page.reload()
            page.get_by_role("heading", name="Накладная SMOKE-001").wait_for()
            expect(page.locator(".pn-input")).to_have_value("MAPPED-PN")
            expect(page.locator(".qty-input")).to_have_value("1000")
            # Standard recognized headers import without showing the mapping form.
            ordinary = xlsx(
                {
                    "Товары": [
                        HEADERS,
                        ["MAPPED-PN", "04640638349704", "1000", "2", 8536699008],
                    ]
                }
            )
            page.locator("#excel-file").set_input_files(
                {
                    "name": "ordinary.xlsx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "buffer": ordinary,
                }
            )
            expect(page.locator("#toast")).to_contain_text(
                "Excel загружен в предпросмотр"
            )
            expect(page.locator("#modal")).not_to_be_visible()
            # Preserve edits on a cancelled navigation, then save before leaving.
            page.get_by_role("button", name="Накладные", exact=True).click()
            expect(page.get_by_role("heading", name="Покинуть документ?")).to_be_visible()
            page.locator("#modal [data-action='close']").click()
            expect(page.locator(".pn-input")).to_have_value("MAPPED-PN")
            page.get_by_role("button", name="Проверить и сохранить состав", exact=True).click()
            expect(page.locator("#document-save-state")).to_contain_text("сохранены")
            page.get_by_role("button", name="Накладные", exact=True).click()
            page.get_by_role("heading", name="Накладные", exact=True).wait_for()
            expect(page.locator('[data-action="batch"]')).to_be_disabled()
            expect(page.locator(".doc-select")).to_be_disabled()
            page.locator('[data-stage="supplier"]').click()
            expect(page.locator("#document-count")).to_have_text("Показано 0 из 1")
            page.get_by_role("button", name="Сбросить фильтры", exact=True).click()
            expect(page.locator("#document-count")).to_have_text("Показано 1 из 1")
            # Real persisted draft invoices test the complete register paging flow.
            # They are not eligible for mass reporting.
            with sqlite3.connect(Path(tmp) / "marking-bm.sqlite3") as db:
                supplier_id, owner = db.execute(
                    "SELECT supplier_id,owner FROM documents WHERE number='SMOKE-001'"
                ).fetchone()
                stamp = datetime.now(timezone.utc).isoformat()
                for index in range(60):
                    db.execute(
                        "INSERT INTO documents(id,number,supplier_id,owner,status,revision,"
                        "created,updated,current_version) VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid4().hex, f"UX-INV-{index:03d}", supplier_id, owner,
                         "draft", 0, stamp, stamp, 0),
                    )
            page.reload()
            page.get_by_role("heading", name="Накладные", exact=True).wait_for()
            expect(page.locator("#document-page-label")).to_contain_text("Строки 1–50 из 61")
            expect(page.locator("#doc-rows tr")).to_have_count(50)
            page.locator("#document-next").click()
            expect(page.locator("#document-page-label")).to_contain_text("Строки 51–61 из 61")
            expect(page.locator("#doc-rows tr")).to_have_count(11)
            page.locator("#document-page-size").select_option("25")
            expect(page.locator("#document-page-label")).to_contain_text("Строки 1–25 из 61")
            page.locator("#document-sort").select_option("number")
            expect(page.locator("#doc-rows tr").first).to_contain_text("SMOKE-001")
            page.locator("#search").fill("UX-INV-059")
            expect(page.locator("#document-page-label")).to_contain_text("Строки 1–1 из 1")
            page.get_by_role("button", name="Сбросить фильтры", exact=True).click()
            expect(page.locator("#document-page-label")).to_contain_text("Строки 1–25 из 61")
            page.screenshot(path=str(out / "documents.png"), full_page=True)
            page.locator("#search").fill("nonexistent")
            assert page.locator(".doc-select").count() == 0
            # Long operation histories are real rows in the disposable test DB,
            # not mocked API responses. The admin table should page them.
            with sqlite3.connect(Path(tmp) / "marking-bm.sqlite3") as db:
                stamp = datetime.now(timezone.utc).isoformat()
                payload = '{"test":"pagination"}'
                for index in range(65):
                    db.execute(
                        "INSERT INTO operations(id,kind,doc_id,op_key,payload,sha256,state,"
                        "external_id,error,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid4().hex, "fetch", None, f"ux:pagination:{index}",
                         payload, hashlib.sha256(payload.encode()).hexdigest(),
                         "done", None, f"Тестовая операция {index}", stamp, stamp),
                    )
            page.get_by_role("button", name="Администрирование", exact=True).click()
            page.locator('.section-nav [data-id="operations"]').click()
            assert "внешней системе" in page.evaluate(
                "operationStatusGuidance({state:'unknown',external_id:'suz:123',stopped:false})"
            )
            assert "Повтор" not in page.evaluate(
                "operationStatusGuidance({state:'done',stopped:false})"
            )
            sample = page.evaluate("""() => renderOperationRow({
              id: 'sample', kind: 'report', state: 'rejected',
              doc_id: null, external_id: 'suz:123',
              error: 'Ошибка НК', created: '2026-01-01T12:00:00Z',
              updated: '2026-01-01T13:00:00Z',
              attention_resolved_at: '2026-01-01T13:30:00Z', stopped: false
            })""")
            assert "Последнее изменение:" in sample
            assert "Статус операции не изменён" in sample
            assert "Изучите причину" in sample
            assert page.evaluate("adminOperationPending({state:'queued', stopped:false})")
            assert not page.evaluate("adminOperationPending({state:'unknown', stopped:false})")
            assert not page.evaluate("adminOperationPending({state:'rejected', stopped:false})")
            assert page.evaluate("adminOperationNeedsAttention({state:'unknown', attention_resolved_at:null})")
            expect(page.locator("#operation-page-label")).to_contain_text("Строки 1–50")
            def displayed_operations():
                return page.evaluate("""
                  () => [...document.querySelectorAll('[data-admin-panel="operations"] table tbody tr')]
                    .filter(row => !row.hidden && !row.querySelector('th')).length
                """)
            assert displayed_operations() == 50
            page.locator("#operation-next").click()
            expect(page.locator("#operation-page-label")).to_contain_text("Строки 51–")
            assert displayed_operations() >= 15
            page.locator("#operation-page-size").select_option("25")
            expect(page.locator("#operation-page-label")).to_contain_text("Строки 1–25")
            assert displayed_operations() == 25
            page.locator("#operation-sort").select_option("oldest")
            expect(page.locator("#operation-page-label")).to_contain_text("Строки 1–25")
            page.locator('.section-nav [data-id="organization"]').click()
            page.locator("#config-form").wait_for()
            page.reload()
            page.locator("#config-form").wait_for()
            expect(page.locator('[data-admin-panel="users"]')).not_to_be_visible()
            page.locator('.section-nav [data-id="overview"]').click()
            page.screenshot(path=str(out / "admin.png"), full_page=True)
            page.get_by_role("button", name="Тёмная тема", exact=True).click()
            page.screenshot(path=str(out / "admin-dark.png"), full_page=True)
            page.get_by_role("button", name="Светлая тема", exact=True).click()
            for section in ["operations", "approvals", "users", "documents", "organization", "connections", "catalog", "suppliers", "audit"]:
                page.locator(f'.section-nav [data-id="{section}"]').click()
                expect(page.locator(f'[data-admin-panel="{section}"]')).to_be_visible()
                expect(page.locator('[data-admin-panel]:visible')).to_have_count(1)
            # Admin can find a working invoice and transfer ownership in the UI.
            page.locator('.section-nav [data-id="documents"]').click()
            page.locator("#admin-doc-q").fill("SMOKE-001")
            page.locator("#admin-doc-search-form button").click()
            expect(page.locator("#admin-doc-results tbody tr")).to_have_count(1)
            expect(page.locator("#admin-doc-results tbody tr td").nth(3)).to_have_text("administrator")
            page.locator("#admin-doc-results select.user-role").select_option("smoke-user")
            page.locator('[data-action="reassign-document"]').click()
            expect(page.get_by_role("heading", name="Сменить ответственного за накладную?")).to_be_visible()
            page.locator("#confirm-action").click()
            expect(page.locator("#admin-doc-results tbody tr td").nth(3)).to_have_text("smoke-user")
            page.screenshot(path=str(out / "admin-documents.png"), full_page=True)
            page.locator('.section-nav [data-id="organization"]').click()
            page.locator('[name="organization"]').fill("Организация smoke")
            page.locator('.section-nav [data-id="users"]').click()
            page.locator('.section-nav [data-id="organization"]').click()
            expect(page.locator('[name="organization"]')).to_have_value("Организация smoke")
            page.screenshot(path=str(out / "admin-organization.png"), full_page=True)
            assert not errors, errors
            # 390px viewport: layout and form stay inside the viewport.
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#admin-mobile-section").select_option("users")
            page.screenshot(path=str(out / "admin-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
            page.get_by_role("button", name="Накладные", exact=True).click()
            page.get_by_role("heading", name="Накладные", exact=True).wait_for()
            page.screenshot(path=str(out / "mobile.png"), full_page=True)
            assert page.evaluate(
                "document.documentElement.scrollWidth<=window.innerWidth"
            ), page.evaluate(
                '[...document.querySelectorAll("body *")].filter(x=>x.getBoundingClientRect().right>innerWidth+1).map(x=>[x.tagName,x.className,x.getBoundingClientRect().right]).slice(0,12)'
            )
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_role("button", name="Выйти", exact=True).click()
            # Build a real finished invoice through the service; only external APIs
            # use the test transport. The browser still talks to the normal server.
            test_app, test_service, _ = workflow_env.__wrapped__(Path(tmp))
            logistics_doc = add_doc(test_service, number="LOGISTICS-READY")
            test_service.order(logistics_doc, "alice")
            advance(test_app)
            test_service.transferred(logistics_doc, "alice", 1)
            test_service.batch("alice", [logistics_doc], "2026-01-01")
            advance(test_app)
            page.get_by_role("button", name="Первый вход / сброс пароля").click()
            page.locator("#login-value").fill("smoke-logist")
            page.locator("#setup-code").fill(logist_setup_code)
            page.locator("#password-new").fill("8characters")
            page.locator("#password-repeat").fill("8characters")
            page.locator("#login-form button").click()
            page.get_by_text("Пароль установлен. Теперь войдите с новым паролем").wait_for()
            page.locator("#login-password").fill("8characters")
            page.locator("#login-form button").click()
            page.get_by_role("heading", name="Реестр АТК", exact=True).wait_for()
            assert page.get_by_role("button", name="Накладные", exact=True).count() == 0
            assert page.get_by_role("button", name="+ Новая накладная").count() == 0
            assert page.locator("#atk-table").count() == 1
            expect(page.locator(".registry-supplier-row")).to_have_count(1)
            expect(page.get_by_role("columnheader", name="Следующее действие")).to_be_visible()
            expect(page.locator("#registry-selection-summary")).to_contain_text("Выберите готовые АТК")
            page.locator(".atk-select").check()
            expect(page.locator("#registry-selection-summary")).to_contain_text("Уникальных комплектов: 1")
            expect(page.locator("#registry-selection-summary")).to_contain_text("Файлов Excel: 2")
            expect(page.locator("#registry-selection-summary")).to_contain_text("LOGISTICS-READY")
            page.locator(".atk-select").uncheck()
            # A historical RESULT file must not be presented as current.
            with sqlite3.connect(Path(tmp) / "marking-bm.sqlite3") as db:
                group = db.execute(
                    "SELECT batch_id || ':' || supplier_id FROM atks LIMIT 1"
                ).fetchone()[0]
                original = db.execute(
                    "SELECT content,sha256,snapshot,created FROM artifacts "
                    "WHERE group_id=? AND kind='result' ORDER BY version DESC LIMIT 1",
                    (group,),
                ).fetchone()
                assert original is not None
                db.execute(
                    "INSERT INTO artifacts(id,doc_id,group_id,version,kind,"
                    "name,content,sha256,snapshot,created) "
                    "VALUES(?,NULL,?,0,'result',?,?,?,?,?)",
                    (uuid4().hex, group, "ux-outdated-RESULT.xlsx", *original),
                )
            page.locator("#atk-table .registry-code").click()
            expect(page.locator("#modal")).to_contain_text("Актуальные файлы АТК + RESULT")
            expect(page.locator("#modal")).to_contain_text("Последнее изменение операции:")
            expect(page.locator("#modal .outdated-artifact")).to_have_count(1)
            expect(page.locator("#modal .outdated-artifact")).to_contain_text("ux-outdated-RESULT.xlsx")
            expect(page.locator("#modal .outdated-artifact")).to_contain_text("Устаревшая версия")
            expect(page.locator("#modal .files a:not(.outdated-artifact)")).to_have_count(2)
            page.locator("#modal [data-action='close']").click()
            expect(page.get_by_role("button", name="Администрирование", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="Открыть для закрытия накладных", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Открыть для закрытия накладных", exact=True)).to_be_in_viewport(ratio=1)
            # The action column remains visible without horizontal table scrolling
            # at all supported desktop viewport widths.
            for width, height in ((1366, 768), (1920, 1080), (2560, 1440)):
                page.set_viewport_size({"width": width, "height": height})
                visible_on_right = page.evaluate("""
                  () => {
                    const wrap = document.querySelector('.registry-table-scroll');
                    const button = document.querySelector('#atk-table td:last-child button');
                    wrap.scrollLeft = 0;
                    const w = wrap.getBoundingClientRect();
                    const b = button.getBoundingClientRect();
                    return b.left >= w.left - 1 && b.right <= w.right + 1;
                  }
                """)
                assert visible_on_right, f"Завершение недоступно по горизонтали при {width}x{height}"
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#atk-search").fill("UNAVAILABLE")
            page.get_by_role("button", name="Применить фильтры", exact=True).click()
            page.get_by_role("heading", name="Реестр АТК", exact=True).wait_for()
            assert page.locator("#atk-search").input_value() == "UNAVAILABLE"
            page.get_by_role("button", name="Сбросить", exact=True).click()
            expect(page.locator("#atk-search")).to_have_value("")
            page.screenshot(path=str(out / "logistics.png"), full_page=True)
            page.get_by_role("button", name="Тёмная тема", exact=True).click()
            page.screenshot(path=str(out / "logistics-dark.png"), full_page=True)
            page.reload()
            page.get_by_role("heading", name="Реестр АТК", exact=True).wait_for()
            assert page.locator("html").get_attribute("data-theme") == "dark"
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(out / "logistics-mobile.png"), full_page=True)
            assert page.evaluate(
                "document.documentElement.scrollWidth<=window.innerWidth"
            )
            assert not errors, errors
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_role("button", name="Открыть для закрытия накладных", exact=True).click()
            expect(page.locator("#modal")).to_contain_text("Закрыть накладную")
            # Opening an ATK detail is navigation, not completion.
            assert test_service.get(logistics_doc, "alice")["status"] == "done"
            page.locator('#modal [data-action="logistics-complete"]').click()
            expect(page.locator("#modal")).to_contain_text("не подтверждает фактическую доставку")
            page.get_by_role("button", name="Отмена", exact=True).click()
            expect(page.locator('#atk-table tbody tr:not(.registry-supplier-row)')).to_have_count(1)
            page.get_by_role("button", name="Открыть для закрытия накладных", exact=True).click()
            page.locator('#modal [data-action="logistics-complete"]').click()
            page.locator('#modal [data-action="confirm-logistics-complete"]').click()
            expect(page.locator('#atk-table tbody tr:not(.registry-supplier-row)')).to_have_count(0)
            page.locator("#atk-archive").check()
            expect(page.locator('#atk-table tbody tr:not(.registry-supplier-row)')).to_have_count(1)
            expect(page.locator('#atk-table')).to_contain_text("LOGISTICS-READY")
            expect(page.get_by_role("button", name="Открыть для закрытия накладных", exact=True)).to_have_count(0)
            page.reload()
            # Archive choice is an in-session filter; the completed invoice stays out of work.
            expect(page.locator('#atk-table tbody tr:not(.registry-supplier-row)')).to_have_count(0)
            # Many real database ATK records check paging without mocked HTTP/UI
            # responses. The temporary test database is discarded after the smoke run.
            with sqlite3.connect(Path(tmp) / "marking-bm.sqlite3") as db:
                orig = db.execute(
                    "SELECT id,batch_id,supplier_id,tnved,version,op_id,state FROM atks LIMIT 1"
                ).fetchone()
                assert orig is not None
                original_id, original_batch, supplier_id, tnved, version, op_id, state = orig
                assert state == "done"
                composition = db.execute(
                    "SELECT composition,created FROM atk_versions WHERE atk_id=? AND version=?",
                    (original_id, version),
                ).fetchone()
                files = db.execute(
                    "SELECT kind,version,name,content,sha256,created FROM artifacts "
                    "WHERE group_id=? AND kind IN ('atk','result')",
                    (original_batch + ":" + supplier_id,),
                ).fetchall()
                assert {"atk", "result"}.issubset({entry[0] for entry in files})
                for index in range(55):
                    batch_id, atk_id = uuid4().hex, uuid4().hex
                    db.execute(
                        "INSERT INTO batches(id,created,actor) VALUES(?,?,?)",
                        (batch_id, "2026-01-01T00:00:00+00:00", "smoke-user"),
                    )
                    db.execute(
                        "INSERT INTO atks(id,code,supplier_id,tnved,batch_id,version,op_id,state)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (atk_id, f"UX-REG-{index:03d}", supplier_id,
                         tnved, batch_id, version, op_id, "done"),
                    )
                    db.execute(
                        "INSERT INTO atk_versions(atk_id,version,composition,created) VALUES(?,?,?,?)",
                        (atk_id, version, *composition),
                    )
                    # Two downloadable ATKs, separated by more than one page.
                    if index in (0, 52):
                        for kind, file_version, name, content, digest, created in files:
                            db.execute(
                                "INSERT INTO artifacts(id,doc_id,group_id,version,kind,"
                                "name,content,sha256,snapshot,created) "
                                "VALUES(?,NULL,?,?,?,?,?,?,?,?)",
                                (uuid4().hex, batch_id + ":" + supplier_id,
                                 file_version, kind, name, content, digest,
                                 '{"documents":[]}', created),
                            )
            page.reload()
            expect(page.locator("#atk-page-label")).to_contain_text("АТК 1–50 из 55")
            expect(page.locator(".atk-select")).to_have_count(50)
            page.locator("#atk-page-size").select_option("25")
            page.locator("#atk-sort").select_option("code")
            expect(page.locator("#atk-page-label")).to_contain_text("АТК 1–25 из 55")
            expect(page.locator(".registry-code").first).to_contain_text("UX-REG-000")
            page.locator(".atk-select:not(:disabled)").check()
            expect(page.locator("#registry-selection-summary")).to_contain_text("Выбрано АТК: 1")
            # Filtering must never silently discard a ready ZIP selection.
            page.locator("#atk-search").fill("nothing")
            page.get_by_role("button", name="Применить фильтры").click()
            expect(page.get_by_role("heading", name="Применить фильтры АТК?")).to_be_visible()
            page.locator("#modal [data-action='close']").click()
            expect(page.locator("#atk-search")).to_have_value("")
            expect(page.locator("#registry-selection-summary")).to_contain_text("Выбрано АТК: 1")
            page.locator("#atk-next").click()
            expect(page.locator("#atk-page-label")).to_contain_text("АТК 26–50 из 55")
            expect(page.locator("#registry-selection-summary")).to_contain_text("на других страницах: 1")
            page.locator("#atk-next").click()
            expect(page.locator("#atk-page-label")).to_contain_text("АТК 51–55 из 55")
            page.locator(".atk-select:not(:disabled)").check()
            expect(page.locator("#registry-selection-summary")).to_contain_text("Выбрано АТК: 2")
            expect(page.locator("#registry-selection-summary")).to_contain_text("на других страницах: 1")
            expect(page.locator("#registry-selection-summary")).to_contain_text("Уникальных комплектов: 2")
            expect(page.locator("#atk-select-all")).to_be_checked()
            page.locator("#clear-atk-selection").click()
            expect(page.locator("#registry-selection-summary")).to_contain_text("Выберите готовые АТК")
            expect(page.locator("#atk-select-all")).not_to_be_checked()
            page.locator("#atk-page-size").select_option("50")
            expect(page.locator("#atk-page-label")).to_contain_text("АТК 1–50 из 55")
            page.locator("#atk-next").click()
            expect(page.locator("#atk-page-label")).to_contain_text("АТК 51–55 из 55")
            page.get_by_role("button", name="Выйти", exact=True).click()
            page.get_by_role("button", name="Первый вход / сброс пароля").click()
            page.locator("#login-value").fill("smoke-user")
            page.locator("#setup-code").fill(manager_setup_code)
            page.locator("#password-new").fill("abcde123")
            page.locator("#password-repeat").fill("abcde123")
            page.locator("#login-form button").click()
            page.get_by_text("Пароль установлен. Теперь войдите с новым паролем").wait_for()
            page.locator("#login-password").fill("abcde123")
            page.locator("#login-form button").click()
            page.get_by_role("heading", name="Накладные", exact=True).wait_for()
            expect(page.locator('[data-stage="preparation"]')).to_be_visible()
            expect(page.get_by_role("button", name="Администрирование", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="Реестр АТК", exact=True)).to_have_count(0)
            expect(page.get_by_role("columnheader", name="Владелец", exact=True)).to_have_count(0)
            # The internal done stage is only an ATK/RESULT handoff. It is NOT
            # the logistics archive/completion event. Test persisted backend data.
            with sqlite3.connect(Path(tmp) / "marking-bm.sqlite3") as db:
                supplier = db.execute(
                    "SELECT supplier_id FROM documents WHERE number='SMOKE-001'"
                ).fetchone()[0]
                completed_doc = uuid4().hex
                stamp = datetime.now(timezone.utc).isoformat()
                db.execute(
                    "INSERT INTO documents(id,number,supplier_id,owner,status,revision,"
                    "created,updated,completed,current_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (completed_doc, "HANDOFF-TO-LOGISTICS", supplier,
                     "smoke-user", "done", 0, stamp, stamp, stamp, 0),
                )
            page.reload()
            expect(page.locator('[data-stage="logistics"]')).to_contain_text("Комплект сформирован")
            expect(page.locator("#doc-rows")).to_contain_text("HANDOFF-TO-LOGISTICS")
            handoff_row = page.locator("#doc-rows tr").filter(has_text="HANDOFF-TO-LOGISTICS")
            expect(handoff_row.locator(".badge.logistics")).to_have_text("Комплект сформирован")
            assert page.evaluate("states.done === 'Завершён'")
            page.locator('[data-stage="logistics"]').click()
            expect(page.locator("#document-count")).to_have_text("Показано 1 из 2")
            expect(page.locator("#doc-rows")).to_contain_text("HANDOFF-TO-LOGISTICS")
            expect(page.locator("#status option[value='done']")).to_have_text("Комплект сформирован")
            page.get_by_role("button", name="HANDOFF-TO-LOGISTICS", exact=True).click()
            expect(page.locator(".doc-status-meta .badge.logistics")).to_have_text("Комплект сформирован")
            expect(page.get_by_role("heading", name="АТК и RESULT сформированы")).to_be_visible()
            expect(page.get_by_text("перевозка и доставка в системе не подтверждены.", exact=False)).to_be_visible()
            expect(page.locator(".steps .step.current")).to_have_text("АТК/RESULT")
            page.get_by_role("button", name="← Накладные", exact=True).click()
            page.locator('[data-stage="all"]').click()
            # Check the new full-row transfer interaction against real API and DB.
            move_source = add_doc(
                test_service, owner="smoke-user", supplier="Поставщик move",
                number="MOVE-READY", labels=1
            )
            test_service.order(move_source, "smoke-user")
            advance(test_app)
            move_target = test_service.create(
                "smoke-user", "MOVE-EMPTY", "Поставщик move"
            )
            test_service.transferred(move_source, "smoke-user", 1)
            page.reload()
            page.locator(".doc-select:not(:disabled)").check()
            expect(page.locator("#selected-count")).to_contain_text("Выбрано накладных: 1")
            page.locator("#search").fill("MOVE-EMPTY")
            expect(page.locator("#selected-count")).to_contain_text("на текущей странице: 0")
            expect(page.locator("#selection-description")).to_contain_text("вне текущих фильтров: 1")
            page.locator('[data-action="batch"]').click()
            expect(page.locator("#modal .batch-preview")).to_contain_text("MOVE-READY")
            expect(page.locator("#modal .batch-preview")).not_to_contain_text("MOVE-EMPTY")
            page.get_by_role("button", name="Отмена", exact=True).click()
            page.locator("#search").fill("")
            page.locator("#clear-doc-selection").click()
            expect(page.locator("#selected-count")).to_contain_text("Накладные не выбраны")
            page.get_by_role("button", name="MOVE-READY", exact=True).click()
            expect(page.get_by_role("button", name="Перенести строки и КМ")).to_be_visible()
            page.locator(".move-line").check()
            assert page.evaluate("gatherRows()[0]") == page.evaluate(
                "([l]) => [l.pn, l.gtin, l.quantity, l.labels, l.tnved]",
                [page.evaluate("doc.lines[0]")],
            ), "Checkbox переноса не должен смещать поля состава"
            expect(page.locator("#document-save-state")).to_contain_text("сохранены")
            page.get_by_role("button", name="Перенести строки и КМ", exact=True).click()
            page.wait_for_timeout(800)
            assert page.locator("#move-lines-form").count(), (
                page.locator("#toast").inner_text(), errors,
                page.locator("#modal").inner_text()
            )
            page.locator('#move-lines-form select[name="target"]').select_option(move_target)
            page.get_by_role("button", name="Проверить перенос").click()
            expect(page.get_by_role("heading", name="Подтвердить перенос")).to_be_visible()
            expect(page.locator(".move-impact-grid")).to_contain_text("Источник")
            expect(page.locator(".move-impact-grid")).to_contain_text("Получатель")
            expect(page.locator(".move-impact-grid")).to_contain_text("Единицы товара")
            # Browser smoke intentionally has no True API credentials. Route
            # this *single* mutating request to the fixture with a mocked,
            # read-only True API gateway; all other UI calls use real FastAPI.
            commit_path = "**/api/documents/" + move_source + "/move-lines"
            def complete_movement(route):
                body = route.request.post_data_json
                committed = test_service.move_lines(
                    move_source, body["target_id"], body["line_ids"],
                    body["fingerprint"], "smoke-user",
                )
                route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps(committed),
                )
            page.route(commit_path, complete_movement)
            page.locator("#confirm-move-lines").click()
            expect(page.locator('[data-action="move-lines"]')).to_have_count(0)
            page.unroute(commit_path)
            assert test_service.get(move_source, "smoke-user")["status"] == "draft"
            page.get_by_role("heading", name="Накладная MOVE-READY").wait_for()
            assert test_service.get(move_target, "smoke-user")["status"] == "ready"
            assert len(test_service.get(move_target, "smoke-user")["codes"]) == 1
            assert test_service.get(move_source, "smoke-user")["status"] == "draft"
            expect(page.locator('a.button.primary[href*="/api/files/"]')).to_have_count(0)
            page.get_by_role("button", name="← Накладные", exact=True).click()
            expect(page.locator('[data-action="batch"]')).to_have_text("Подтвердить нанесение и создать отчёты")
            page.get_by_role("button", name="MOVE-EMPTY", exact=True).click()
            expect(page.get_by_role("button", name="Подтвердить передачу этикеток", exact=True)).to_be_visible()
            page.get_by_role("button", name="Подтвердить передачу этикеток", exact=True).click()
            expect(page.get_by_role("heading", name="Подтвердить передачу этикеток поставщику?")).to_be_visible()
            expect(page.locator("#modal")).to_contain_text("версия")
            page.locator("#modal [data-action='close']").click()
            assert test_service.get(move_target, "smoke-user")["status"] == "ready"
            page.get_by_role("button", name="Подтвердить передачу этикеток", exact=True).click()
            page.get_by_role("button", name="Передача подтверждена", exact=True).click()
            try:
                expect(page.locator('[data-action="report"]')).to_have_text(
                    "Подтвердить нанесение и создать отчёт", timeout=8000
                )
            except Exception as exc:
                raise AssertionError(
                    f"Передача после подтверждения не завершилась: "
                    f"toast={page.locator('#toast').inner_text()}, errors={errors}"
                ) from exc
            assert test_service.get(move_target, "smoke-user")["status"] == "transferred"
            page.get_by_role("button", name="← Накладные", exact=True).click()
            page.get_by_role("button", name="+ Новая накладная", exact=True).click()
            page.locator("#number").fill("STOP-UI")
            page.locator("#new-supplier").fill("Поставщик smoke")
            page.get_by_role("button", name="Создать черновик", exact=True).click()
            page.get_by_role("heading", name="Накладная STOP-UI", exact=True).wait_for()
            page.get_by_role("button", name="Остановить и архивировать", exact=True).click()
            expect(page.locator("#stop-document-submit")).to_be_disabled()
            page.locator("#stop-document-number").fill("stop-ui")
            expect(page.locator("#stop-document-submit")).to_be_disabled()
            page.locator("#stop-document-number").fill("STOP-UI")
            expect(page.locator("#stop-document-submit")).to_be_enabled()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.screenshot(path=str(out / "stop-confirmation.png"), full_page=True)
            page.get_by_role("button", name="Отмена", exact=True).click()
            expect(page.locator('[data-action="stop-document"]')).to_be_visible()
            page.get_by_role("button", name="Остановить и архивировать", exact=True).click()
            page.locator("#stop-document-number").fill("STOP-UI")
            page.locator("#stop-document-submit").click()
            expect(page.locator('[data-action="stop-document"]')).to_have_count(0)
            expect(page.locator('[data-action="restore-document"]')).to_have_count(0)
            page.get_by_role("heading", name="Обработка остановлена", exact=True).wait_for()
            page.reload()
            page.get_by_role("heading", name="Обработка остановлена", exact=True).wait_for()
            page.screenshot(path=str(out / "stopped-invoice.png"), full_page=True)
            page.get_by_role("button", name="← Архив накладных", exact=True).click()
            page.locator('[data-stage="stopped"]').click()
            expect(page.locator("#doc-rows")).to_contain_text("STOP-UI")
            page.get_by_role("button", name="Выйти", exact=True).click()
            page.get_by_role("button", name="Вход администратора", exact=True).click()
            page.locator("#login-value").fill("browser-smoke-password")
            page.locator("#login-form button").click()
            page.get_by_role("heading", name="Администрирование", exact=True).wait_for()
            page.get_by_role("button", name="Архив", exact=True).click()
            page.get_by_role("button", name="STOP-UI", exact=True).click()
            page.get_by_role("button", name="Вернуть из архива", exact=True).click()
            page.locator('#restore-document-form [name="reason"]').fill("Покупка подтверждена")
            page.get_by_role("button", name="Вернуть в работу", exact=True).click()
            expect(page.locator('[data-action="restore-document"]')).to_have_count(0)
            expect(page.locator('[data-action="stop-document"]')).to_be_visible()
            expect(page.locator('[data-action="add-line"]')).to_be_visible()
            page.get_by_role("button", name="← Накладные", exact=True).click()
            expect(page.locator("#doc-rows")).to_contain_text("STOP-UI")
            expect(page.locator('[data-stage="all"]')).to_have_attribute("aria-pressed", "true")
            # Inject only a terminal external response into the test DB.
            # The browser still talks to the real backend, with external writes disabled.
            with sqlite3.connect(Path(tmp) / "marking-bm.sqlite3") as db:
                doc_id = db.execute(
                    "SELECT id FROM documents WHERE number='STOP-UI'"
                ).fetchone()[0]
                stamp = datetime.now(timezone.utc).isoformat()
                payload = '{"sntins":[],"attributes":{}}'
                db.execute("UPDATE documents SET status='reporting' WHERE id=?", (doc_id,))
                db.execute(
                    "INSERT INTO operations(id,kind,doc_id,op_key,payload,sha256,state,"
                    "external_id,error,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, "report", doc_id, "test:report:error:" + uuid4().hex,
                     payload, hashlib.sha256(payload.encode()).hexdigest(), "rejected",
                     str(uuid4()), "Карточка в НК ещё не готова",
                     stamp, stamp),
                )
            page.reload()
            page.get_by_role("heading", name="Накладные", exact=True).wait_for()
            row = page.locator("#doc-rows tr", has=page.get_by_role("button", name="STOP-UI"))
            expect(row).to_contain_text("Ошибка")
            expect(page.locator('[data-stage="attention"]')).to_contain_text("1")
            row.get_by_role("button", name="STOP-UI").click()
            expect(page.locator(".topbar .badge")).to_have_text("Ошибка")
            expect(page.get_by_role("heading", name="Ошибка отчёта о нанесении")).to_be_visible()
            expect(page.locator('.next-step [data-action="retry-report-doc"]')).to_have_text("Отправить повторно")
            page.screenshot(path=str(out / "report-error-retry.png"), full_page=True)
            assert not errors, errors
            browser.close()
        print(
            "Browser smoke: managers and logistics roles, registry, completion, typed stop and admin-only restoration, shared light/dark themes, saved preference, invoice editing, Excel automatic detection and mapping, persistence, filters and mobile layout OK. External writes disabled."
        )
    finally:
        process.terminate()
        process.wait(timeout=20)
