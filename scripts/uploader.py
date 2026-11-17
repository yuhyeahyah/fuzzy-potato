#!/usr/bin/env python3
"""
/uploader — resolve, baixa, re-hospeda, descompacta, junta partes ou envia pro Telegram (com split ZIP).

client_payload:
  chat_id, thread_id
  mode : "telegram" (padrão) | "link" | "upload" | "unzip" | "join"
  host : gofile | viking | pixeldrain | buzz | filebin | telegram   (destino de upload/unzip/join)
  items: [ {"url", "select"?} | {"file_id", "name", "size"} ... ]
         select = chaves marcadas nos botões (null/ausente = todos os arquivos do link)
  opts : {"pick": true} -> se o link tiver vários arquivos, manda a lista ao Worker (botões) e encerra
  cb   : {"url", "token"} -> endpoint /upl do Worker que recebe a lista
  (formato antigo ainda aceito: url, file_id, file_name)

Secrets: BOT_TOKEN, TG_API_ID, TG_API_HASH, TG_PREMIUM_SESSION (opcional, ~4 GB)
"""
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from html import escape as esc, unescape
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

PAYLOAD = json.loads(os.environ["PAYLOAD"])
ARGS = PAYLOAD  # aliases
CHAT_ID = int(PAYLOAD["chat_id"])
THREAD_ID = PAYLOAD.get("thread_id")
BOT_TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_BOT_BYTES = 49 * 1024 * 1024
MAX_MT_BYTES = 1950 * 1024 * 1024
MAX_PREMIUM_BYTES = 3900 * 1024 * 1024
MAX_ZIP_VOLUMES = 80

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
# cabeçalhos de navegador (hosts atrás de Cloudflare recusam o UA "pelado" do requests)
BROWSER = {
    **UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def say(text, html=False):
    data = {"chat_id": CHAT_ID, "text": str(text)[:4000], "disable_web_page_preview": True}
    if THREAD_ID:
        data["message_thread_id"] = int(THREAD_ID)
    if html:
        data["parse_mode"] = "HTML"
    try:
        requests.post(f"{API}/sendMessage", json=data, timeout=30)
    except Exception as e:
        print("say falhou:", e, file=sys.stderr)


def _multipart(fields, files=None):
    boundary = "----uploader" + os.urandom(8).hex()
    out = bytearray()
    for k, v in (fields or {}).items():
        if v is None:
            continue
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for field, fpath, fname, mime in files or []:
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fname}\"\r\n".encode()
        out += f"Content-Type: {mime}\r\n\r\n".encode()
        out += Path(fpath).read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def api(method, fields=None, files=None):
    body, ctype = _multipart(fields or {}, files)
    r = requests.post(f"{API}/{method}", data=body, headers={"Content-Type": ctype}, timeout=900)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": r.text[:300]}


# ---------------------------------------------------------------------------
# MTProto (arquivos grandes) — mesmo padrão do twitch_recovery.py
# ---------------------------------------------------------------------------
_BOT_CLIENT = None
_USER_CLIENT = None


def bot_client():
    global _BOT_CLIENT
    if _BOT_CLIENT is None:
        api_id = os.getenv("TG_API_ID", "")
        api_hash = os.getenv("TG_API_HASH", "")
        if not api_id or not api_hash:
            raise RuntimeError("TG_API_ID / TG_API_HASH ausentes (necessários para >49 MB)")
        from pyrogram import Client
        _BOT_CLIENT = Client("uploader-bot", api_id=int(api_id), api_hash=api_hash,
                             bot_token=BOT_TOKEN, in_memory=True, no_updates=True)
        _BOT_CLIENT.start()
    return _BOT_CLIENT


def select_uploader():
    global _USER_CLIENT
    session = os.getenv("TG_PREMIUM_SESSION", "").strip()
    if not session or CHAT_ID > 0:
        return None, MAX_MT_BYTES, "bot (~2 GB)"
    api_id = os.getenv("TG_API_ID", "")
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        return None, MAX_MT_BYTES, "bot (~2 GB)"
    try:
        from pyrogram import Client
        _USER_CLIENT = Client("uploader-premium", api_id=int(api_id), api_hash=api_hash,
                              session_string=session, in_memory=True, no_updates=True)
        _USER_CLIENT.start()
        user = _USER_CLIENT.get_me()
        if not getattr(user, "is_premium", False):
            _USER_CLIENT.stop()
            _USER_CLIENT = None
            return None, MAX_MT_BYTES, "bot (~2 GB)"
        _USER_CLIENT.get_chat(CHAT_ID)
        return _USER_CLIENT, MAX_PREMIUM_BYTES, "conta Premium (~4 GB)"
    except Exception as e:
        say(f"⚠️ Premium indisponível, usando bot (~2 GB): {e}")
        try:
            if _USER_CLIENT:
                _USER_CLIENT.stop()
        except Exception:
            pass
        _USER_CLIENT = None
        return None, MAX_MT_BYTES, "bot (~2 GB)"


def stop_clients():
    global _BOT_CLIENT, _USER_CLIENT
    for c in (_BOT_CLIENT, _USER_CLIENT):
        if c:
            try:
                c.stop()
            except Exception:
                pass
    _BOT_CLIENT = _USER_CLIENT = None


def send_document(path, caption="", premium_client=None):
    path = Path(path)
    sz = path.stat().st_size
    cap = caption[:1024]
    if premium_client:
        kwargs = {"chat_id": CHAT_ID, "document": str(path), "file_name": path.name, "caption": cap}
        if THREAD_ID:
            kwargs["message_thread_id"] = int(THREAD_ID)
        premium_client.send_document(**kwargs)
        return
    if sz <= MAX_BOT_BYTES:
        r = api("sendDocument", {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "caption": cap},
                [("document", path, path.name, "application/octet-stream")])
        if not r.get("ok"):
            raise RuntimeError(r.get("description", "sendDocument falhou"))
        return
    kwargs = {"chat_id": CHAT_ID, "document": str(path), "file_name": path.name, "caption": cap}
    if THREAD_ID:
        kwargs["message_thread_id"] = int(THREAD_ID)
    bot_client().send_document(**kwargs)


# ---------------------------------------------------------------------------
# Split ZIP (copiado/adaptado do twitch_recovery.py)
# ---------------------------------------------------------------------------
class ZipVolumeWriter:
    def __init__(self, archive_path, volume_size):
        self.archive_path = Path(archive_path)
        self.volume_size = int(volume_size)
        self.parts = []
        self.current = None
        self.part_bytes = 0
        self.total_bytes = 0

    def write(self, buffer):
        data = memoryview(buffer)
        remaining = len(data)
        offset = 0
        while remaining:
            if self.current is None or self.part_bytes >= self.volume_size:
                if self.current:
                    self.current.close()
                name = self.archive_path.with_name(f"{self.archive_path.name}.{len(self.parts)+1:03d}")
                self.current = name.open("wb")
                self.parts.append(name)
                self.part_bytes = 0
            n = min(remaining, self.volume_size - self.part_bytes)
            written = self.current.write(data[offset:offset+n])
            if written != n:
                raise OSError("falha ao gravar volume")
            self.part_bytes += n
            self.total_bytes += n
            offset += n
            remaining -= n
        return len(data)

    def flush(self):
        if self.current:
            self.current.flush()

    def close(self):
        if self.current:
            self.current.close()
            self.current = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def create_split_zip(video_path, output_dir, volume_bytes):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    file_size = video_path.stat().st_size
    approx = math.ceil((file_size + 65536) / volume_bytes)
    if approx > MAX_ZIP_VOLUMES:
        raise RuntimeError(f"Exigiria ~{approx} volumes. Use um intervalo menor ou qualidade mais baixa.")
    archive_path = output_dir / (video_path.stem + ".zip")
    writer = ZipVolumeWriter(archive_path, volume_bytes)
    with writer:
        with zipfile.ZipFile(writer, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            zf.write(video_path, arcname=video_path.name)
    return list(writer.parts)


def deliver(path, caption, premium_client, volume_limit):
    path = Path(path)
    size = path.stat().st_size
    if size <= volume_limit:
        say(f"📤 Enviando arquivo ({size/1024**3:.2f} GiB)…")
        send_document(path, caption, premium_client)
        return 1
    count = math.ceil((size + 65536) / volume_limit)
    say(f"📦 Arquivo {size/1024**3:.2f} GiB → ~{count} volumes ZIP.\nBaixe TODOS e junte na ordem.")
    zip_dir = path.parent / "zip_volumes"
    parts = create_split_zip(path, zip_dir, volume_limit)
    path.unlink(missing_ok=True)
    total = len(parts)
    for i, part in enumerate(parts, 1):
        cap = (f"📦 Parte {i:03d}/{total:03d} | {part.stat().st_size/1024**3:.2f} GiB\n{caption}\n"
               "Para reconstruir: coloque todos na mesma pasta e extraia o .zip final.")
        say(f"⬆️ Enviando ZIP {i:03d}/{total:03d}…")
        send_document(part, cap, premium_client)
        part.unlink(missing_ok=True)
    return total


# ---------------------------------------------------------------------------
# Resolvers (FileHost Toolkit — máximo de sites possível no servidor)
# ---------------------------------------------------------------------------
def _get(url, **kw):
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", 25)
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r


def _head(url, **kw):
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", 15)
    kw.setdefault("allow_redirects", True)
    return requests.head(url, **kw)


TERABOX_HOSTS = (
    "terabox.com", "nephobox.com", "4funbox.com", "mirrobox.com", "momerybox.com",
    "teraboxapp.com", "1024tera.com", "terabox.app", "gibibox.com", "goaibox.com",
    "terasharelink.com", "teraboxlink.com", "freeterabox.com", "1024terabox.com",
    "teraboxshare.com", "terafileshare.com", "terabox.club",
)
STREAMTAPE_HOSTS = (
    "streamtape.com", "streamtape.co", "streamtape.cc", "streamtape.to",
    "streamtape.net", "streamta.pe", "streamtape.xyz",
)
DOOD_HOSTS = (
    "dood.watch", "doodstream.com", "dood.to", "dood.so", "dood.cx", "dood.la", "dood.ws",
    "dood.sh", "doodstream.co", "dood.pm", "dood.wf", "dood.re", "dood.video", "dooood.com",
    "dood.yt", "doods.yt", "dood.stream", "doods.pro", "ds2play.com", "d0o0d.com",
    "ds2video.com", "do0od.com", "d000d.com",
)


def resolve(url: str) -> dict:
    """Devolve {"url": link_direto, "name": nome_sugerido, "note": "..."}"""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().replace("www.", "")

    # ---------- links diretos óbvios ----------
    if host.endswith("githubusercontent.com") or host.startswith(("cdn.", "download.")):
        name = Path(parsed.path).name or None
        return {"url": url, "name": name, "note": "link direto"}

    # ---------- Dropbox (link já direto) ----------
    if host.endswith("dropboxusercontent.com"):
        return {"url": url, "name": Path(parsed.path).name or None, "note": "Dropbox (link direto)"}

    # ---------- PixelDrain ----------
    if any(host.endswith(d) for d in ("pixeldrain.com", "pixeldrain.net", "pixeldrain.dev", "pixeldra.in")):
        m = re.search(r"/(?:u|api/file)/([A-Za-z0-9]+)", url)
        if m:
            name = None
            try:  # o nome real vem do /info (o ?download sozinho nomeava o arquivo com o ID)
                name = (_get(f"https://pixeldrain.com/api/file/{m.group(1)}/info").json() or {}).get("name")
            except Exception:
                pass
            return {"url": f"https://pixeldrain.com/api/file/{m.group(1)}?download",
                    "name": name, "note": "PixelDrain"}

    # ---------- GoFile ----------
    if "gofile.io" in host:
        m = re.search(r"/d/([A-Za-z0-9]+)", url)
        if m:
            try:
                r = _get(f"https://api.gofile.io/contents/{m.group(1)}")
                j = r.json()
                if j.get("status") == "ok":
                    contents = (j.get("data") or {}).get("contents") or {}
                    files = [v for v in contents.values() if isinstance(v, dict) and v.get("type") == "file"]
                    if files:
                        f = files[0]
                        return {"url": f.get("link") or f.get("directLink") or url,
                                "name": f.get("name"), "note": "GoFile"}
            except Exception:
                pass
        return {"url": url, "name": None, "note": "GoFile (não resolvido)"}

    # ---------- VikingFile ----------
    if "vikingfile.com" in host or "vik1ngfile" in host:
        return {"url": url, "name": None, "note": "VikingFile"}

    # ---------- MediaFire ----------
    if "mediafire.com" in host:
        try:
            r = _get(url)
            m = re.search(r'href="(https://download[^"]+)"', r.text)
            if not m:
                m = re.search(r'aria-label="Download file"[^>]*href="([^"]+)"', r.text)
            if m:
                return {"url": m.group(1), "name": None, "note": "MediaFire"}
        except Exception as e:
            return {"url": url, "name": None, "note": f"MediaFire ({e})"}

    # ---------- Google Drive ----------
    if any(x in host for x in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")):
        m = re.search(r"/d/([A-Za-z0-9_-]+)", url) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
        if m:
            link, name = gdrive_resolve(m.group(1))
            return {"url": link, "name": name, "note": "Google Drive"}

    # ---------- Filebin ----------
    if "filebin.net" in host:
        return {"url": url, "name": Path(parsed.path).name or None, "note": "Filebin"}

    # ---------- imgur.gg ----------
    if "imgur.gg" in host:
        try:
            if "/f/" in url or "/archive/" in url or "/zip/" in url:
                r = _get(url)
                m = re.search(r'bucketKey["\s:]+["\']([^"\']+)', r.text)
                if m:
                    return {"url": f"https://b.imgur.gg/{m.group(1)}", "name": None, "note": "imgur.gg"}
        except Exception:
            pass
        return {"url": url, "name": None, "note": "imgur.gg"}

    # ---------- Pillows / Pillowcase ----------
    if "pillows.su" in host or "pillowcase.su" in host:
        m = re.search(r"([0-9a-f]{32})", url, re.I)
        if m:
            return {"url": f"https://api.pillows.su/api/download/{m.group(1)}",
                    "name": None, "note": "Pillows"}

    # ---------- ExoShare ----------
    if "exoshare.org" in host or "exoshare" in host:
        return {"url": url, "name": None, "note": "ExoShare (API pode precisar de share id)"}

    # ---------- BuzzHeavier ----------
    if "buzzheavier.com" in host or "bzzhr" in host:
        m = re.search(r"/([A-Za-z0-9]+)/?$", parsed.path)
        if m:
            return {"url": f"https://buzzheavier.com/{m.group(1)}/download",
                    "name": None, "note": "BuzzHeavier"}

    # ---------- KrakenFiles ----------
    if "krakenfiles.com" in host:
        try:
            r = _get(url)
            m = re.search(r'href="(/download/[^"]+)"', r.text)
            if m:
                return {"url": "https://krakenfiles.com" + m.group(1), "name": None, "note": "KrakenFiles"}
        except Exception:
            pass
        return {"url": url, "name": None, "note": "KrakenFiles"}

    # ---------- upload.ee ----------
    if "upload.ee" in host:
        try:
            r = _get(url)
            m = re.search(r'href="(https://[^"]+\.upload\.ee/[^"]+)"', r.text)
            if m:
                return {"url": m.group(1), "name": None, "note": "upload.ee"}
        except Exception:
            pass

    # ---------- send.cm / tmpsend ----------
    if "send.cm" in host or "tmpsend.com" in host:
        return {"url": url, "name": None, "note": "send.cm / tmpsend"}

    # ---------- qiwi.gg ----------
    if "qiwi.gg" in host:
        return {"url": url, "name": None, "note": "qiwi.gg"}

    # ---------- mp4upload ----------
    if "mp4upload.com" in host:
        return {"url": url, "name": None, "note": "mp4upload"}

    # ---------- akmfiles / hxfile / shrdsk ----------
    if any(x in host for x in ("akmfiles.com", "akmfls.xyz", "hxfile.co", "shrdsk.me")):
        return {"url": url, "name": None, "note": host}

    # ---------- pCloud ----------
    if "pcloud.link" in host or "u.pcloud.link" in host:
        return {"url": url, "name": None, "note": "pCloud"}

    # ---------- transfer.it ----------
    if "transfer.it" in host:
        return {"url": url, "name": None, "note": "transfer.it"}

    # ---------- solidfiles ----------
    if "solidfiles.com" in host:
        try:
            r = _get(url)
            m = re.search(r'"downloadUrl":"([^"]+)"', r.text)
            if m:
                return {"url": m.group(1).replace("\\/", "/"), "name": None, "note": "SolidFiles"}
        except Exception:
            pass

    # ---------- Yandex Disk ----------
    if "yadi.sk" in host or "disk.yandex" in host:
        try:
            api_url = f"https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key={quote(url)}"
            r = _get(api_url)
            j = r.json()
            if j.get("href"):
                return {"url": j["href"], "name": None, "note": "Yandex Disk"}
        except Exception:
            pass
        return {"url": url, "name": None, "note": "Yandex Disk"}

    # ---------- WeTransfer ----------
    if "wetransfer.com" in host or "we.tl" in host:
        return {"url": url, "name": None, "note": "WeTransfer (pode precisar de interação)"}

    # ---------- SwissTransfer ----------
    if "swisstransfer.com" in host:
        return {"url": url, "name": None, "note": "SwissTransfer"}

    # ---------- SendGB ----------
    if "sendgb.com" in host:
        return {"url": url, "name": None, "note": "SendGB"}

    # ---------- Dropbox ----------
    if "dropbox.com" in host:
        folder = bool(re.search(r"/(?:scl/fo|sh)/", parsed.path)) and "/file/" not in parsed.path
        if folder:
            u = url.split("#")[0].replace("dl=0", "dl=1")
            if "dl=1" not in u:
                u += ("&" if "?" in u else "?") + "dl=1"
            return {"url": u, "name": None, "server_name": True, "note": "Dropbox (pasta → zip)"}
        if "?" in url:
            return {"url": url.replace("?dl=0", "?dl=1").split("&")[0] + ("&dl=1" if "dl=1" not in url else ""),
                    "name": None, "note": "Dropbox"}
        return {"url": url + ("&" if "?" in url else "?") + "dl=1", "name": None, "note": "Dropbox"}

    # ---------- Terabox ----------
    if any(x in host for x in TERABOX_HOSTS):
        return {"url": url, "name": None, "note": "Terabox (resolução limitada no servidor)"}

    # ---------- Box ----------
    if "box.com" in host:
        return {"url": url, "name": None, "note": "Box (use o modo zip_download se possível)"}

    # ---------- OneDrive ----------
    if "onedrive.live.com" in host or "1drv.ms" in host:
        return {"url": url, "name": None, "note": "OneDrive (melhor via downloadUrl capturado)"}

    # ---------- BerkasDrive ----------
    if "berkasdrive.com" in host:
        try:
            r = _get(url)
            m = re.search(r"<script\b[^>]*>(.*?)</script>", r.text, re.S | re.I)
            if m:
                parts = m.group(1).split('"')
                if len(parts) > 1:
                    direct = base64.b64decode(parts[1]).decode("utf-8", "ignore").strip()
                    if direct.startswith(("http://", "https://")):
                        return {"url": direct, "name": None, "note": "BerkasDrive"}
        except Exception as e:
            return {"url": url, "name": None, "note": f"BerkasDrive ({e})"}

    # ---------- OSDN ----------
    if "osdn.net" in host:
        try:
            r = _get(url)
            tag = re.search(r'<a\b[^>]*\bclass="[^"]*\bmirror_link\b[^"]*"[^>]*>', r.text)
            href = re.search(r'href="([^"]+)"', tag.group(0)) if tag else None
            if href:
                link = unescape(href.group(1))
                if link.startswith("/"):
                    link = "https://osdn.net" + link
                return {"url": link, "name": None, "note": "OSDN"}
        except Exception as e:
            return {"url": url, "name": None, "note": f"OSDN ({e})"}

    # ---------- send.now (XFileSharing: a página tem um form op=download2) ----------
    if host == "send.now" or host.endswith((".send.now", ".send.cm")):
        try:
            return sendnow_resolve(url)
        except Exception as e:
            return {"url": url, "name": None, "note": f"send.now ({e})"}

    # ---------- SourceForge ----------
    if "sourceforge.net" in host:
        if "/download" not in url:
            return {"url": url.rstrip("/") + "/download", "name": None, "note": "SourceForge"}
        return {"url": url, "name": None, "note": "SourceForge"}

    # ---------- GitHub Releases ----------
    if "github.com" in host and "/releases/" in url:
        return {"url": url, "name": Path(parsed.path).name, "note": "GitHub Release"}

    # ---------- Streamtape / Dood (limitado) ----------
    if any(host == d or host.endswith("." + d) for d in STREAMTAPE_HOSTS + DOOD_HOSTS):
        return {"url": url, "name": None, "note": f"{host} (player, resolução limitada)"}

    # ---------- fuckingfast / lulacloud / devuploads / uploadhaven ----------
    if any(x in host for x in ("fuckingfast.co", "lulacloud.com", "devuploads.com", "uploadhaven.com")):
        return {"url": url, "name": None, "note": host}

    # ---------- Fallback HEAD ----------
    try:
        h = _head(url)
        cd = h.headers.get("content-disposition", "")
        ct = h.headers.get("content-type", "")
        if "attachment" in cd.lower() or ct.startswith(("application/", "video/", "audio/", "image/")):
            name = None
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            if m:
                name = unquote(m.group(1))
            return {"url": h.url, "name": name, "note": "link direto (HEAD)"}
    except Exception:
        pass

    return {"url": url, "name": None, "note": "não resolvido (usando o link original)"}


# ---------------------------------------------------------------------------
# Utilidades de arquivo
# ---------------------------------------------------------------------------
def safe_name(s, default="arquivo"):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(s or "")).strip().strip(".")
    return (s or default)[:150]


def unique_path(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / safe_name(name)
    if not p.exists():
        return p
    n = 1
    while True:
        q = folder / f"{p.stem} ({n}){p.suffix}"
        if not q.exists():
            return q
        n += 1


def download_to(url, dest: Path, name_hint=None, headers=None):
    dest.mkdir(parents=True, exist_ok=True)
    h = dict(UA)
    h.update(headers or {})
    with requests.get(url, headers=h, stream=True, timeout=120) as r:
        r.raise_for_status()
        # nome a partir do Content-Disposition se não tiver
        if not name_hint:
            cd = r.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            if m:
                name_hint = unquote(m.group(1)).replace('"', "")
        name = name_hint or unquote(Path(urlparse(r.url or url).path).name) or "arquivo"
        ct = (r.headers.get("content-type") or "").lower()
        if ct.startswith("text/html") and not name.lower().endswith((".html", ".htm")):
            raise RuntimeError("o link não é um download direto (o site devolveu uma página HTML)")
        final = unique_path(dest, name)
        with final.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    return final


def tg_download(file_id, dest: Path, name=None):
    target = unique_path(dest, name or "arquivo")
    got = bot_client().download_media(file_id, file_name=str(target))
    if not got:
        raise RuntimeError("download do Telegram falhou")
    return Path(got)


def say_long(lines, limit=3800):
    buf = ""
    for ln in lines:
        ln = str(ln)
        if len(ln) > limit:
            ln = ln[: limit - 1] + "…"
        if buf and len(buf) + 1 + len(ln) > limit:
            say(buf)
            buf = ln
        else:
            buf = f"{buf}\n{ln}" if buf else ln
    if buf:
        say(buf)


# ---------------------------------------------------------------------------
# Expansão de links (um link pode virar vários arquivos: pastas)
#   expand(url) -> {"title", "entries": [{key,name,size,path,url,headers}], "warn", "note"}
# ---------------------------------------------------------------------------
PICK_MAX = 40  # quantos itens vão para os botões (igual ao UP_MAX_BUTTONS do Worker)


def _post(url, **kw):
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", 25)
    r = requests.post(url, **kw)
    r.raise_for_status()
    return r


def _finalize(entries):
    """Nome, pasta e chave única; ordem estável (a mesma nas duas execuções)."""
    out, seen = [], {}
    for e in entries:
        if not e.get("url"):
            continue
        e["name"] = e.get("name") or unquote(Path(urlparse(e["url"]).path).name) or "arquivo"
        e["path"] = e.get("path") or ""
        e["size"] = e.get("size") or 0
        out.append(e)
    out.sort(key=lambda e: (e["path"].lower(), e["name"].lower()))
    for e in out:
        key = (e["path"] + "/" if e["path"] else "") + e["name"]
        seen[key] = seen.get(key, 0) + 1
        e["key"] = key if seen[key] == 1 else f"{key}#{seen[key]}"
    return out


def gofile_expand(url: str) -> dict:
    """Pasta ou arquivo do GoFile (mesma lógica do FileHost Toolkit: token de conta + X-Website-Token)."""
    pw = ""
    if "::" in url:
        parts = url.split("::")
        pw = hashlib.sha256(parts[-1].encode()).hexdigest()
        url = parts[-2]
    root = url.split("?")[0].rstrip("/").split("/")[-1]
    ua = UA["User-Agent"]
    tk = _post("https://api.gofile.io/accounts", headers={"User-Agent": ua}).json()
    if tk.get("status") != "ok":
        raise RuntimeError("GoFile: sem token")
    token = tk["data"]["token"]
    salt = "12af056dacea0b"
    try:
        js = _get("https://gofile.io/js/wt.obf.js").text
        js = re.sub(r"\\x([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), js)
        i = js.find("generateWT")
        m = re.search(r"'([0-9a-f]{14})'", js[i:]) if i >= 0 else None
        if m:
            salt = m.group(1)
    except Exception:
        pass
    wt = hashlib.sha256(f"{ua}::en-US::{token}::{int(time.time() / 14400)}::{salt}".encode()).hexdigest()
    hdr = {"User-Agent": ua, "Authorization": f"Bearer {token}", "X-Website-Token": wt, "X-BL": "en-US"}
    cookie = {"Cookie": f"accountToken={token}"}
    entries, title = [], [""]

    def walk(cid, path, depth=0):
        j = _get(f"https://api.gofile.io/contents/{cid}?cache=true" + (f"&password={pw}" if pw else ""),
                 headers=hdr).json()
        if j.get("status") != "ok":
            raise RuntimeError(f"GoFile: {j.get('status')}")
        d = j.get("data") or {}
        if depth == 0:
            title[0] = d.get("name") or ""
        ch = d.get("children")
        if ch is None:  # o id é de um arquivo
            entries.append({"url": d.get("link"), "name": d.get("name"), "size": d.get("size"),
                            "path": path, "headers": cookie})
            return
        for c in ch.values():
            if not isinstance(c, dict):
                continue
            if c.get("type") == "folder":
                if c.get("public") is not False and depth < 5:
                    walk(c.get("id"), (path + "/" if path else "") + (c.get("name") or ""), depth + 1)
            else:
                entries.append({"url": c.get("link"), "name": c.get("name"), "size": c.get("size"),
                                "path": path, "headers": cookie})

    walk(root, "")
    ents = _finalize(entries)
    if not ents:
        raise RuntimeError("GoFile: nenhum arquivo encontrado (pasta vazia, privada ou com senha)")
    return {"title": title[0], "entries": ents, "warn": "", "note": "GoFile"}


def pixeldrain_list(url: str) -> dict:
    m = re.search(r"/l/([A-Za-z0-9]+)", url)
    if not m:
        raise RuntimeError("link de lista do PixelDrain inválido")
    j = _get(f"https://pixeldrain.com/api/list/{m.group(1)}").json()
    entries = [{"url": f"https://pixeldrain.com/api/file/{f['id']}?download", "name": f.get("name"),
                "size": f.get("size")} for f in (j.get("files") or []) if f.get("id")]
    ents = _finalize(entries)
    if not ents:
        raise RuntimeError("PixelDrain: lista vazia")
    return {"title": j.get("title") or "", "entries": ents, "warn": "", "note": "PixelDrain (lista)"}


def onedrive_expand(url: str) -> dict:
    """Link público do OneDrive pessoal (1drv.ms / onedrive.live.com) pela API de shares.
    Lista os arquivos do primeiro nível; subpastas não são abertas."""
    enc = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    base = f"https://api.onedrive.com/v1.0/shares/{enc}"
    hdr = {**UA, "Accept": "application/json"}
    j = _get(base + "/root?expand=children", headers=hdr).json()
    entries, folders, nolink = [], 0, 0

    def add(c):
        nonlocal nolink
        dl = c.get("@content.downloadUrl")
        if not dl:
            nolink += 1
            return
        entries.append({"url": dl, "name": c.get("name"), "size": c.get("size")})

    if "folder" in j or "children" in j:
        children = list(j.get("children") or [])
        nxt = j.get("children@odata.nextLink")
        for _ in range(20):
            if not nxt:
                break
            page = _get(nxt, headers=hdr).json()
            children += page.get("value") or []
            nxt = page.get("@odata.nextLink")
        for c in children:
            if "folder" in c:
                folders += 1
            else:
                add(c)
    else:
        add(j)
    ents = _finalize(entries)
    if not ents:
        raise RuntimeError("OneDrive: nenhum arquivo com link direto (link privado/empresarial?)")
    warn = []
    if folders:
        warn.append(f"{folders} subpasta(s) não foram abertas")
    if nolink:
        warn.append(f"{nolink} arquivo(s) sem link direto")
    return {"title": j.get("name") or "", "entries": ents, "warn": "; ".join(warn), "note": "OneDrive"}


# ---------- Box (mesma leitura do box2gofile.py: Box.postStreamData da página) ----------
BOX_MAX_DEPTH = 6
BOX_MAX_FILES = 2000


def box_expand(url: str) -> dict:
    p = urlparse(url)
    m = re.match(r"^(/(?:s|v)/[A-Za-z0-9_\-]+)", p.path)
    base = f"{p.scheme}://{p.netloc}"
    if "zip_download" in url or "box_download_shared_file" in url:
        return {"title": "Box", "entries": _finalize([{"url": url, "name": None, "size": 0}]),
                "warn": "", "note": "Box (link direto)"}
    if not m:
        raise RuntimeError("não parece um link compartilhado do Box (/s/... ou /v/...)")
    root = m.group(1)
    st = {"shared": None, "warn": []}

    def load(u):
        html = _get(u, timeout=30).text
        mm = re.search(r"Box\.postStreamData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
        if not mm:
            return None, html
        try:
            data = json.loads(mm.group(1))
        except ValueError:
            return None, html
        si = data.get("/app-api/enduserapp/shared-item") or {}
        if si.get("sharedName") and not st["shared"]:
            st["shared"] = si["sharedName"]   # no /v/nome o shared_name real é outro
        return data, html

    def direct(fid):
        return (f"{base}/index.php?rm=box_download_shared_file"
                f"&shared_name={st['shared']}&file_id=f_{fid}")

    def parse(data):
        folder = data.get("/app-api/enduserapp/shared-folder")
        if not folder:
            return None, []
        if (folder.get("pageCount") or 1) > 1:
            st["warn"].append(f"'{folder.get('currentFolderName')}' tem mais de uma página; só a primeira foi lida")
        return folder.get("currentFolderName"), folder.get("items") or []

    entries = []

    def walk(items, path, depth):
        for it in items:
            if len(entries) >= BOX_MAX_FILES:
                return
            name = it.get("name") or "arquivo"
            if it.get("type") == "file":
                entries.append({"url": direct(it["id"]), "name": name, "size": it.get("itemSize") or 0, "path": path})
            elif depth < BOX_MAX_DEPTH:
                d, _ = load(f"{base}{root}/folder/{it['id']}")
                if not d:
                    st["warn"].append(f"não consegui abrir a pasta '{name}'")
                    continue
                _, sub = parse(d)
                walk(sub, (path + "/" if path else "") + safe_name(name), depth + 1)
            else:
                st["warn"].append(f"pasta '{name}' muito funda, ignorada")

    data, html = load(url)
    title = ""
    if data:
        si = data.get("/app-api/enduserapp/shared-item") or {}
        if si.get("itemType") == "file":
            fi = data.get("/app-api/enduserapp/shared-file") or {}
            for k, v in data.items():
                if not fi and k.startswith("/app-api/enduserapp/item/") and isinstance(v, dict):
                    fi = next((x for x in v.get("items") or [] if str(x.get("id")) == str(si["itemID"])), {})
            if not fi.get("name"):
                n = (re.search(r'<meta property="og:title" content="([^"]+?)(?: \| Powered by Box)?"', html or "")
                     or re.search(r"<title>(.*?) \|", html or "", re.S))
                fi = {"name": unescape(n.group(1)).strip() if n else None, "itemSize": fi.get("itemSize")}
            entries.append({"url": direct(si["itemID"]), "name": fi.get("name"), "size": fi.get("itemSize") or 0})
            title = fi.get("name") or ""
        else:
            title, items = parse(data)
            walk(items, "", 0)
    if not entries:
        f = re.search(r'"typedID":"f_(\d+)"', html or "")
        if f and st["shared"]:
            entries.append({"url": direct(f.group(1)), "name": None, "size": 0})
    ents = _finalize(entries)
    if not ents:
        raise RuntimeError("Box: não consegui listar os arquivos (link privado ou expirado?)")
    if len(entries) >= BOX_MAX_FILES:
        st["warn"].append(f"parei em {BOX_MAX_FILES} arquivos")
    return {"title": title or "Box", "entries": ents, "warn": "; ".join(st["warn"]), "note": "Box"}


# ---------- SendGB (sendgb.com/CODIGO ou /download/CODIGO) ----------
SENDGB_API = "https://api.sendgb.com/api/download"
SENDGB_KEY = re.compile(r"\b([A-Z0-9]{5}(?:-[A-Z0-9]{5}){3}/[^\s\"'<>?#\\]+)")


def _sendgb_files(obj, out):
    """Procura no JSON objetos que pareçam arquivos: {key|path|file_key: 'XXXXX-.../nome', name, size}."""
    if isinstance(obj, dict):
        key = next((obj[k] for k in ("key", "file_key", "fileKey", "path", "object_key", "s3_key")
                    if isinstance(obj.get(k), str) and SENDGB_KEY.fullmatch(obj[k])), None)
        if key:
            out.append({"key": key,
                        "name": obj.get("name") or obj.get("original_name") or obj.get("originalName")
                        or obj.get("file_name") or obj.get("filename"),
                        "size": obj.get("size") or obj.get("file_size") or obj.get("fileSize") or 0,
                        "path": obj.get("relative_path") or obj.get("folder") or ""})
        for v in obj.values():
            _sendgb_files(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _sendgb_files(v, out)


def sendgb_expand(url: str) -> dict:
    m = re.search(r"sendgb\.com/(?:[a-z]{2}/)?(?:download/)?([A-Za-z0-9]{6,20})(?:[/?#]|$)", url)
    if not m:
        raise RuntimeError("código do SendGB não encontrado no link")
    code = m.group(1)
    hdr = {**UA, "Accept": "application/json", "Origin": "https://www.sendgb.com",
           "Referer": f"https://www.sendgb.com/download/{code}"}
    found, title, err = [], "", ""
    for ep in ("", "/files", "/info"):
        try:
            r = requests.get(f"{SENDGB_API}/{code}{ep}", headers=hdr, timeout=25)
            if r.status_code != 200 or "json" not in (r.headers.get("content-type") or ""):
                err = f"HTTP {r.status_code}"
                continue
            j = r.json()
            _sendgb_files(j, found)
            d = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else j
            if isinstance(d, dict):
                title = d.get("subject") or d.get("transfer_name") or d.get("name") or title
            if found:
                break
        except Exception as e:
            err = str(e)
    if not found:  # plano B: chaves que aparecem no HTML da página
        try:
            html = _get(f"https://www.sendgb.com/download/{code}", timeout=30).text.replace("\\/", "/")
            for k in dict.fromkeys(SENDGB_KEY.findall(html)):
                found.append({"key": k, "name": None, "size": 0, "path": ""})
        except Exception as e:
            err = str(e)
    seen, entries = set(), []
    for f in found:
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        name = f["name"] or unquote(f["key"].split("/")[-1])
        entries.append({"url": f"{SENDGB_API}/{code}/presign?key={quote(f['key'], safe='')}",
                        "presign": True, "name": name, "size": int(f["size"] or 0),
                        "path": str(f["path"] or "").strip("/"), "headers": hdr})
    ents = _finalize(entries)
    if not ents:
        raise RuntimeError(f"SendGB: não achei a lista de arquivos ({err or 'resposta vazia'}); "
                           "a transferência pode ter senha ou ter expirado")
    return {"title": title or f"SendGB {code}", "entries": ents, "warn": "", "note": "SendGB"}


def sendgb_presign(e: dict) -> str:
    """O presign devolve o link assinado do R2 (JSON com url, texto puro ou redirect)."""
    r = requests.get(e["url"], headers=e.get("headers") or UA, timeout=25, allow_redirects=False)
    if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
        return r.headers["location"]
    r.raise_for_status()
    t = r.text.strip()
    try:
        j = r.json()
        stack = [j]
        while stack:
            o = stack.pop()
            if isinstance(o, str) and o.startswith("https://"):
                return o
            if isinstance(o, dict):
                for k in ("url", "presigned_url", "presignedUrl", "download_url", "link"):
                    if isinstance(o.get(k), str) and o[k].startswith("https://"):
                        return o[k]
                stack += list(o.values())
            elif isinstance(o, list):
                stack += o
    except ValueError:
        if t.startswith("https://"):
            return t.split()[0]
    raise RuntimeError("SendGB: o presign não devolveu um link")


# ---------- imgur.gg (arquivos /f/ID; o JSON da página traz bucketKey e nome) ----------
def imgurgg_expand(url: str) -> dict:
    html = _get(url, timeout=30).text
    if "/archive/" in urlparse(url).path:
        return imgurgg_archive(url, html)
    txt = html.replace('\\"', '"').replace("\\u0026", "&")
    entries, seen = [], set()
    for mk in re.finditer(r'"bucketKey":"([^"]+)"', txt):
        key = mk.group(1)
        if key in seen:
            continue
        seen.add(key)
        ctx = txt[max(0, mk.start() - 800): mk.start()]
        on = re.findall(r'"originalName":"((?:[^"\\]|\\.)*)"', ctx)
        sz = re.findall(r'"size":(\d+)', ctx)
        name = (on[-1] if on else key.split("-", 1)[-1])
        try:
            name = json.loads(f'"{name}"')
        except ValueError:
            pass
        entries.append({"url": f"https://b.imgur.gg/{quote(key)}?download={quote(name)}", "name": name,
                        "size": int(sz[-1]) if sz else 0,
                        "headers": {"Referer": "https://imgur.gg/"}})
    if not entries:
        m = re.search(r"/f/([A-Za-z0-9]+)", url)
        if not m:
            raise RuntimeError("imgur.gg: não achei o arquivo na página")
        entries.append({"url": f"https://imgur.gg/api/file/{m.group(1)}/download", "name": None, "size": 0,
                        "headers": {"Referer": url}})
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    title = unescape(t.group(1)).replace(" - imgur.gg", "").strip() if t else "imgur.gg"
    return {"title": title, "entries": _finalize(entries), "warn": "", "note": "imgur.gg"}


# ---------- imgur.gg /archive/ID (lista de arquivos no JSON da página) ----------
def _json_after(txt: str, marker: str):
    """Decodifica o valor JSON que vem logo depois de `marker` (ex.: '"files":')."""
    i = txt.find(marker)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(txt[i + len(marker):])[0]
    except ValueError:
        return None


def imgurgg_archive(url: str, html: str) -> dict:
    txt = html.replace('\\"', '"').replace("\\u0026", "&").replace("\\\\", "\\")
    files = _json_after(txt, '"files":') or []
    entries = []
    for f in files:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        fid, stored = f["id"], f.get("name") or ""
        name = f.get("originalName") or stored or fid
        direct = f"https://b.imgur.gg/{quote(fid + '-' + stored)}?download={quote(name)}" if stored else None
        api_dl = f"https://imgur.gg/api/file/{fid}/download"
        entries.append({"url": direct or api_dl, "fallback": api_dl if direct else None,
                        "name": name, "size": f.get("size") or 0,
                        "headers": {"Referer": "https://imgur.gg/"}})
    if not entries:
        raise RuntimeError("imgur.gg: não achei a lista de arquivos do arquivo/álbum")
    m = re.search(r"/archive/([A-Za-z0-9]+)", url)
    return {"title": f"imgur.gg archive {m.group(1) if m else ''}".strip(),
            "entries": _finalize(entries), "warn": "", "note": "imgur.gg (archive)"}


# ---------- Pillows / Pillowcase (o nome real vem no JSON do SvelteKit na página) ----------
def pillows_expand(url: str) -> dict:
    m = re.search(r"([0-9a-f]{32})", url, re.I)
    if not m:
        raise RuntimeError("Pillows: não achei o ID no link")
    fid = m.group(1).lower()
    name, size = None, 0
    try:
        html = _get(f"https://pillows.su/f/{fid}", timeout=30).text
        n = (re.search(r'filename:"((?:[^"\\]|\\.)*)"', html)
             or re.search(r'<meta property="og:title" content="([^"]+)"', html)
             or re.search(r"<title>(.*?) \| Pillowcase</title>", html, re.S))
        if n:
            name = unescape(n.group(1))
            try:
                name = json.loads(f'"{name}"')
            except ValueError:
                pass
        s = re.search(r"fileSize:(\d+)n?", html)
        size = int(s.group(1)) if s else 0
    except Exception as e:
        print("pillows: página falhou:", e, file=sys.stderr)
    if not name:  # sem página: tenta o nome que o servidor manda no download
        try:
            h = _head(f"https://api.pillows.su/api/download/{fid}")
            c = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', h.headers.get("content-disposition", ""))
            name = unquote(c.group(1)) if c else None
        except Exception:
            pass
    ents = _finalize([{"url": f"https://api.pillows.su/api/download/{fid}", "name": name or fid, "size": size,
                       "fallback": f"https://api.pillows.su/api/get/{fid}"}])
    return {"title": name or "Pillows", "entries": ents, "warn": "" if name else "não achei o nome do arquivo",
            "note": "Pillows"}


# ---------- WeTransfer (lista no __NEXT_DATA__; link assinado pedido na hora de baixar) ----------
WT_API = "https://wetransfer.com/api/v4/transfers"


def _wt_session(page_url: str):
    s = requests.Session()
    s.headers.update(UA)
    r = s.get(page_url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
    s.headers.update({"x-requested-with": "XMLHttpRequest", "Origin": "https://wetransfer.com",
                      "Referer": r.url, "Accept": "application/json"})
    if csrf:
        s.headers["x-csrf-token"] = csrf.group(1)
    return s, r


def _wt_parse(u: str):
    p = [x for x in urlparse(u).path.split("/") if x]
    if "downloads" not in p:
        return None
    p = p[p.index("downloads") + 1:]
    if len(p) == 2:
        return p[0], None, p[1]
    if len(p) >= 3:
        return p[0], p[1], p[2]
    return None


def wetransfer_expand(url: str) -> dict:
    s, r = _wt_session(url)          # segue o redirect do we.tl
    ids = _wt_parse(r.url) or _wt_parse(url)
    if not ids:
        raise RuntimeError("WeTransfer: link sem /downloads/ID/HASH")
    tid, rid, sec = ids
    tr = {}
    nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if nd:
        try:
            tr = json.loads(nd.group(1))["props"]["pageProps"].get("initialTransfer") or {}
        except (ValueError, KeyError, TypeError):
            tr = {}
    if not tr.get("items"):
        body = {"security_hash": sec}
        if rid:
            body["recipient_id"] = rid
        tr = s.post(f"{WT_API}/{tid}/prepare-download", json=body, timeout=30).json()
    if tr.get("password_protected"):
        raise RuntimeError("WeTransfer: transferência protegida por senha")
    if tr.get("state") not in (None, "downloadable"):
        raise RuntimeError(f"WeTransfer: transferência indisponível ({tr.get('state')})")
    entries = []
    for it in tr.get("items") or []:
        if it.get("item_type", "file") != "file" or not it.get("id"):
            continue
        full = it.get("name") or it["id"]
        path, _, name = full.rpartition("/")
        entries.append({"url": f"{WT_API}/{tid}/download#{it['id']}", "presign": "wetransfer",
                        "wt": {"tid": tid, "rid": rid, "sec": sec, "fid": it["id"]},
                        "name": name, "path": path, "size": it.get("size") or 0})
    if not entries:
        raise RuntimeError("WeTransfer: nenhum arquivo na transferência")
    warn = []
    total = sum(e["size"] for e in entries)
    free = shutil.disk_usage(".").free
    if max(e["size"] for e in entries) > free:
        warn.append(f"tem arquivo maior que o disco livre do runner ({free / 2**30:.0f} GiB); esse vai falhar")
    elif total > free:
        warn.append(f"o total ({total / 2**30:.1f} GiB) passa do disco livre ({free / 2**30:.0f} GiB); escolha menos arquivos")
    return {"title": tr.get("display_name") or tr.get("recommended_filename") or "WeTransfer",
            "entries": _finalize(entries), "warn": "; ".join(warn), "note": "WeTransfer"}


def wetransfer_link(e: dict) -> str:
    w = e["wt"]
    page = f"https://wetransfer.com/downloads/{w['tid']}/" + (f"{w['rid']}/" if w.get("rid") else "") + w["sec"]
    s, _ = _wt_session(page)
    body = {"security_hash": w["sec"], "intent": "single_file", "file_ids": [w["fid"]]}
    if w.get("rid"):
        body["recipient_id"] = w["rid"]
    r = s.post(f"{WT_API}/{w['tid']}/download", json=body, timeout=30)
    try:
        j = r.json()
    except ValueError:
        j = {}
    link = j.get("direct_link")
    if not link:
        raise RuntimeError(f"WeTransfer não devolveu o link (HTTP {r.status_code}: {str(j or r.text)[:200]})")
    return link


def _follow_link(url: str, headers=None) -> str:
    """Endpoints que respondem com redirect, JSON ou texto contendo o link assinado."""
    h = dict(UA)
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=30, allow_redirects=False, stream=True)
    try:
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location") or ""
            return requests.compat.urljoin(url, loc) if loc else url
        ct = (r.headers.get("content-type") or "").lower()
        if r.status_code == 200 and ("json" in ct or "text/plain" in ct):
            body = r.raw.read(65536, decode_content=True).decode("utf-8", "ignore").strip()
            try:
                j = json.loads(body)
            except ValueError:
                j = None
            if isinstance(j, list) and j:
                j = j[0]
            if isinstance(j, dict):
                for k in ("url", "g", "link", "download_url", "downloadUrl", "direct_link"):
                    v = j.get(k) or (j.get("data") or {}).get(k) if isinstance(j.get("data"), dict) else j.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
            elif body.startswith("http") and len(body) < 4000:
                return body
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} ao pedir o link")
        return url
    finally:
        r.close()


# ---------- SwissTransfer (/dl/LINK; a página traz um JSON data-page com os arquivos) ----------
def swisstransfer_expand(url: str) -> dict:
    html = _get(url, timeout=30).text
    m = re.search(r'<script[^>]*data-page="app"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("SwissTransfer: não achei os dados da transferência na página")
    props = json.loads(m.group(1)).get("props") or {}
    link, tr = props.get("link") or {}, props.get("transfer") or {}
    lid = link.get("id") or (re.search(r"/dl/([0-9a-f-]{36})", url) or [None, None])[1]
    if not lid or not tr.get("files"):
        raise RuntimeError("SwissTransfer: transferência vazia, expirada ou com senha")
    entries = []
    for f in tr["files"]:
        path, _, name = (f.get("path") or f.get("name") or f["id"]).rpartition("/")
        entries.append({"url": f"https://www.swisstransfer.com/api/1/links/{lid}/files/{f['id']}",
                        "presign": "follow", "headers": {"Referer": url},
                        "name": name, "path": path, "size": f.get("size") or 0})
    return {"title": tr.get("title") or "SwissTransfer", "entries": _finalize(entries), "warn": "",
            "note": "SwissTransfer"}


# ---------- BuzzHeavier (o botão faz hx-get /ID/download?t=...; a resposta traz HX-Redirect) ----------
def buzzheavier_expand(url: str) -> dict:
    m = re.search(r"buzzheavier\.com/([A-Za-z0-9]+)", url)
    if not m:
        raise RuntimeError("BuzzHeavier: não achei o ID no link")
    fid = m.group(1)
    page = f"https://buzzheavier.com/{fid}"
    html = _get(page, timeout=30, headers={**BROWSER, "Referer": "https://buzzheavier.com/"}).text
    n = re.search(r'<p class="file-name">(.*?)</p>', html, re.S) or re.search(r"<title>(.*?)</title>", html, re.S)
    name = unescape(n.group(1)).strip() if n else None
    sz = re.search(r'class="size">([\d.]+)\s*([KMGT]?B)<', html)
    size = int(float(sz.group(1)) * 1024 ** "BKMGT".index(sz.group(2)[0])) if sz else 0
    ents = _finalize([{"url": page + "/download", "presign": "buzzheavier", "bz": fid,
                       "name": name or fid, "size": size}])
    return {"title": name or "BuzzHeavier", "entries": ents, "warn": "", "note": "BuzzHeavier"}


def buzzheavier_link(e: dict) -> str:
    page = f"https://buzzheavier.com/{e['bz']}"
    html = _get(page, timeout=30, headers={**BROWSER, "Referer": "https://buzzheavier.com/"}).text
    t = re.search(r'hx-get="(/' + re.escape(e["bz"]) + r'/download\?t=[^"&]+)', html)
    dl = "https://buzzheavier.com" + unescape(t.group(1)) if t else page + "/download"
    h = dict(UA)
    h.update({"HX-Request": "true", "HX-Current-URL": page, "Referer": page})
    r = requests.get(dl, headers=h, timeout=30, allow_redirects=False)
    loc = r.headers.get("hx-redirect") or r.headers.get("location")
    if not loc:
        raise RuntimeError(f"BuzzHeavier não devolveu o link (HTTP {r.status_code}; talvez Cloudflare)")
    return requests.compat.urljoin(dl, loc)


# ---------- transfer.it (MEGA; /t/ID; lista via API cs?x=ID) ----------
def _b64u(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/").replace(",", "")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _mega_attr(a: str, k: str):
    """Decifra o atributo de nome do MEGA (AES-128-CBC, IV zero). None se não der."""
    if not a:
        return None
    raw = None
    try:
        raw = _b64u(a)
        if raw.startswith(b"MEGA{"):
            return json.loads(raw[4:].rstrip(b"\0").decode()).get("n")
    except Exception:
        pass
    if not k or ":" in k or raw is None:
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome
        kb = _b64u(k)
        if len(kb) == 32:
            kb = bytes(x ^ y for x, y in zip(kb[:16], kb[16:]))
        if len(kb) != 16:
            return None
        dec = AES.new(kb, AES.MODE_CBC, b"\0" * 16).decrypt(raw[: len(raw) // 16 * 16])
        if dec.startswith(b"MEGA{"):
            return json.loads(dec[4:].rstrip(b"\0").decode("utf-8", "ignore")).get("n")
    except Exception:
        return None
    return None


def transferit_expand(url: str) -> dict:
    m = re.search(r"transfer\.it/t/([A-Za-z0-9_-]+)", url)
    if not m:
        raise RuntimeError("transfer.it: não achei o ID no link (/t/ID)")
    xh = m.group(1)
    r = requests.post(f"https://g.api.mega.co.nz/cs?id={int(time.time())}&x={xh}",
                      data=json.dumps([{"a": "f", "c": 1, "r": 1}]),
                      headers={**UA, "Content-Type": "application/json", "Origin": "https://transfer.it"}, timeout=30)
    j = r.json()
    if isinstance(j, list):
        j = j[0]
    if not isinstance(j, dict) or not j.get("f"):
        raise RuntimeError(f"transfer.it: a API não listou os arquivos (resposta {str(j)[:80]})")
    nodes = {n["h"]: n for n in j["f"] if isinstance(n, dict) and n.get("h")}
    names = {h: _mega_attr(n.get("a"), n.get("k")) for h, n in nodes.items()}

    def path_of(n):
        parts, p, seen = [], n.get("p"), set()
        while p in nodes and p not in seen:
            seen.add(p)
            if nodes[p].get("p") in nodes:  # a raiz (nome da transferência) fica de fora
                parts.append(safe_name(names.get(p) or p))
            p = nodes[p].get("p")
        return "/".join(reversed(parts))

    entries, unnamed = [], 0
    for h, n in nodes.items():
        if n.get("t") != 0:
            continue
        name = names.get(h)
        if not name:
            unnamed += 1
        q = f"x={xh}&n={h}" + (f"&fn={quote(name)}" if name else "")
        entries.append({"url": f"https://g.api.mega.co.nz/cs/g?{q}", "presign": "follow",
                        "headers": {"Referer": "https://transfer.it/"},
                        "name": name or f"arquivo_{h}", "server_name": not name,
                        "path": path_of(n), "size": n.get("s") or 0})
    if not entries:
        raise RuntimeError("transfer.it: nenhum arquivo na transferência")
    root = next((h for h, n in nodes.items() if n.get("t") == 1 and n.get("p") not in nodes), None)
    warn = f"{unnamed} arquivo(s) sem nome legível; o nome vem do servidor no download" if unnamed else ""
    return {"title": names.get(root) or "transfer.it", "entries": _finalize(entries), "warn": warn,
            "note": "transfer.it"}


def pixeldrain_file(url: str) -> dict:
    m = re.search(r"/(?:u|api/file)/([A-Za-z0-9]+)", url)
    if not m:
        raise RuntimeError("PixelDrain: id não encontrado")
    info = {}
    try:
        info = _get(f"https://pixeldrain.com/api/file/{m.group(1)}/info").json() or {}
    except Exception:
        pass
    ents = _finalize([{"url": f"https://pixeldrain.com/api/file/{m.group(1)}?download",
                       "name": info.get("name"), "size": info.get("size") or 0}])
    return {"title": info.get("name") or "PixelDrain", "entries": ents, "warn": "", "note": "PixelDrain"}


# ---------- send.now / send.cm (XFileSharing: form op=download2 -> link em usercdn/usercontent) ----------
def sendnow_resolve(url: str) -> dict:
    s = requests.Session()
    s.headers.update(BROWSER)
    html = s.get(url, timeout=30).text
    fid = (re.search(r'name="id"\s+value="([^"]+)"', html) or [None, None])[1]
    name = (re.search(r'feather feather-file[^>]*></svg>\s*([^<]+?)\s*</h6>', html)
            or re.search(r'<h6[^>]*>[^<]*?([^<>\n]+\.[A-Za-z0-9]{1,8})\s*</h6>', html)
            or re.search(r"<title>([^<]+)</title>", html))
    name = unescape(name.group(1)).strip() if name else None
    if not fid:
        raise RuntimeError("não achei o formulário de download")
    fields = {"op": "download2", "id": fid,
              "rand": (re.search(r'name="rand"\s+value="([^"]*)"', html) or [None, ""])[1],
              "referer": url, "method_free": "", "method_premium": "", "down_direct": "1"}
    r = s.post(url, data=fields, headers={**BROWSER, "Referer": url,
               "Content-Type": "application/x-www-form-urlencoded"},
               allow_redirects=False, timeout=60)
    link = r.headers.get("location")
    if not link:
        body = r.text
        mm = (re.search(r'href="(https?://[^"]*(?:usercdn|usercontent|userstorage|/d/)[^"]+)"', body)
              or re.search(r'(https?://[a-z0-9.\-]*usercdn\.com/[^\s"\'<>]+)', body)
              or re.search(r'id="direct_link"[^>]*value="([^"]+)"', body))
        link = unescape(mm.group(1)) if mm else None
        wait = re.search(r"(?:wait|aguarde)[^0-9]{0,40}(\d+)\s*(?:second|segundo)", body, re.I)
        if not link and wait:
            raise RuntimeError(f"tem contador de {wait.group(1)}s na página (conta grátis)")
    if not link:
        raise RuntimeError("o servidor não devolveu o link direto (pode exigir espera/conta)")
    ents = _finalize([{"url": link, "name": name, "headers": {"Referer": url}, "size": 0}])
    return {"title": name or "send.now", "entries": ents, "warn": "", "note": "send.now"}


# ---------- MEGA (mega.nz / mega.io: file e folder, com descriptografia) ----------
def _mega_cs(params, data, node=None):
    q = f"https://g.api.mega.co.nz/cs?id={int(time.time())}{params}"
    r = requests.post(q, data=json.dumps(data),
                      headers={**UA, "Content-Type": "application/json", "Origin": "https://mega.nz"}, timeout=60)
    j = r.json()
    if isinstance(j, int):
        raise RuntimeError(f"a API do MEGA recusou (código {j})")
    return j[0] if isinstance(j, list) else j


def _mega_filekey(k32: bytes):
    """key(32B) -> (aes16, nonce8). Para pasta, 16B já é a chave AES (nonce zero)."""
    if len(k32) == 16:
        return k32, b"\0" * 8
    import struct
    u = struct.unpack(">8I", k32)
    aes = struct.pack(">4I", u[0] ^ u[4], u[1] ^ u[5], u[2] ^ u[6], u[3] ^ u[7])
    return aes, k32[16:24]


def _mega_name(attr_b64: str, aes16: bytes):
    try:
        from Crypto.Cipher import AES
        raw = _b64u(attr_b64)
        dec = AES.new(aes16, AES.MODE_CBC, b"\0" * 16).decrypt(raw[: len(raw) // 16 * 16])
        if dec.startswith(b"MEGA{"):
            return json.loads(dec[4:].rstrip(b"\0").decode("utf-8", "ignore")).get("n")
    except Exception:
        pass
    return None


def mega_expand(url: str) -> dict:
    frag = url.split("#", 1)
    body = frag[1] if len(frag) > 1 else ""
    mf = re.search(r"/(?:file|fm)/([A-Za-z0-9]+)", url) or re.search(r"!([A-Za-z0-9]+)!", url)
    mfo = re.search(r"/folder/([A-Za-z0-9]+)", url)
    if mf and not mfo:  # arquivo único
        handle = mf.group(1)
        keyb = body.split("!")[-1] if "!" in body else body
        g = _mega_cs("", [{"a": "g", "g": 1, "p": handle}])
        aes, nonce = _mega_filekey(_b64u(keyb)) if keyb else (None, None)
        name = _mega_name(g.get("at"), aes) if aes else None
        ents = _finalize([{"url": g["g"], "presign": "mega", "mega_key": keyb,
                           "name": name, "server_name": not name, "size": g.get("s") or 0}])
        return {"title": name or "MEGA", "entries": ents, "warn": "" if keyb else "link sem chave (#): não dá para decifrar",
                "note": "MEGA"}
    if not mfo:
        raise RuntimeError("MEGA: use um link /file/… ou /folder/…")
    fh = mfo.group(1)
    fkey = _b64u((body.split("/")[0] if body else "").split("!")[-1])
    if len(fkey) != 16:
        raise RuntimeError("MEGA: link de pasta sem chave válida")
    from Crypto.Cipher import AES
    res = _mega_cs(f"&n={fh}", [{"a": "f", "c": 1, "r": 1, "ca": 1}])
    nodes = {n["h"]: n for n in res.get("f") or [] if isinstance(n, dict) and n.get("h")}
    names, keys = {}, {}
    for h, n in nodes.items():
        kf = (n.get("k") or "")
        enc = kf.split(":")[-1] if ":" in kf else kf
        try:
            raw = AES.new(fkey, AES.MODE_ECB).decrypt(_b64u(enc)) if enc else b""
        except Exception:
            raw = b""
        aes = _mega_filekey(raw)[0] if raw else None
        keys[h] = enc
        names[h] = _mega_name(n.get("a"), aes) if aes else None

    def path_of(n):
        parts, p, seen = [], n.get("p"), set()
        while p in nodes and p not in seen:
            seen.add(p)
            if nodes[p].get("p") in nodes:
                parts.append(safe_name(names.get(p) or p))
            p = nodes[p].get("p")
        return "/".join(reversed(parts))

    entries, unnamed = [], 0
    for h, n in nodes.items():
        if n.get("t") != 0:
            continue
        g = _mega_cs(f"&n={fh}", [{"a": "g", "g": 1, "n": h}])
        if not isinstance(g, dict) or not g.get("g"):
            continue
        nm = names.get(h)
        if not nm:
            unnamed += 1
        entries.append({"url": g["g"], "presign": "mega", "mega_key": keys.get(h),
                        "name": nm or f"arquivo_{h}", "server_name": not nm,
                        "path": path_of(n), "size": g.get("s") or n.get("s") or 0})
    if not entries:
        raise RuntimeError("MEGA: pasta vazia ou sem arquivos acessíveis")
    root = next((h for h, n in nodes.items() if n.get("t") == 1 and n.get("p") not in nodes), None)
    warn = f"{unnamed} arquivo(s) sem nome legível" if unnamed else ""
    return {"title": names.get(root) or "MEGA", "entries": _finalize(entries), "warn": warn, "note": "MEGA"}


def mega_download(e: dict, dest: Path) -> Path:
    """Baixa um arquivo do MEGA e decifra em streaming (AES-CTR)."""
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
    aes, nonce = _mega_filekey(_b64u(e["mega_key"]))
    ctr = Counter.new(64, prefix=nonce, initial_value=0)
    cipher = AES.new(aes, AES.MODE_CTR, counter=ctr)
    dest.mkdir(parents=True, exist_ok=True)
    final = unique_path(dest, e.get("name") or "arquivo")
    with requests.get(e["url"], headers=UA, stream=True, timeout=120) as r:
        r.raise_for_status()
        with final.open("wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    f.write(cipher.decrypt(chunk))
    return final


# ---------- Google Drive (arquivo e pasta) ----------
def gdrive_resolve(fid: str):
    """Devolve (link_direto, nome). Trata a página de aviso de vírus dos arquivos grandes."""
    base = f"https://drive.usercontent.google.com/download?id={fid}&export=download"
    s = requests.Session()
    s.headers.update(UA)
    name = None
    try:
        r = s.get(base, timeout=30)
        ct = (r.headers.get("content-type") or "").lower()
        if not ct.startswith("text/html"):  # arquivo pequeno: o download já veio
            cd = r.headers.get("content-disposition", "")
            mm = re.search(r"filename\*?=(?:UTF-8\'\')?\"?([^\";]+)", cd)
            return base, (unquote(mm.group(1)) if mm else None)
        form = re.search(r'<form[^>]+id="download-form"[^>]+action="([^"]+)"(.*?)</form>', r.text, re.S)
        nm = re.search(r'<span class="uc-name-size"><a[^>]*>([^<]+)</a>', r.text)
        name = unescape(nm.group(1)) if nm else None
        if form:
            action = unescape(form.group(1))
            fields = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', form.group(2)))
            q = "&".join(f"{k}={quote(v)}" for k, v in fields.items())
            return (f"{action}?{q}" if q else action), name
    except Exception as e:
        print("gdrive:", e, file=sys.stderr)
    return f"{base}&confirm=t", name


GD_FOLDER = "application/vnd.google-apps.folder"


def gdrive_folder_expand(url: str) -> dict:
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", url)
    if not m:
        raise RuntimeError("Google Drive: id da pasta não encontrado")
    html = _get(f"https://drive.google.com/drive/folders/{m.group(1)}", timeout=30).text
    iv = re.search(r"_DRIVE_ivd'\]\s*=\s*'((?:\\x[0-9a-fA-F]{2}|\\.|[^'\\])*)'", html)
    if not iv:
        raise RuntimeError("Google Drive: não achei a lista da pasta (precisa estar pública / 'qualquer um com o link')")
    data = json.loads(iv.group(1).encode().decode("unicode_escape"))
    rows = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
    entries, folders = [], 0
    for e in rows:
        if not isinstance(e, list) or len(e) < 4 or not isinstance(e[0], str):
            continue
        fid, name, mime = e[0], e[2], e[3]
        if mime == GD_FOLDER:
            folders += 1
            continue
        size = e[13] if len(e) > 13 and isinstance(e[13], int) else 0
        entries.append({"url": f"https://drive.usercontent.google.com/download?id={fid}&export=download",
                        "presign": "gdrive", "gid": fid, "name": name, "size": size})
    if not entries:
        raise RuntimeError("Google Drive: a pasta não tem arquivos (ou só tem subpastas)")
    title = re.search(r'<title>([^<]+?)(?: - Google Drive)?</title>', html)
    warn = f"{folders} subpasta(s) não foram abertas (só os arquivos da raiz)" if folders else ""
    return {"title": unescape(title.group(1)).strip() if title else "Google Drive",
            "entries": _finalize(entries), "warn": warn, "note": "Google Drive (pasta)"}


def entry_url(e: dict) -> str:
    """Link final de uma entrada (alguns hosts só assinam o link na hora de baixar)."""
    if e.get("presign") == "gdrive":
        return gdrive_resolve(e["gid"])[0]
    if e.get("presign") == "wetransfer":
        return wetransfer_link(e)
    if e.get("presign") == "buzzheavier":
        return buzzheavier_link(e)
    if e.get("presign") == "follow":
        return _follow_link(e["url"], e.get("headers"))
    if e.get("presign"):
        return sendgb_presign(e)
    return e["url"]


def expand(url: str) -> dict:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().replace("www.", "")
    warn = ""
    try:
        if "gofile.io" in host and "/d/" in parsed.path:
            return gofile_expand(url)
        if host.endswith(("pixeldrain.com", "pixeldrain.net", "pixeldrain.dev", "pixeldra.in")) and "/l/" in parsed.path:
            return pixeldrain_list(url)
        if host.endswith(("pixeldrain.com", "pixeldrain.net", "pixeldrain.dev", "pixeldra.in")) and re.search(r"/(?:u|api/file)/", parsed.path):
            return pixeldrain_file(url)
        if host.endswith(("onedrive.live.com", "1drv.ms")):
            return onedrive_expand(url)
        if host == "box.com" or host.endswith((".box.com", ".app.box.com")) or host == "app.box.com":
            return box_expand(url)
        if host.endswith("sendgb.com"):
            return sendgb_expand(url)
        if host in ("imgur.gg", "m.imgur.gg"):
            return imgurgg_expand(url)
        if host in ("pillows.su", "pillowcase.su", "api.pillows.su"):
            return pillows_expand(url)
        if host.endswith(("wetransfer.com", "we.tl")):
            return wetransfer_expand(url)
        if host.endswith("swisstransfer.com") and "/dl/" in parsed.path:
            return swisstransfer_expand(url)
        if host.endswith("buzzheavier.com") and re.match(r"^/[A-Za-z0-9]+/?$", parsed.path):
            return buzzheavier_expand(url)
        if host == "transfer.it" and parsed.path.startswith("/t/"):
            return transferit_expand(url)
        if host.endswith(("mega.nz", "mega.io", "mega.co.nz")):
            return mega_expand(url)
        if host.endswith("google.com") and "/folders/" in parsed.path:
            return gdrive_folder_expand(url)
        if host == "send.now" or host.endswith((".send.now", ".send.cm")):
            return sendnow_resolve(url)
    except Exception as e:  # cai no resolve() simples
        print("expand falhou:", e, file=sys.stderr)
        warn = f"não consegui listar o conteúdo ({e})"
    r = resolve(url)
    ents = _finalize([{"url": r["url"], "name": r.get("name"), "size": 0,
                       "server_name": r.get("server_name")}])
    return {"title": host, "entries": ents, "warn": warn, "note": r.get("note") or ""}


def post_picker(cb, ex) -> bool:
    """Entrega a lista de arquivos ao Worker (que mostra os botões). True se entregou."""
    url, token = (cb or {}).get("url"), (cb or {}).get("token")
    if not url or not token:
        return False
    ents = ex["entries"]
    body = {
        "token": token, "ok": True, "title": ex.get("title") or "", "total": len(ents),
        "warn": ex.get("warn") or "",
        "items": [{"key": e["key"], "name": e["name"], "size": e.get("size") or 0, "path": e.get("path") or ""}
                  for e in ents[:PICK_MAX]],
    }
    data = json.dumps(body, ensure_ascii=False).encode()
    for wait in (0, 3, 8, 15, 30, 45):
        if wait:
            time.sleep(wait)
        try:
            r = requests.post(url, data=data, timeout=30,
                              headers={"Content-Type": "application/json", "User-Agent": "uploader-actions/1.0"})
            if r.status_code == 200 or r.status_code == 410:  # 410: já entregue numa tentativa cuja resposta se perdeu
                return True
            if r.status_code in (400, 413):
                return False
            # 404: o token ainda não propagou no KV; 5xx: Worker instável -> tenta de novo
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Uploaders
# ---------------------------------------------------------------------------
class _MultipartFile:
    """Corpo multipart/form-data em streaming: o arquivo não é carregado na memória."""

    def __init__(self, path: Path, field="file", fields=None):
        b = self.boundary = uuid.uuid4().hex
        fname = path.name.replace('"', "_").replace("\r", " ").replace("\n", " ")
        pre = b""
        for k, v in (fields or {}).items():
            pre += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        pre += (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        post = f"\r\n--{b}--\r\n".encode()
        self._parts = [io.BytesIO(pre), path.open("rb"), io.BytesIO(post)]
        self._len = len(pre) + path.stat().st_size + len(post)

    @property
    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return self._len

    def read(self, n=-1):
        out = bytearray()
        while self._parts and (n < 0 or len(out) < n):
            chunk = self._parts[0].read(-1 if n < 0 else n - len(out))
            if not chunk:
                self._parts.pop(0).close()
                continue
            out += chunk
        return bytes(out)

    def close(self):
        for p in self._parts:
            p.close()
        self._parts = []


def _post_multipart(url, path: Path, fields=None):
    body = _MultipartFile(path, "file", fields)
    try:
        return requests.post(url, data=body, headers={"Content-Type": body.content_type}, timeout=None)
    finally:
        body.close()


def upload_gofile(path: Path, folder_id=None):
    last = None
    for attempt in range(2):
        if attempt == 0:
            ep = "https://upload.gofile.io/uploadfile"
        else:
            try:
                sv = requests.get("https://api.gofile.io/servers", timeout=15).json()
                ep = f"https://{sv['data']['servers'][0]['name']}.gofile.io/uploadfile"
            except Exception:
                break
        try:
            r = _post_multipart(ep, path, {"folderId": folder_id} if folder_id else None)
            j = r.json()
            if j.get("status") == "ok":
                return j["data"].get("downloadPage") or j["data"].get("parentFolder")
            last = str(j)[:300]
        except Exception as e:
            last = str(e)[:300]
    raise RuntimeError(f"GoFile: {last}")


def upload_pixeldrain(path: Path):
    with path.open("rb") as f:
        r = requests.put(
            f"https://pixeldrain.com/api/file/{quote(path.name)}",
            data=f, timeout=None
        )
    j = r.json()
    if not j.get("id"):
        raise RuntimeError(str(j))
    return f"https://pixeldrain.com/u/{j['id']}"


def upload_viking(path: Path):
    # tenta servidor padrão
    try:
        sv = requests.get("https://vikingfile.com/api/get-server", timeout=15).json()
        server = sv.get("server", "https://upload.vikingfile.com")
    except Exception:
        server = "https://upload.vikingfile.com"
    r = _post_multipart(server, path, {"user": ""})
    j = r.json()
    if not j.get("url"):
        raise RuntimeError(str(j))
    return j["url"]


def upload_buzz(path: Path):
    with path.open("rb") as f:
        r = requests.put(
            f"https://w.buzzheavier.com/{quote(path.name)}",
            data=f, timeout=None
        )
    if r.status_code >= 400:
        raise RuntimeError(r.text[:300])
    file_code = r.text.strip().strip('"')
    return f"https://buzzheavier.com/{file_code}"


def upload_filebin(path: Path):
    import secrets
    bin_id = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12))
    with path.open("rb") as f:
        r = requests.post(
            "https://filebin.net/",
            data=f,
            headers={"bin": bin_id, "filename": path.name, "Content-Type": "application/octet-stream"},
            timeout=None,
        )
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])
    return f"https://filebin.net/{bin_id}/{quote(path.name)}"


UPLOADERS = {
    "gofile": upload_gofile,
    "viking": upload_viking,
    "pixeldrain": upload_pixeldrain,
    "buzz": upload_buzz,
    "filebin": upload_filebin,
}


def do_upload(path: Path, host: str, quiet=False):
    fn = UPLOADERS.get(host)
    if not fn:
        raise RuntimeError(f"Host desconhecido: {host}. Use: {', '.join(UPLOADERS)}, telegram")
    if not quiet:
        say(f"⬆️ Enviando para {host}…")
    link = fn(path)
    if not quiet:
        say(f"✅ {host}: {link}")
    return link


def send_result(path: Path, dest: str, caption: str, premium_client, volume_limit) -> str:
    """Manda um arquivo pro destino (host ou Telegram). Devolve o link ou um texto curto."""
    if dest == "telegram":
        deliver(path, caption, premium_client, volume_limit)
        return "(enviado ao Telegram)"
    return do_upload(path, dest, quiet=True)


# ---------------------------------------------------------------------------
# Partes (.zip.001, .z01…), juntar e descompactar
# ---------------------------------------------------------------------------
SPLIT_RE = re.compile(r"^(?P<base>.+\.[A-Za-z0-9]{2,5})\.(?P<num>\d{3})$")   # a.zip.001, a.7z.001, a.mp4.001
ZNN_RE = re.compile(r"^(?P<base>.+)\.[zZ](?P<num>\d{2})$")                   # a.z01, a.z02 (+ a.zip no fim)
LATER_VOL = re.compile(r"(\.z\d{2}|\.r\d{2}|\.part0*(?:[2-9]|[1-9]\d+)\.rar)$", re.I)


def is_later_volume(name: str) -> bool:
    return bool(LATER_VOL.search(name))


def group_parts(paths):
    """Separa os arquivos em conjuntos de partes e arquivos soltos."""
    num, zs, used = {}, {}, set()
    for p in paths:
        m = SPLIT_RE.match(p.name)
        if m:
            g = num.setdefault(m["base"].lower(), {"kind": "num", "name": m["base"], "parts": {}})
            g["parts"].setdefault(int(m["num"]), p)
            used.add(p)
            continue
        m = ZNN_RE.match(p.name)
        if m:
            g = zs.setdefault(m["base"].lower(), {"kind": "z", "name": m["base"] + ".zip", "parts": {}, "zip": None})
            g["parts"].setdefault(int(m["num"]), p)
            used.add(p)
    for p in paths:
        if p.suffix.lower() == ".zip" and p.stem.lower() in zs and zs[p.stem.lower()]["zip"] is None:
            zs[p.stem.lower()]["zip"] = p
            used.add(p)
    groups = []
    for g in list(num.values()) + list(zs.values()):
        nums = sorted(g["parts"])
        g["nums"] = nums
        g["parts"] = [g["parts"][n] for n in nums]
        groups.append(g)
    singles = [p for p in paths if p not in used]
    return groups, singles


def join_group(g, out_dir: Path, for_extract=False) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    missing = [n for n in range(1, max(g["nums"]) + 1) if n not in g["nums"]]
    if missing:
        raise RuntimeError("faltam partes: " + ", ".join(f"{n:03d}" for n in missing[:10]))
    if g["kind"] == "num":
        out = unique_path(out_dir, g["name"])
        with out.open("wb") as w:
            for p in g["parts"]:
                with p.open("rb") as r:
                    shutil.copyfileobj(r, w, 1024 * 1024)
                p.unlink(missing_ok=True)  # libera disco à medida que junta
        return out
    # conjunto .z01, .z02 … + .zip final
    zipf = g.get("zip")
    if not zipf:
        raise RuntimeError("falta o arquivo .zip (a última parte do conjunto .z01, .z02…)")
    if shutil.which("zip"):
        out = unique_path(out_dir, zipf.name)
        r = subprocess.run(["zip", "-s", "0", str(zipf), "--out", str(out)],
                           capture_output=True, text=True, cwd=str(zipf.parent))
        if r.returncode == 0 and out.exists():
            for p in g["parts"] + [zipf]:
                p.unlink(missing_ok=True)
            return out
    if for_extract:
        return zipf  # o 7z abre o .zip e acha os .zNN na mesma pasta
    raise RuntimeError("não consegui juntar o conjunto .zip/.z01 (precisa do comando zip)")


def find_7z():
    return shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


def extract_archive(arc: Path, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(arc):
        with zipfile.ZipFile(arc) as zf:
            zf.extractall(out)
        return
    b = find_7z()
    if not b:
        raise RuntimeError("não é um ZIP e o 7z não está instalado neste runner")
    r = subprocess.run([b, "x", str(arc), f"-o{out}", "-y"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("7z falhou: " + (r.stderr or r.stdout or "")[-300:].strip())


def run_unzip(files, dest, tmp: Path, premium_client, volume_limit):
    """Descompacta e envia o conteúdo. Devolve (linhas_ok, erros)."""
    groups, singles = group_parts(files)
    archives, errors = [], []
    for g in groups:
        try:
            if len(g["parts"]) > 1 or g["kind"] == "z":
                say(f"🧩 Juntando {len(g['parts'])} parte(s) de {g['name']}…")
            archives.append(join_group(g, tmp / "joined", for_extract=True))
        except Exception as e:
            errors.append(f"{g['name']}: {e}")
    archives += [p for p in singles if not is_later_volume(p.name)]
    if not archives:
        raise RuntimeError("nenhum arquivo compactado para extrair" + (f" ({errors[0]})" if errors else ""))
    lines = []
    for idx, arc in enumerate(archives, 1):
        out = tmp / "extracted" / f"{idx:02d}"
        try:
            say(f"📦 Extraindo {arc.name}…")
            extract_archive(arc, out)
            extracted = sorted(p for p in out.rglob("*") if p.is_file())
            if not extracted:
                raise RuntimeError("nenhum arquivo dentro do arquivo compactado")
            say(f"{len(extracted)} arquivo(s) em {arc.name}. Enviando para {dest}…")
            for f in extracted:
                rel = f.relative_to(out).as_posix()
                try:
                    lines.append(f"{rel}: {send_result(f, dest, rel, premium_client, volume_limit)}")
                except Exception as e:
                    errors.append(f"{rel}: {e}")
                f.unlink(missing_ok=True)
        except Exception as e:
            errors.append(f"{arc.name}: {e}")
        finally:
            shutil.rmtree(out, ignore_errors=True)
            arc.unlink(missing_ok=True)
    return lines, errors


def run_join(files, dest, tmp: Path, premium_client, volume_limit):
    """Junta partes e envia o arquivo completo. Devolve (linhas_ok, erros)."""
    groups, singles = group_parts(files)
    if not groups:
        raise RuntimeError("não achei partes para juntar. Os nomes precisam seguir o padrão "
                           ".zip.001, .zip.002… (ou .z01, .z02… + .zip).")
    lines = []
    errors = [f"ignorado: {p.name} (não parece parte de um arquivo dividido)" for p in singles]
    for g in groups:
        try:
            say(f"🧩 Juntando {len(g['parts'])} parte(s) de {g['name']}…")
            out = join_group(g, tmp / "joined")
            if out.suffix.lower() == ".zip" and not zipfile.is_zipfile(out):
                say(f"⚠️ {out.name} não parece um ZIP válido (alguma parte pode estar corrompida). Enviando mesmo assim.")
            lines.append(f"{out.name}: {send_result(out, dest, out.name, premium_client, volume_limit)}")
            out.unlink(missing_ok=True)
        except Exception as e:
            errors.append(f"{g['name']}: {e}")
    return lines, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
MODES = ("telegram", "link", "upload", "unzip", "join")


def normalize_items(payload):
    items = payload.get("items")
    if not isinstance(items, list) or not items:  # formato antigo: url / file_id / file_name
        items = []
        if payload.get("file_id"):
            items.append({"file_id": payload["file_id"], "name": payload.get("file_name")})
        elif payload.get("url"):
            items.append({"url": payload["url"]})
    return [i for i in items if isinstance(i, dict)]


def job_label(job):
    if job["kind"] == "tg":
        return job.get("name") or "arquivo do Telegram"
    return job["entry"].get("name") or job["entry"]["url"][:80]


def fetch_job(job, dl_dir: Path) -> Path:
    if job["kind"] == "tg":
        return tg_download(job["file_id"], dl_dir, job.get("name"))
    e = job["entry"]
    size = e.get("size") or 0
    if size:
        free = shutil.disk_usage(dl_dir if dl_dir.exists() else ".").free
        if size > free - 512 * 2**20:
            raise RuntimeError(f"{e.get('name')}: {size / 2**30:.1f} GiB não cabe no disco do runner "
                               f"({free / 2**30:.1f} GiB livres)")
    hint = None if e.get("server_name") else e.get("name")
    if e.get("presign") == "mega":
        return mega_download(e, dl_dir)
    if e.get("presign") == "gdrive":
        return download_to(entry_url(e), dl_dir, hint)
    if e.get("presign") in ("follow", "buzzheavier"):
        return download_to(entry_url(e), dl_dir, hint, e.get("headers") if e.get("presign") == "follow" else None)
    if e.get("presign"):
        return download_to(entry_url(e), dl_dir, hint)
    try:
        return download_to(e["url"], dl_dir, hint, e.get("headers"))
    except Exception:
        if not e.get("fallback"):
            raise
        return download_to(e["fallback"], dl_dir, hint, e.get("headers"))


def pack_jobs(jobs, dl: Path, tmp: Path, title: str, errors: list):
    """Baixa um por um e já grava no ZIP (apaga cada arquivo depois): usa pouco disco."""
    zpath = unique_path(tmp / "pack", safe_name(title) + ".zip")
    used, n_ok, total = set(), 0, len(jobs)
    say(f"📦 Modo ZIP: {total} arquivo(s) num único {zpath.name}")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for n, job in enumerate(jobs, 1):
            label = job_label(job)
            path = None
            try:
                say(f"⬇️ {n}/{total}: {label}")
                path = fetch_job(job, dl)
                sub = (job.get("entry") or {}).get("path") or ""
                arc = (sub + "/" if sub else "") + path.name
                base, i = arc, 1
                while arc in used:
                    stem, dot, ext = base.rpartition(".")
                    arc = f"{stem} ({i}).{ext}" if dot else f"{base} ({i})"
                    i += 1
                used.add(arc)
                zf.write(path, arcname=arc)
                n_ok += 1
            except Exception as e:
                errors.append(f"{label}: {e}")
            finally:
                if path:
                    path.unlink(missing_ok=True)
    return zpath, n_ok


def main():
    mode = (PAYLOAD.get("mode") or "telegram").lower()
    host = (PAYLOAD.get("host") or "").lower() or None
    opts = PAYLOAD.get("opts") if isinstance(PAYLOAD.get("opts"), dict) else {}
    cb = PAYLOAD.get("cb") if isinstance(PAYLOAD.get("cb"), dict) else None
    items = normalize_items(PAYLOAD)

    if mode not in MODES:
        raise RuntimeError(f"Modo desconhecido: {mode}")
    if mode in ("upload", "unzip", "join") and not host:
        host = "gofile"
    if mode == "upload" and host == "telegram":
        mode = "telegram"
    if mode in ("telegram", "link"):
        host = None
    if host and host != "telegram" and host not in UPLOADERS:
        raise RuntimeError(f"Host desconhecido: {host}. Use: {', '.join(UPLOADERS)}, telegram")
    if not items:
        raise RuntimeError("Nenhum url nem file_id recebido")
    if mode == "link" and any(i.get("file_id") for i in items):
        raise RuntimeError("O modo link precisa de uma URL (não de um arquivo do Telegram)")

    dest = "telegram" if mode == "telegram" else host
    if dest == "telegram":
        premium_client, volume_limit, mode_desc = select_uploader()
    else:
        premium_client, volume_limit, mode_desc = None, MAX_MT_BYTES, ""
    say(f"🔧 /uploader | modo={mode}" + (f" | destino={dest}" if dest else "")
        + (f" | envio={mode_desc}" if mode_desc else ""))

    # ---- 1) resolve: cada item vira um ou mais downloads ----
jobs, pick_ex, pack_title = [], None, None
force_global_zip = False  # caso especial do /docs

for it in items:
    if it.get("file_id"):
        jobs.append({"kind": "tg", "file_id": it["file_id"], "name": it.get("name") or it.get("file_name")})
        continue
    url = (it.get("url") or "").strip()
    if not url:
        continue
    ex = expand(url)
    ents = ex["entries"]
    sel = it.get("select")
    if sel is not None:
        wanted = {str(k) for k in sel}
        ents = [e for e in ents if e["key"] in wanted]
        if not ents:
            raise RuntimeError("Nenhum dos arquivos escolhidos foi achado de novo no link")
    elif opts.get("pick") and len(items) == 1 and len(ents) > 1:
        pick_ex = ex
    if len(ents) == 1:
        say(f"🔍 Resolvido: {ex.get('note')}\n{ents[0]['name']}")
    else:
        say(f"🔍 {ex.get('title') or 'Link'}: {len(ents)} arquivo(s) ({ex.get('note')})")
    if ex.get("warn"):
        say(f"⚠️ {ex['warn']}")
    jobs += [{"kind": "url", "entry": e} for e in ents]
    custom_path = (it.get("path") or "").strip().strip("/")
    if custom_path:
        for j in jobs[-len(ents):]:
            if j.get("kind") == "url":
                j["entry"]["path"] = custom_path
    if it.get("zip"):
        force_global_zip = True
        pack_title = pack_title or (ex.get("title") or "docs")

if not jobs:
    raise RuntimeError("Nada para baixar")

    # ---- 2) vários arquivos e o usuário ainda não escolheu: o Worker mostra os botões ----
    if pick_ex is not None:
        if post_picker(cb, pick_ex):
            print("lista entregue ao Worker; aguardando a escolha")
            return
        say("⚠️ Não consegui abrir o seletor de arquivos; vou usar todos os arquivos do link.")

    # ---- 3) só links ----
    if mode == "link":
        lines = []
        for j in jobs:
            e = j["entry"]
            try:
                link = entry_url(e)
            except Exception as ex:
                link = f"(falhou: {ex})"
            lines.append(f"{e['name']}: {link}")
            if e.get("headers") and not e.get("presign"):
                lines.append("   ↳ precisa dos headers: " + "; ".join(f"{k}: {v}" for k, v in e["headers"].items()))
        say_long(["🔗 Links diretos:"] + lines)
        return

    # ---- 4) baixa e processa ----
done, errors, results = 0, [], []
with tempfile.TemporaryDirectory(prefix="uploader-") as td:
    tmp = Path(td)
    dl = tmp / "dl"

    if force_global_zip and mode in ("telegram", "upload"):
        zpath, n_ok = pack_jobs(jobs, dl, tmp, pack_title or "docs", errors)
        if n_ok:
            try:
                if mode == "telegram":
                    deliver(zpath, f"📦 {zpath.name} ({n_ok} arquivo(s))", premium_client, volume_limit)
                else:
                    results.append(f"{zpath.name}: {do_upload(zpath, host)}")
                done += 1
            except Exception as e:
                errors.append(f"{zpath.name}: {e}")
    elif mode in ("telegram", "upload") and pack_title:
        zpath, n_ok = pack_jobs(jobs, dl, tmp, pack_title, errors)
        if n_ok:
            try:
                if mode == "telegram":
                    deliver(zpath, f"📦 {zpath.name} ({n_ok} arquivo(s))", premium_client, volume_limit)
                else:
                    results.append(f"{zpath.name}: {do_upload(zpath, host)}")
                done += 1
            except Exception as e:
                errors.append(f"{zpath.name}: {e}")
    elif mode in ("telegram", "upload"):
        total = len(jobs)
        for n, job in enumerate(jobs, 1):
            label = job_label(job)
            path = None
            try:
                if total > 1:
                    say(f"▶️ {n}/{total}: {label}")
                path = fetch_job(job, dl)
                if mode == "telegram":
                    deliver(path, path.name, premium_client, volume_limit)
                else:
                    results.append(f"{path.name}: {do_upload(path, host, quiet=total > 1)}")
                done += 1
            except Exception as e:
                print(f"erro em {label}: {e}", file=sys.stderr)
                errors.append(f"{label}: {e}")
            finally:
                if path:
                    path.unlink(missing_ok=True)
    else:  # unzip / join
        files = []
        for n, job in enumerate(jobs, 1):
            label = job_label(job)
            try:
                if len(jobs) > 1:
                    say(f"⬇️ {n}/{len(jobs)}: {label}")
                files.append(fetch_job(job, dl))
            except Exception as e:
                errors.append(f"{label}: {e}")
        if files:
            runner = run_unzip if mode == "unzip" else run_join
            lines, errs = runner(files, dest, tmp, premium_client, volume_limit)
            results += lines
            errors += errs
            done += len(lines)

    if results and (len(results) > 1 or mode in ("unzip", "join")):
        say_long(["✅ Links:"] + results)
    if errors:
        say_long(["⚠️ Falhas:"] + errors)
    if not done:
        raise RuntimeError("nada foi enviado" + (f": {errors[0]}" if errors else ""))
    say("✅ /uploader concluído." + (f" ({len(errors)} com erro)" if errors else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        say(f"❌ /uploader: {e}")
        raise
    finally:
        stop_clients()
