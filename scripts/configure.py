"""Create deployment settings. No operator credentials or browser session secrets."""
import argparse
import os
from pathlib import Path
import secrets


def configure(root):
    target = root / '.env'
    if target.exists():
        raise SystemExit('Existing .env preserved. Follow the documented migration; do not reset state.')
    target.write_text('HUB_HOST_PORT=8785\nHUB_CONNECTOR_HOST_PORT=8788\nHUB_PUBLIC_URL=https://hub.example.com\nHUB_CONNECTOR_PUBLIC_URL=https://connect.example.com\nHUB_PROXY_SECRET=' + secrets.token_urlsafe(36) + '\nHUB_COMMIT=unknown\nNB_SETUP_KEY=\nNB_MANAGEMENT_URL=\n', encoding='utf-8', newline='\n')
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    print('Private .env created. Configure your origins and preauth proxy. No user login token is needed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    configure(args.root.resolve())
