"""One-time setup using an authenticated local Azure CLI and GitHub CLI.
Creates only a managed identity and a VM-scoped role assignment, not a new VM.
"""
import argparse
import json
import re
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--repo', default='fiftycommit/nuage')
parser.add_argument('--resource-group', required=True)
parser.add_argument('--vm', required=True)
parser.add_argument('--identity', default='nuage-github')
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
    raise SystemExit('Invalid repository')

def run(*command, payload=None):
    result = subprocess.run(command, input=None if payload is None else json.dumps(payload),
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()

def az(*command):
    return json.loads(run('az', *command, '--output', 'json', '--only-show-errors'))

run('gh', 'auth', 'status')
account = az('account', 'show')
vm = az('vm', 'show', '--resource-group', args.resource_group, '--name', args.vm)
print(f'Configure GitHub OIDC for {args.repo}, VM {args.resource_group}/{args.vm}.')
print('The new identity can invoke root commands on this VM; no app secrets are uploaded.')
run('gh', 'variable', 'set', 'NUAGE_AUTO_DEPLOY', '--repo', args.repo, '--body', 'false')
run('gh', 'api', '--method', 'PUT', f'repos/{args.repo}/environments/production', '--input', '-',
    payload={'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}})
policies = json.loads(run('gh', 'api', f'repos/{args.repo}/environments/production/deployment-branch-policies'))
if not any(p['name'] == 'main' and p.get('type', 'branch') == 'branch' for p in policies['branch_policies']):
    run('gh', 'api', '--method', 'POST', f'repos/{args.repo}/environments/production/deployment-branch-policies',
        '--input', '-', payload={'name': 'main', 'type': 'branch'})
identity = az('identity', 'create', '--resource-group', args.resource_group, '--name', args.identity,
              '--location', vm['location'])
credentials = az('identity', 'federated-credential', 'list', '--resource-group', args.resource_group,
                 '--identity-name', args.identity)
subject = f'repo:{args.repo}:environment:production'
existing = next((c for c in credentials if c['name'] == 'github-production'), None)
if existing:
    if existing['subject'] != subject or existing['issuer'] != 'https://token.actions.githubusercontent.com':
        raise SystemExit('Existing federation does not match; no credential was replaced')
else:
    az('identity', 'federated-credential', 'create', '--resource-group', args.resource_group,
       '--identity-name', args.identity, '--name', 'github-production',
       '--issuer', 'https://token.actions.githubusercontent.com', '--subject', subject,
       '--audiences', 'api://AzureADTokenExchange')
assignments = az('role', 'assignment', 'list', '--scope', vm['id'])
if not any(a['principalId'] == identity['principalId'] and a['roleDefinitionName'] == 'Virtual Machine Contributor'
           and a['scope'].lower() == vm['id'].lower() for a in assignments):
    az('role', 'assignment', 'create', '--assignee-object-id', identity['principalId'],
       '--assignee-principal-type', 'ServicePrincipal', '--role', 'Virtual Machine Contributor', '--scope', vm['id'])
values = {'AZURE_CLIENT_ID': identity['clientId'], 'AZURE_TENANT_ID': identity['tenantId'],
          'AZURE_SUBSCRIPTION_ID': account['id'], 'AZURE_RESOURCE_GROUP': args.resource_group, 'AZURE_VM_NAME': args.vm}
for name, value in values.items():
    run('gh', 'variable', 'set', name, '--repo', args.repo, '--env', 'production', '--body', value)
run('gh', 'variable', 'set', 'NUAGE_AUTO_DEPLOY', '--repo', args.repo, '--body', 'true')
run('gh', 'workflow', 'run', 'ci.yml', '--repo', args.repo, '--ref', 'main')
print('OIDC configured. CI was requested; deployment runs only after successful CI on main.')
