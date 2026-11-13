"""Restart only explicitly named, project-scoped synthetic Hub/world-b fixtures.

All targets are required: --allow-disposable --base-url --proxy-secret-file
--hub-container --ingress-container --world-container --world-b-base-url --project. Container names
may vary by isolated run. Named volumes must be <project>_hub-data and
<project>_world-b-state; no foreign or extra writable mounts are accepted.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request
from urllib.parse import quote

from e2e import (HarnessError, HubClient, fixture_opener, read_json, require,
                 until, validate_base_url)


CONTAINER_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
PROJECT_NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,127}\Z')
CONTAINER_ID = re.compile(r'[a-f0-9]{64}\Z')


def choose_engine(engine):
    require(engine in {'auto', 'docker', 'podman'}, 'Choose Docker or Podman')
    candidates = ('docker', 'podman') if engine == 'auto' else (engine,)
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    raise HarnessError('Selected container engine is not available')


def engine_call(engine, args, *, timeout=30):
    try:
        return subprocess.run([engine, *args], check=True, capture_output=True,
                              text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        # Inspect output includes connector credentials. Never include it in errors.
        raise HarnessError('Fixture container operation failed or timed out') from None


def compose_label(labels, key, expected):
    values = [labels[name] for name in ('com.docker.compose.' + key, 'io.podman.compose.' + key)
              if name in labels]
    require(values and all(value == expected for value in values),
            'Container does not belong to the approved Compose project/service')


def inspect_fixture(engine, target, project, service):
    require(CONTAINER_NAME.fullmatch(target) is not None, 'Invalid fixture container name')
    try:
        items = json.loads(engine_call(engine, ['container', 'inspect', target]))
        require(isinstance(items, list) and len(items) == 1, 'Expected exactly one fixture container')
        item = items[0]
        ident = item['Id']
        require(CONTAINER_ID.fullmatch(ident) is not None, 'Invalid fixture container identity')
        require(target in {item['Name'].lstrip('/'), ident}, 'Container inspection returned another target')
        labels = item['Config'].get('Labels') or {}
        compose_label(labels, 'project', project)
        compose_label(labels, 'service', service)
        require(item.get('State', {}).get('Running') is True, 'Fixture container must already be running')
        volume, destination = (('hub-data', '/data') if service == 'hub' else ('world-b-state', '/world'))
        mounts = item.get('Mounts')
        require(isinstance(mounts, list), 'Missing fixture mount inspection')
        state_mounts = [mount for mount in mounts if mount.get('Destination') == destination]
        require(len(state_mounts) == 1 and state_mounts[0].get('Type') == 'volume'
                and state_mounts[0].get('Name') == project + '_' + volume
                and state_mounts[0].get('RW') is True,
                'Fixture state must use the approved project-scoped named volume')
        # Only the versioned, read-only world seed files may be host-mounted.
        seed_files = {'/seed/config.json': '/tests/world-b.json',
                      '/seed/entrypoint.sh': '/tests/world-entrypoint.sh'} if service == 'world-b' else {}
        seen = set()
        for mount in mounts:
            if mount is state_mounts[0]:
                continue
            dest = mount.get('Destination')
            source = str(mount.get('Source', '')).replace('\\', '/')
            require(dest in seed_files and dest not in seen and mount.get('Type') == 'bind'
                    and mount.get('RW') is False and source.endswith(seed_files[dest]),
                    'Refusing a fixture with foreign, extra, or writable host mounts')
            seen.add(dest)
        if service == 'world-b':
            environment = dict(entry.split('=', 1) for entry in item['Config'].get('Env', []) if '=' in entry)
            require(environment.get('HUB_WORLD_ID') == 'test-b'
                    and environment.get('HUB_ALLOWED_AGENTS') == 'helper',
                    'World restart target is not the synthetic test-b/helper fixture')
        return {'id': ident, 'name': item['Name'], 'mounts': mounts}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HarnessError('Invalid fixture container inspection') from None


def restart_fixture(engine, approved, project, service, client):
    # Pin the inspected identity, recheck all guards, then restart only that ID.
    client.worlds()
    current = inspect_fixture(engine, approved['id'], project, service)
    require(current == approved, 'Approved fixture identity or mounts changed before restart')
    engine_call(engine, ['restart', '--time', '15', approved['id']], timeout=60)


def inspect_ingress(engine, target, project, hub):
    require(CONTAINER_NAME.fullmatch(target) is not None, 'Invalid ingress fixture name')
    try:
        items = json.loads(engine_call(engine, ['container', 'inspect', target]))
        require(isinstance(items, list) and len(items) == 1, 'Expected one ingress fixture')
        item = items[0]
        ident = item['Id']
        require(CONTAINER_ID.fullmatch(ident) is not None
                and target in {item['Name'].lstrip('/'), ident}, 'Invalid ingress identity')
        labels = item['Config'].get('Labels') or {}
        compose_label(labels, 'project', project)
        compose_label(labels, 'service', 'ingress')
        require(item.get('State', {}).get('Running') is True, 'Ingress fixture must be running')
        require(item.get('HostConfig', {}).get('NetworkMode')
                in {'container:' + hub['id'], 'container:' + hub['name'].lstrip('/')},
                'Ingress does not share the approved Hub namespace')
        expected = {'/etc/hub/nginx.conf.template': '/ingress/nginx.conf.template',
                    '/etc/hub/start.sh': '/ingress/start.sh'}
        mounts = item.get('Mounts')
        require(isinstance(mounts, list) and len(mounts) == len(expected),
                'Unexpected ingress fixture mounts')
        for mount in mounts:
            suffix = expected.pop(mount.get('Destination'), None)
            require(suffix is not None and mount.get('Type') == 'bind'
                    and mount.get('RW') is False
                    and str(mount.get('Source', '')).replace('\\', '/').endswith(suffix),
                    'Refusing ingress with foreign or writable mounts')
        return {'id': ident, 'name': item['Name'], 'mounts': mounts}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HarnessError('Invalid ingress fixture inspection') from None


def restart_hub_and_ingress(engine, hub, ingress, project, client):
    # Namespace-sharing sidecars must rejoin after Podman replaces the Hub's
    # network namespace on restart. Never select one by a guessed name.
    require(inspect_ingress(engine, ingress['id'], project, hub) == ingress,
            'Approved ingress identity or mounts changed before restart')
    restart_fixture(engine, hub, project, 'hub', client)
    require(inspect_fixture(engine, hub['id'], project, 'hub') == hub,
            'Hub identity or mounts changed during restart')
    require(inspect_ingress(engine, ingress['id'], project, hub) == ingress,
            'Ingress identity or mounts changed during restart')
    engine_call(engine, ['restart', '--time', '15', ingress['id']], timeout=60)


def run(base_url, proxy_secret_file, hub_container, ingress_container, world_container, world_b_base_url,
        project, *, allow_disposable=False, engine='auto'):
    require(allow_disposable, 'Explicit --allow-disposable approval is required')
    validate_base_url(base_url)
    validate_base_url(world_b_base_url)
    require(world_b_base_url != base_url, 'The world portal must be separate from the Hub ingress')
    require(PROJECT_NAME.fullmatch(project) is not None, 'Invalid explicit fixture project')
    require(hub_container != world_container, 'Hub and world fixtures must be different containers')
    engine = choose_engine(engine)
    client = HubClient(base_url, proxy_secret_file)
    client.worlds()  # Exactly test-a/test-b + helper, before any mutation or restart.
    hub = inspect_fixture(engine, hub_container, project, 'hub')
    ingress = inspect_ingress(engine, ingress_container, project, hub)
    world = inspect_fixture(engine, world_container, project, 'world-b')
    require(hub['id'] != world['id'], 'Hub and world fixture identities must differ')

    def current(*, connected=True):
        return next(item for item in client.worlds(connected=connected) if item['id'] == 'test-b')

    def command(action, payload):
        return client.command('test-b', action, payload, seconds=75)

    created = command('start_conversation', {'agentId': 'helper', 'title': 'Recovery E2E',
                                             'message': 'Please ask me a question'})
    conversation = created['result']['conversationId']
    checkpoint = until(current, lambda item: any(question['conversationId'] == conversation
                       for question in item['snapshot']['questions']), 'pending checkpoint', 75)
    question = next(row for row in checkpoint['snapshot']['questions'] if row['conversationId'] == conversation)
    restart_fixture(engine, world, project, 'world-b', client)
    after = until(lambda: current(connected=False),
                  lambda item: item['connected'] is True
                  and item['snapshot']['observedAt'] != checkpoint['snapshot']['observedAt']
                  and any(row['requestId'] == question['requestId'] for row in item['snapshot']['questions']),
                  'world checkpoint after restart', 75)
    require(after['snapshot']['questions'], 'Pending checkpoint was not restored')
    command('answer_question', {'agentId': 'helper', 'conversationId': conversation,
                                'requestId': question['requestId'], 'text': 'Recovery Aurora',
                                'selectedValues': [], 'cancelled': False})
    until(current, lambda item: not any(row['requestId'] == question['requestId']
          for row in item['snapshot']['questions']), 'cleared checkpoint', 75)

    # Portal handoff has a different trust boundary: no Hub secret, Origin, cookie,
    # or inherited authentication, even on a redirect (which is always refused).
    portal_opener = fixture_opener()

    def history():
        request = urllib.request.Request(world_b_base_url + '/api/conversations/'
                                         + quote(conversation, safe='') + '/history?limit=20',
                                         headers={'Accept': 'application/json'})
        return read_json(portal_opener, request)[1]

    until(history, lambda item: any(entry.get('role') == 'assistant'
          and 'received your answer' in str(entry.get('content', '')) for entry in item['entries']),
          'resumed actual agent reply', 75)
    restart_hub_and_ingress(engine, hub, ingress, project, client)
    after = until(lambda: client.worlds(connected=False),
                  lambda items: all(item['connected'] is True for item in items),
                  'Hub persistence/reconnect', 75)
    require({item['id'] for item in after} == {'test-a', 'test-b'}, 'Hub registry changed after restart')
    saved = client.request('/api/commands/' + quote(created['id'], safe=''))[1]
    require(saved['result']['conversationId'] == conversation, 'Hub command receipt was not persisted')
    print(json.dumps({'recoveryChecks': 8, 'result': 'passed', 'conversationId': conversation,
                      'worldRestartCheckpoint': True, 'resumedReply': True,
                      'hubRestartPersistence': True}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-disposable', action='store_true', required=True)
    parser.add_argument('--base-url', required=True, help='Exact loopback operator ingress origin')
    parser.add_argument('--proxy-secret-file', type=Path, required=True)
    parser.add_argument('--hub-container', required=True)
    parser.add_argument('--ingress-container', required=True, help='Explicit namespace-sharing synthetic ingress')
    parser.add_argument('--world-container', required=True, help='Explicit synthetic world-b container')
    parser.add_argument('--world-b-base-url', required=True, help='Loopback world-b portal origin; no Hub credentials sent')
    parser.add_argument('--project', required=True, help='Explicit Compose project label and named-volume prefix')
    parser.add_argument('--engine', choices=('auto', 'docker', 'podman'), default='auto')
    args = parser.parse_args()
    try:
        run(args.base_url, args.proxy_secret_file, args.hub_container, args.ingress_container, args.world_container,
            args.world_b_base_url, args.project, allow_disposable=args.allow_disposable, engine=args.engine)
    except HarnessError as error:
        parser.exit(1, str(error) + '\n')
    except (OSError, KeyError, TypeError, ValueError):
        parser.exit(1, 'Unexpected synthetic fixture response\n')
