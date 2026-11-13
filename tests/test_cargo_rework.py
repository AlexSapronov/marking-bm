import sqlite3
import pytest
from conftest import advance, client
from test_stop_archive import make_stage
from marking_bm.service import Conflict


def test_rework_move_applied_and_rebuild_without_new_codes(env):
    app, s, remote = env
    source = make_stage(env, 'done')
    admin = client(app, admin=True)
    manager = client(app)
    before = s.store.all('SELECT * FROM codes ORDER BY id')
    assert manager.post(f'/api/admin/documents/{source}/cargo-rework', json={'number':'STOP-001'}).status_code == 403
    r = admin.post(f'/api/admin/documents/{source}/cargo-rework', json={'number':'STOP-001'})
    assert r.status_code == 200, r.text
    assert s.get(source, 'alice')['status'] == 'cargo_breakup'
    advance(app)
    assert s.get(source, 'alice')['status'] == 'cargo_edit', s.store.all('SELECT kind,state,error FROM operations')
    target = s.create('alice', 'TARGET', 'Supplier A')
    line = s.get(source, 'alice')['lines'][0]
    with pytest.raises(PermissionError):
        s.move_lines_preview(source, target, [line['id']], 'alice')
    plan = s.move_lines_preview(source, target, [line['id']], 'administrator', True)
    s.move_lines(source, target, [line['id']], plan['fingerprint'], 'administrator', True)
    after = s.store.all('SELECT * FROM codes ORDER BY id')
    for a, b in zip(before, after):
        for key in ('raw', 'cis', 'gtin', 'quantity', 'pn', 'tnved', 'source_op', 'external_status'):
            assert a[key] == b[key]
        assert b['doc_id'] == target
    with pytest.raises(sqlite3.IntegrityError):
        with s.store.transaction() as db:
            db.execute('UPDATE codes SET quantity=quantity+1 WHERE doc_id=?', (target,))
    with pytest.raises(sqlite3.IntegrityError):
        with s.store.transaction() as db:
            db.execute('UPDATE codes SET doc_id=? WHERE doc_id=?', (source, target))
    current = s.get(target, 'alice')
    assert admin.post(f'/api/admin/documents/{target}/rename', json={'number':'CORRECT','revision':current['revision']}).status_code == 200
    assert manager.post(f'/api/documents/{target}/lines', json={'rows':[], 'revision':0}).status_code == 409
    counts = (len(remote.orders), len(remote.reports))
    s.rebuild_cargo(target, 'administrator')
    advance(app)
    assert s.get(target, 'alice')['status'] == 'done'
    assert (len(remote.orders), len(remote.reports)) == counts
    assert s.store.one('SELECT COUNT(*) n FROM codes')['n'] == len(before)
    assert not s.store.all('SELECT * FROM cargo_move_permits')
    with s.store.read() as db:
        assert s.introduction_scope(db, target)[0]['atks']


def test_rework_rejects_introduction_and_stale_rename(env):
    app, s, _ = env
    doc = make_stage(env, 'done')
    d = s.get(doc, 'alice')
    with pytest.raises(Conflict):
        s.rename_document(doc, 'X', d['revision']-1, 'administrator')
    with s.store.transaction() as db:
        a = db.execute('SELECT * FROM atks').fetchone()
        db.execute('INSERT INTO introduction_atks VALUES(?,?)', (a['id'], a['op_id']))
    with pytest.raises(Conflict):
        s.cargo_rework(doc, d['number'], 'administrator')


def test_bridge_revoke_blocks_disaggregation_until_ack_and_retries_same_revision(env):
    import json
    import httpx
    from marking_bm.receipt_bridge import deliver
    app, s, _ = env
    doc = make_stage(env, 'done')
    s.settings.receipt_bridge_url = 'http://warehouse'
    s.settings.receipt_bridge_key = 'k'*48
    s.cargo_rework(doc, 'STOP-001', 'administrator')
    op = s.store.one("SELECT * FROM operations WHERE kind='disaggregate'")
    with s.store.read() as db:
        assert s.operation_stopped(db, op)
    seen = []
    def fail(request):
        seen.append(request.content)
        assert request.url.path.endswith('/receipts/revoke')
        return httpx.Response(409)
    deliver(s, httpx.MockTransport(fail))
    assert s.store.one('SELECT state FROM receipt_deliveries')['state'] == 'error'
    assert s.get(doc, 'alice')['status'] == 'cargo_breakup'
    with s.store.transaction() as db:
        db.execute('UPDATE receipt_deliveries SET next_attempt=0')
    def ok(request):
        assert request.content == seen[0]
        body = json.loads(request.content)
        return httpx.Response(200, json={'document_id':'warehouse-id', 'revision':body['revision'],'status':'revoked'})
    deliver(s, httpx.MockTransport(ok))
    assert s.store.one('SELECT state FROM receipt_deliveries')['state'] == 'revoked'
    with s.store.read() as db:
        assert not s.operation_stopped(db, op)
    # Avoid actual HTTP during mocked CRPT ticks; there are no pending bridge jobs now.
    advance(app)
    assert s.get(doc, 'alice')['status'] == 'cargo_edit'


def test_rename_completed_invoice_preserves_atk_and_regenerates_exports(env):
    app, s, remote = env
    doc = make_stage(env, 'done')
    before = s.store.all('SELECT * FROM codes ORDER BY id')
    d = s.get(doc, 'alice')
    s.rename_document(doc, 'NEW-NUMBER', d['revision'], 'administrator')
    assert s.get(doc, 'alice')['number'] == 'NEW-NUMBER'
    assert s.store.all('SELECT * FROM codes ORDER BY id') == before
    assert s.store.one("SELECT name FROM artifacts WHERE kind='atk' ORDER BY version DESC LIMIT 1")['name'].find('NEW-NUMBER') >= 0


def test_partial_move_preserves_remaining_codes_and_rebuilds_each_invoice(env):
    from conftest import GTIN, TN, add_doc
    app, s, remote = env
    source = add_doc(s, number='PARTIAL')
    raw = [['PN-A', GTIN, 5, 2, TN], ['PN-A', GTIN, 9, 1, TN]]
    plan = s.preview(source, 'alice', raw)
    s.save_lines(source, 'alice', raw, 1, plan['fingerprint'])
    s.order(source, 'alice')
    advance(app)
    s.transferred(source, 'alice', 1)
    s.batch('alice', [source], '2026-01-01')
    advance(app)
    s.cargo_rework(source, 'PARTIAL', 'administrator')
    advance(app)
    target = s.create('alice', 'OTHER', 'Supplier A')
    line = next(l for l in s.get(source, 'alice')['lines'] if l['quantity'] == 9)
    preview = s.move_lines_preview(source, target, [line['id']], 'administrator', True)
    assert preview['codes'] == 1 and preview['units'] == 9
    s.move_lines(source, target, [line['id']], preview['fingerprint'], 'administrator', True)
    assert len(s.get(source, 'alice')['codes']) == 2
    assert len(s.get(target, 'alice')['codes']) == 1
    counts = len(remote.orders), len(remote.reports)
    for doc in (source, target):
        s.rebuild_cargo(doc, 'administrator')
    advance(app)
    for doc in (source, target):
        assert s.get(doc, 'alice')['status'] == 'done'
        with s.store.read() as db:
            assert s.introduction_scope(db, doc)[0]['atks']
    assert counts == (len(remote.orders), len(remote.reports))
