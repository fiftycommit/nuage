"""Publish real PNG captures to a dedicated branch; never change main or secrets."""
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

repository = os.environ['GITHUB_REPOSITORY']
revision = os.environ['GITHUB_SHA']
token = os.environ['GH_TOKEN']
assert re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
assert re.fullmatch(r'[0-9a-f]{40}', revision)
base = f'https://api.github.com/repos/{repository}'

def request(path, method='GET', payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base+path, data=data, method=method, headers={
        'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

try:
    request('/git/ref/heads/screenshots')
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
    request('/git/refs', 'POST', {'ref': 'refs/heads/screenshots', 'sha': revision})
for image in sorted((Path(__file__).resolve().parents[1]/'docs/screenshots').glob('*.png')):
    remote = '/contents/docs/screenshots/'+image.name
    payload = {'message': 'docs: capture Nuage demo '+image.stem,
               'branch': 'screenshots', 'content': base64.b64encode(image.read_bytes()).decode()}
    try:
        payload['sha'] = request(remote+'?ref=screenshots')['sha']
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    request(remote, 'PUT', payload)
    print('Published demo capture:', image.name)
