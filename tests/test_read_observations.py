from types import SimpleNamespace
import base64
import json

import pytest
from fastapi import HTTPException

from mediavpn.seedbox import Seedbox


def test_read_cache_never_reconciles_and_mutations_revalidate(tmp_path):
    box = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    calls = []
    ready = True
    box.reconcile = lambda: calls.append('reconcile')
    box.status = lambda verify_client=True: calls.append(('verify', verify_client)) or {'ready': ready}
    box.child = lambda: 'synthetic'
    box.execute = lambda *args: calls.append('execute') or {'status':200,'body':'W10='}
    assert box.rpc('torrents/info','GET',{'limit':500})['status'] == 200
    box.rpc('torrents/info','GET',{'limit':500})
    assert calls.count('execute') == 1 and 'reconcile' not in calls
    ready = False
    with pytest.raises(HTTPException) as error:
        box.rpc('torrents/info','GET',{'limit':500})
    assert error.value.status_code == 409  # Cached data never bypasses namespace health.
    with pytest.raises(HTTPException):
        box.rpc('torrents/start','POST',{'hashes':'a'*40})
    assert 'reconcile' in calls and ('verify',True) in calls
    assert not box.read_cache


@pytest.mark.parametrize('previous', [None, (19, {'ready': True, 'downloadsEnabled': True})])
@pytest.mark.parametrize('ready', [True, False])
def test_expired_or_missing_display_observation_rechecks_binding(tmp_path, monkeypatch, previous, ready):
    box = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    monkeypatch.setattr('mediavpn.seedbox.time.monotonic',lambda:30)
    box.status_snapshot = previous
    calls = []
    box.reconcile = lambda: pytest.fail('Display must not change runtime')
    def status(verify_client=True):
        calls.append(verify_client)
        return {'ready': ready, 'downloadsEnabled': ready}
    box.status = status
    assert box.display_status() == {'ready': ready, 'downloadsEnabled': ready}
    assert box.display_status()['ready'] is ready
    assert calls == [True]  # Concurrent/repeated readers reuse the new observation.


def test_expired_display_never_reuses_ready_sample_after_check_failure(tmp_path, monkeypatch):
    box = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    monkeypatch.setattr('mediavpn.seedbox.time.monotonic', lambda: 30)
    box.status_snapshot = (19, {'ready': True, 'downloadsEnabled': True})
    def failed_status():
        raise HTTPException(503, 'Synthetic observation failure')
    box.status = failed_status
    with pytest.raises(HTTPException) as error:
        box.display_status()
    assert error.value.status_code == 503


def test_supervisor_batches_status_and_rejects_wrong_binding(tmp_path):
    box = Seedbox(SimpleNamespace(enabled=False), tmp_path)
    box.status = lambda **kwargs: {'ready':True,'downloadsEnabled':True,'internalPort':1234,'forwardedPort':1234}
    box.child = lambda: 'synthetic'
    paths = []
    preferences = {'current_network_interface':'tun0','listen_port':1234,'upnp':False}
    def encoded(value):
        return {'status':200,'body':base64.b64encode(json.dumps(value).encode()).decode()}
    def execute(child,path,*args):
        paths.append(path)
        return encoded({'app/preferences':encoded(preferences),'torrents/info':encoded([]),'transfer/info':encoded({})})
    box.execute = execute
    box.refresh_snapshot()
    assert paths == ['mediaseed/status']
    assert box.display_status()['ready']
    assert len(box.read_cache) == 3
    preferences['current_network_interface'] = 'eth0'
    box.refresh_snapshot()
    assert not box.display_status()['ready'] and not box.read_cache
