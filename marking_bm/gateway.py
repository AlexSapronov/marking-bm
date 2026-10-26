"""Live clients only. Injectable HTTP transport is used by tests, never runtime modes."""

import base64
import json
import re
from uuid import UUID
import httpx
from .db import dumps


class RemoteError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class ReportRejected(ValueError):
    """SÚZ explicitly rejected a report; a new report ID is required for retry."""


def report_rejection_message(response):
    # Read only bounded text from a known rejected report status. Never echo
    # arbitrary upstream bodies, which may contain confidential fields.
    fragments = []
    def walk(value, depth=0):
        if depth > 6 or len(fragments) >= 60:
            return
        if isinstance(value, str):
            fragments.append(value[:2000])
        elif isinstance(value, dict):
            for key in ("error", "errors", "message", "errorMessage",
                        "bufferErrors", "buffers", "reportErrors", "description"):
                if key in value:
                    walk(value[key], depth + 1)
            for value2 in list(value.values())[:15]:
                if isinstance(value2, (dict, list)):
                    walk(value2, depth + 1)
        elif isinstance(value, list):
            for item in value[:25]:
                walk(item, depth + 1)
    walk(response)
    details = " ".join(fragments).lower()
    if ("не найдена карточка товара" in details
            or ("буфер для gtin" in details and "нет прав" in details)):
        gtins = sorted(set(re.findall(r"(?<!\\d)\\d{14}(?!\\d)", details)))[:10]
        suffix = " GTIN: " + ", ".join(gtins) + "." if gtins else ""
        return (
            "Карточка в Национальном каталоге ещё не готова или нет прав "
            "на этот GTIN. Попробуйте сформировать отчёт позже "
            "или свяжитесь с ответственным." + suffix
        )
    return "Отчёт отклонён СУЗ. Проверьте причину в ЛК ЧЗ перед повторной отправкой."


class Gateway:
    def __init__(self, settings, config, transport=None):
        self.settings, self.config, self.transport = settings, config, transport

    def request(
        self,
        method,
        url,
        *,
        body=None,
        params=None,
        oms=False,
        mutating=False,
        signature=None,
        auth=False,
    ):
        headers = {"Accept": "application/json"}
        if not auth:
            kind = "oms" if oms else "true"
            token = self.settings.secrets().get(kind, {}).get("token")
            if not token:
                raise ValueError(
                    "Обновите доступ к " + ("СУЗ" if oms else "ГИС МТ") + " в админке"
                )
            headers["clientToken" if oms else "Authorization"] = (
                token if oms else "Bearer " + token
            )
        if body is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        if signature:
            headers["X-Signature"] = signature
        if oms:
            params = {"omsId": self.config()["oms_id"], **(params or {})}
        if mutating and not self.settings.send_enabled:
            raise ValueError("Отправка операций отключена администратором сервера")
        try:
            with httpx.Client(
                timeout=httpx.Timeout(40, connect=10),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                r = client.request(
                    method, url, content=body, params=params, headers=headers
                )
        except httpx.HTTPError as e:
            raise RemoteError(
                "Нет подтверждения от ЧЗ" if mutating else "ЧЗ недоступен", mutating
            ) from e
        if r.status_code not in (200, 201):
            # Do not echo external messages: responses may contain signatures/tokens.
            raise RemoteError(
                f"ЧЗ: HTTP {r.status_code}. Проверьте права и состояние операции в ЛК.",
                mutating and (r.status_code >= 500 or r.status_code in (408, 409, 429)),
            )
        try:
            return r.json()
        except ValueError:
            if re.fullmatch(r"[0-9a-fA-F-]{36}", r.text.strip()):
                return r.text.strip()
            raise RemoteError("Нераспознанный ответ ЧЗ", mutating)

    def oms(self, path, **kwargs):
        return self.request(
            "POST" if kwargs.get("body") is not None else "GET",
            self.settings.oms_url + "/api/v3/" + path,
            oms=True,
            **kwargs,
        )

    def info(self, cises):
        result = {}
        for i in range(0, len(cises), 1000):
            expected = cises[i : i + 1000]
            r = self.request(
                "POST",
                self.settings.true_url + "/api/v3/true-api/cises/info",
                body=dumps(expected).encode(),
                params={"pg": "radio"},
            )
            if not isinstance(r, list):
                raise ValueError("Неверный ответ проверки КМ")
            for item in r:
                if (
                    not isinstance(item, dict)
                    or item.get("errorCode")
                    or item.get("errorMessage")
                ):
                    raise ValueError("ЧЗ не подтвердил сведения обо всех КМ")
                ci = item.get("cisInfo", {})
                cis = ci.get("requestedCis", ci.get("cis"))
                if cis not in expected or cis in result:
                    raise ValueError("ЧЗ вернул другой или повторный КМ")
                result[cis] = ci
            if not all(c in result for c in expected):
                raise ValueError("ЧЗ не вернул все запрошенные КМ")
        return result

    def verify(self, codes, status, *, aggregate=False, parent=None):
        info = self.info([c["cis"] for c in codes])
        for code in codes:
            ci = info[code["cis"]]
            if ci.get("status") != status or ci.get("ownerInn") != self.config()["inn"]:
                raise ValueError(
                    "Статус или собственник КМ в ЧЗ не соответствует операции"
                )
            if aggregate:
                if ci.get("emissionType") != "FOREIGN" or ci.get("statusEx") not in (
                    None,
                    "EMPTY",
                    "FTS_RESPOND_NOT_OK",
                ):
                    raise ValueError("ЧЗ не разрешает включение КМ в АТК")
                if ci.get("parent") not in (None, "", parent):
                    raise ValueError("КМ уже входит в другой агрегат")
                if ci.get("tnVedEaes") != code["tnved"]:
                    raise ValueError("ЧЗ не подтвердил полный ТН ВЭД для КМ")
        return info

    @staticmethod
    def mutation_id(value):
        # The request already reached the external API. Invalid response IDs do
        # not prove rejection and must never turn into a retryable validation error.
        if not isinstance(value, str):
            raise RemoteError("ЧЗ не подтвердил ID отправленной операции. Нужна сверка с ЛК", True)
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError) as error:
            raise RemoteError("ЧЗ вернул некорректный ID отправленной операции. Нужна сверка с ЛК", True) from error

    def submit(self, op):
        kind = op["kind"]
        payload = op["payload"].encode()
        sig = op.get("signature")
        if kind in ("auth_true", "auth_oms"):
            challenge = json.loads(op["payload"])
            path = "/api/v3/true-api/auth/simpleSignIn"
            if kind == "auth_oms":
                path += "/" + self.config()["oms_connection"]
            body = {"uuid": challenge["uuid"], "data": sig, "inn": self.config()["inn"]}
            if kind == "auth_true":
                body["unitedToken"] = True
            r = self.request(
                "POST",
                self.settings.true_url + path,
                body=dumps(body).encode(),
                auth=True,
                mutating=True,
            )
            token = (
                r.get("token" if kind == "auth_oms" else "uuidToken")
                if isinstance(r, dict)
                else None
            )
            if not token:
                raise RemoteError("ЧЗ не выдал токен", True)
            self.settings.save_token(
                "oms" if kind == "auth_oms" else "true", token, r.get("expireDate")
            )
            return {
                "id": op["id"],
                "complete": True,
                "receipt": {"authorized": True, "expires": r.get("expireDate")},
            }
        if kind == "order":
            r = self.oms("order", body=payload, signature=sig, mutating=True)
            key = "orderId"
        elif kind == "report":
            r = self.oms("utilisation", body=payload, signature=sig, mutating=True)
            key = "reportId"
        elif kind == "fetch":
            r = self.oms("codes", params=json.loads(op["payload"]), mutating=True)
            if (
                not isinstance(r, dict)
                or not r.get("blockId")
                or not isinstance(r.get("codes"), list)
            ):
                raise RemoteError("Не подтверждено получение блока КМ", True)
            return {"id": r["blockId"], "complete": True, "receipt": r}
        elif kind in ("atk", "transform"):
            body = {
                "document_format": "MANUAL",
                "product_document": base64.b64encode(payload).decode(),
                "type": "ATK_AGGREGATION" if kind == "atk" else "ATK_TRANSFORMATION",
                "signature": sig,
            }
            r = self.request(
                "POST",
                self.settings.true_url + "/api/v3/true-api/lk/documents/create",
                body=dumps(body).encode(),
                params={"pg": "radio"},
                mutating=True,
            )
            if not isinstance(r, str):
                raise RemoteError("Не подтверждён ID документа АТК", True)
            return {"id": self.mutation_id(r), "complete": False}
        else:
            raise ValueError("Недопустимый тип операции")
        if not isinstance(r, dict) or not r.get(key):
            raise RemoteError("ЧЗ не подтвердил ID операции", True)
        return {"id": self.mutation_id(r[key]), "complete": False}

    def poll(self, op):
        ext = op["external_id"]
        kind = op["kind"]
        if kind == "order":
            r = self.oms("order/status", params={"orderId": ext})
            if not isinstance(r, list):
                raise ValueError("Неверный ответ статуса заказа")
            if any(x.get("bufferStatus") == "REJECTED" for x in r):
                raise ValueError("Заказ отклонён СУЗ")
            return {"complete": False, "buffers": r, "receipt": r}
        if kind == "report":
            r = self.oms("report/info", params={"reportId": ext})
            if r.get("reportId") != ext:
                raise ValueError("Ответ относится к другому отчёту")
            if r.get("reportStatus") == "REJECTED":
                raise ReportRejected(report_rejection_message(r))
            if r.get("reportStatus") != "SUCCESS":
                return {"complete": False, "receipt": r}
            rec = self.oms("receipts/receipt", params={"resultDocId": ext})
            results = rec.get("results", [])
            if not results or any(
                x.get("resultDocId") != ext
                or x.get("state") != "SUCCESS"
                or x.get("code") != 0
                for x in results
            ):
                raise ValueError("Нет окончательной успешной квитанции отчёта")
            return {"complete": True, "receipt": rec}
        if kind in ("atk", "transform"):
            r = self.request(
                "GET",
                self.settings.true_url + f"/api/v4/true-api/doc/{ext}/info",
                params={"pg": "radio", "body": "true", "content": "true"},
            )
            if not isinstance(r, list) or len(r) != 1 or r[0].get("number") != ext:
                raise ValueError("ЧЗ не подтвердил документ АТК")
            doc = r[0]
            if (
                doc.get("errors")
                or doc.get("commonErrors")
                or doc.get("status") in ("CHECKED_NOT_OK", "PARSE_ERROR")
            ):
                raise ValueError("Документ АТК обработан с ошибкой")
            if doc.get("status") != "CHECKED_OK":
                return {"complete": False, "receipt": r}
            return {"complete": True, "receipt": r}
        raise ValueError("Проверка для данного типа не предусмотрена")

    def blocks(self, payload):
        r = self.oms(
            "order/codes/blocks",
            params={"orderId": payload["orderId"], "gtin": payload["gtin"]},
        )
        if (
            not isinstance(r, dict)
            or r.get("orderId") != payload["orderId"]
            or r.get("gtin") != payload["gtin"]
            or not isinstance(r.get("blocks"), list)
        ):
            raise ValueError("ЧЗ не подтвердил список блоков заказа")
        return r["blocks"]

    def recover_block(self, block_id):
        r = self.oms("order/codes/retry", params={"blockId": block_id})
        if (
            not isinstance(r, dict)
            or r.get("blockId") != block_id
            or not isinstance(r.get("codes"), list)
        ):
            raise ValueError("ЧЗ не подтвердил восстановленный блок")
        return r

    def challenge(self):
        r = self.request(
            "GET", self.settings.true_url + "/api/v3/true-api/auth/key", auth=True
        )
        if (
            not isinstance(r, dict)
            or not isinstance(r.get("uuid"), str)
            or not isinstance(r.get("data"), str)
        ):
            raise ValueError("Неверный ответ авторизации")
        return r
