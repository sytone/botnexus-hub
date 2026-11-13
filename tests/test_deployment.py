"""Container contracts are checked without touching a running gateway."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_test_proxy_is_unprivileged():
    compose=(ROOT/'compose.yaml').read_text()
    proxy=compose.split('  test-portals:',1)[1].split('\nnetworks:',1)[0]
    assert 'user: "101:101"' in proxy
    assert 'cap_drop: [ALL]' in proxy
    config=(ROOT/'tests/portals.conf').read_text()
    for key in ['client_body_temp_path','proxy_temp_path','fastcgi_temp_path','uwsgi_temp_path','scgi_temp_path']:
        assert key+' /tmp/' in config

def test_worlds_have_no_external_network_or_host_data():
    compose=(ROOT/'compose.yaml').read_text()
    for name in ['world-a','world-b']:
        section=compose.split('  '+name+':',1)[1].split('\n  ',1)[0] if False else compose.split('  '+name+':',1)[1]
        if name=='world-a': section=section.split('\n  world-b:',1)[0]
        else: section=section.split('\n  test-portals:',1)[0]
        assert 'networks: [test-net]' in section
        assert '5005' not in section and '.botnexus' not in section
        assert 'ports:' not in section
