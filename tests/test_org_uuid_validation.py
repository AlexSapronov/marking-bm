"""Organization input validation must identify the bad UUID field without a traceback."""
from conftest import client


def _payload(service):
    config = service.config()
    return {key: config[key] for key in (
        "organization", "inn", "oms_id", "oms_connection",
        "template_id", "kpp", "fias_id",
    )}


def test_organization_uuid_error_identifies_field(env):
    app, service, _ = env
    c = client(app, "administrator", True)
    for key, label in (
        ("oms_id", "OMS ID"),
        ("oms_connection", "omsConnection"),
        ("fias_id", "ФИАС места деятельности"),
    ):
        payload = _payload(service)
        payload[key] = "00000000-0000-0000-0000-00000000000а"  # Cyrillic a
        r = c.put("/api/admin/config", json=payload)
        assert r.status_code == 422, r.text
        assert label in r.json()["detail"]
        assert "badly formed hexadecimal UUID string" not in r.json()["detail"]


def test_pasted_uuids_trimmed_and_canonicalized(env):
    app, service, _ = env
    c = client(app, "administrator", True)
    payload = _payload(service)
    payload["oms_id"] = " " + payload["oms_id"].upper() + " "
    payload["oms_connection"] = "\t" + payload["oms_connection"] + "\n"
    payload["fias_id"] = " "
    r = c.put("/api/admin/config", json=payload)
    assert r.status_code == 200, r.text
    assert service.config()["oms_id"] == payload["oms_id"].strip().lower()
    assert service.config()["oms_connection"] == payload["oms_connection"].strip()
    assert service.config()["fias_id"] == ""
