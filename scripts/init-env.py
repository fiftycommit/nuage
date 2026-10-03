"""Create a private local config without echoing or overwriting credentials."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets

parser = argparse.ArgumentParser()
parser.add_argument('--file', default='.env')
parser.add_argument('--root', default='./data')
parser.add_argument('--helper', default='/usr/local/bin/mediafire-get')
args = parser.parse_args()
target = Path(args.file)
if target.exists():
    raise SystemExit('Config already exists; no values were replaced')
password = getpass.getpass('Choisis le mot de passe du site (8 caractères minimum) : ')
if len(password) < 8:
    raise SystemExit('Password must contain at least 8 characters')
values = {'PORTAL_PASSWORD': password, 'PORTAL_SESSION_SECRET': secrets.token_urlsafe(48),
          'PORTAL_ROOT': args.root, 'PORTAL_MEDIAFIRE_HELPER': args.helper}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    for name, value in values.items():
        stream.write(name+'='+json.dumps(value)+'\n')
print('Private config created:', target)
