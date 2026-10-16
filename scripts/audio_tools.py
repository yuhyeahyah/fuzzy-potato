#!/usr/bin/env python3
"""
Ferramentas de áudio do bot do Telegram, executadas no GitHub Actions.

Chamado pelo workflow audio-tools.yml (repository_dispatch, event_type "audio-tool").
Payload enviado pelo Worker:

    op        : info | capa | limpar | renomear | tag |
                converter | cortar | normalizar | juntar | espectro
    args      : parâmetros do comando (depende do op)
    chat_id   : chat de destino
    thread_id : tópico de destino (pode ser null)
    files     : [{ file_id, name, title, performer, duration, size, mime, pos,
                   fields, out_name, erase }]

SOBRE O LIMITE DE 20 MB
    O teto de 20 MB NÃO é do Cloudflare Worker: é da própria Bot API (getFile).
    O GitHub Actions usando a mesma Bot API bate no mesmo limite. Por isso, para
    arquivos grandes este script baixa (e, acima de 50 MB, envia) por MTProto,
    logando o MESMO bot com api_id/api_hash (biblioteca Kurigram, módulo "pyrogram").
    Precisa dos secrets TG_API_ID e TG_API_HASH (https://my.telegram.org).
    Arquivos até 20 MB continuam indo pela Bot API HTTP, sem depender disso.
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from html import escape as esc
from pathlib import Path
from urllib import error, parse, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
PAYLOAD = json.loads(os.environ["PAYLOAD"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_UPLOAD = 49 * 1024 * 1024        # envio pela Bot API HTTP: 50 MB, deixo folga
BOT_DL_LIMIT = 20 * 1024 * 1024      # download pela Bot API HTTP (getFile)

CHAT_ID = PAYLOAD["chat_id"]
THREAD_ID = PAYLOAD.get("thread_id")
OP = PAYLOAD["op"]
ARGS = PAYLOAD.get("args") or {}
FILES = PAYLOAD.get("files") or []


# ---------- HTTP (Bot API) ----------

def _read_json(req, timeout):
    try:
        with request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": str(e)}


def _multipart(fields, files=None):
    """fields: dict; files: lista de (campo, caminho, nome, mime)."""
    boundary = "----tagbot" + os.urandom(8).hex()
    out = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode()
    for field, fpath, fname, mime in files or []:
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'.encode()
        out += f"Content-Type: {mime}\r\n\r\n".encode()
        out += Path(fpath).read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def api(method, fields, files=None):
    body, ctype = _multipart(fields, files)
    req = request.Request(f"{API}/{method}", data=body, headers={"Content-Type": ctype})
    return _read_json(req, 300)


def say(text, html=False):
    api("sendMessage", {
        "chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "text": text[:4000],
        "parse_mode": "HTML" if html else None, "link_preview_options": '{"is_disabled":true}' if html else None,
    })


# ---------- MTProto (arquivos grandes) ----------

_MT = None


def mt_ready():
    return bool(os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH"))


def mt():
    """Cliente MTProto (Kurigram/Pyrogram) logado como o próprio bot. Criado só quando preciso."""
    global _MT
    if _MT is not None:
        return _MT
    if not mt_ready():
        raise RuntimeError(
            "arquivo acima de 20 MB: faltam os secrets TG_API_ID e TG_API_HASH no repositório "
            "(crie em https://my.telegram.org > API development tools)"
        )
    from pyrogram import Client  # pip install kurigram tgcrypto

    _MT = Client("tagbot", api_id=int(os.environ["TG_API_ID"]), api_hash=os.environ["TG_API_HASH"],
                 bot_token=BOT_TOKEN, in_memory=True, no_updates=True)
    _MT.start()
    atexit.register(_mt_stop)
    return _MT


def _mt_stop():
    try:
        if _MT is not None:
            _MT.stop()
    except Exception:  # noqa: BLE001
        pass


def mt_download(file_id, dest):
    got = mt().download_media(file_id, file_name=str(dest))
    if not got:
        raise RuntimeError("download via MTProto não devolveu o arquivo")
    return Path(got)


def mt_send(kind, path, name, title=None, performer=None, duration=None, thumb=None, caption=None):
    kw = {"chat_id": CHAT_ID}
    if THREAD_ID:
        kw["message_thread_id"] = THREAD_ID
    try:
        c = mt()
        if kind == "audio":
            c.send_audio(audio=str(path), file_name=name, title=title, performer=performer,
                         duration=int(duration or 0), thumb=str(thumb) if thumb else None,
                         caption=caption, **kw)
        elif kind == "photo":
            c.send_photo(photo=str(path), caption=caption, **kw)
        else:
            c.send_document(document=str(path), file_name=name, caption=caption, **kw)
        return True
    except Exception as e:  # noqa: BLE001
        say(f"❌ {name}: envio por MTProto falhou: {e}\n"
            "(Em chat privado o bot às vezes não consegue resolver o usuário por MTProto; em grupo costuma funcionar.)")
        return False


# ---------- download / envio ----------

def download(file_id, dest, size=0):
    """Baixa um arquivo do Telegram. Até 20 MB pela Bot API; acima disso por MTProto."""
    dest = Path(dest)
    if size and size > BOT_DL_LIMIT:
        return mt_download(file_id, dest)
    r = _read_json(f"{API}/getFile?file_id={parse.quote(file_id)}", 120)
    if not r.get("ok"):
        desc = r.get("description", "getFile falhou")
        if "too big" in desc.lower():
            return mt_download(file_id, dest)
        raise RuntimeError(desc)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{r['result']['file_path']}"
    with request.urlopen(url, timeout=600) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


def make_thumb(audio_path):
    """O cartão de áudio do Telegram só mostra a capa se ela vier como thumbnail (JPEG, até 320 px / 200 KB)."""
    try:
        raw = Path(audio_path).with_suffix(".cover.jpg")
        r = subprocess.run(["ffmpeg", "-y", "-i", str(audio_path), "-an", "-map", "0:v:0", "-c:v", "copy", str(raw)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not raw.exists() or raw.stat().st_size < 100:
            return None
        thumb = Path(audio_path).with_suffix(".thumb.jpg")
        for side, q in ((320, "5"), (256, "8")):
            subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-vf",
                            f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease",
                            "-q:v", q, str(thumb)], capture_output=True, text=True)
            if thumb.exists() and thumb.stat().st_size <= 200 * 1024:
                return thumb
        return None
    except Exception:  # noqa: BLE001
        return None


def _too_big_warning(name, size):
    say(f"⚠️ {name}: {size // (1024 * 1024)} MB, acima dos 50 MB do envio pela Bot API. "
        "Para enviar arquivo grande, crie os secrets TG_API_ID e TG_API_HASH no repositório (my.telegram.org).")
    return False


def send_audio(path, name, title=None, performer=None, thumb=None, caption=None, duration=None):
    path = Path(path)
    size = path.stat().st_size
    thumb = thumb or make_thumb(path)
    if size > MAX_UPLOAD:
        if not mt_ready():
            return _too_big_warning(name, size)
        return mt_send("audio", path, name, title, performer, duration, thumb, caption)
    files = [("audio", path, name, "audio/mpeg")]
    if thumb:
        files.append(("thumbnail", thumb, "thumb.jpg", "image/jpeg"))
    r = api("sendAudio", {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "title": title,
                          "performer": performer, "caption": caption,
                          "duration": int(duration) if duration else None}, files)
    if not r.get("ok"):
        say(f"❌ {name}: {r.get('description')}")
    return r.get("ok", False)


def send_document(path, name, caption=None):
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_UPLOAD:
        if not mt_ready():
            return _too_big_warning(name, size)
        return mt_send("document", path, name, caption=caption)
    r = api("sendDocument", {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "caption": caption,
                             "disable_content_type_detection": "true"},
            [("document", path, name, "application/octet-stream")])
    if not r.get("ok"):
        say(f"❌ {name}: {r.get('description')}")
    return r.get("ok", False)


def send_photo(path, name, caption=None, mime="image/png"):
    r = api("sendPhoto", {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "caption": caption},
            [("photo", path, name, mime)])
    if not r.get("ok"):
        say(f"❌ {name}: {r.get('description')}")
    return r.get("ok", False)


AUDIO_AS_AUDIO = {".mp3", ".m4a"}  # o Telegram só trata esses dois como "música"


def send_any(path, name, f, caption=None):
    """MP3 e M4A vão como áudio (com capa no cartão); o resto vai como arquivo, sem recompressão."""
    if Path(name).suffix.lower() in AUDIO_AS_AUDIO:
        return send_audio(path, name, f.get("title"), f.get("performer"), caption=caption, duration=f.get("duration"))
    return send_document(path, name, caption=caption)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "ffmpeg falhou").strip().splitlines()[-1][:300])


# ---------- utilidades ----------

def safe(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(name or "")).strip()
    return (name or "faixa")[:120]


def stem(name):
    return re.sub(r"\.[a-z0-9]{2,5}$", "", str(name or ""), flags=re.I).strip()


def out_name(f, ext):
    base = stem(f.get("name")) or f.get("title") or "audio"
    return f"{safe(base)}.{ext}"


def fmt_dur(sec):
    sec = int(float(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def fmt_bytes(n):
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1048576:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1048576:.1f} MB"


def fold(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def src_path(tmp, i, f):
    return tmp / f"in{i}{Path(f.get('name') or '').suffix or '.bin'}"


def get_src(tmp, i, f):
    return download(f["file_id"], src_path(tmp, i, f), f.get("size"))


# A capa de um MP3 é um "stream de vídeo" (imagem anexada). Mapeamos o áudio e,
# se existir, a imagem, copiando sem recomprimir. O "?" deixa o -map opcional.
def cover_map(fmt):
    if fmt in ("mp3", "m4a", "aac", "flac"):
        return ["-map", "0:a", "-map", "0:v:0?", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    return ["-map", "0:a"]


def encode_args(fmt, bitrate):
    if fmt == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{bitrate or 320}k", "-id3v2_version", "3"]
    if fmt == "flac":
        return ["-c:a", "flac"]
    if fmt in ("m4a", "aac"):
        return ["-c:a", "aac", "-b:a", f"{bitrate or 256}k"]
    if fmt == "ogg":
        return ["-c:a", "libvorbis", "-b:a", f"{bitrate or 256}k"]
    if fmt == "opus":
        return ["-c:a", "libopus", "-b:a", f"{bitrate or 160}k"]
    if fmt == "wav":
        return ["-c:a", "pcm_s16le"]
    return ["-c:a", "libmp3lame", "-b:a", "320k"]


# ---------- ffprobe / exiftool ----------

def probe_json(path):
    p = subprocess.run(["ffprobe", "-hide_banner", "-v", "error", "-show_format", "-show_streams",
                        "-show_chapters", "-of", "json", "-i", str(path)], capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        return {}


def probe_text(path):
    p = subprocess.run(["ffprobe", "-hide_banner", "-v", "error", "-show_format", "-show_streams",
                        "-show_chapters", "-i", str(path)], capture_output=True, text=True, errors="replace")
    return (p.stdout + p.stderr).strip()


def exiftool_text(path):
    if not shutil.which("exiftool"):
        return "exiftool não encontrado no runner"
    p = subprocess.run(["exiftool", "-a", "-u", "-G1", "-s", str(path)], capture_output=True, text=True, errors="replace")
    return (p.stdout + p.stderr).strip()


def ixml_text(path):
    if not shutil.which("exiftool"):
        return ""
    p = subprocess.run(["exiftool", "-u", "-b", "-Unknown_ixml", str(path)], capture_output=True)
    return p.stdout.decode("utf-8", "replace").strip()


# ---------- texto dentro dos chunks / átomos / frames ----------

STR_RE = re.compile(rb"[\x20-\x7e]{6,}")
READ_CAP = 4_000_000


def strings_of(b, limit=40):
    out = []
    for m in STR_RE.finditer(b):
        s = m.group().decode("ascii", "replace").strip()
        if s:
            out.append(s)
        if len(out) >= limit:
            out.append("...")
            break
    return out


def scan_riff(path):
    """WAV / RF64: lista os chunks e o texto legível dentro de cada um (como o wavmeta.ps1)."""
    total = path.stat().st_size
    lines = []
    ds64_data = None
    with open(path, "rb") as f:
        head = f.read(12)
        lines.append(f"[{head[:4].decode('ascii', 'replace')}] formato {head[8:12].decode('ascii', 'replace')}")
        while f.tell() + 8 <= total:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[:4].decode("ascii", "replace")
            sz = int.from_bytes(hdr[4:], "little")
            if cid == "data":
                if sz == 0xFFFFFFFF and ds64_data:
                    sz = ds64_data
                lines.append(f"[data] {sz} bytes de áudio (ignorado)")
                f.seek(sz, 1)
            else:
                n = max(0, min(sz, total - f.tell()))
                body = f.read(min(n, READ_CAP))
                f.seek(n - len(body), 1)
                if cid == "ds64" and len(body) >= 16:
                    ds64_data = int.from_bytes(body[8:16], "little")
                lines.append(f"[{cid}] {sz} bytes")
                lines += ["    " + s for s in strings_of(body)]
            if sz % 2 and f.tell() < total:
                f.seek(1, 1)
    return lines


def scan_flac(path):
    names = {0: "STREAMINFO", 1: "PADDING", 2: "APPLICATION", 3: "SEEKTABLE", 4: "VORBIS_COMMENT", 5: "CUESHEET", 6: "PICTURE"}
    lines = []
    with open(path, "rb") as f:
        f.read(4)
        while True:
            h = f.read(4)
            if len(h) < 4:
                break
            last, t, ln = h[0] & 0x80, h[0] & 0x7F, int.from_bytes(h[1:4], "big")
            body = f.read(min(ln, READ_CAP))
            f.seek(ln - len(body), 1)
            lines.append(f"[{names.get(t, t)}] {ln} bytes")
            if t in (2, 4, 5):
                lines += ["    " + s for s in strings_of(body)]
            if last:
                break
    return lines


MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"ilst", b"edts", b"dinf"}


def scan_mp4(path):
    total = path.stat().st_size
    lines = []
    with open(path, "rb") as f:
        def walk(end, depth):
            while f.tell() + 8 <= end:
                start = f.tell()
                hdr = f.read(8)
                sz, typ, hsz = int.from_bytes(hdr[:4], "big"), hdr[4:8], 8
                if sz == 1:
                    sz, hsz = int.from_bytes(f.read(8), "big"), 16
                elif sz == 0:
                    sz = end - start
                if sz < hsz:
                    break
                pad = "  " * depth
                lines.append(f"{pad}[{typ.decode('latin1')}] {sz} bytes")
                bend = min(start + sz, end)
                if typ == b"mdat":
                    pass
                elif typ in MP4_CONTAINERS:
                    walk(bend, depth + 1)
                elif typ == b"meta":
                    f.seek(4, 1)
                    walk(bend, depth + 1)
                else:
                    body = f.read(max(0, min(bend - f.tell(), 2_000_000)))
                    lines.extend(f"{pad}    {s}" for s in strings_of(body, 10))
                f.seek(bend)

        walk(total, 0)
    return lines


def _id3_text(b):
    codec = {0: "latin1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(b[0], "latin1")
    try:
        return b[1:].decode(codec, "replace").strip("\x00").replace("\x00", " / ")[:200]
    except Exception:  # noqa: BLE001
        return ""


def scan_id3(path):
    with open(path, "rb") as f:
        h = f.read(10)
        ss = lambda b: (b[0] & 0x7F) << 21 | (b[1] & 0x7F) << 14 | (b[2] & 0x7F) << 7 | (b[3] & 0x7F)  # noqa: E731
        size = ss(h[6:10])
        body = f.read(min(size, 8_000_000))
    ver = h[3]
    idl, hl = (3, 6) if ver == 2 else (4, 10)
    lines = [f"[ID3v2.{ver}] {size} bytes de tag"]
    pos = 0
    while pos + hl <= len(body):
        fid = body[pos:pos + idl]
        if not re.fullmatch(rb"[A-Z0-9]+", fid):
            break
        if ver == 2:
            fs = int.from_bytes(body[pos + 3:pos + 6], "big")
        elif ver == 4:
            fs = ss(body[pos + 4:pos + 8])
        else:
            fs = int.from_bytes(body[pos + 4:pos + 8], "big")
        if fs <= 0 or pos + hl + fs > len(body):
            break
        data = body[pos + hl:pos + hl + fs]
        name = fid.decode()
        lines.append(f"  [{name}] {fs} bytes")
        if name.startswith("T") and name != "TXXX":
            lines.append(f"      {_id3_text(data)}")
        elif name in ("COMM", "USLT", "COM", "ULT") and len(data) > 4:
            lines.append(f"      {_id3_text(data[:1] + data[4:])}")
        elif name not in ("APIC", "PIC", "GEOB", "PRIV"):
            lines += ["      " + s for s in strings_of(data, 6)]
        pos += hl + fs
    return lines


def scan_chunks(path):
    with open(path, "rb") as f:
        magic = f.read(12)
    if magic[:4] in (b"RIFF", b"RF64"):
        return "RIFF/WAV", scan_riff(path)
    if magic[:4] == b"fLaC":
        return "FLAC", scan_flac(path)
    if magic[4:8] == b"ftyp":
        return "MP4/M4A", scan_mp4(path)
    if magic[:3] == b"ID3":
        return "MP3 (ID3v2)", scan_id3(path)
    return None, []


# ---------- /info ----------

def summarize(meta, f, path, kind, chunk_lines):
    fmt = meta.get("format", {})
    streams = meta.get("streams", [])
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    size = path.stat().st_size

    def row(label, val):
        return f"{label}: <b>{esc(str(val))}</b>" if val not in (None, "", "0") else None

    br_total = int(fmt.get("bit_rate") or 0)
    bits = a.get("bits_per_raw_sample") or (a.get("bits_per_sample") if str(a.get("bits_per_sample", "0")) != "0" else None)
    sr = a.get("sample_rate")
    top = [r for r in (
        row("Formato", f"{fmt.get('format_long_name', '?')} ({fmt.get('format_name', '?')})"),
        row("Tamanho", fmt_bytes(size)),
        row("Duração", fmt_dur(fmt.get("duration")) if fmt.get("duration") else None),
        row("Bitrate total", f"{br_total // 1000} kbps" if br_total else None),
    ) if r]
    aud = [r for r in (
        row("Codec", f"{a.get('codec_long_name', '?')} ({a.get('codec_name', '?')})" if a else None),
        row("Amostragem", f"{int(sr) / 1000:g} kHz" if sr else None),
        row("Profundidade", f"{bits} bits" if bits else None),
        row("Formato da amostra", None if bits else a.get("sample_fmt")),
        row("Canais", f"{a.get('channels')} ({a.get('channel_layout', '?')})" if a.get("channels") else None),
        row("Bitrate do áudio", f"{int(a['bit_rate']) // 1000} kbps" if a.get("bit_rate") else None),
    ) if r]
    extra = []
    if v:
        extra.append(f"Capa: <b>{esc(v.get('codec_name', '?'))} {v.get('width', '?')}×{v.get('height', '?')}</b>")
    if meta.get("chapters"):
        extra.append(f"Capítulos: <b>{len(meta['chapters'])}</b>")
    top_chunks = [m.group(1) for m in (re.match(r"^\[(\S+)\]", l) for l in chunk_lines) if m]
    if top_chunks:
        extra.append(f"Estrutura ({esc(kind)}): <code>{esc(' '.join(top_chunks[:20]))}</code>")

    tags = {}
    for src in (fmt.get("tags") or {}, a.get("tags") or {}):
        for k, val in src.items():
            tags.setdefault(k, val)
    tag_lines = [f"{esc(k)}: {esc(' '.join(str(val).split())[:150])}" for k, val in list(tags.items())[:30]]

    out = [f"<b>ℹ️ {esc(f.get('name') or 'Áudio')}</b>", "", "<b>Arquivo</b>", *top, "", "<b>Áudio</b>", *aud]
    if extra:
        out += ["", *extra]
    out += ["", "<b>Tags</b>", *(tag_lines or ["<i>sem tags</i>"])]
    return "\n".join(out)


def build_report(path, f, meta, kind, chunk_lines):
    secs = [
        ("ARQUIVO", f"{f.get('name')}\n{fmt_bytes(path.stat().st_size)}\nRecebido pelo Telegram (sem Zone.Identifier: não vem de download de navegador)"),
        ("FFPROBE", probe_text(path)),
        ("EXIFTOOL (todos os chunks)", exiftool_text(path)),
    ]
    ix = ixml_text(path)
    if ix:
        secs.append(("iXML", ix))
    secs.append((f"TEXTO DENTRO DOS CHUNKS ({kind or 'formato não reconhecido'})", "\n".join(chunk_lines) or "(nada para listar)"))
    return "\n".join(f"==================== {t} ====================\n{b.rstrip()}\n" for t, b in secs)


def op_info(tmp):
    ok = 0
    for i, f in enumerate(FILES):
        try:
            src = get_src(tmp, i, f)
        except Exception as e:  # noqa: BLE001
            say(f"❌ {f.get('name')}: não consegui baixar: {e}")
            continue
        meta = probe_json(src)
        kind, chunk_lines = scan_chunks(src)
        say(summarize(meta, f, src, kind, chunk_lines), html=True)
        txt = tmp / f"{safe(stem(f.get('name')))} (info).txt"
        txt.write_text(build_report(src, f, meta, kind, chunk_lines), encoding="utf-8")
        ok += send_document(txt, txt.name, caption="Relatório completo: ffprobe + exiftool + iXML + texto dos chunks")
    say(f"ℹ️ /info: {ok}/{len(FILES)} relatório(s).")


# ---------- /capa ----------

def op_capa(tmp):
    ok = 0
    for i, f in enumerate(FILES):
        try:
            src = get_src(tmp, i, f)
        except Exception as e:  # noqa: BLE001
            say(f"❌ {f.get('name')}: não consegui baixar: {e}")
            continue
        v = next((s for s in probe_json(src).get("streams", []) if s.get("codec_type") == "video"), None)
        if not v:
            say(f"🖼 {f.get('name')}: sem capa embutida.")
            continue
        codec = v.get("codec_name")
        ext = "png" if codec == "png" else "jpg"
        out = tmp / f"capa{i}.{ext}"
        cmd = ["ffmpeg", "-y", "-i", str(src), "-an", "-map", "0:v:0", "-frames:v", "1"]
        cmd += ["-c:v", "copy"] if codec in ("mjpeg", "png") else []
        try:
            run(cmd + [str(out)])
        except RuntimeError as e:
            say(f"❌ {f.get('name')}: {e}")
            continue
        cap = f"🖼 Capa {v.get('width', '?')}×{v.get('height', '?')} · {fmt_bytes(out.stat().st_size)}"
        mime = "image/png" if ext == "png" else "image/jpeg"
        if out.stat().st_size < 9 * 1024 * 1024:
            send_photo(out, f"capa.{ext}", cap, mime)
        ok += send_document(out, f"{safe(stem(f.get('name')))} (capa).{ext}", caption="Arquivo original, sem compressão")
    say(f"🖼 /capa: {ok}/{len(FILES)}.")


# ---------- /limpar ----------

def op_limpar(tmp):
    ok = 0
    for i, f in enumerate(FILES):
        try:
            src = get_src(tmp, i, f)
        except Exception as e:  # noqa: BLE001
            say(f"❌ {f.get('name')}: não consegui baixar: {e}")
            continue
        ext = Path(f.get("name") or "").suffix.lower() or ".mp3"
        dst = tmp / f"limpo{i}{ext}"
        cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0:a", "-c", "copy", "-map_metadata", "-1",
               "-fflags", "+bitexact", "-flags:a", "+bitexact"]
        if ext in (".mp3", ".wav"):
            cmd += ["-write_id3v2", "0"]
        try:
            run(cmd + [str(dst)])
        except RuntimeError as e:
            say(f"❌ {f.get('name')}: {e}")
            continue
        removed = src.stat().st_size - dst.stat().st_size
        ok += send_any(dst, f.get("name") or dst.name, f, caption=f"🧽 Sem tags e sem capa (removi {fmt_bytes(max(removed, 0))})")
    say(f"🧽 /limpar: {ok}/{len(FILES)}.")


# ---------- /renomear ----------

def render_template(tpl, v):
    out = re.sub(r"\{([^}]{1,20})\}", lambda m: str(v.get(fold(m.group(1)), "") or ""), tpl)
    out = re.sub(r"(\s*[-–·]\s*){2,}", " - ", out)
    out = re.sub(r"^[\s\-–·]+|[\s\-–·]+$", "", out)
    return safe(out)


def _num(s):
    m = re.match(r"\s*(\d+)", str(s or ""))
    return int(m.group(1)) if m else None


def op_renomear(tmp):
    tpl = (ARGS.get("template") or "").strip()
    if not tpl:
        say("Uso: /renomear {n} - {artista} - {titulo}  (ou, respondendo a um arquivo, /renomear Novo nome)")
        return
    total = int(ARGS.get("total") or len(FILES))
    ok = 0
    for i, f in enumerate(FILES):
        try:
            src = get_src(tmp, i, f)
        except Exception as e:  # noqa: BLE001
            say(f"❌ {f.get('name')}: não consegui baixar: {e}")
            continue
        ext = Path(f.get("name") or "").suffix or ".mp3"
        if "{" in tpl:
            meta = probe_json(src)
            tags = {k.lower(): v for k, v in {**(meta.get("streams", [{}])[0].get("tags") or {}), **(meta.get("format", {}).get("tags") or {})}.items()}
            pos = int(f.get("pos") or i + 1)
            width = max(2, len(str(total)))
            trk = _num(tags.get("track"))
            title = tags.get("title") or f.get("title") or stem(f.get("name")) or f"Faixa {pos}"
            artist = tags.get("artist") or f.get("performer") or ""
            vals = {
                "n": str(pos).zfill(width), "num": str(pos).zfill(width), "numero": str(pos).zfill(width),
                "titulo": title, "title": title, "artista": artist, "artist": artist,
                "album": tags.get("album", ""), "ano": (re.search(r"\d{4}", tags.get("date", "")) or [""])[0],
                "disco": str(_num(tags.get("disc")) or ""), "genero": tags.get("genre", ""),
                "faixa": str(trk).zfill(width) if trk else "", "arquivo": stem(f.get("name")),
            }
            vals["year"], vals["disc"], vals["genre"] = vals["ano"], vals["disco"], vals["genero"]
            name = render_template(tpl, vals) + ext
        else:
            name = safe(stem(tpl)) + ext
        ok += send_any(src, name, f)
    say(f"✏️ /renomear: {ok}/{len(FILES)} arquivo(s).")


# ---------- /tag (gravação de tags em arquivo grande) ----------
# Usa mutagen: edita só o que foi pedido e preserva o resto da tag e o áudio.
# Campos vindos do Worker: title artist albumArtist album year genre disc composer lyricist
#   label copyright bpm isrc subtitle track comment lyrics.  null = manter o que já está no arquivo;
#   chaves em "erase" = apagar do arquivo.

def _id3_apply(tags, fields, erase, cover, drop_cover):
    from mutagen.id3 import (APIC, COMM, TALB, TBPM, TCON, TCOM, TCOP, TEXT, TIT2, TIT3, TPE1, TPE2, TPOS, TPUB,
                             TRCK, TSRC, TYER, USLT)
    text = {"title": TIT2, "artist": TPE1, "albumArtist": TPE2, "album": TALB, "year": TYER, "genre": TCON,
            "disc": TPOS, "composer": TCOM, "lyricist": TEXT, "label": TPUB, "copyright": TCOP, "bpm": TBPM,
            "isrc": TSRC, "subtitle": TIT3, "track": TRCK}
    for k, cls in text.items():
        fid = cls.__name__
        if k in erase:
            tags.delall(fid)
            if k == "year":
                tags.delall("TDRC")
        elif fields.get(k):
            tags.setall(fid, [cls(encoding=1, text=[str(fields[k])])])
            if k == "year":
                tags.delall("TDRC")
    if "comment" in erase:
        tags.delall("COMM")
    elif fields.get("comment"):
        tags.setall("COMM", [COMM(encoding=1, lang="por", desc="", text=[fields["comment"]])])
    if "lyrics" in erase:
        tags.delall("USLT")
    elif fields.get("lyrics"):
        tags.setall("USLT", [USLT(encoding=1, lang="por", desc="", text=fields["lyrics"])])
    if cover:
        tags.setall("APIC", [APIC(encoding=0, mime=cover[0], type=3, desc="", data=cover[1])])
    elif drop_cover:
        tags.delall("APIC")


def _tag_mp3(path, fields, erase, cover, drop_cover):
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    _id3_apply(tags, fields, erase, cover, drop_cover)
    tags.save(str(path), v2_version=3)


def _tag_wav(path, fields, erase, cover, drop_cover):
    from mutagen.wave import WAVE
    w = WAVE(str(path))
    if w.tags is None:
        w.add_tags()
    _id3_apply(w.tags, fields, erase, cover, drop_cover)
    w.save(v2_version=3)


def _tag_flac(path, fields, erase, cover, drop_cover):
    from mutagen.flac import FLAC, Picture
    a = FLAC(str(path))
    keys = {"title": "title", "artist": "artist", "albumArtist": "albumartist", "album": "album", "year": "date",
            "genre": "genre", "disc": "discnumber", "composer": "composer", "lyricist": "lyricist", "label": "label",
            "copyright": "copyright", "bpm": "bpm", "isrc": "isrc", "subtitle": "subtitle", "track": "tracknumber",
            "comment": "comment", "lyrics": "lyrics"}
    for k, tag in keys.items():
        if k in erase:
            a.pop(tag, None)
        elif fields.get(k):
            a[tag] = [str(fields[k])]
    if cover or drop_cover:
        a.clear_pictures()
    if cover:
        p = Picture()
        p.type, p.mime, p.data = 3, cover[0], cover[1]
        a.add_picture(p)
    a.save()


def _tag_mp4(path, fields, erase, cover, drop_cover):
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    a = MP4(str(path))
    if a.tags is None:
        a.add_tags()
    t = a.tags
    atoms = {"title": "\xa9nam", "artist": "\xa9ART", "albumArtist": "aART", "album": "\xa9alb", "year": "\xa9day",
             "genre": "\xa9gen", "composer": "\xa9wrt", "copyright": "cprt", "comment": "\xa9cmt", "lyrics": "\xa9lyr"}
    free = {"lyricist": "LYRICIST", "label": "LABEL", "isrc": "ISRC", "subtitle": "SUBTITLE"}
    pair = lambda s: (_num(s) or 0, _num(str(s).split("/")[1]) if "/" in str(s) else 0)  # noqa: E731
    for k, atom in atoms.items():
        if k in erase:
            t.pop(atom, None)
        elif fields.get(k):
            t[atom] = [str(fields[k])]
    for k, atom in (("track", "trkn"), ("disc", "disk")):
        if k in erase:
            t.pop(atom, None)
        elif fields.get(k):
            t[atom] = [pair(fields[k])]
    if "bpm" in erase:
        t.pop("tmpo", None)
    elif fields.get("bpm") and _num(fields["bpm"]):
        t["tmpo"] = [_num(fields["bpm"])]
    for k, name in free.items():
        key = f"----:com.apple.iTunes:{name}"
        if k in erase:
            t.pop(key, None)
        elif fields.get(k):
            t[key] = [MP4FreeForm(str(fields[k]).encode("utf-8"))]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if cover[0] == "image/png" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(cover[1], imageformat=fmt)]
    elif drop_cover:
        t.pop("covr", None)
    a.save()


TAG_WRITERS = {".mp3": _tag_mp3, ".wav": _tag_wav, ".flac": _tag_flac, ".m4a": _tag_mp4, ".mp4": _tag_mp4}


def op_tag(tmp):
    cover = None
    if ARGS.get("cover_file_id"):
        try:
            cp = download(ARGS["cover_file_id"], tmp / "capa_nova")
            b = cp.read_bytes()
            cover = ("image/png" if b[:2] == b"\x89P" else "image/jpeg", b)
        except Exception as e:  # noqa: BLE001
            say(f"⚠️ Não consegui baixar a capa nova ({e}); sigo sem ela.")
    drop_cover = ARGS.get("cover") == "remove"
    ok = 0
    for i, f in enumerate(FILES):
        name = f.get("out_name") or f.get("name") or f"faixa{i + 1}.mp3"
        ext = Path(f.get("name") or name).suffix.lower() or ".mp3"
        writer = TAG_WRITERS.get(ext)
        if not writer:
            say(f"❌ {name}: não sei gravar tags em {ext} (aceito: {', '.join(sorted(TAG_WRITERS))}).")
            continue
        try:
            src = get_src(tmp, i, f)
            fields = f.get("fields") or {}
            writer(src, fields, set(f.get("erase") or []), cover, drop_cover)
        except Exception as e:  # noqa: BLE001
            say(f"❌ {name}: {e}")
            continue
        info = dict(f, title=fields.get("title") or f.get("title"), performer=fields.get("artist") or f.get("performer"))
        ok += send_any(src, name, info)
    say(f"🏷 /tag: {ok}/{len(FILES)} arquivo(s) taggeado(s).")


# ---------- operações com ffmpeg (já existiam) ----------

def op_converter(tmp):
    fmt = ARGS.get("fmt", "mp3")
    bitrate = ARGS.get("bitrate")
    ok = 0
    for i, f in enumerate(FILES):
        src = get_src(tmp, i, f)
        name = f"{safe(stem(f['name']) or f.get('title') or 'audio')}.{fmt}"
        dst = tmp / name
        try:
            run(["ffmpeg", "-y", "-i", str(src), *cover_map(fmt), "-map_metadata", "0", *encode_args(fmt, bitrate), str(dst)])
        except RuntimeError as e:
            say(f"❌ {f['name']}: {e}")
            continue
        if fmt == "mp3":
            ok += send_audio(dst, name, f.get("title"), f.get("performer"))
        else:
            ok += send_document(dst, name)
    say(f"✅ /converter para {fmt}{(' ' + str(bitrate) + 'k') if bitrate and fmt not in ('flac', 'wav') else ''}: {ok}/{len(FILES)} arquivo(s).")


def op_cortar(tmp):
    start = ARGS.get("start", 0)
    end = ARGS.get("end")
    fade = ARGS.get("fade")
    ok = 0
    for i, f in enumerate(FILES):
        src = get_src(tmp, i, f)
        name = f"{safe(stem(f['name']) or 'corte')} (corte).mp3"
        dst = tmp / name
        dur = (end - start) if end is not None else None
        cmd = ["ffmpeg", "-y", "-ss", str(start)]
        if dur is not None:
            cmd += ["-t", str(dur)]
        cmd += ["-i", str(src)]
        if fade and dur:
            fade_len = min(2.0, dur / 4)
            cmd += ["-af", f"afade=t=in:st=0:d={fade_len:.2f},afade=t=out:st={dur - fade_len:.2f}:d={fade_len:.2f}"]
        cmd += [*cover_map("mp3"), "-map_metadata", "0", "-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3", str(dst)]
        try:
            run(cmd)
        except RuntimeError as e:
            say(f"❌ {f['name']}: {e}")
            continue
        ok += send_audio(dst, name, f.get("title"), f.get("performer"))
    span = f"{fmt_dur(start)}" + (f" a {fmt_dur(end)}" if end is not None else " até o fim")
    say(f"✂️ /cortar ({span}): {ok}/{len(FILES)} arquivo(s).")


def op_normalizar(tmp):
    lufs = ARGS.get("lufs", -14)
    ok = 0
    for i, f in enumerate(FILES):
        src = get_src(tmp, i, f)
        name = out_name(f, "mp3")
        dst = tmp / name
        try:
            p = subprocess.run(
                ["ffmpeg", "-i", str(src), "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                capture_output=True, text=True,
            )
            m = re.search(r"\{[^{}]*\"input_i\"[\s\S]*?\}", p.stderr)
            af = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
            if m:
                d = json.loads(m.group(0))
                af += (
                    f":measured_I={d['input_i']}:measured_TP={d['input_tp']}"
                    f":measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}"
                    f":offset={d['target_offset']}:linear=true"
                )
            run(["ffmpeg", "-y", "-i", str(src), "-af", af, *cover_map("mp3"), "-map_metadata", "0",
                 "-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3", str(dst)])
        except (RuntimeError, KeyError, ValueError) as e:
            say(f"❌ {f['name']}: {e}")
            continue
        ok += send_audio(dst, name, f.get("title"), f.get("performer"))
    say(f"🔊 /normalizar (alvo {lufs} LUFS): {ok}/{len(FILES)} arquivo(s).")


def op_juntar(tmp):
    name = safe(ARGS.get("name") or "Mix")
    fmt = ARGS.get("fmt", "mp3")
    paths, chapters, pos, cover = [], [], 0.0, None
    for i, f in enumerate(FILES):
        src = get_src(tmp, i, f)
        norm = tmp / f"n{i}.mp3"  # tudo para 44.1k estéreo 320k, senão o concat falha com formatos mistos
        run(["ffmpeg", "-y", "-i", str(src), "-map", "0:a", "-ar", "44100", "-ac", "2",
             "-c:a", "libmp3lame", "-b:a", "320k", str(norm)])
        paths.append(norm)
        if cover is None:
            pic = tmp / f"cover{i}.jpg"
            r = subprocess.run(["ffmpeg", "-y", "-i", str(src), "-an", "-map", "0:v:0", "-c:v", "copy", str(pic)],
                               capture_output=True, text=True)
            if r.returncode == 0 and pic.exists() and pic.stat().st_size > 100:
                cover = pic
        pr = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                             "-of", "default=nw=1:nk=1", str(norm)], capture_output=True, text=True)
        dur = float(pr.stdout.strip() or 0) or float(f.get("duration") or 0)
        title = f.get("title") or stem(f["name"]) or f"Faixa {i + 1}"
        chapters.append((pos, pos + dur, title))
        pos += dur

    listfile = tmp / "list.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in paths))
    meta = tmp / "chapters.txt"
    lines = [";FFMETADATA1", f"title={name}"]
    for start, end, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(end * 1000)}", f"title={title}"]
    meta.write_text("\n".join(lines) + "\n")

    out = tmp / f"{name}.{fmt}"
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-i", str(meta)]
    if cover is not None and fmt in ("mp3", "m4a", "flac"):
        cmd += ["-i", str(cover), "-map", "0:a", "-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-map_metadata", "1", *encode_args(fmt, None), str(out)]
    try:
        run(cmd)
    except RuntimeError as e:
        say(f"❌ Não consegui juntar: {e}")
        return
    sent = send_audio(out, f"{name}.{fmt}", name, FILES[0].get("performer"), duration=pos) if fmt == "mp3" else send_document(out, f"{name}.{fmt}")
    if sent:
        say(f"🔗 /juntar: {len(FILES)} faixas em um arquivo de {fmt_dur(pos)}, com capítulos.")


def op_espectro(tmp):
    ok = 0
    for i, f in enumerate(FILES):
        src = get_src(tmp, i, f)
        png = tmp / f"spec{i}.png"
        try:
            run(["ffmpeg", "-y", "-i", str(src), "-lavfi", "showspectrumpic=s=1280x540:legend=1:gain=3", str(png)])
        except RuntimeError as e:
            say(f"❌ {f['name']}: {e}")
            continue
        cap = (f"🎛 {stem(f['name']) or f.get('title') or 'Áudio'}\n"
               "Se a energia some numa linha reta bem abaixo do topo, o arquivo foi convertido de uma fonte de qualidade menor.")
        ok += send_photo(png, f"espectro{i}.png", cap)
    say(f"🎛 /espectro: {ok}/{len(FILES)} imagem(ns).")


OPS = {
    "info": op_info,
    "capa": op_capa,
    "limpar": op_limpar,
    "renomear": op_renomear,
    "tag": op_tag,
    "converter": op_converter,
    "cortar": op_cortar,
    "normalizar": op_normalizar,
    "juntar": op_juntar,
    "espectro": op_espectro,
}


def main():
    if OP not in OPS:
        say(f"Operação desconhecida: {OP}")
        return
    if not FILES:
        say("Nenhum arquivo recebido.")
        return
    with tempfile.TemporaryDirectory() as d:
        try:
            OPS[OP](Path(d))
        except Exception as e:  # noqa: BLE001
            say(f"Falhou no /{OP}: {e}")
            raise


if __name__ == "__main__":
    main()
