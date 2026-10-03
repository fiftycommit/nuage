"""Send a validated commit to Azure Run Command and fail on absent success proof."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
revision = os.environ['NUAGE_REVISION']
repository = os.environ['GITHUB_REPOSITORY']
if not re.fullmatch(r'[0-9a-f]{40}', revision):
    raise SystemExit('Invalid revision')
if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
    raise SystemExit('Invalid repository')
prelude = f'export NUAGE_REVISION={shlex.quote(revision)}\nexport NUAGE_REPOSITORY={shlex.quote(repository)}\n'
with tempfile.TemporaryDirectory(prefix='nuage-deploy-') as temporary:
    script = Path(temporary)/'deploy.sh'
    script.write_text(prelude + (root/'scripts/deploy-remote.sh').read_text())
    result = subprocess.run(['az', 'vm', 'run-command', 'invoke', '--resource-group',
        os.environ['AZURE_RESOURCE_GROUP'], '--name', os.environ['AZURE_VM_NAME'],
        '--command-id', 'RunShellScript', '--scripts', '@'+str(script), '--output', 'json',
        '--only-show-errors'], capture_output=True, text=True, check=True)
    response = json.loads(result.stdout)
    messages = '\n'.join(value.get('message', '') for value in response.get('value', []))
    # Azure can return HTTP success even when the remote shell exits non-zero.
    if not re.search(r'^NUAGE_DEPLOY_OK '+revision+r'\s*$', messages, re.MULTILINE):
        raise SystemExit('Remote deployment did not report a verified revision; inspect Azure Run Command.')
print('Deployed and verified revision:', revision)
