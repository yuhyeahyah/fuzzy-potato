#!/usr/bin/env python3
"""Telegram /twitch: TwitchTracker -> VodRecovery CLI -> Telegram.

Started from .github/workflows/twitch-tools.yml (repository_dispatch: twitch-tool).
External VodRecovery is installed to a temporary checkout by the workflow.
"""
import json
import math
import shutil
import zipfile
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

PAYLOAD = json.loads(os.environ["PAYLOAD"])
ARGS = PAYLOAD.get("args") or {}
CHAT_ID = int(PAYLOAD["chat_id"])
THREAD_ID = PAYLOAD.get("thread_id")
BOT_TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
VOD_SCRIPT = Path(os.environ.get("VODRECOVERY_SCRIPT", "/tmp/vodrecovery/vod_recovery.py"))
VOD_SETTINGS = Path(os.environ.get("VODRECOVERY_CONFIG", "/tmp/vodrecovery/config/settings.json"))
MAX_BOT_BYTES = 49 * 1024 * 1024
# Limites conservadores: o bot NÃO herda o Premium do dono.
# MTProto permite ~2000 MiB por bot e ~4000 MiB por usuário Premium.
# Deixar margem evita erros de FILE_PARTS_INVALID.
MAX_MT_BYTES = 1950 * 1024 * 1024
MAX_PREMIUM_BYTES = 3900 * 1024 * 1024
MAX_PARTS = 24                 # apenas quando "parte=" é especificado
MAX_ZIP_VOLUMES = 120         # proteção contra envios absurdamente longos
QUALITY_CHOICES = {"best", "source", "chunked", "2160p60", "2160p30", "2160p20",
                   "1440p60", "1440p30", "1440p20", "1080p60", "1080p30", "1080p20",
                   "720p60", "720p30", "720p20", "480p60", "480p30", "360p60",
                   "360p30", "160p60", "160p30"}


def say(message):
    data = {"chat_id": CHAT_ID, "text": message[:4000],
            "disable_web_page_preview": True}
    if THREAD_ID:
        data["message_thread_id"] = int(THREAD_ID)
    try:
        r = requests.post(f"{API}/sendMessage", json=data, timeout=25)
        if not r.ok:
            print(f"Telegram sendMessage HTTP {r.status_code}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Telegram indisponível: {e}", file=sys.stderr)


def hms(seconds):
    seconds = int(seconds)
    hh, rem = divmod(seconds, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def parse_hms(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2,3}:[0-5]\d:[0-5]\d", value):
        raise ValueError("Horário inválido; use HH:MM:SS (ex.: 01:20:00).")
    hh, mm, ss = map(int, value.split(":"))
    return hh * 3600 + mm * 60 + ss


def valid_tracker_url(raw):
    u = urlsplit(str(raw or ""))
    if u.scheme != "https" or u.username or u.password or u.port or u.hostname not in ("twitchtracker.com", "www.twitchtracker.com"):
        return False
    return bool(re.fullmatch(r"/[A-Za-z0-9_]{1,30}/streams/\d{6,20}/?", u.path))


def _safe_error_context(stdout, stderr):
    """Traz a CAUSA provável, não só a última linha ('Download failed').

    Nunca publica URLs temporárias de playlists no Telegram/log.
    """
    combined = "\n".join((stdout or "", stderr or ""))
    # Remover URLs (podem conter assinaturas/tokens efêmeros).
    combined = re.sub(r"https?://[^\s<>\"']+", "[URL ocultada]", combined, flags=re.I)
    combined = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", combined)
    lines = [x.strip()[:220] for x in combined.splitlines() if x.strip()]
    interesting = [x for x in lines if re.search(
        r"(?i)error|failed|forbidden|not found|denied|HTTP|403|404|410|invalid|unavailable|segment|timeout|unable|no such file",
        x)]
    selected = interesting[-6:] if interesting else lines[-6:]
    if not selected:
        return "VODRecovery falhou, mas não forneceu detalhes"
    return " | ".join(selected)[-900:]


def invoke_vod(args, timeout=600):
    cmd = [sys.executable, str(VOD_SCRIPT), *args]
    # Nunca passa o link como comando de shell.
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, errors="replace")
    if p.returncode:
        detail = _safe_error_context(p.stdout, p.stderr)
        raise RuntimeError(f"VODRecovery (código {p.returncode}): {detail}")
    return p.stdout


def meta_for_url(url):
    raw = invoke_vod(["--url", url, "--metadata"], timeout=750)
    data = json.loads(raw)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    if not data.get("m3u8_url"):
        raise RuntimeError("O VODRecovery não encontrou um vídeo recuperável para esse link.")
    return data


def pick_quality(requested, available):
    if not isinstance(available, list) or not available:
        if requested == "auto":
            return "best"
        return requested
    avail = [str(v).lower() for v in available]
    if requested != "auto":
        normalized = "chunked" if requested == "source" else requested
        if normalized not in avail and requested != "best":
            raise RuntimeError("Qualidade indisponível. Disponíveis: " + ", ".join(avail))
        return requested
    # Automático: prioriza qualidade até 720p, para reduzir tamanho e custo de envio.
    for q in ("720p60", "720p30", "720p20", "480p60", "480p30", "360p60", "360p30", "160p60", "160p30"):
        if q in avail:
            return q
    return "best"


def config_output(folder, downloader="ffmpeg", unmute=True):
    config = json.loads(VOD_SETTINGS.read_text(encoding="utf-8"))
    config["DEFAULT_DIRECTORY"] = str(folder)
    config["DEFAULT_VIDEO_FORMAT"] = ".mp4"
    config["DEFAULT_DOWNLOADER"] = downloader
    config["USE_PROGRESS_BAR"] = False
    # Alguns VODs apontam para trechos "-muted" sem uma versão "-unmuted"
    # recuperável. Nesse caso tentamos a playlist ORIGINAL, sem substituição.
    config["UNMUTE_VIDEO"] = bool(unmute)
    # O próprio VODRecovery trata mute; validar todos os segmentos adicionaria
    # centenas de chamadas e não garantiria a recuperação de trechos perdidos.
    config["CHECK_SEGMENTS"] = False
    VOD_SETTINGS.write_text(json.dumps(config, indent=2), encoding="utf-8")


def video_probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True, timeout=30)
    try:
        return max(0, int(float(p.stdout.strip())))
    except (ValueError, OverflowError):
        return 0


def download_slice(m3u8_url, quality, start, end, folder):
    """Tenta o download normal e, em seguida, a playlist sem reescrever mute.

    O primeiro modo deixa o VODRecovery aplicar suas correções. Se ambas as
    alternativas de download falharem, testamos o M3U8 original (que pode ter
    áudio silenciado). Não inventa segmentos que já não existam no CDN.
    """
    folder.mkdir(parents=True, exist_ok=True)
    args = ["--m3u8", m3u8_url, "--start", hms(start), "--end", hms(end),
            "--quality", quality]
    errors = []
    tries = (("ffmpeg", True), ("yt-dlp", True),
             ("ffmpeg", False), ("yt-dlp", False))
    for idx, (downloader, unmute) in enumerate(tries):
        mode = "tratado" if unmute else "original"
        output = folder / f"{idx + 1:02d}_{downloader.replace('-', '')}_{mode}"
        output.mkdir(parents=True, exist_ok=True)
        config_output(output, downloader=downloader, unmute=unmute)
        if idx == 1:
            say("⚠️ FFmpeg falhou; tentando yt-dlp…")
        elif idx == 2:
            say("⚠️ Não foi possível baixar a playlist tratada. Testando a playlist original, preservando eventuais trechos silenciados…")
        elif idx == 3:
            say("⚠️ Tentando yt-dlp também na playlist original…")
        try:
            invoke_vod(args, timeout=160 * 60)
            videos = sorted(output.rglob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not videos or videos[0].stat().st_size <= 0:
                raise RuntimeError("Nenhum MP4 foi produzido")
            duration = video_probe(videos[0])
            if duration < 1:
                raise RuntimeError("MP4 gerado não é reproduzível (ffprobe sem duração)")
            if duration + 15 < end - start:
                say(f"⚠️ Trecho obtido com apenas {hms(duration)} de {hms(end-start)} solicitados; pode ter segmentos ausentes.")
            say(f"✅ Download concluído: método {downloader}, playlist {mode} ({hms(duration)}).")
            return videos[0]
        except Exception as exc:
            errors.append(f"{downloader}/{mode}: {str(exc)[:270]}")
            shutil.rmtree(output, ignore_errors=True)
            if re.search(r"code -11\b|Segmentation fault|SIGSEGV", str(exc)):
                raise RuntimeError("ffmpeg do runner deu segfault (SIGSEGV): o binário está quebrado, "
                                   "não é problema do VOD. " + errors[-1]) from exc
    raise RuntimeError("Todas as 4 tentativas falharam. " + " | ".join(errors))



# ---------------------------------------------------------------------------
#  Telegram: o bot envia ate 1950 MiB (MTProto), a conta Premium opcional
#  ate 3900 MiB. A conta Premium nao e o bot: suas mensagens aparecem
#  como enviadas pela conta, que precisa ter acesso ao chat/topico.
# ---------------------------------------------------------------------------

_BOT_CLIENT = None
_USER_CLIENT = None


def bot_client():
    global _BOT_CLIENT
    if _BOT_CLIENT is None:
        api_id, api_hash = os.getenv("TG_API_ID", ""), os.getenv("TG_API_HASH", "")
        if not api_id or not api_hash:
            raise RuntimeError("Configure TG_API_ID e TG_API_HASH para enviar arquivos acima de 49 MB.")
        from pyrogram import Client
        _BOT_CLIENT = Client("twitchvod-bot", api_id=int(api_id), api_hash=api_hash,
                             bot_token=BOT_TOKEN, in_memory=True, no_updates=True)
        _BOT_CLIENT.start()
    return _BOT_CLIENT


def select_uploader():
    """Retorna (cliente de conta Premium ou None, limite bytes, descricao).

    A presenca de Premium no DONO do bot nao altera o limite do BOT.
    TG_PREMIUM_SESSION habilita explicitamente a conta de usuario separada.
    """
    global _USER_CLIENT
    session = os.getenv("TG_PREMIUM_SESSION", "").strip()
    if not session:
        return None, MAX_MT_BYTES, "bot (ate ~2 GB por arquivo)"
    if CHAT_ID > 0:
        # Chats privados com o bot nao sao o mesmo chat que o usuario Premium
        # tera com o destinatario. Usar bot para preservar o destinatario.
        say("ℹ️ Neste chat privado com o bot, envio pelo proprio bot (~2 GB). "
            "Para enviar 4 GB via conta Premium, use um grupo onde essa conta seja membro.")
        return None, MAX_MT_BYTES, "bot (ate ~2 GB por arquivo)"
    api_id, api_hash = os.getenv("TG_API_ID", ""), os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        say("⚠️ TG_PREMIUM_SESSION definido, mas faltam TG_API_ID/TG_API_HASH. Usando bot (~2 GB).")
        return None, MAX_MT_BYTES, "bot (ate ~2 GB por arquivo)"
    try:
        from pyrogram import Client
        _USER_CLIENT = Client("twitchvod-premium", api_id=int(api_id), api_hash=api_hash,
                              session_string=session, in_memory=True, no_updates=True)
        _USER_CLIENT.start()
        user = _USER_CLIENT.get_me()
        if not getattr(user, "is_premium", False):
            say("⚠️ A conta TG_PREMIUM_SESSION nao esta identificada como Premium. Usando bot (~2 GB).")
            _USER_CLIENT.stop()
            _USER_CLIENT = None
            return None, MAX_MT_BYTES, "bot (ate ~2 GB por arquivo)"
        # Confirmar ao menos a leitura do destino; enviar exige permissoes adicionais.
        _USER_CLIENT.get_chat(CHAT_ID)
        return _USER_CLIENT, MAX_PREMIUM_BYTES, "conta Premium (~4 GB por arquivo)"
    except Exception as exc:
        say("⚠️ Conta Premium indisponivel neste chat. Usando bot (~2 GB). " + str(exc)[:140])
        try:
            if _USER_CLIENT is not None:
                _USER_CLIENT.stop()
        except Exception:
            pass
        _USER_CLIENT = None
        return None, MAX_MT_BYTES, "bot (ate ~2 GB por arquivo)"


def stop_clients():
    global _BOT_CLIENT, _USER_CLIENT
    for c in (_BOT_CLIENT, _USER_CLIENT):
        if c is not None:
            try:
                c.stop()
            except Exception:
                pass
    _BOT_CLIENT = None
    _USER_CLIENT = None


def send_video(path, caption, premium_client=None):
    """Envia MP4 sem compactar; 50 MB HTTP ou MTProto por bot/conta."""
    sz = path.stat().st_size
    cap = caption[:1024]
    if premium_client is not None:
        args = dict(chat_id=CHAT_ID, video=str(path), file_name=path.name,
                    duration=video_probe(path), caption=cap, supports_streaming=True)
        if THREAD_ID:
            args["message_thread_id"] = int(THREAD_ID)
        premium_client.send_video(**args)
        return
    if sz <= MAX_BOT_BYTES:
        data = {"chat_id": CHAT_ID, "caption": cap, "supports_streaming": "true"}
        if THREAD_ID:
            data["message_thread_id"] = int(THREAD_ID)
        with path.open("rb") as fp:
            response = requests.post(f"{API}/sendVideo", data=data,
                                     files={"video": (path.name, fp, "video/mp4")}, timeout=900)
        js = response.json()
        if not js.get("ok"):
            raise RuntimeError(str(js.get("description", "Telegram recusou o video")))
        return
    if sz > MAX_MT_BYTES:
        raise RuntimeError("O MP4 excede o limite seguro do bot: precisa criar ZIPs.")
    kwargs = {"chat_id": CHAT_ID, "video": str(path), "file_name": path.name,
              "duration": video_probe(path), "caption": cap,
              "supports_streaming": True}
    if THREAD_ID:
        kwargs["message_thread_id"] = int(THREAD_ID)
    bot_client().send_video(**kwargs)


def send_zip_volume(path, caption, premium_client=None):
    """Envia volumes como documentos (nao como video)."""
    kwargs = {"chat_id": CHAT_ID, "document": str(path), "file_name": path.name,
              "caption": caption[:1024]}
    if THREAD_ID:
        kwargs["message_thread_id"] = int(THREAD_ID)
    (premium_client or bot_client()).send_document(**kwargs)


class ZipVolumeWriter:
    """Escritor nao-seekable que corta um ZIP valido em .zip.001, .zip.002...

    A concatenacao dos volumes reconstitui um ZIP comum. Nenhum dado e
    descartado e o zipfile usa ZIP64 para suportar videos maiores que 4 GB.
    Os volumes individuais NAO sao ZIPs independentes.
    """
    def __init__(self, archive_path, volume_size):
        self.archive_path = Path(archive_path)
        self.volume_size = int(volume_size)
        if self.volume_size < 128:
            raise ValueError("volume_size demasiado pequeno")
        self.parts = []
        self.current = None
        self.part_bytes = 0
        self.total_bytes = 0

    def tell(self):
        return self.total_bytes

    def seekable(self):
        return False

    def writable(self):
        return True

    def write(self, buffer):
        data = memoryview(buffer)
        remaining = len(data)
        offset = 0
        while remaining:
            if self.current is None or self.part_bytes >= self.volume_size:
                if self.current is not None:
                    self.current.close()
                name = self.archive_path.with_name(f"{self.archive_path.name}.{len(self.parts) + 1:03d}")
                self.current = name.open("wb")
                self.parts.append(name)
                self.part_bytes = 0
            n = min(remaining, self.volume_size - self.part_bytes)
            written = self.current.write(data[offset:offset + n])
            if written != n:
                raise OSError("Falha ao gravar parte ZIP completa")
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

    def __exit__(self, typ, val, tb):
        self.close()


def create_split_zip(video_path, output_dir, volume_bytes):
    """Cria ZIP multivolume sem re-encode e sem compressao do MP4.

    Nao faz copia intermediaria do ZIP inteiro, apenas grava cada volume.
    Requer espaco livre adicional aproximadamente igual ao tamanho do MP4.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    file_size = video_path.stat().st_size
    approx_parts = math.ceil((file_size + 65536) / volume_bytes)
    if approx_parts > MAX_ZIP_VOLUMES:
        raise RuntimeError(f"O video exigiria cerca de {approx_parts} ZIPs. Use inicio/fim ou qualidade menor.")
    disk = shutil.disk_usage(output_dir)
    if disk.free < file_size + 128 * 1024 * 1024:
        raise RuntimeError("Espaco insuficiente no GitHub Actions para preparar o ZIP: "
                           f"livre {disk.free / 1024**3:.1f} GiB, "
                           f"necessario ~{(file_size + 128 * 1024 * 1024) / 1024**3:.1f} GiB. "
                           "Reduza o intervalo com inicio/fim ou a qualidade.")
    archive_path = output_dir / (video_path.stem + ".zip")
    writer = ZipVolumeWriter(archive_path, volume_bytes)
    with writer:
        with zipfile.ZipFile(writer, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            # Formato ZIP64; preservar filename mas nunca caminhos absolutos.
            archive.write(video_path, arcname=video_path.name)
    result = list(writer.parts)
    if not result or any(f.stat().st_size > volume_bytes for f in result):
        raise RuntimeError("Falha ao finalizar as partes do ZIP")
    return result


def deliver(path, caption, premium_client, volume_limit):
    size = path.stat().st_size
    if size <= volume_limit:
        say(f"📤 Enviando MP4 inteiro ({size / 1024**3:.2f} GiB).")
        send_video(path, caption, premium_client)
        return 1

    count = math.ceil((size + 65536) / volume_limit)
    say(f"📦 Video com {size / 1024**3:.2f} GiB; criando ~{count} volumes ZIP numerados. "
        "Baixe TODOS para reconstruir o MP4."
        " Os volumes nao funcionam isoladamente.")
    zip_dir = path.parent / "zip_volumes"
    parts = create_split_zip(path, zip_dir, volume_limit)
    path.unlink()  # libera espaco antes de comecar a subir os volumes
    total = len(parts)
    for idx, part in enumerate(parts, start=1):
        part_cap = (f"📦 Twitch ZIP {idx:03d}/{total:03d} | "
                    f"{part.stat().st_size / 1024**3:.2f} GiB\n" + caption +
                    "\nPara reconstruir: deixe todos os arquivos na mesma pasta, "
                    "junte .zip.001 + .zip.002 + ... e extraia o .zip final.")
        say(f"⬆️ Enviando ZIP {idx:03d}/{total:03d}…")
        send_zip_volume(part, part_cap, premium_client)
        part.unlink(missing_ok=True)
    return total


def main():
    url = str(ARGS.get("url") or "").strip()
    if not valid_tracker_url(url):
        raise ValueError("Link invalido. Use https://twitchtracker.com/canal/streams/1234567890")
    quality_req = str(ARGS.get("quality") or "auto").lower()
    if quality_req not in QUALITY_CHOICES and quality_req != "auto":
        raise ValueError("Qualidade invalida.")
    selected_part = ARGS.get("part_minutes")
    if selected_part is not None and not 2 <= int(selected_part) <= 30:
        raise ValueError("parte deve ficar entre 2 e 30 minutos.")
    say("🔍 Twitch: consultando VOD com VODRecovery…")
    meta = meta_for_url(url)
    quality = pick_quality(quality_req, meta.get("available_qualities"))
    dur = meta.get("duration_seconds")
    if not dur:
        mins = meta.get("tracker_duration_minutes")
        dur = float(mins) * 60 if mins else None
    dur = max(0, int(float(dur))) if dur is not None else None
    streamer = re.sub(r"[^A-Za-z0-9_]", "", str(meta.get("streamer") or "streamer"))[:30]
    vid = re.sub(r"\D", "", str(meta.get("video_id") or urlsplit(url).path.split("/")[-1]))
    available = ", ".join(meta.get("available_qualities") or []) or "nao informadas"
    if ARGS.get("info") is True:
        say(f"🎥 Twitch VOD\nCanal: {streamer}\nID: {vid}\nTitulo: {meta.get('title') or '(nao informado)'}\n"
            f"Duracao: {hms(dur) if dur else 'desconhecida'}\nQualidades: {available}\n"
            "O envio padrao e pelo bot (~2 GB/arquivo), salvo se houver conta Premium configurada.")
        return

    start = parse_hms(ARGS.get("start") or "00:00:00")
    stop = parse_hms(ARGS["end"]) if ARGS.get("end") else dur
    if stop is None:
        raise ValueError("Nao consegui identificar a duracao. Informe fim=HH:MM:SS.")
    if dur is not None:
        stop = min(stop, dur)
    if start >= stop:
        raise ValueError("O inicio precisa ser anterior ao fim do VOD.")

    # "parte=10" mantem a opcao anterior de dividir por minutos.
    # Sem "parte": tenta o VOD selecionado inteiro e so divide por TAMANHO.
    part_mins = int(selected_part) if selected_part is not None else None
    chunks = math.ceil((stop - start) / (part_mins * 60)) if part_mins else 1
    if chunks > MAX_PARTS:
        raise ValueError(f"O trecho exige {chunks} partes de tempo. Limite {MAX_PARTS}; use inicio/fim.")

    premium_client, limit, mode = select_uploader()
    say(f"🎬 {streamer} | {meta.get('title') or 'VOD'}\n"
        f"Duracao selecionada: {hms(stop - start)}\nQualidade: {quality}\n"
        f"Envio: {mode}\n"
        + (f"Divisao manual por tempo: {chunks} x {part_mins} min + ZIP se necessario."
           if part_mins else "Divisao automatica por tamanho: MP4 inteiro ou ZIP .001/.002…"))

    total_sent = 0
    failures = []
    with tempfile.TemporaryDirectory(prefix="twitch-vod-") as td:
        base = Path(td)
        for i in range(chunks):
            a = start + i * part_mins * 60 if part_mins else start
            b = min(stop, a + part_mins * 60) if part_mins else stop
            folder = base / f"periodo_{i + 1:03d}"
            say(f"⏳ Twitch: baixando {i + 1}/{chunks} ({hms(a)}–{hms(b)})…")
            try:
                path = download_slice(meta["m3u8_url"], quality, a, b, folder)
                out_name = (f"{streamer}_{vid}.mp4" if not part_mins else
                            f"{streamer}_{vid}_trecho_{i + 1:03d}.mp4")
                path = path.rename(folder / out_name)
                caption = f"🎥 {streamer} | {hms(a)}–{hms(b)} | {quality}\n{url}"
                total_sent += deliver(path, caption, premium_client, limit)
            except Exception as exc:
                failures.append(i + 1)
                say(f"❌ Twitch: periodo {i + 1}/{chunks} falhou: {str(exc)[:1200]}")
            finally:
                shutil.rmtree(folder, ignore_errors=True)
    say(f"{'✅' if not failures else '⚠️'} /twitch: {total_sent} arquivo(s) enviados. "
        + (f"Periodos com erro: {failures}" if failures else "Concluido."))
    if failures:
        raise RuntimeError(f"Periodos com erro: {failures}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        say(f"❌ /twitch: {str(exc)[:600]}")
        raise
    finally:
        stop_clients()
