"""Small, isolated download portal for the temporary Azure transfer VM."""

from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import os
import pty
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import errno
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
import rootz
import filekeeper


HERE = Path(__file__).resolve().parent
ROOT = Path(os.getenv("PORTAL_ROOT", "/data/transfer-portal")).resolve()
PASSWORD = os.getenv("PORTAL_PASSWORD", "")
SECRET = os.getenv("PORTAL_SESSION_SECRET", "").encode()
MEDIAFIRE_HELPER = os.getenv("PORTAL_MEDIAFIRE_HELPER", "/usr/local/bin/mediafire-get")
if len(PASSWORD) < 8 or len(SECRET) < 32:
    raise RuntimeError("PORTAL_PASSWORD (8+) and PORTAL_SESSION_SECRET (32+) are required")
if not ROOT.is_absolute():
    raise RuntimeError("PORTAL_ROOT must be an absolute path")

PRIVATE = ROOT / "private"
PUBLIC = ROOT / "public" / "files"
DB = ROOT / "jobs.sqlite3"
RETENTION_SECONDS = 5 * 24 * 60 * 60
for directory in (PRIVATE, PUBLIC):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700 if directory == PRIVATE else 0o755)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
db_lock = threading.Lock()
worker_lock = threading.Lock()
login_lock = threading.Lock()
login_failures: dict[str, list[float]] = {}


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


with connect() as connection:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, created INTEGER NOT NULL, updated INTEGER NOT NULL,
            state TEXT NOT NULL, mode TEXT NOT NULL, items TEXT NOT NULL,
            archive_password TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
            files TEXT NOT NULL DEFAULT '[]'
        )"""
    )
    columns = {column[1] for column in connection.execute("PRAGMA table_info(jobs)")}
    if "progress" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN progress TEXT NOT NULL DEFAULT '{}'")
    if "extraction" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN extraction TEXT NOT NULL DEFAULT '{}'")
    if "display_name" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    if "package_state" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN package_state TEXT NOT NULL DEFAULT 'none'")
    if "package_completed" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN package_completed INTEGER NOT NULL DEFAULT 0")
    if "package_total" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN package_total INTEGER NOT NULL DEFAULT 0")
    if "package_message" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN package_message TEXT NOT NULL DEFAULT ''")
    if "expires_at" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0")
    connection.execute(
        "UPDATE jobs SET expires_at=updated+? WHERE expires_at=0 AND state IN ('ready','failed')",
        (RETENTION_SECONDS,),
    )
    connection.execute("UPDATE jobs SET package_state='queued' WHERE package_state='building'")
    connection.execute(
        "UPDATE jobs SET state='queued', message='Reprise après redémarrage' "
        "WHERE state IN ('downloading', 'extracting', 'publishing')"
    )


def update(job_id: str, **fields: object) -> None:
    fields["updated"] = int(time.time())
    with db_lock, connect() as connection:
        assignment = ", ".join(f"{key}=?" for key in fields)
        connection.execute(
            f"UPDATE jobs SET {assignment} WHERE id=?", [*fields.values(), job_id]
        )


def fetch(job_id: str) -> dict:
    with connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Tâche introuvable")
    return dict(row)


def public_job(row: dict) -> dict:
    items = json.loads(row["items"])
    return {
        "id": row["id"],
        "created": row["created"],
        "updated": row["updated"],
        "expires_at": row["expires_at"],
        "state": row["state"],
        "mode": row["mode"],
        "message": row["message"],
        "items": [
            {key: item.get(key) for key in ("name", "size", "host", "mime")}
            for item in items
        ],
        "files": json.loads(row["files"]),
        "progress": json.loads(row["progress"]),
        "extraction": json.loads(row["extraction"]),
        "display_name": row["display_name"] or default_display_name(items, row["mode"]),
        "folder_url": f"/job/{row['id']}" if row["state"] == "ready" else None,
        "package": {
            "state": row["package_state"],
            "completed": row["package_completed"],
            "total": row["package_total"],
            "message": row["package_message"],
            "url": f"/files/{row['id']}/download-{row['id']}.zip"
                   if row["package_state"] == "ready" else None,
        },
    }


def default_display_name(items: list[dict], mode: str) -> str:
    if not items:
        return "Mes fichiers"
    if mode == "rar":
        first = next((item["name"] for item in items if re.search(
            r"\.part0*1\.rar$", item["name"], re.I)), items[0]["name"])
        return re.sub(r"\.part0*1\.rar$", "", first, flags=re.I)
    if len(items) == 1:
        return Path(items[0]["name"]).stem
    return f"Lot de {len(items)} fichiers"


def cookie_value() -> str:
    stamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    body = f"{stamp}.{nonce}"
    signature = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def signed_in(request: Request) -> bool:
    value = request.cookies.get("portal_session", "")
    pieces = value.split(".")
    if len(pieces) != 3:
        return False
    stamp, nonce, signature = pieces
    if not stamp.isdigit() or not nonce or len(nonce) > 64:
        return False
    if abs(time.time() - int(stamp)) > 7 * 86400:
        return False
    expected = hmac.new(SECRET, f"{stamp}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def require_login(request: Request, mutation: bool = False) -> None:
    if not signed_in(request):
        raise HTTPException(401, "Connexion requise")
    if mutation:
        if (ROOT / ".deploying").exists():
            raise HTTPException(503, "Mise à jour en cours ; réessaie dans quelques instants")
        origin = request.headers.get("origin", "")
        host = request.headers.get("host", "")
        if origin and urllib.parse.urlsplit(origin).netloc != host:
            raise HTTPException(403, "Origine refusée")


class Login(BaseModel):
    password: str


class Links(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=30)


class NewJob(Links):
    mode: str = "files"
    archive_password: str = Field(default="", max_length=200)


class RetryArchive(BaseModel):
    archive_password: str = Field(min_length=1, max_length=200)


class RenameFolder(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@app.get("/")
def home() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/api/health")
def health() -> dict:
    revision_file = HERE / "REVISION"
    revision = revision_file.read_text().strip() if revision_file.exists() else "development"
    return {"status": "ok", "revision": revision}


@app.get("/assets/file-tiles.js")
def file_tiles_asset() -> FileResponse:
    return FileResponse(HERE / "file-tiles.js", media_type="application/javascript")


@app.get("/job/{job_id}")
def job_page(job_id: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{24}", job_id):
        raise HTTPException(404, "Tâche introuvable")
    return FileResponse(HERE / "index.html")


@app.get("/downloads")
def downloads_page() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.post("/api/login")
def login(payload: Login, request: Request) -> JSONResponse:
    # The shared password is not written to disk or logs.
    address = request.headers.get("x-real-ip", request.client.host if request.client else "unknown")[:80]
    with login_lock:
        recent = [moment for moment in login_failures.get(address, []) if time.time() - moment < 900]
        login_failures[address] = recent
        if len(recent) >= 10:
            raise HTTPException(429, "Trop d'essais. Réessaie dans quelques minutes")
    candidate = hashlib.sha256(payload.password.encode()).digest()
    expected = hashlib.sha256(PASSWORD.encode()).digest()
    if not hmac.compare_digest(candidate, expected):
        with login_lock:
            login_failures.setdefault(address, []).append(time.time())
        time.sleep(0.7)
        raise HTTPException(401, "Mot de passe incorrect")
    with login_lock:
        login_failures.pop(address, None)
    response = JSONResponse({"ok": True})
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        "portal_session", cookie_value(), httponly=True, secure=scheme == "https",
        samesite="strict", max_age=7 * 86400, path="/",
    )
    return response


@app.post("/api/logout")
def logout(request: Request) -> JSONResponse:
    require_login(request, mutation=True)
    response = JSONResponse({"ok": True})
    response.delete_cookie("portal_session", path="/")
    return response


@app.get("/api/me")
def me(request: Request) -> dict:
    return {"authenticated": signed_in(request)}


@app.get("/api/storage")
def storage_status(request: Request) -> dict:
    require_login(request)
    usage = shutil.disk_usage(ROOT)
    return {"total": usage.total, "used": usage.used, "free": usage.free,
            "percent_used": round(usage.used * 100 / usage.total, 1) if usage.total else 0}


@app.get("/api/auth/file")
def file_auth(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True}, status_code=200 if signed_in(request) else 401)


def provider(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("Utilise un lien HTTPS valide")
    if host in ("akirabox.com", "www.akirabox.com", "akirabox.to", "www.akirabox.to"):
        return "AkiraBox"
    if host in ("rootz.so", "www.rootz.so"):
        rootz.share_url(url.strip())
        return "Rootz"
    if host in ("filekeeper.net", "www.filekeeper.net"):
        filekeeper.share_url(url.strip())
        return "Filekeeper"
    if host in ("mediafire.com", "www.mediafire.com") or re.fullmatch(
        r"download\d+\.mediafire\.com", host
    ):
        return "MediaFire"
    raise ValueError("Hébergeur pas encore pris en charge")


def clean_name(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    if not name or name in (".", "..") or any(ord(char) < 32 for char in name):
        raise ValueError("Nom de fichier invalide")
    return name[:240]


def parse_size(value: object) -> int | None:
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"\s*([\d.,]+)\s*([KMGT]?i?B)\s*", value, re.I)
    if not match:
        return None
    amount = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper()
    factor = 1024 if "I" in unit else 1000
    exponent = "KMGT".find(unit[0]) + 1 if unit[0] in "KMGT" else 0
    return int(amount * factor**exponent)


def akira_metadata(url: str) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.path.startswith("/download/"):
        name = clean_name(urllib.parse.unquote(parsed.path.rstrip("/").split("/")[-1]))
        expiry = urllib.parse.parse_qs(parsed.query).get("expiration", [None])[0]
        if expiry and expiry.isdigit() and int(expiry) <= time.time():
            raise ValueError("Lien direct expiré : colle un nouveau lien ou le lien de partage")
        if expiry and expiry.isdigit() and int(expiry) - time.time() < 300:
            raise ValueError("Ce lien expire dans moins de 5 minutes ; génère un nouveau lien")
        request = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                mime = response.headers.get("Content-Type", "").lower()
                if "text/html" in mime:
                    raise ValueError("Ce lien renvoie une page, pas le fichier")
                size = parse_size(response.headers.get("Content-Length"))
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Lien direct indisponible (HTTP {exc.code})") from exc
        return {"name": name, "size": size, "host": "AkiraBox", "url": url,
                "mime": mime or None, "note": "Lien temporaire"}
    if not re.fullmatch(r"/[A-Za-z0-9]+/file/?", parsed.path):
        raise ValueError("Lien de partage AkiraBox invalide")
    stable_url = "https://akirabox.com" + parsed.path
    api = "https://akirabox.com/api/files?" + urllib.parse.urlencode({"url": stable_url})
    with urllib.request.urlopen(api, timeout=15) as response:
        data = json.load(response)
    if data.get("status") != 200:
        raise ValueError(data.get("message", "Fichier AkiraBox indisponible"))
    return {"name": clean_name(data["name"]), "size": parse_size(data.get("size")),
            "mime": data.get("mime"), "host": "AkiraBox", "url": url,
            "note": "Infos vérifiées ; ouvre AkiraBox, termine sa vérification puis colle le lien /download/",
            "needs_direct": True}


def mediafire_metadata(url: str) -> dict:
    # Reuses the existing resolver on the transfer VM. --probe must not download a body.
    result = subprocess.run(
        [MEDIAFIRE_HELPER, "--probe", url], capture_output=True, text=True, timeout=35
    )
    if result.returncode:
        raise ValueError("Lien MediaFire inaccessible ou vérification indisponible")
    output = result.stdout + "\n" + result.stderr
    name_match = re.search(r"(?:Fichier|File)\s*:\s*(.+)", output)
    size_match = re.search(r"(?:taille|size)\s*:\s*([\d,]+)\s*(?:octets|bytes)?", output, re.I)
    mime_match = re.search(r"(?:Type|MIME)\s*:\s*([^;\n]+)", output, re.I)
    if not name_match:
        raise ValueError("Le vérificateur MediaFire n'a pas renvoyé de nom")
    return {"name": clean_name(name_match.group(1)),
            "size": int(size_match.group(1).replace(",", "")) if size_match else None,
            "mime": mime_match.group(1).strip() if mime_match else None,
            "host": "MediaFire", "url": url, "note": "Vérifié sans téléchargement"}


def probe_one(url: str) -> dict:
    url = url.strip()
    if len(url) > 4000:
        raise ValueError("Lien trop long")
    host = provider(url)
    if host == "Filekeeper":
        item = filekeeper.metadata(url)
        item["name"] = clean_name(item["name"])
        return item
    if host == "Rootz":
        item = rootz.metadata(url)
        item["name"] = clean_name(item["name"])
        return item
    return akira_metadata(url) if host == "AkiraBox" else mediafire_metadata(url)


def probe_all(urls: list[str]) -> list[dict]:
    normalized = [url.strip() for url in urls if url.strip()]
    if not normalized or len(normalized) > 30 or len(normalized) != len(set(normalized)):
        raise HTTPException(400, "Ajoute de 1 à 30 liens différents")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(probe_one, url) for url in normalized]
        for url, future in zip(normalized, futures):
            try:
                results.append({**future.result(timeout=45), "ok": True})
            except (ValueError, OSError, KeyError, TimeoutError, urllib.error.URLError) as exc:
                results.append({"url": url, "ok": False, "error": str(exc)})
    return results


@app.post("/api/probe")
def probe(payload: Links, request: Request) -> dict:
    require_login(request, mutation=True)
    results = probe_all(payload.urls)
    visible = []
    for item in results:
        row = {key: value for key, value in item.items() if key != "url"}
        if row.get("needs_direct"):
            row["ok"] = False
            row["error"] = row["note"]
        visible.append(row)
    return {"items": visible,
            "total_size": sum(item.get("size") or 0 for item in results if item.get("ok")),
            "all_ready": all(item["ok"] and not item.get("needs_direct") for item in results)}


@app.post("/api/jobs")
def create_job(payload: NewJob, request: Request) -> dict:
    require_login(request, mutation=True)
    if payload.mode not in ("files", "rar"):
        raise HTTPException(400, "Mode invalide")
    items = probe_all(payload.urls)
    if not all(item["ok"] for item in items):
        raise HTTPException(400, "Vérifie les liens en erreur avant de télécharger")
    if any(item.get("needs_direct") for item in items):
        raise HTTPException(400, "AkiraBox demande un lien /download/ après sa vérification humaine")
    if len({item["name"] for item in items}) != len(items):
        raise HTTPException(400, "Deux liens portent le même nom de fichier")
    if payload.mode == "rar":
        parts = sorted(
            int(match.group(1)) for item in items
            if (match := re.search(r"\.part(\d+)\.rar$", item["name"], re.I))
        )
        if len(parts) != len(items) or parts != list(range(1, len(items) + 1)):
            raise HTTPException(400, "Ajoute toutes les parties RAR, de part01 à la dernière")
    known = sum(int(item["size"]) for item in items if isinstance(item.get("size"), int))
    if known > 400 * 1000**3:
        raise HTTPException(400, "Le lot dépasse la limite de 400 Go")
    with connect() as connection:
        queued = connection.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('queued','downloading','extracting','publishing')"
        ).fetchone()[0]
    if queued >= 10:
        raise HTTPException(429, "File d'attente pleine ; réessaie plus tard")
    if queued and any(item["host"] == "AkiraBox" for item in items):
        raise HTTPException(429, "Un lot est en cours ; attends pour éviter l'expiration des liens AkiraBox")
    free = shutil.disk_usage(ROOT).free
    if free < max(50 * 1024**3, known * (2.5 if payload.mode == "rar" else 1.2)):
        raise HTTPException(507, "Espace disque insuffisant pour ce lot")
    job_id = secrets.token_hex(12)
    now = int(time.time())
    with db_lock, connect() as connection:
        connection.execute(
            "INSERT INTO jobs(id,created,updated,state,mode,items,archive_password,message) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (job_id, now, now, "queued", payload.mode, json.dumps(items),
             payload.archive_password, "En attente du téléchargement"),
        )
    return {"id": job_id, "state": "queued"}


@app.get("/api/jobs")
def list_jobs(request: Request) -> dict:
    require_login(request)
    with connect() as connection:
        rows = connection.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 50").fetchall()
    return {"jobs": [public_job(dict(row)) for row in rows]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    require_login(request)
    return public_job(fetch(job_id))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, request: Request) -> dict:
    require_login(request, mutation=True)
    if not re.fullmatch(r"[0-9a-f]{24}", job_id):
        raise HTTPException(400, "Identifiant de tâche invalide")
    # Workers claim queued jobs under this same lock before accessing files.
    with db_lock, connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Tâche introuvable")
        if row["state"] not in ("queued", "ready", "failed", "expired") or row["package_state"] == "building":
            raise HTTPException(409, "Attends la fin du traitement avant de supprimer ce téléchargement")
        try:
            remove_job_files(job_id)
        except OSError:
            raise HTTPException(500, "Impossible d'effacer tous les fichiers ; réessaie la suppression") from None
        connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"id": job_id, "deleted": True}


@app.post("/api/jobs/{job_id}/retry-extract")
def retry_extract(job_id: str, payload: RetryArchive, request: Request) -> dict:
    require_login(request, mutation=True)
    row = fetch(job_id)
    if row["state"] != "failed" or row["mode"] != "rar":
        raise HTTPException(409, "Cette tâche ne peut pas reprendre l'extraction")
    items = json.loads(row["items"])
    downloads = PRIVATE / job_id / "downloads"
    for item in items:
        target = downloads / clean_name(item["name"])
        if (not target.is_file() or Path(str(target) + ".aria2").exists() or
                isinstance(item.get("size"), int) and target.stat().st_size != item["size"]):
            raise HTTPException(409, "Une partie RAR est incomplète")
    update(job_id, state="queued", expires_at=0, archive_password=payload.archive_password,
           message="Reprise de l'extraction avec les fichiers existants")
    return {"id": job_id, "state": "queued"}


@app.post("/api/jobs/{job_id}/rename")
def rename_folder(job_id: str, payload: RenameFolder, request: Request) -> dict:
    require_login(request, mutation=True)
    row = fetch(job_id)
    if row["state"] != "ready":
        raise HTTPException(409, "Le dossier doit être prêt avant d'être renommé")
    name = payload.name.strip()
    if (not name or name in (".", "..") or any(char in name for char in "/\\") or
            any(ord(char) < 32 for char in name)):
        raise HTTPException(400, "Nom de dossier invalide")
    update(job_id, display_name=name)
    return {"id": job_id, "display_name": name}


@app.post("/api/jobs/{job_id}/package")
def queue_package(job_id: str, request: Request) -> dict:
    require_login(request, mutation=True)
    row = fetch(job_id)
    if row["state"] != "ready":
        raise HTTPException(409, "Les fichiers doivent être prêts avant de créer le ZIP")
    if row["package_state"] in ("queued", "building", "ready"):
        return {"id": job_id, "state": row["package_state"]}
    total = sum(file["size"] for file in json.loads(row["files"]))
    if shutil.disk_usage(ROOT).free < total + 20 * 1024**3:
        raise HTTPException(507, "Espace disque insuffisant pour préparer le ZIP")
    with db_lock, connect() as connection:
        result = connection.execute(
            "UPDATE jobs SET package_state='queued',package_completed=0,package_total=?,"
            "package_message='Préparation du ZIP',updated=? "
            "WHERE id=? AND package_state IN ('none','failed')",
            (total, int(time.time()), job_id),
        )
    return {"id": job_id, "state": "queued" if result.rowcount else fetch(job_id)["package_state"]}


def update_item_progress(job_id: str, name: str, completed: int, total: int | None,
                         speed: int = 0) -> None:
    with db_lock, connect() as connection:
        row = connection.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        progress = json.loads(row["progress"])
        progress[name] = {"completed": max(0, completed), "total": total,
                          "speed": max(0, speed)}
        connection.execute("UPDATE jobs SET progress=?,updated=? WHERE id=?",
                           (json.dumps(progress), int(time.time()), job_id))


READOUT = re.compile(r"\[#\w+\s+([\d.]+[KMGT]?i?B)/([\d.]+[KMGT]?i?B)"
                     r"\((\d+)%\).*?\bDL:([\d.]+[KMGT]?i?B)", re.I)


def run_aria2(command: list[str], job_id: str, name: str,
              expected_size: int | None, destination: Path) -> int:
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [*command[:1], "--summary-interval=1", "--show-console-readout=true", *command[1:]],
            cwd=destination, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
        )
    except Exception:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    buffer = ""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            lines = re.split(r"[\r\n]+", buffer)
            buffer = lines.pop()[-1000:]
            for line in lines:
                match = READOUT.search(line)
                if match:
                    current = parse_size(match.group(1)) or 0
                    total = expected_size or parse_size(match.group(2))
                    if total is not None:
                        current = min(current, total)
                    update_item_progress(job_id, name, current, total,
                                         parse_size(match.group(4)) or 0)
    finally:
        os.close(master)
    return process.wait()


def resolve_akira(url: str) -> str:
    if "/download/" in urllib.parse.urlsplit(url).path:
        return url
    raise RuntimeError("AkiraBox demande un lien /download/ obtenu après sa vérification humaine")


def download_one(item: dict, destination: Path, job_id: str) -> None:
    name = clean_name(item["name"])
    target = destination / name
    if target.is_file() and not Path(str(target) + ".aria2").exists():
        expected_size = item.get("size")
        if isinstance(expected_size, int) and target.stat().st_size == expected_size:
            with target.open("rb") as stream:
                signature = stream.read(16)
            if not signature.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                valid_rar = not name.lower().endswith(".rar") or signature.startswith(b"Rar!\x1a\x07")
                valid_7z = not name.lower().endswith(".7z") or signature.startswith(b"7z\xbc\xaf\x27\x1c")
                if valid_rar and valid_7z:
                    update_item_progress(job_id, name, target.stat().st_size,
                                         target.stat().st_size)
                    return
    url = item["url"]
    if item["host"] == "Rootz":
        rootz.download(item, target, lambda current, total, speed:
                       update_item_progress(job_id, name, current, total, speed))
        return
    if item["host"] == "Filekeeper":
        resolved = filekeeper.resolve(url)
        if clean_name(resolved["name"]) != name or resolved["size"] != item.get("size"):
            raise RuntimeError("Le fichier Filekeeper a changé depuis sa vérification")
        command = ["aria2c", "--continue=true", "--max-tries=8", "--retry-wait=5",
                   "--split=4", "--max-connection-per-server=4", "--min-split-size=32M",
                   "--dir", str(destination), "--out", name, resolved["direct"]]
    elif item["host"] == "AkiraBox":
        url = resolve_akira(url)
        command = ["aria2c", "--continue=true", "--max-tries=8", "--retry-wait=5",
                   "--split=4", "--max-connection-per-server=4", "--min-split-size=32M",
                   "--dir", str(destination), "--out", name, url]
    else:
        # Reuse the installed helper's validated resolver while controlling
        # aria2c's destination. Its CLI writes to the shared /data/downloads.
        loader = importlib.machinery.SourceFileLoader(
            "_portal_mediafire", MEDIAFIRE_HELPER
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None:
            raise RuntimeError("Résolveur MediaFire absent")
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        _, _, expected_size, referer, direct = helper.resolve_link(url, name)
        if isinstance(expected_size, int) and item.get("size") not in (None, expected_size):
            raise RuntimeError("La taille MediaFire a changé depuis la vérification")
        command = ["aria2c", "--continue=true", "--max-tries=8", "--retry-wait=5",
                   "--split=4", "--max-connection-per-server=4", "--min-split-size=32M",
                   "--dir", str(destination), "--out", name,
                   "--referer", referer, direct]
    return_code = run_aria2(command, job_id, name, item.get("size"), destination)
    if return_code or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(f"Téléchargement échoué (aria2c {return_code}) : {name}")
    with target.open("rb") as stream:
        first = stream.read(512).lstrip().lower()
    if first.startswith((b"<!doctype html", b"<html")):
        raise RuntimeError(f"L'hébergeur a renvoyé une page au lieu du fichier : {name}")
    if isinstance(item.get("size"), int) and target.stat().st_size != item["size"]:
        raise RuntimeError(f"Taille incorrecte : {name}")
    if name.lower().endswith(".7z") and not first.startswith(b"7z\xbc\xaf\x27\x1c"):
        raise RuntimeError(f"Le fichier téléchargé n'est pas une archive 7z valide : {name}")
    if name.lower().endswith(".rar") and not first.startswith(b"rar!\x1a\x07"):
        raise RuntimeError(f"Le fichier téléchargé n'est pas une archive RAR valide : {name}")
    update_item_progress(job_id, name, target.stat().st_size, target.stat().st_size)


def safe_archive_entries(first: Path, password: str) -> None:
    result = subprocess.run(["unrar", "lb", str(first)], input=(password + "\n") * 5,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("Archive chiffrée : saisis le bon mot de passe puis reprends l'extraction")
    for entry in result.stdout.splitlines():
        normalized = entry.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (path.is_absolute() or ".." in path.parts or not path.parts or
                ":" in path.parts[0] or any(ord(char) < 32 for char in normalized)):
            raise RuntimeError("L'archive contient un chemin non sûr")


def run_unrar(first: Path, output: Path, password: str, job_id: str) -> int:
    # Capture Unrar's live terminal percentage readout (including backspaces).
    # Keep the password on stdin, never in the command line or public progress.
    master, slave = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            ["unrar", "x", "-y", "-o-", str(first), str(output) + "/"],
            stdin=subprocess.PIPE, stdout=slave, stderr=slave,
        )
        os.close(slave)
        slave = None
        try:
            process.stdin.write(((password + "\n") * 10).encode())
            process.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        buffer = ""
        percent = -1
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            # Match Unrar's terminal readout, not '%' in an archive filename.
            matches = re.findall(r"\x08{4} *(\d{1,3})%", buffer)
            # Reserve 100% for a successful exit (including CRC verification).
            current = max((min(99, int(value)) for value in matches
                           if int(value) <= 100), default=percent)
            if current > percent:
                percent = current
                update(job_id, extraction=json.dumps({"percent": percent}))
            buffer = buffer[-128:]
        code = process.wait()
        if code == 0:
            update(job_id, extraction=json.dumps({"percent": 100}))
        return code
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()


def published_files(directory: Path, job_id: str) -> list[dict]:
    files = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("L'archive contient un lien symbolique non autorisé")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            files.append({"name": relative, "size": path.stat().st_size,
                          "url": f"/files/{job_id}/{urllib.parse.quote(relative)}"})
        if len(files) > 10_000:
            raise RuntimeError("Trop de fichiers dans le lot")
    return sorted(files, key=lambda file: file["name"].casefold())


def process(row: dict) -> None:
    job_id = row["id"]
    items = json.loads(row["items"])
    workspace = PRIVATE / job_id
    downloads = workspace / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    try:
        update(job_id, state="downloading", message=f"Téléchargement de {len(items)} fichier(s)",
               extraction="{}")
        workers = min(2 if all(item["host"] == "MediaFire" for item in items) else 3, len(items))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(download_one, item, downloads, job_id) for item in items]
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                future.result()
                completed += 1
                update(job_id, message=f"{completed}/{len(items)} fichier(s) téléchargé(s)")
        if row["mode"] == "rar":
            update(job_id, state="extracting", message="Décompression des archives RAR",
                   extraction=json.dumps({"percent": None}))
            first = next(downloads / item["name"] for item in items
                         if re.search(r"\.part0*1\.rar$", item["name"], re.I))
            safe_archive_entries(first, row["archive_password"])
            output = workspace / "extracted"
            output.mkdir(exist_ok=True)
            if run_unrar(first, output, row["archive_password"], job_id):
                raise RuntimeError("Extraction RAR échouée ; vérifie le mot de passe et les parties")
        else:
            output = downloads
        files = published_files(output, job_id)
        if not files:
            raise RuntimeError("Aucun fichier à publier")
        update(job_id, state="publishing", message="Publication des fichiers")
        os.replace(output, PUBLIC / job_id)
        update(job_id, state="ready", message="Prêt à télécharger", files=json.dumps(files),
               archive_password="", expires_at=int(time.time()) + RETENTION_SECONDS)
    except Exception as exc:
        update(job_id, state="failed", message=str(exc)[:300], archive_password="",
               expires_at=int(time.time()) + RETENTION_SECONDS)


def build_package(row: dict) -> None:
    job_id = row["id"]
    files = json.loads(row["files"])
    source_root = PUBLIC / job_id
    staging = PRIVATE / job_id / "download-all.zip.partial"
    target = source_root / f"download-{job_id}.zip"
    completed = 0
    last_report = 0.0
    try:
        staging.unlink(missing_ok=True)
        update(job_id, package_state="building", package_completed=0,
               package_message="Création du ZIP sans recompression")
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for file in files:
                relative = PurePosixPath(file["name"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Chemin de fichier invalide dans le lot")
                source = source_root.joinpath(*relative.parts)
                if not source.is_file() or source.is_symlink():
                    raise FileNotFoundError("Un fichier du lot n'est plus disponible")
                with source.open("rb") as reader, archive.open(
                    file["name"], "w", force_zip64=True
                ) as writer:
                    while chunk := reader.read(8 * 1024 * 1024):
                        writer.write(chunk)
                        completed += len(chunk)
                        if time.monotonic() - last_report >= 2:
                            update(job_id, package_completed=completed)
                            last_report = time.monotonic()
        os.replace(staging, target)
        update(job_id, package_state="ready", package_completed=completed,
               package_message="ZIP prêt à télécharger")
    except Exception as exc:
        staging.unlink(missing_ok=True)
        update(job_id, package_state="failed", package_message=str(exc)[:200])


def claim_next_job(package: bool = False) -> dict | None:
    with db_lock, connect() as connection:
        condition = "state='ready' AND package_state='queued'" if package else "state='queued'"
        order = "updated" if package else "created"
        row = connection.execute(
            f"SELECT * FROM jobs WHERE {condition} ORDER BY {order} LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        field, value = ("package_state", "building") if package else ("state", "downloading")
        connection.execute(f"UPDATE jobs SET {field}=?,updated=? WHERE id=?",
                           (value, int(time.time()), row["id"]))
        return {**dict(row), field: value}


def package_worker() -> None:
    while True:
        try:
            with worker_lock:
                row = claim_next_job(package=True)
                if row:
                    build_package(row)
        except Exception:
            pass
        time.sleep(2)


def remove_job_files(job_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{24}", job_id):
        raise ValueError("Identifiant de tâche invalide")
    for directory in (PRIVATE / job_id, PUBLIC / job_id):
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory)


def expire_job(row: dict) -> None:
    job_id = row["id"]
    remove_job_files(job_id)
    items = json.loads(row["items"])
    retained_items = [
        {key: item.get(key) for key in ("name", "size", "host", "mime")}
        for item in items
    ]
    update(job_id, state="expired", message="Fichiers supprimés après 5 jours",
           items=json.dumps(retained_items), files="[]", progress="{}", extraction="{}", archive_password="",
           package_state="expired", package_completed=0, package_total=0,
           package_message="", expires_at=row["expires_at"])


def cleanup_worker() -> None:
    while True:
        try:
            with worker_lock, connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE expires_at>0 AND expires_at<=? "
                    "AND state IN ('ready','failed') "
                    "AND package_state NOT IN ('queued','building') ORDER BY expires_at LIMIT 20",
                    (int(time.time()),),
                ).fetchall()
                for row in rows:
                    try:
                        expire_job(dict(row))
                    except OSError:
                        # Keep the row and retry on the next cleanup pass.
                        continue
        except Exception:
            pass
        time.sleep(60)


def worker() -> None:
    while True:
        try:
            with worker_lock:
                row = claim_next_job()
                if row:
                    process(row)
        except Exception:
            pass  # Keep the worker alive; the next queued job can still run.
        time.sleep(2)


threading.Thread(target=worker, daemon=True, name="transfer-worker").start()
threading.Thread(target=package_worker, daemon=True, name="package-worker").start()
threading.Thread(target=cleanup_worker, daemon=True, name="cleanup-worker").start()
