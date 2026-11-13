"""Container restart guards use exact identities and never widen fixture scope."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
spec = importlib.util.spec_from_file_location('recovery_harness', TESTS / 'recovery_e2e.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def ingress_item():
    return {'Id': 'a' * 64, 'Name': '/fixture-ingress',
            'Config': {'Labels': {'com.docker.compose.project': 'fixture',
                                  'com.docker.compose.service': 'ingress'}},
            'State': {'Running': True},
            'HostConfig': {'NetworkMode': 'container:' + 'b' * 64},
            'Mounts': [{'Destination': '/etc/hub/nginx.conf.template', 'Type': 'bind',
                        'RW': False, 'Source': '/source/ingress/nginx.conf.template'},
                       {'Destination': '/etc/hub/start.sh', 'Type': 'bind',
                        'RW': False, 'Source': '/source/ingress/start.sh'}]}


def test_expected_ingress_is_accepted(monkeypatch):
    item = ingress_item()
    monkeypatch.setattr(recovery, 'engine_call', lambda *args, **kwargs: json.dumps([item]))
    result = recovery.inspect_ingress('podman', 'fixture-ingress', 'fixture',
                                      {'id': 'b' * 64, 'name': '/fixture-hub'})
    assert result['id'] == 'a' * 64
    assert result['mounts'] == item['Mounts']


@pytest.mark.parametrize('bad', ['project', 'service', 'namespace', 'writable', 'extra', 'source', 'identity', 'stopped'])
def test_foreign_ingress_is_rejected(monkeypatch, bad):
    item = ingress_item()
    if bad == 'project':
        item['Config']['Labels']['com.docker.compose.project'] = 'other'
    elif bad == 'service':
        item['Config']['Labels']['com.docker.compose.service'] = 'world-b'
    elif bad == 'namespace':
        item['HostConfig']['NetworkMode'] = 'container:' + 'c' * 64
    elif bad == 'writable':
        item['Mounts'][0]['RW'] = True
    elif bad == 'extra':
        item['Mounts'].append(copy.deepcopy(item['Mounts'][0]))
    elif bad == 'source':
        item['Mounts'][0]['Source'] = '/private/config'
    elif bad == 'identity':
        item['Name'] = '/another-container'
    else:
        item['State']['Running'] = False
    monkeypatch.setattr(recovery, 'engine_call', lambda *args, **kwargs: json.dumps([item]))
    with pytest.raises(recovery.HarnessError):
        recovery.inspect_ingress('podman', 'fixture-ingress', 'fixture',
                                 {'id': 'b' * 64, 'name': '/fixture-hub'})


def test_restart_orders_hub_before_verified_ingress(monkeypatch):
    hub = {'id': 'b' * 64, 'name': '/fixture-hub'}
    ingress = {'id': 'a' * 64, 'name': '/fixture-ingress'}
    calls = []
    monkeypatch.setattr(recovery, 'inspect_ingress', lambda *args: ingress)
    monkeypatch.setattr(recovery, 'inspect_fixture', lambda *args: hub)
    monkeypatch.setattr(recovery, 'restart_fixture', lambda *args: calls.append('hub'))
    monkeypatch.setattr(recovery, 'engine_call', lambda engine, args, **kwargs: calls.append(args))
    recovery.restart_hub_and_ingress('podman', hub, ingress, 'fixture', object())
    assert calls == ['hub', ['restart', '--time', '15', 'a' * 64]]


def test_changed_ingress_prevents_any_restart(monkeypatch):
    hub = {'id': 'b' * 64, 'name': '/fixture-hub'}
    ingress = {'id': 'a' * 64, 'name': '/fixture-ingress'}
    monkeypatch.setattr(recovery, 'inspect_ingress', lambda *args: {'id': 'c' * 64})
    monkeypatch.setattr(recovery, 'restart_fixture', lambda *args: pytest.fail('must not restart'))
    with pytest.raises(recovery.HarnessError):
        recovery.restart_hub_and_ingress('podman', hub, ingress, 'fixture', object())
