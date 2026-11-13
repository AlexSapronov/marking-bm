import pytest


@pytest.mark.parametrize('actual,expected', [
    (None, 'не заполнен'), ('', 'не заполнен'), ('854141', "'854141'"),
    ('8541410004', "'8541410004'"), (8541410002, 'тип int'),
])
def test_mismatch_reports_product_and_both_values_without_code(env, monkeypatch, actual, expected):
    _, service, _ = env
    gateway = service.gateway
    code = {'cis': 'private-cis', 'raw': 'private-crypto', 'pn': 'LTST-C195TGKRKT',
            'gtin': '04630750324741', 'tnved': '8541410002'}
    ci = {'status': 'APPLIED', 'ownerInn': service.config()['inn'],
          'emissionType': 'FOREIGN', 'tnVedEaes': actual}
    monkeypatch.setattr(gateway, 'info', lambda _: {'private-cis': ci})
    with pytest.raises(ValueError) as error:
        gateway.verify([code], 'APPLIED', aggregate=True)
    message = str(error.value)
    assert all(value in message for value in [code['pn'], code['gtin'], code['tnved'], expected])
    assert 'private-cis' not in message and 'private-crypto' not in message
    ci['tnVedEaes'] = code['tnved']
    assert gateway.verify([code], 'APPLIED', aggregate=True)['private-cis'] == ci
