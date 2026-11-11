import json
from io import BytesIO
import zipfile

import pytest
from conftest import GTIN, TN, advance, client, sign_all
from marking_bm.service import Conflict

NEW_GTIN = '04640638342118'


def failed_document(env, partial=True):
    app, s, registry = env
    doc = s.create('alice', 'CORRECTION', 'Supplier A')
    raw = [['GOOD', GTIN, 5, 2, TN], ['BAD', GTIN, 7, 1, TN]]
    p = s.preview(doc, 'alice', raw)
    s.save_lines(doc, 'alice', raw, 0, p['fingerprint'])
    s.order(doc, 'alice')
    advance(app)
    s.transferred(doc, 'alice', 1)
    s.batch('alice', [doc], '2026-01-01')
    sign_all(s)
    app.state.worker.tick()
    op = s.store.one("SELECT * FROM operations WHERE kind='report' AND doc_id=?", (doc,))
    good = s.store.all("SELECT * FROM codes WHERE doc_id=? AND pn='GOOD'", (doc,))
    if partial:
        registry.partial_report[op['external_id']] = {c['cis'] for c in good}
    else:
        registry.fail_report.add(op['external_id'])
    app.state.worker.tick()
    assert s.get(doc, 'alice')['status'] == 'reporting'
    return doc, good, op


def test_correction_full_cycle_preserves_applied_codes_and_batch(env):
    app, s, registry = env
    doc, good, original = failed_document(env)
    admin = client(app, 'administrator', True)
    base = f'/api/admin/documents/{doc}'
    assert client(app).get(base + '/correction-preview').status_code == 403
    plan = admin.get(base + '/correction-preview').json()
    assert {l['pn']: l['editable'] for l in plan['lines']} == {'GOOD': False, 'BAD': True}
    assert admin.post(base + '/return-for-correction', json={'fingerprint': plan['fingerprint']}).status_code == 200
    assert admin.post(base + '/return-for-correction', json={'fingerprint': plan['fingerprint']}).status_code == 409
    lines = {l['pn']: l for l in plan['lines']}
    assert admin.post(base + '/correct-gtin', json={'replacements': {str(lines['GOOD']['id']): NEW_GTIN}}).status_code == 409
    bad = s.store.one("SELECT * FROM codes WHERE doc_id=? AND pn='BAD'", (doc,))
    response = admin.post(base + '/correct-gtin', json={'replacements': {str(lines['BAD']['id']): NEW_GTIN}})
    assert response.status_code == 200, response.text
    advance(app)
    d = s.get(doc, 'alice')
    assert d['status'] == 'ready', d.get('last_error')
    assert d['current_version'] == 2
    assert {c['cis'] for c in d['codes'] if c['pn'] == 'GOOD'} == {c['cis'] for c in good}
    assert s.store.one('SELECT local_state,reuse_allowed,doc_id FROM codes WHERE id=?', (bad['id'],)) == {'local_state': 'retired', 'reuse_allowed': 0, 'doc_id': None}
    art = s.store.one("SELECT * FROM artifacts WHERE doc_id=? AND version=2 AND kind='labels'", (doc,))
    snapshot = json.loads(art['snapshot'])
    assert len(snapshot) == 3 and bad['cis'] not in {c['cis'] for c in snapshot}
    with zipfile.ZipFile(BytesIO(art['content'])) as archive:
        assert len(archive.namelist()) == 2
        assert any(NEW_GTIN in name for name in archive.namelist())
    assert len(registry.orders) == 2  # one initial order plus one replacement
    with pytest.raises(Conflict):
        s.transferred(doc, 'alice', 1)
    s.transferred(doc, 'alice', 2)
    old_batch = s.store.one('SELECT batch_id FROM batch_docs WHERE doc_id=?', (doc,))['batch_id']
    registry.partial_report.clear()
    assert s.batch('alice', [doc], '2026-01-01') == old_batch
    report = s.store.one("SELECT * FROM operations WHERE doc_id=? AND kind='report' ORDER BY rowid DESC LIMIT 1", (doc,))
    payload = json.loads(report['payload'])
    assert len(payload['sntins']) == 1 and NEW_GTIN in payload['sntins'][0]
    assert json.loads(original['payload'])['sntins'] == json.loads(s.store.one('SELECT payload FROM operations WHERE id=?', (original['id'],))['payload'])['sntins']
    advance(app)
    assert s.get(doc, 'alice')['status'] == 'done'
    assert s.store.one('SELECT COUNT(*) n FROM atks')['n'] == 1


def test_correction_refuses_uncertain_report_or_registry(env):
    app, s, registry = env
    doc, good, original = failed_document(env)
    with pytest.raises(PermissionError):
        s.correction_plan(doc, 'alice')
    registry.codes[good[0]['cis']]['status'] = 'INTRODUCED'
    with pytest.raises(Conflict):
        s.correction_plan(doc, 'administrator', True)
    registry.codes[good[0]['cis']]['status'] = 'APPLIED'
    with s.store.transaction() as db:
        db.execute("UPDATE operations SET state='unknown' WHERE id=?", (original['id'],))
    with pytest.raises(Conflict):
        s.correction_plan(doc, 'administrator', True)


def test_correction_preview_becomes_stale_when_registry_changes(env):
    app, s, registry = env
    doc, good, original = failed_document(env)
    plan = s.correction_plan(doc, 'administrator', True)
    bad = s.store.one("SELECT cis FROM codes WHERE doc_id=? AND pn='BAD'", (doc,))
    registry.codes[bad['cis']]['status'] = 'APPLIED'
    with pytest.raises(Conflict):
        s.return_for_correction(doc, 'administrator', plan['fingerprint'], True)
    assert s.get(doc, 'alice')['status'] == 'reporting'


def test_rejected_report_keeps_unchanged_emitted_codes(env):
    app, s, registry = env
    doc, good, original = failed_document(env, partial=False)
    plan = s.correction_plan(doc, 'administrator', True)
    assert all(l['editable'] for l in plan['lines'])
    s.return_for_correction(doc, 'administrator', plan['fingerprint'], True)
    bad = next(l for l in plan['lines'] if l['pn'] == 'BAD')
    with pytest.raises(Conflict):
        s.save_correction(doc, 'administrator', {str(bad['id']): '12345678901234'}, True)
    assert s.get(doc, 'alice')['status'] == 'correction'
    s.save_correction(doc, 'administrator', {str(bad['id']): NEW_GTIN}, True)
    advance(app)
    assert s.get(doc, 'alice')['status'] == 'ready'
    assert {c['cis'] for c in s.get(doc, 'alice')['codes'] if c['pn'] == 'GOOD'} == {c['cis'] for c in good}
    s.transferred(doc, 'alice', 2)
    s.batch('alice', [doc], '2026-01-01')
    report = s.store.one("SELECT payload FROM operations WHERE kind='report' AND doc_id=? ORDER BY rowid DESC LIMIT 1", (doc,))
    assert len(json.loads(report['payload'])['sntins']) == 3
    advance(app)
    assert s.get(doc, 'alice')['status'] == 'done'


def test_partial_line_is_locked_and_registry_change_blocks_save(env):
    app, s, registry = env
    doc, good, original = failed_document(env)
    registry.codes[good[0]['cis']]['status'] = 'EMITTED'
    plan = s.correction_plan(doc, 'administrator', True)
    line = next(l for l in plan['lines'] if l['pn'] == 'GOOD')
    assert line['applied'] == line['emitted'] == 1 and not line['editable']
    s.return_for_correction(doc, 'administrator', plan['fingerprint'], True)
    bad = next(l for l in plan['lines'] if l['pn'] == 'BAD')
    old = s.store.one("SELECT * FROM codes WHERE doc_id=? AND pn='BAD'", (doc,))
    registry.codes[old['cis']]['status'] = 'APPLIED'
    with pytest.raises(Conflict):
        s.save_correction(doc, 'administrator', {str(bad['id']): NEW_GTIN}, True)
    assert s.store.one('SELECT doc_id FROM codes WHERE id=?', (old['id'],))['doc_id'] == doc


def test_replacement_uses_verified_pool_and_pdf_retry_preserves_assignments(env):
    app, s, registry = env
    donor = s.create('alice', 'POOL', 'Supplier B')
    raw = [['POOL-PN', NEW_GTIN, 1, 1, TN]]
    p = s.preview(donor, 'alice', raw)
    s.save_lines(donor, 'alice', raw, 0, p['fingerprint'])
    s.order(donor, 'alice')
    advance(app)
    with s.store.transaction() as db:
        s.release(db, donor)
        db.execute("UPDATE documents SET status='draft' WHERE id=?", (donor,))
    doc, good, original = failed_document(env)
    plan = s.correction_plan(doc, 'administrator', True)
    s.return_for_correction(doc, 'administrator', plan['fingerprint'], True)
    bad = next(l for l in plan['lines'] if l['pn'] == 'BAD')
    orders_before = len(registry.orders)
    s.save_correction(doc, 'administrator', {str(bad['id']): NEW_GTIN}, True)
    assert s.get(doc, 'alice')['status'] == 'ready'
    assert len(registry.orders) == orders_before
    with s.store.transaction() as db:
        db.execute("UPDATE documents SET status='label_error' WHERE id=?", (doc,))
    s.retry_labels(doc, 'alice')
    assert s.get(doc, 'alice')['status'] == 'ready'
    assert s.store.one('SELECT COUNT(*) n FROM assignments WHERE doc_id=? AND active=1', (doc,))['n'] == 3
