#!/usr/bin/env python3
"""
/uploader — resolve, baixa, re-hospeda ou envia pro Telegram (com split ZIP).

Modos (client_payload):
  mode: "telegram" (padrão) | "link" | "upload" | "unzip"
  host: gofile | viking | pixeldrain | buzz | filebin   (para upload/unzip)
  url:  link a resolver/baixar
  file_id / file_name: opcional, se o usuário mandou/respondeu um arquivo

Secrets necessários (mesmo dos outros workflows):
  BOT_TOKEN, TG_API_ID, TG_API_HASH
  TG_PREMIUM_SESSION (opcional, para ~4 GB)
"""
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html import escape as esc
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
# Resolvers (principais hosts do FileHost Toolkit)
# ---------------------------------------------------------------------------
def resolve(url: str) -> dict:
    """Devolve {"url": link_direto, "name": nome_sugerido, "note": "..."}"""
    url = url.strip()
    host = urlparse(url).hostname or ""
    host = host.lower().replace("www.", "")

    # Link direto óbvio
    if any(host.endswith(h) for h in ("githubusercontent.com", "cdn.", "media.", "download.")):
        return {"url": url, "name": Path(urlparse(url).path).name or "arquivo", "note": "link direto"}

    # PixelDrain
    if "pixeldrain.com" in host or "pixeldra.in" in host:
        m = re.search(r"/u/([A-Za-z0-9]+)", url)
        if m:
            return {"url": f"https://pixeldrain.com/api/file/{m.group(1)}?download",
                    "name": None, "note": "PixelDrain API"}

    # GoFile
    if "gofile.io" in host:
        m = re.search(r"/d/([A-Za-z0-9]+)", url)
        if m:
            # tenta pegar o conteúdo
            try:
                r = requests.get(f"https://api.gofile.io/contents/{m.group(1)}", headers=UA, timeout=20)
                j = r.json()
                if j.get("status") == "ok":
                    contents = j["data"].get("contents") or {}
                    files = [v for v in contents.values() if v.get("type") == "file"]
                    if files:
                        f = files[0]
                        return {"url": f.get("link") or f.get("directLink"),
                                "name": f.get("name"), "note": "GoFile"}
            except Exception:
                pass
        return {"url": url, "name": None, "note": "GoFile (não resolvido automaticamente)"}

    # VikingFile
    if "vikingfile.com" in host or "vik1ngfile" in host:
        return {"url": url, "name": None, "note": "VikingFile (use o link da página; download costuma ser direto após o clique)"}

    # MediaFire
    if "mediafire.com" in host:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            m = re.search(r'href="(https://download[^"]+)"', r.text)
            if not m:
                m = re.search(r"aria-label=\"Download file\"[^>]*href=\"([^\"]+)\"", r.text)
            if m:
                return {"url": m.group(1), "name": None, "note": "MediaFire"}
        except Exception as e:
            return {"url": url, "name": None, "note": f"MediaFire (falha ao resolver: {e})"}

    # Google Drive (público)
    if "drive.google.com" in host or "docs.google.com" in host:
        m = re.search(r"/d/([A-Za-z0-9_-]+)", url) or re.search(r"id=([A-Za-z0-9_-]+)", url)
        if m:
            fid = m.group(1)
            return {"url": f"https://drive.google.com/uc?export=download&id={fid}",
                    "name": None, "note": "Google Drive (público)"}

    # Filebin
    if "filebin.net" in host:
        return {"url": url, "name": Path(urlparse(url).path).name, "note": "Filebin"}

    # Fallback: tenta HEAD e vê se já é download
    try:
        h = requests.head(url, headers=UA, allow_redirects=True, timeout=15)
        cd = h.headers.get("content-disposition", "")
        if "attachment" in cd.lower() or h.headers.get("content-type", "").startswith(("application/", "video/", "audio/")):
            name = None
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            if m:
                name = unquote(m.group(1))
            return {"url": h.url, "name": name, "note": "link direto (HEAD)"}
    except Exception:
        pass

    return {"url": url, "name": None, "note": "não resolvido (usando o link original)"}


def download_to(url, dest: Path, name_hint=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    say(f"⬇️ Baixando…")
    with requests.get(url, headers=UA, stream=True, timeout=120) as r:
        r.raise_for_status()
        # nome a partir do Content-Disposition se não tiver
        if not name_hint:
            cd = r.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            if m:
                name_hint = unquote(m.group(1)).replace('"', "")
        final = dest / (name_hint or Path(urlparse(url).path).name or "arquivo")
        with final.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    return final


# ---------------------------------------------------------------------------
# Uploaders
# ---------------------------------------------------------------------------
def upload_gofile(path: Path, folder_id=None):
    # servidor
    sv = requests.get("https://api.gofile.io/servers", timeout=15).json()
    server = sv["data"]["servers"][0]["name"]
    with path.open("rb") as f:
        files = {"file": (path.name, f)}
        data = {}
        if folder_id:
            data["folderId"] = folder_id
        r = requests.post(f"https://{server}.gofile.io/uploadfile", files=files, data=data, timeout=None)
    j = r.json()
    if j.get("status") != "ok":
        raise RuntimeError(str(j))
    return j["data"].get("downloadPage") or j["data"].get("parentFolder")


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
    with path.open("rb") as f:
        r = requests.post(server, files={"file": (path.name, f)}, data={"user": ""}, timeout=None)
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
    return f"https://buzzheavier.com/{r.text.strip().strip('\"')}"


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


def do_upload(path: Path, host: str):
    fn = UPLOADERS.get(host)
    if not fn:
        raise RuntimeError(f"Host desconhecido: {host}. Use: {', '.join(UPLOADERS)}")
    say(f"⬆️ Enviando para {host}…")
    link = fn(path)
    say(f"✅ {host}: {link}")
    return link


# ---------------------------------------------------------------------------
# Unzip
# ---------------------------------------------------------------------------
def unzip_and_upload(archive: Path, host: str, tmp: Path):
    extract_dir = tmp / "extracted"
    extract_dir.mkdir(exist_ok=True)
    say("📦 Extraindo…")
    if archive.suffix.lower() == ".zip" or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)
    else:
        # tenta 7z
        r = subprocess.run(["7z", "x", str(archive), f"-o{extract_dir}", "-y"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("Não consegui extrair (não é ZIP e 7z falhou)")
    files = [p for p in extract_dir.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError("Nenhum arquivo extraído")
    say(f"Encontrados {len(files)} arquivo(s). Enviando para {host}…")
    links = []
    for f in files:
        try:
            link = do_upload(f, host)
            links.append(f"{f.name}: {link}")
        except Exception as e:
            links.append(f"{f.name}: ERRO {e}")
    say("\n".join(links) or "Nada enviado")
    return links


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    mode = (PAYLOAD.get("mode") or "telegram").lower()
    host = (PAYLOAD.get("host") or "gofile").lower()
    url = PAYLOAD.get("url")
    file_id = PAYLOAD.get("file_id")
    file_name = PAYLOAD.get("file_name")

    premium_client, volume_limit, mode_desc = select_uploader()
    say(f"🔧 /uploader | modo={mode} | envio={mode_desc}")

    with tempfile.TemporaryDirectory(prefix="uploader-") as td:
        tmp = Path(td)
        local = None
        suggested_name = file_name

        if file_id:
            say("⬇️ Baixando do Telegram…")
            # usa MTProto se for grande, senão Bot API
            # (simplificado: sempre tenta MTProto se disponível)
            try:
                dest = tmp / (file_name or "arquivo")
                got = bot_client().download_media(file_id, file_name=str(dest))
                local = Path(got)
            except Exception as e:
                raise RuntimeError(f"Download do Telegram falhou: {e}")
        elif url:
            resolved = resolve(url)
            say(f"🔍 Resolvido: {resolved.get('note')}\n{resolved['url'][:200]}")
            if mode == "link":
                say(f"🔗 Link direto:\n{resolved['url']}")
                return
            local = download_to(resolved["url"], tmp, resolved.get("name") or suggested_name)
            suggested_name = local.name
        else:
            raise RuntimeError("Nenhum url nem file_id recebido")

        if not local or not local.exists():
            raise RuntimeError("Arquivo não encontrado após download")

        caption = suggested_name or local.name

        if mode == "link":
            # já tratado acima
            return
        elif mode == "upload":
            do_upload(local, host)
        elif mode == "unzip":
            unzip_and_upload(local, host, tmp)
        else:  # telegram
            deliver(local, caption, premium_client, volume_limit)

    say("✅ /uploader concluído.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        say(f"❌ /uploader: {e}")
        raise
    finally:
        stop_clients()
