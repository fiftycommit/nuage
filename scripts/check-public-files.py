"""Fail without printing secret values if the publishable tree contains credentials."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
result = subprocess.run(['git', 'ls-files', '--cached'], cwd=root, capture_output=True, text=True)
if result.returncode == 0:
    files = [root/name for name in result.stdout.splitlines()]
else:
    files = [p for p in root.rglob('*') if p.is_file() and not any(
        part in {'.venv', '__pycache__', '.git', 'data', '.pytest_cache'} for part in p.relative_to(root).parts)]
patterns = [re.compile(rb'gh[pousr]_[A-Za-z0-9]{20,}'),
            re.compile(rb'github_pat_[A-Za-z0-9_]{30,}'),
            re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]
errors = []
for path in files:
    name = path.name
    if (name == '.env' or (name.startswith('.env.') and name != '.env.example') or
            path.suffix in {'.pem', '.key', '.p12', '.pfx', '.sqlite3'}):
        errors.append(f'{path.relative_to(root)}: sensitive file must not be published')
        continue
    if not path.exists():
        continue
    body = path.read_bytes()
    if any(pattern.search(body) for pattern in patterns):
        errors.append(f'{path.relative_to(root)}: credential signature detected')
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
print('Public-file credential checks passed')
