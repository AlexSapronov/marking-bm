import pytest
from conftest import advance, client, sign_all
from test_stop_archive import make_stage


def test_admin_can_force_only_atk_without_changing_payload_or_codes(env):
    app, s, remote = env
    make_stage(env, 'atk_signing')
    op = s.store.one("SELECT * FROM operations WHERE kind='atk'")
    before = s.store.all('SELECT raw,quantity,tnved FROM codes ORDER BY id')
    for ci in remote.codes.values():
        ci['tnVedEaes'] = '8541410002'
    sign_all(s)
    app.state.worker.tick()
    assert s.store.one('SELECT state FROM operations WHERE id=?', (op['id'],))['state'] == 'preflight_blocked'
    url = f"/api/admin/operations/{op['id']}/force-atk"
    body = {'confirm': True, 'reason': 'Разрешено расхождение с карточкой'}
    assert client(app).post(url, json=body).status_code == 403
    admin = client(app, admin=True)
    assert admin.post(url, json={'confirm': False, 'reason': 'test'}).status_code == 422
    assert admin.post(url, json=body).status_code == 200
    assert admin.post(url, json=body).status_code == 409
    advance(app)
    after = s.store.one('SELECT * FROM operations WHERE id=?', (op['id'],))
    assert after['state'] == 'done', after
    assert after['payload'] == op['payload'] and after['sha256'] == op['sha256']
    assert s.store.all('SELECT raw,quantity,tnved FROM codes ORDER BY id') == before
    report = s.store.one("SELECT id FROM operations WHERE kind='report'")
    assert admin.post(f"/api/admin/operations/{report['id']}/force-atk", json=body).status_code == 409


@pytest.mark.parametrize('field,value', [('status','EMITTED'), ('ownerInn','wrong'), ('parent','other-atk'), ('emissionType','LOCAL'), ('statusEx','FTS_CONTROL')])
def test_force_keeps_other_preflight_guards(env, monkeypatch, field, value):
    _, s, _ = env
    code = {'cis':'test', 'tnved':'8541410002'}
    ci = {'status':'APPLIED', 'ownerInn':s.config()['inn'], 'emissionType':'FOREIGN', 'tnVedEaes':'8541410004'}
    monkeypatch.setattr(s.gateway, 'info', lambda _: {'test':ci})
    s.gateway.verify([code], 'APPLIED', aggregate=True, allow_tnved_mismatch=True)
    ci[field] = value
    with pytest.raises(ValueError):
        s.gateway.verify([code], 'APPLIED', aggregate=True, allow_tnved_mismatch=True)
