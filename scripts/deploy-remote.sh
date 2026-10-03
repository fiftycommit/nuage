#!/bin/sh
# Executed by Azure Run Command as root, for an existing Nuage installation.
set -eu
: "${NUAGE_REVISION:?Missing revision}"
: "${NUAGE_REPOSITORY:?Missing repository}"
python3 - <<'PY'
import os, re
assert re.fullmatch(r'[0-9a-f]{40}', os.environ['NUAGE_REVISION'])
assert re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', os.environ['NUAGE_REPOSITORY'])
PY
app_dir=/opt/transfer-portal
data_dir=/data/transfer-portal
lock_dir=/run/nuage-deploy.lock
marker="$data_dir/.deploying"
deployment_id="$NUAGE_REVISION-$(date +%s)-$$"
stage_dir="/opt/nuage-staging/$deployment_id"
venv_dir="/opt/nuage-venvs/$deployment_id"
backup_dir="/opt/nuage-backups/$(date +%Y%m%d%H%M%S)-$NUAGE_REVISION"
failed_dir="/opt/nuage-backups/failed-$(date +%Y%m%d%H%M%S)-$NUAGE_REVISION"
test -d "$app_dir"
test ! -L "$app_dir"
test -f /etc/transfer-portal.env
test -f "$data_dir/jobs.sqlite3"
test ! -e "$marker"
command -v aria2c >/dev/null
command -v unrar >/dev/null
command -v curl >/dev/null
# Existing helper is a VM prerequisite and is preserved by deployment.
test -x /usr/local/bin/mediafire-get
mkdir "$lock_dir"
switched=0
service_stopped=0
marker_owned=0
cleanup() {
  status=$?
  trap - 0 1 2 15
  if [ "$status" -ne 0 ] && [ "$switched" -eq 1 ]; then
    systemctl stop transfer-portal || true
    if [ -d "$app_dir" ]; then mv "$app_dir" "$failed_dir"; fi
    mv "$backup_dir" "$app_dir"
    systemctl start transfer-portal || true
    printf '%s\n' 'Deployment failed; previous code restored.' >&2
  elif [ "$status" -ne 0 ] && [ "$service_stopped" -eq 1 ]; then
    systemctl start transfer-portal || true
  fi
  if [ "$marker_owned" -eq 1 ]; then
    python3 -c 'from pathlib import Path; Path("/data/transfer-portal/.deploying").unlink(missing_ok=True)'
  fi
  rmdir "$lock_dir"
  exit "$status"
}
trap cleanup 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 143' 15
mkdir -p /opt/nuage-staging /opt/nuage-venvs /opt/nuage-backups
# Do not overwrite a previous staging area or venv.
test ! -e "$stage_dir"
test ! -e "$venv_dir"
mkdir "$stage_dir"
archive="$stage_dir/source.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "https://codeload.github.com/$NUAGE_REPOSITORY/tar.gz/$NUAGE_REVISION" -o "$archive"
tar -xzf "$archive" -C "$stage_dir" --strip-components=1
python3 -m venv "$venv_dir"
"$venv_dir/bin/pip" install --disable-pip-version-check -q -r "$stage_dir/requirements.txt" -r "$stage_dir/requirements-dev.txt"
PLAYWRIGHT_BROWSERS_PATH="$venv_dir/share/browsers" "$venv_dir/bin/playwright" install chromium --only-shell
# Verify Chromium before stopping or replacing the running service.
PLAYWRIGHT_BROWSERS_PATH="$venv_dir/share/browsers" "$venv_dir/bin/python" - <<'PY'
import asyncio
from playwright.async_api import async_playwright
async def check():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        await browser.close()
asyncio.run(check())
PY
(cd "$stage_dir" && "$venv_dir/bin/python" -m unittest discover -s tests -p 'test_*.py' -q)
ln -s "$venv_dir" "$stage_dir/.venv"
printf '%s\n' "$NUAGE_REVISION" > "$stage_dir/REVISION"
# Prevent new mutations, then let current downloads/extractions/ZIPs finish.
touch "$marker"
marker_owned=1
active_count() {
  python3 - <<'PY'
import sqlite3
with sqlite3.connect('file:/data/transfer-portal/jobs.sqlite3?mode=ro', uri=True) as c:
    print(c.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','downloading','extracting','publishing') OR package_state IN ('queued','building')").fetchone()[0])
PY
}
attempt=0
while [ "$(active_count)" -gt 0 ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 180 ]; then
    printf '%s\n' 'VM busy after 30 minutes; no code replaced.' >&2
    exit 1
  fi
  sleep 10
done
systemctl stop transfer-portal
service_stopped=1
# Covers the first upgrade, whose old code does not yet honour the marker.
if [ "$(active_count)" -gt 0 ]; then
  systemctl start transfer-portal
  printf '%s\n' 'A task arrived during maintenance; deployment cancelled.' >&2
  exit 1
fi
mv "$app_dir" "$backup_dir"
switched=1
mv "$stage_dir" "$app_dir"
chown -R root:root "$app_dir"
systemctl start transfer-portal
attempt=0
until curl --fail --silent http://127.0.0.1:8765/api/health | \
  python3 -c 'import json,os,sys; d=json.load(sys.stdin); assert d["status"]=="ok" and d["revision"]==os.environ["NUAGE_REVISION"]'; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 20 ]; then exit 1; fi
  sleep 1
done
systemctl is-active --quiet transfer-portal
printf 'NUAGE_DEPLOY_OK %s\n' "$NUAGE_REVISION"
