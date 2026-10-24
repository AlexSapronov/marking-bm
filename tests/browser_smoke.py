"""Real Chromium against the live application. No external writes or simulated UI data."""

import os
from pathlib import Path
import socket
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
            page.goto(url + "/admin")
            page.locator("#login-value").fill("browser-smoke-password")
            page.locator("#login-form button").click()
            page.get_by_role("heading", name="Администрирование", exact=True).wait_for()
            expect(page.locator('[data-admin-panel="overview"]')).to_be_visible()
            expect(page.locator("#user-form")).not_to_be_visible()
            page.locator('.section-nav [data-id="users"]').click()
            page.locator("#username").fill("smoke-user")
            page.locator("#user-form button").click()
            page.get_by_text("smoke-user", exact=True).wait_for()
            page.locator("#username").fill("smoke-logist")
            page.locator("#new-user-role").select_option("logistics")
            page.locator("#user-form button").click()
            page.get_by_text("smoke-logist", exact=True).wait_for()
            page.locator('[data-section-search="users"]').fill("smoke-logist")
            expect(page.get_by_text("smoke-user", exact=True)).not_to_be_visible()
            expect(page.get_by_text("smoke-logist", exact=True)).to_be_visible()
            page.locator('[data-section-search="users"]').fill("")
            assert (
                page.locator('.user-role[data-user="smoke-logist"]').input_value()
                == "logistics"
            )
            page.get_by_role("button", name="Накладные", exact=True).click()
            page.get_by_role("button", name="+ Новая накладная").click()
            page.locator("#number").fill("SMOKE-001")
            page.locator("#new-supplier").fill("Поставщик smoke")
            page.get_by_role("button", name="Создать черновик").click()
            page.get_by_role("heading", name="Накладная SMOKE-001").wait_for()
            expect(page.locator("#document-save-state")).to_contain_text("сохранены")
            page.get_by_role("button", name="+ Добавить строку").click()
            expect(page.get_by_role("button", name="Заказать КМ", exact=True)).to_be_disabled()
            expect(page.locator("#document-save-state")).to_contain_text("несохранённые")
            page.locator(".pn-input").fill("L-132XYD")
            page.locator(".gtin-input").fill("04640638349704")
            page.locator(".qty-input").fill("5")
            page.locator(".labels-input").fill("3")
            page.locator(".tnved-input").fill("8536699008")
            page.get_by_role("button", name="Проверить и сохранить состав").click()
            page.get_by_text("Состав сохранён", exact=True).wait_for()
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
            page.get_by_role("heading", name="Подтверждение нанесения", exact=True).wait_for()
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
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.get_by_role("button", name="Накладные", exact=True).click()
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
            page.screenshot(path=str(out / "documents.png"), full_page=True)
            page.locator("#search").fill("nonexistent")
            assert page.locator(".doc-select").count() == 0
            page.get_by_role("button", name="Администрирование", exact=True).click()
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
            for section in ["operations", "approvals", "users", "organization", "connections", "catalog", "suppliers", "audit"]:
                page.locator(f'.section-nav [data-id="{section}"]').click()
                expect(page.locator(f'[data-admin-panel="{section}"]')).to_be_visible()
                expect(page.locator('[data-admin-panel]:visible')).to_have_count(1)
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
            page.locator("#login-value").fill("smoke-logist")
            page.locator("#login-form button").click()
            page.get_by_role("heading", name="Реестр АТК", exact=True).wait_for()
            assert page.get_by_role("button", name="Накладные", exact=True).count() == 0
            assert page.get_by_role("button", name="+ Новая накладная").count() == 0
            assert page.locator("#atk-table").count() == 1
            expect(page.get_by_role("button", name="Администрирование", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="Завершить накладную", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Завершить накладную", exact=True)).to_be_in_viewport(ratio=1)
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
            page.get_by_role("button", name="Завершить накладную", exact=True).click()
            page.locator('#modal [data-action="logistics-complete"]').click()
            page.get_by_role("button", name="Отмена", exact=True).click()
            expect(page.locator('#atk-table tbody tr')).to_have_count(1)
            page.get_by_role("button", name="Завершить накладную", exact=True).click()
            page.locator('#modal [data-action="logistics-complete"]').click()
            page.locator('#modal [data-action="confirm-logistics-complete"]').click()
            expect(page.locator('#atk-table tbody tr')).to_have_count(0)
            page.locator("#atk-archive").check()
            expect(page.locator('#atk-table tbody tr')).to_have_count(1)
            expect(page.locator('#atk-table')).to_contain_text("LOGISTICS-READY")
            expect(page.get_by_role("button", name="Завершить накладную", exact=True)).to_have_count(0)
            page.reload()
            # Archive choice is an in-session filter; the completed invoice stays out of work.
            expect(page.locator('#atk-table tbody tr')).to_have_count(0)
            page.get_by_role("button", name="Выйти", exact=True).click()
            page.locator("#login-value").fill("smoke-user")
            page.locator("#login-form button").click()
            page.get_by_role("heading", name="Накладные", exact=True).wait_for()
            expect(page.locator('[data-stage="preparation"]')).to_be_visible()
            expect(page.get_by_role("button", name="Администрирование", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="Реестр АТК", exact=True)).to_have_count(0)
            expect(page.get_by_role("columnheader", name="Владелец", exact=True)).to_have_count(0)
            assert not errors, errors
            browser.close()
        print(
            "Browser smoke: managers and logistics roles, registry and local completion, shared light/dark themes, saved preference, invoice editing, Excel automatic detection and mapping, persistence, filters and mobile layout OK. External writes disabled."
        )
    finally:
        process.terminate()
        process.wait(timeout=20)
