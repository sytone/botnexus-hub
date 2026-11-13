"""Static deployment gates complement mandatory live ingress probes.

The ASGI backend has no operator authentication by design. Only the provided,
private-backend ingress is a supported external deployment boundary.
"""
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_raw_backend_is_loopback_only_and_not_published():
    docker=(ROOT/'Dockerfile').read_text()
    assert '"--host", "127.0.0.1"' in docker
    assert 'EXPOSE 8785' not in docker
    compose=(ROOT/'compose.yaml').read_text()
    assert ':8785"' not in compose
    assert ':8080"' in compose and ':8081"' in compose
    assert 'network_mode: service:hub' in compose
    assert 'HUB_ADMIN_TOKEN' not in compose and 'HUB_SESSION_SECRET' not in compose


def test_operator_ingress_requires_private_proxy_proof_not_identity_headers():
    config=(ROOT/'ingress/nginx.conf.template').read_text()
    operator=config.split('listen 8080 default_server;',1)[1].split('listen 8081 default_server;',1)[0]
    assert 'if ($external_preauth = 0) { return 403; }' in operator
    assert 'location ^~ /api/connect { return 404; }' in operator
    assert 'proxy_set_header X-Hub-Proxy-Secret "";' in operator
    assert 'proxy_set_header Cookie "";' in operator
    assert 'proxy_set_header Authorization "";' in operator
    assert '~^${HUB_PROXY_SECRET}$ 1;' in config
    assert '$http_x_netbird_user' not in config and '$http_x_forwarded_for' not in config
    assert 'access_log off;' in config and 'error_log /dev/stderr crit;' in config


def test_connector_ingress_is_allowlist_and_never_uses_proxy_proof():
    config=(ROOT/'ingress/nginx.conf.template').read_text().split('listen 8081 default_server;',1)[1]
    assert '"^/api/connect/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"' in config
    assert 'if ($request_method != GET)' in config
    assert 'if ($is_websocket = 0)' in config
    assert 'if ($args != "")' in config
    assert 'location / { return 404; }' in config
    assert '$external_preauth' not in config
    assert 'proxy_set_header Authorization $http_authorization;' in config
    assert 'proxy_set_header Host connector-ingress;' in config


def test_ingress_secret_has_exact_case_sensitive_comparison_and_safe_rendering():
    start=(ROOT/'ingress/start.sh').read_text()
    assert "^[A-Za-z0-9_-]{43,128}$" in start
    assert "envsubst '${HUB_PROXY_SECRET}'" in start
    assert 'nginx -T' not in start


def test_backend_has_no_native_operator_session_implementation():
    app=(ROOT/'hub/app.py').read_text()
    security=(ROOT/'hub/security.py').read_text()
    for value in ('issue_session','session_hash','admin_token','set_cookie','revoke_session'):
        assert value not in app
    assert 'def load_secret' not in security
    assert 'def issue_session' not in security


def test_default_compose_is_provider_agnostic_and_optional_netbird():
    compose=(ROOT/'compose.yaml').read_text()
    block=compose.split('  netbird:',1)[1].split('  mock-provider:',1)[0]
    assert 'profiles: [netbird]' in block
    assert 'NB_MANAGEMENT_URL: ${NB_MANAGEMENT_URL:-}' in block
    assert 'https://' not in block
