#!/usr/bin/env python3
"""
Executa as ferramentas de áudio com ffmpeg para o bot do Telegram.

Chamado pelo workflow audio-tools.yml, que recebe o payload do Worker via
repository_dispatch. O payload tem:

    op        : converter | cortar | normalizar | juntar | espectro
    args      : parâmetros do comando (depende do op)
    chat_id   : chat de destino
    thread_id : tópico de destino (pode ser null)
    files     : [{ file_id, name, title, performer, duration }]

Aqui o limite da Bot API é maior do que no Worker: download direto do arquivo
pela URL do getFile sem o teto de 20 MB que atrapalha o Worker, e envio de até
50 MB. Arquivos acima de 50 MB no envio são avisados, não quebram o job.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib import request, parse

BOT_TOKEN = os.environ["BOT_TOKEN"]
PAYLOAD = json.loads(os.environ["PAYLOAD"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_UPLOAD = 49 * 1024 * 1024  # a Bot API aceita até 50 MB; deixo uma folga

CHAT_ID = PAYLOAD["chat_id"]
THREAD_ID = PAYLOAD.get("thread_id")
OP = PAYLOAD["op"]
ARGS = PAYLOAD.get("args") or {}
FILES = PAYLOAD.get("files") or []


# ---------- Telegram ----------

def _multipart(fields, file_field=None, file_path=None, file_name=None, file_mime=None):
    boundary = "----tagbot" + os.urandom(8).hex()
    out = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode()
    if file_field:
        data = Path(file_path).read_bytes()
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        ).encode()
        out += f"Content-Type: {file_mime}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def api(method, fields, file_field=None, file_path=None, file_name=None, file_mime="application/octet-stream"):
    body, ctype = _multipart(fields, file_field, file_path, file_name, file_mime)
    req = request.Request(f"{API}/{method}", data=body, headers={"Content-Type": ctype})
    try:
        with request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": str(e)}


def say(text):
    api("sendMessage", {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "text": text[:4000]})


def send_audio(path, name, title=None, performer=None, thumb=None):
    size = Path(path).stat().st_size
    if size > MAX_UPLOAD:
        say(f"⚠️ {name}: ficou com {size // (1024*1024)} MB, acima do limite de 50 MB do bot. Não enviei.")
        return False
    fields = {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "title": title, "performer": performer}
    r = api("sendAudio", fields, "audio", path, name, "audio/mpeg")
    if not r.get("ok"):
        say(f"❌ {name}: {r.get('description')}")
    return r.get("ok", False)


def send_document(path, name):
    size = Path(path).stat().st_size
    if size > MAX_UPLOAD:
        say(f"⚠️ {name}: {size // (1024*1024)} MB, acima do limite de 50 MB. Não enviei.")
        return False
    r = api(
        "sendDocument",
        {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "disable_content_type_detection": "true"},
        "document", path, name, "application/octet-stream",
    )
    if not r.get("ok"):
        say(f"❌ {name}: {r.get('description')}")
    return r.get("ok", False)


def send_photo(path, name, caption=None):
    r = api("sendPhoto", {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "caption": caption},
            "photo", path, name, "image/png")
    if not r.get("ok"):
        say(f"❌ {name}: {r.get('description')}")
    return r.get("ok", False)


def download(file_id, dest):
    r = json.loads(request.urlopen(f"{API}/getFile?file_id={parse.quote(file_id)}", timeout=120).read())
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "getFile falhou"))
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{r['result']['file_path']}"
    with request.urlopen(url, timeout=600) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


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
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


MIME = {"mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4", "ogg": "audio/ogg", "opus": "audio/opus", "wav": "audio/wav"}


# A capa de um MP3 é um "stream de vídeo" (imagem anexada). Mapeamos o áudio e,
# se existir, a imagem (disp:attached_pic), copiando a imagem sem recomprimir.
# O "?" deixa o -map opcional, para não falhar em arquivo sem capa. Vale para
# os formatos que guardam capa embutida (mp3, m4a, flac); wav/ogg/opus não levam.
def cover_map(fmt):
    if fmt in ("mp3", "m4a", "aac", "flac"):
        return ["-map", "0:a", "-map", "0:v:0?", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    return ["-map", "0:a"]


def encode_args(fmt, bitrate):
    """Argumentos de codec do ffmpeg para o formato de saída."""
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


# ---------- operações ----------

def op_converter(tmp):
    fmt = ARGS.get("fmt", "mp3")
    bitrate = ARGS.get("bitrate")
    ok = 0
    for i, f in enumerate(FILES):
        src = download(f["file_id"], tmp / f"in{i}{Path(f['name']).suffix or '.bin'}")
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
            ok += send_document(dst, name)  # outros formatos vão como arquivo, sem recompressão do Telegram
    say(f"✅ /converter para {fmt}{(' ' + str(bitrate) + 'k') if bitrate and fmt != 'flac' and fmt != 'wav' else ''}: {ok}/{len(FILES)} arquivo(s).")


def op_cortar(tmp):
    start = ARGS.get("start", 0)
    end = ARGS.get("end")
    fade = ARGS.get("fade")
    ok = 0
    for i, f in enumerate(FILES):
        src = download(f["file_id"], tmp / f"in{i}.mp3")
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
        src = download(f["file_id"], tmp / f"in{i}.mp3")
        name = out_name(f, "mp3")
        dst = tmp / name
        # loudnorm em dois passos: mede e depois aplica, para um volume mais previsível
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
    paths = []
    chapters = []
    pos = 0.0
    cover = None  # capa da 1ª faixa que tiver uma; vai no arquivo final
    for i, f in enumerate(FILES):
        src = download(f["file_id"], tmp / f"in{i}.mp3")
        # as faixas do concat saem só com áudio (a capa aqui atrapalharia a junção)
        norm = tmp / f"n{i}.mp3"  # tudo para 44.1k estéreo 320k, senão o concat falha com formatos mistos
        run(["ffmpeg", "-y", "-i", str(src), "-map", "0:a", "-ar", "44100", "-ac", "2",
             "-c:a", "libmp3lame", "-b:a", "320k", str(norm)])
        paths.append(norm)
        if cover is None:
            pic = tmp / f"cover{i}.jpg"
            # extrai a capa, se houver (código 0 = achou e gravou a imagem)
            r = subprocess.run(["ffmpeg", "-y", "-i", str(src), "-an", "-map", "0:v:0", "-c:v", "copy", str(pic)],
                               capture_output=True, text=True)
            if r.returncode == 0 and pic.exists() and pic.stat().st_size > 100:
                cover = pic
        dur = float(f.get("duration") or 0)
        if not dur:
            pr = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                                 "-of", "default=nw=1:nk=1", str(norm)], capture_output=True, text=True)
            dur = float(pr.stdout.strip() or 0)
        title = f.get("title") or stem(f["name"]) or f"Faixa {i+1}"
        chapters.append((pos, pos + dur, title))
        pos += dur

    listfile = tmp / "list.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in paths))

    # capítulos (metadata do ffmpeg): players mostram cada faixa dentro do arquivo único
    meta = tmp / "chapters.txt"
    lines = [";FFMETADATA1", f"title={name}"]
    for start, end, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(start*1000)}", f"END={int(end*1000)}", f"title={title}"]
    meta.write_text("\n".join(lines) + "\n")

    out = tmp / f"{name}.{fmt}"
    # entradas: 0 = áudio juntado, 1 = capítulos, 2 = capa (se houver)
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-i", str(meta)]
    with_cover = cover is not None and fmt in ("mp3", "m4a", "flac")
    if with_cover:
        cmd += ["-i", str(cover), "-map", "0:a", "-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-map_metadata", "1", *encode_args(fmt, None), str(out)]
    try:
        run(cmd)
    except RuntimeError as e:
        say(f"❌ Não consegui juntar: {e}")
        return
    sent = send_audio(out, f"{name}.{fmt}", name, FILES[0].get("performer")) if fmt == "mp3" else send_document(out, f"{name}.{fmt}")
    if sent:
        say(f"🔗 /juntar: {len(FILES)} faixas em um arquivo de {fmt_dur(pos)}, com capítulos.")


def op_espectro(tmp):
    ok = 0
    for i, f in enumerate(FILES):
        src = download(f["file_id"], tmp / f"in{i}.mp3")
        png = tmp / f"spec{i}.png"
        try:
            run(["ffmpeg", "-y", "-i", str(src),
                 "-lavfi", "showspectrumpic=s=1280x540:legend=1:gain=3", str(png)])
        except RuntimeError as e:
            say(f"❌ {f['name']}: {e}")
            continue
        # um corte brusco de energia no topo costuma indicar MP3 remontado de fonte pior
        cap = (f"🎛 {stem(f['name']) or f.get('title') or 'Áudio'}\n"
               "Se a energia some numa linha reta bem abaixo do topo, o arquivo foi convertido de uma fonte de qualidade menor.")
        ok += send_photo(png, f"espectro{i}.png", cap)
    say(f"🎛 /espectro: {ok}/{len(FILES)} imagem(ns).")


OPS = {
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
