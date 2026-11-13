"""Cross-component boundaries missed by isolated Hub and connector suites."""
import secrets
import time
import pytest
from fastapi.testclient import TestClient
from hub.app import create_app
from hub.normalize import normalize_command


def test_external_tls_origin_is_independent_of_upstream_host(tmp_path):
    app=create_app(tmp_path/'hub.sqlite',public_url='https://hub.example.test',connector_url='https://connect.example.test')
    with TestClient(app,base_url='http://127.0.0.1:8785') as client:
        body={'id':'test-a','name':'A','baseUrl':'https://world.example.test'}
        good=client.post('/api/worlds',headers={'X-Hub-Request':'1','Origin':'https://hub.example.test'},json=body)
        assert good.status_code==201 and 'set-cookie' not in good.headers
        assert client.post('/api/worlds',headers={'X-Hub-Request':'1','Origin':'http://127.0.0.1:8785'},json=body).status_code==403
        assert client.post('/api/worlds',headers={'X-Hub-Request':'1','Origin':'https://connect.example.test'},json=body).status_code==403


def test_connector_unknown_outcome_is_uncertain_not_failed(tmp_path):
    token = secrets.token_urlsafe(32)
    app = create_app(tmp_path/'hub.sqlite', public_url="http://127.0.0.1", connector_url="http://127.0.0.1:8081")
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1',50000)) as client:
        auth={'X-Hub-Request':'1','Origin':'http://127.0.0.1'}
        world=client.post('/api/worlds', headers=auth, json={'id':'test-a','name':'A','baseUrl':'http://localhost:8786'}).json()
        with client.websocket_connect('ws://127.0.0.1/api/connect/test-a', headers={'Authorization':'Bearer '+world['connectorToken']}) as ws:
            ws.send_json({'type':'snapshot','protocolVersion':1,'worldId':'test-a','observedAt':'2026-11-12T03:00:00Z','agents':[{'id':'helper','name':'Helper'}],'conversations':[],'questions':[]})
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                rows=client.get('/api/worlds',headers=auth).json()['items']
                if rows[0]['snapshot'] is not None: break
                time.sleep(.005)
            result=client.post('/api/worlds/test-a/commands',headers={**auth,'Idempotency-Key':'unknown-outcome'},json={'action':'start_conversation','payload':{'agentId':'helper','message':'hello'}})
            assert result.status_code==202
            cmd=ws.receive_json()
            ws.send_json({'type':'result','id':cmd['id'],'ok':False,'error':'outcome_unknown'})
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                saved=client.get('/api/commands/'+cmd['id'],headers=auth).json()
                if saved['status'] in ['succeeded','failed','uncertain']: break
                time.sleep(.005)
            assert saved['status']=='uncertain'
            assert saved['error']=='outcome_unknown'


def test_hub_title_limit_matches_connector():
    with pytest.raises(ValueError):
        normalize_command({'action':'start_conversation','payload':{'agentId':'helper','title':'x'*201,'message':'hello'}})


def test_websocket_tls_exception_uses_peer_not_host():
    from types import SimpleNamespace
    from hub.security import websocket_transport_allowed
    peer = SimpleNamespace(client=SimpleNamespace(host='198.51.100.10'), url=SimpleNamespace(scheme='ws', hostname='localhost'))
    assert not websocket_transport_allowed(peer, False)
    peer.client.host='127.0.0.1'
    assert websocket_transport_allowed(peer, False)
    peer.client.host='100.81.42.2'
    assert not websocket_transport_allowed(peer, False, 'https://hub.example', '')
    assert websocket_transport_allowed(peer, False, 'https://hub.example', '100.64.0.0/10')
