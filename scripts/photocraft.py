#!/usr/bin/env python3
"""PhotoCraft Telegram integration: single-file edition 0.2.2.

Contains installer, bounded JSON-lines bridge, native tests, Telegram job and
Worker patcher. Runtime: Python 3.12+, Pillow, requests, and optionally Kurigram/
TgCrypto for large Telegram files. The native PhotoCraft CLI is still required.

Examples:
  python scripts/photocraft.py --help
  python scripts/photocraft.py selftest
  python scripts/photocraft.py check --dir .photocraft --out photocraft-results
  python scripts/photocraft.py run --dir .photocraft --out photocraft-results
  python scripts/photocraft.py patch-worker --input worker.js --output worker-new.js

`selftest` exercises the adapter with a transport fixture; it is NOT rendering.
`test`/`check` use the REAL engine. No missing/failed native test is a success.
Secrets and PHOTOCRAFT_ALLOWED_USERS are NOT embedded or reset by this file.
The original PhotoCraft engine remains pinned to 0.23.1, without silent upgrade.
"""
from __future__ import annotations

# Source merged from photocraft-bot-lab; see SOURCES.json for input hashes.
INTEGRATION_VERSION = '0.2.2-single-file-cli-discovery'


# ============================================================================
# Incorporated from photocraft_bridge.py
# ============================================================================
import json
import math
import os
import queue
import re
import shutil
import signal
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

MAX_EDGE = 12000
MAX_PIXELS = 32_000_000
MAX_ASSETS = 24
MAX_STEPS = 160
MAX_PLAN_BYTES = 48_000
MAX_LINE = 8 * 1024 * 1024
EXPORTS = {"png", "jpg", "webp", "psd", "pcraft"}
INPUTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".psd", ".psb", ".pcraft"}
# Broad editing families; no scripts, plugins, preferences, network or ambient IO.
PREFIXES = ("image.adjustments.", "layer.", "filter.", "select.", "type.", "shape.", "path.", "channel.", "paint.")
EXACT = {"edit.undo", "edit.redo", "edit.fill", "edit.stroke", "edit.freeTransform", "edit.transform.scale", "edit.transform.rotate", "edit.transform.flipHorizontal", "edit.transform.flipVertical", "image.imageSize", "image.canvasSize", "image.crop", "tools.setColors", "document.inspect", "document.pixel"}
BAD_ID = re.compile(r"(?:export|import|install|reload|presets|replacecontents|relink|editcontents|savecontents|convertto(layers|linked)|linked|script|automate|dataset|network)", re.I)
BAD_KEY = re.compile(r"(?:path|directory|filename|file_id|filepath|url|uri|token|password|script|command|steps|plugin|executable|argv|environment|env|root)$", re.I)

class PhotoError(RuntimeError):
    pass


def finite_int(value: Any, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not low <= value <= high:
        raise PhotoError(f"{label}: esperado inteiro entre {low} e {high}.")
    return int(value)


def validate_tree(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = budget if budget is not None else [0]
    budget[0] += 1
    if depth > 12 or budget[0] > 12000:
        raise PhotoError("Parametros JSON profundos ou grandes demais.")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or len(k) > 120 or k in {"__proto__", "prototype", "constructor"} or BAD_KEY.search(k):
                raise PhotoError("Parametro de arquivo, codigo ou configuracao nao permitido: " + str(k)[:100])
            validate_tree(v, depth + 1, budget)
    elif isinstance(value, list):
        for v in value:
            validate_tree(v, depth + 1, budget)
    elif isinstance(value, str):
        if len(value) > 8000 or "\x00" in value:
            raise PhotoError("Texto grande demais ou com NUL.")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise PhotoError("Numero nao finito.")
    elif value is not None and not isinstance(value, (bool, int)):
        raise PhotoError("Tipo JSON invalido.")


def command_allowed(command: Any) -> bool:
    return bool(isinstance(command, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{1,119}", command)
                and (command.startswith(PREFIXES) or command in EXACT) and not BAD_ID.search(command))


def validate_plan(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PhotoError("Receita deve ser um objeto JSON.")
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise PhotoError("JSON invalido.") from exc
    if len(raw) > MAX_PLAN_BYTES:
        raise PhotoError("Receita excede 48 KB.")
    if set(value) - {"schema", "canvas", "base", "assets", "steps"}:
        raise PhotoError("Campo desconhecido na receita.")
    if value.get("schema", 1) != 1:
        raise PhotoError("Versao de receita nao suportada.")
    assets = value.get("assets", [])
    if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
        raise PhotoError("Use ate 24 arquivos por projeto.")
    for a in assets:
        if not isinstance(a, dict) or set(a) - {"file_id", "name", "size"}:
            raise PhotoError("Arquivo invalido.")
        if not isinstance(a.get("file_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,1024}", a["file_id"]):
            raise PhotoError("file_id invalido.")
        name = a.get("name", "input.png")
        if not isinstance(name, str) or len(name) > 250 or "\x00" in name or Path(name).suffix.lower() not in INPUTS:
            raise PhotoError("Formato de entrada nao permitido.")
        finite_int(a.get("size", 0), 0, 200 * 1024 * 1024, "Tamanho do arquivo")
    base = value.get("base")
    canvas = value.get("canvas")
    if base is not None:
        finite_int(base, 1, len(assets), "Arquivo base")
        if canvas is not None:
            raise PhotoError("Use base OU canvas, nao os dois.")
    elif canvas is not None:
        if not isinstance(canvas, dict) or set(canvas) - {"width", "height", "background"}:
            raise PhotoError("Canvas invalido.")
        w = finite_int(canvas.get("width"), 1, MAX_EDGE, "Largura")
        h = finite_int(canvas.get("height"), 1, MAX_EDGE, "Altura")
        if w * h > MAX_PIXELS:
            raise PhotoError("Canvas excede 32 megapixels.")
        if canvas.get("background", "white") not in {"white", "transparent", "black"}:
            raise PhotoError("Fundo deve ser white, black ou transparent.")
    else:
        raise PhotoError("Crie um canvas ou escolha o arquivo base.")
    steps = value.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise PhotoError("Use ate 160 operacoes por projeto.")
    for step in steps:
        if not isinstance(step, dict):
            raise PhotoError("Operacao deve ser objeto JSON.")
        if "command" in step:
            if set(step) - {"command", "params"} or not command_allowed(step["command"]):
                raise PhotoError("Comando fora do conjunto de edicao permitido: " + str(step.get("command")))
            if not isinstance(step.get("params", {}), dict):
                raise PhotoError("params deve ser um objeto.")
            validate_tree(step.get("params", {}))
        elif step.get("method") in {"place", "replace"}:
            if set(step) - {"method", "asset", "params"}:
                raise PhotoError("Campo desconhecido na insercao de arquivo.")
            finite_int(step.get("asset"), 1, len(assets), "Arquivo da operacao")
            p = step.get("params", {})
            allowed = {"scale", "fit", "center"} if step["method"] == "place" else {"layer"}
            if not isinstance(p, dict) or set(p) - allowed:
                raise PhotoError("Parametros de colocacao/substituicao invalidos.")
            validate_tree(p)
            if "fit" in p and type(p["fit"]) is not bool:
                raise PhotoError("fit deve ser booleano.")
            if "scale" in p and (isinstance(p["scale"], bool) or not isinstance(p["scale"], (int, float)) or not 0.01 <= p["scale"] <= 1000):
                raise PhotoError("Escala deve estar entre 0.01 e 1000 por cento.")
            if "center" in p:
                if not isinstance(p["center"], list) or len(p["center"]) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not -MAX_EDGE <= v <= 2*MAX_EDGE for v in p["center"]):
                    raise PhotoError("Centro deve ter duas coordenadas numericas.")
        else:
            raise PhotoError("Operacao desconhecida.")
    return json.loads(raw)


def binary_path() -> str:
    configured = os.environ.get("PHOTOCRAFT_BIN", "photocraft-cli")
    found = shutil.which(configured)
    if not found:
        raise PhotoError("photocraft-cli nao instalado. Rode o workflow photocraft-check primeiro.")
    return str(Path(found).resolve())


class PhotoRPC:
    """A separate process per job; JSON is never interpreted as a shell command."""
    def __init__(self, read_root: Path, write_root: Path, *, command: list[str] | None = None,
                 timeout: float = 90, total_timeout: float = 480, memory_mb: int = 4096):
        self.read_root, self.write_root = Path(read_root).resolve(), Path(write_root).resolve()
        self.read_root.mkdir(parents=True, exist_ok=True)
        self.write_root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.deadline = time.monotonic() + total_timeout
        self.seq = 0
        self.trace: list[dict] = []
        self.warnings: list[str] = []
        self.messages: queue.Queue = queue.Queue(maxsize=8)
        self.stderr = bytearray()
        self.command = command or [binary_path(), "serve", "--automation-read-root", str(self.read_root),
                                  "--automation-write-root", str(self.write_root)]
        home = self.write_root / ".home"
        home.mkdir(exist_ok=True)
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "FONTCONFIG_PATH") if k in os.environ}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / "config"), XDG_CACHE_HOME=str(home / "cache"),
                   TMPDIR=str(home), PHOTOCRAFT_GPU="0")
        def bound():
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024**2,) * 2)
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (int(total_timeout)+15,) * 2)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, start_new_session=True,
                                     preexec_fn=bound if os.name == "posix" else None)
        os.set_blocking(self.proc.stdin.fileno(), False)
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.proc.stdout.readline(MAX_LINE + 1)
                if not line:
                    self.messages.put(PhotoError("PhotoCraft encerrou sem resposta JSON.")); return
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    self.messages.put(PhotoError("Resposta PhotoCraft excede o limite.")); return
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeError):
                    self.messages.put(PhotoError("Resposta nao JSON no stdout do PhotoCraft.")); return
                self.messages.put(obj)
        except Exception as exc:
            self.messages.put(PhotoError(type(exc).__name__ + " na leitura do PhotoCraft."))

    def _err(self):
        try:
            while data := self.proc.stderr.read1(1024):
                if len(self.stderr) < 131072:
                    self.stderr.extend(data[:131072 - len(self.stderr)])
        except (OSError, ValueError):
            pass

    def call(self, method: str, params: dict | None = None) -> Any:
        if self.closed:
            raise PhotoError("Sessao encerrada.")
        remaining = min(self.timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            self.close(); raise PhotoError("Tempo total de processamento excedido.")
        self.seq += 1
        payload = {"id": self.seq, "method": method, "params": params or {}}
        data = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(data) > 900_000:
            raise PhotoError("Requisicao grande demais.")
        start = time.monotonic()
        try:
            view = memoryview(data)
            expires = start + remaining
            while view:
                wait = expires - time.monotonic()
                if wait <= 0:
                    raise queue.Empty
                _, ready, _ = select.select([], [self.proc.stdin.fileno()], [], wait)
                if not ready:
                    raise queue.Empty
                try:
                    count = os.write(self.proc.stdin.fileno(), view)
                    view = view[count:]
                except BlockingIOError:
                    continue
            wait = expires - time.monotonic()
            if wait <= 0:
                raise queue.Empty
            response = self.messages.get(timeout=wait)
        except queue.Empty:
            self.close(); raise PhotoError("PhotoCraft excedeu o tempo da operacao; nenhum retry automatico foi feito.") from None
        except (OSError, ValueError):
            self.close(); raise PhotoError("PhotoCraft fechou o canal de comandos.") from None
        if isinstance(response, Exception):
            self.close(); raise response
        if not isinstance(response, dict) or response.get("id") != self.seq:
            self.close(); raise PhotoError("ID de resposta invalido; sessao descartada.")
        self.trace.append({"method": method, "command": (params or {}).get("command"),
                           "ok": response.get("ok") is True, "seconds": round(time.monotonic()-start, 3)})
        if response.get("ok") is not True:
            message = response.get("error", "erro sem detalhes")
            raise PhotoError("PhotoCraft recusou " + method + ": " + str(message)[:1800])
        result = response.get("result")
        for source in (response, result if isinstance(result, dict) else {}):
            notes = source.get("warnings", [])
            if isinstance(notes, str):
                notes = [notes]
            if isinstance(notes, list):
                for warning in notes[:50]:
                    warning = str(warning)[:2000]
                    if warning not in self.warnings and len(self.warnings) < 100:
                        self.warnings.append(warning)
        return result

    def commands(self, term: str = "") -> list[dict]:
        result = self.call("engine.commands", {"filter": term})
        if not isinstance(result, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in result):
            raise PhotoError("Catalogo inesperado: engine.commands deve retornar uma lista.")
        return result

    def execute(self, command: str, params: dict | None = None):
        if not command_allowed(command):
            raise PhotoError("Comando bloqueado pelo adaptador.")
        validate_tree(params or {})
        result = self.call("engine.execute", {"command": command, "params": params or {}, "wait": True})
        if isinstance(result, dict) and result.get("pending"):
            raise PhotoError("Motor devolveu job pendente apesar de wait=true; exportacao cancelada.")
        return result

    def close(self):
        if getattr(self, "closed", True):
            return
        self.closed = True
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            if self.proc.poll() is None:
                if os.name == "posix":
                    os.killpg(self.proc.pid, signal.SIGKILL)
                else:
                    self.proc.kill()
                self.proc.wait(timeout=3)
        for pipe in (self.proc.stdout, self.proc.stderr):
            try: pipe.close()
            except (OSError, ValueError): pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def apply_plan(rpc: PhotoRPC, plan: dict, local_assets: list[str]) -> dict:
    plan = validate_plan(plan)
    if len(local_assets) != len(plan.get("assets", [])):
        raise PhotoError("Numero de arquivos locais difere da receita.")
    if plan.get("base") is not None:
        rpc.call("doc.open", {"path": local_assets[plan["base"] - 1]})
    else:
        rpc.call("doc.new", {**plan["canvas"], "mode": "rgb", "depth": 8})
    available = {x["id"] for x in rpc.commands()}
    for n, step in enumerate(plan.get("steps", []), 1):
        try:
            if "command" in step:
                if step["command"] not in available:
                    raise PhotoError("Comando nao existe na versao instalada: " + step["command"])
                rpc.execute(step["command"], step.get("params", {}))
            else:
                method = "doc.place" if step["method"] == "place" else "doc.replaceSmartObject"
                rpc.call(method, {**step.get("params", {}), "path": local_assets[step["asset"] - 1]})
        except PhotoError as exc:
            raise PhotoError(f"Etapa {n}: {exc}") from exc
    return rpc.call("doc.inspect")


# ============================================================================
# Incorporated from install_photocraft.py
# ============================================================================
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.request

VERSION="0.23.1"
REPO="storytold/photocraft"
MAX_ARCHIVE=900*1024*1024


# Authentication is for release metadata on api.github.com only. Public release
# downloads and redirects to a CDN must NEVER receive the workflow credential.
import urllib.error
from urllib.parse import urlsplit
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MAX_METADATA = 8 * 1024 * 1024
INSTALL_HTTP_ATTEMPTS = 3
INSTALL_RETRY_BUDGET = 120  # seconds of explicit retry waits per request


def _https_parts(url):
    if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise PhotoError("URL de instalacao invalida.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise PhotoError("URL de instalacao invalida.") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or port not in (None, 443)):
        raise PhotoError("Instalacao exige HTTPS sem credenciais na URL e porta padrao.")
    return parts


def _api_url(url):
    return _https_parts(url).hostname.lower() == "api.github.com"


class _InstallRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = _https_parts(newurl)  # refuse an HTTPS -> HTTP downgrade
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            source = _https_parts(req.full_url)
            same_api = source.hostname.lower() == target.hostname.lower() == "api.github.com"
            if not same_api:
                # Remove both regular and unredirected header forms.
                for mapping in (redirected.headers, redirected.unredirected_hdrs):
                    for key in list(mapping):
                        if key.lower() in {"authorization", "proxy-authorization", "cookie"}:
                            del mapping[key]
        return redirected


def _install_headers(url):
    api = _api_url(url)
    headers = {"User-Agent": "PhotoCraft-Telegram-Integration/" + INTEGRATION_VERSION,
               "Accept": "application/vnd.github+json" if api else "*/*"}
    if api:
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
        if token:
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token):
                raise PhotoError("Token de instalacao contem caracteres invalidos.")
            headers["Authorization"] = "Bearer " + token
    return headers


def _install_log(event, **fields):
    # Callers provide only host names, booleans, counters and enum-like values.
    # Neither URLs with query strings, response bodies nor credentials are logged.
    print("[photocraft:install] " + json.dumps({"event": event, **fields}), flush=True)


def _rate_limit_wait(headers, attempt, now=None):
    now = time.time() if now is None else now
    waits = []
    retry = str(headers.get("Retry-After", "")).strip()
    if retry:
        try:
            if retry.isdigit():
                waits.append(int(retry))
            else:
                date = parsedate_to_datetime(retry)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                waits.append(max(0, math.ceil(date.timestamp() - now)))
        except (ValueError, TypeError, OverflowError):
            pass
    if str(headers.get("X-RateLimit-Remaining", "")) == "0":
        reset = str(headers.get("X-RateLimit-Reset", ""))
        if reset.isdigit():
            # One extra second ensures the next call is after the reset time.
            waits.append(max(1, math.ceil(int(reset) - now) + 1))
    if not waits:
        waits.append(60 * (2 ** attempt))
    return max(1, max(waits))


def _install_http_error(host, status, authenticated, limited, headers):
    mode = "autenticada" if authenticated else "sem autenticacao"
    if limited:
        detail = f"GitHub: limite de requisicoes atingido em {host} (HTTP {status}; {mode})."
        reset = str(headers.get("X-RateLimit-Reset", ""))
        if reset.isdigit():
            try:
                stamp = datetime.fromtimestamp(int(reset), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                detail += " Reset informado: " + stamp + "."
            except (ValueError, OverflowError, OSError):
                pass
        if not authenticated and host == "api.github.com":
            detail += " No passo do Python, configure GITHUB_TOKEN: ${{ github.token }} no workflow."
        detail += " Nenhuma nova requisicao sera feita nesta tentativa; respeite o reset/Retry-After."
        return PhotoError(detail)
    if status == 401 and host == "api.github.com":
        return PhotoError("GitHub: token recusado (HTTP 401). Use o token automatico do workflow; nao cole tokens no codigo.")
    return PhotoError(f"Instalacao: HTTP {status} em {host} ({mode}); nao e uma falha grafica.")


def get(url, dest=None):
    headers = _install_headers(url)
    host = _https_parts(url).hostname.lower()
    authenticated = "Authorization" in headers
    opener = urllib.request.build_opener(_InstallRedirect())
    waited = 0
    for attempt in range(INSTALL_HTTP_ATTEMPTS):
        part = None
        _install_log("request", host=host, authenticated=authenticated, attempt=attempt + 1)
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=90) as response:
                if dest is None:
                    data = response.read(MAX_METADATA + 1)
                    if len(data) > MAX_METADATA:
                        raise PhotoError("Metadados da release excederam o limite.")
                    return data
                # An interrupted response must not replace a previous valid archive.
                destination = Path(dest)
                with tempfile.NamedTemporaryFile(mode="wb", prefix=destination.name + ".",
                                                 suffix=".part", dir=destination.parent,
                                                 delete=False) as stream:
                    part = Path(stream.name)
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_ARCHIVE:
                            raise PhotoError("Download excedeu o limite.")
                        stream.write(chunk)
                declared = str(response.headers.get("Content-Length", ""))
                if declared.isdigit() and size != int(declared):
                    raise PhotoError("Download da release incompleto; arquivo temporario descartado.")
                part.replace(destination)
                part = None
                return None
        except urllib.error.HTTPError as exc:
            # A redirect may fail on a CDN rather than on the API itself.
            error_host = _https_parts(exc.geturl()).hostname.lower()
            error_auth = authenticated and error_host == "api.github.com"
            status = exc.code
            error_headers = exc.headers or {}
            try:
                error_body = exc.read(4096).decode("utf-8", "replace").lower()
            except Exception:
                error_body = ""
            finally:
                exc.close()
            limited = status == 429 or (status == 403 and (
                str(error_headers.get("X-RateLimit-Remaining", "")) == "0"
                or "rate limit" in error_body or "rate-limit" in error_body))
            delay = None
            if limited:
                delay = _rate_limit_wait(error_headers, attempt)
            elif status in (500, 502, 503, 504):
                delay = 2 ** (attempt + 1)
                # A server error may also carry Retry-After. Never retry earlier.
                if error_headers.get("Retry-After"):
                    delay = max(delay, _rate_limit_wait(error_headers, attempt))
            if (delay is not None and attempt + 1 < INSTALL_HTTP_ATTEMPTS
                    and delay <= INSTALL_RETRY_BUDGET - waited):
                _install_log("retry", host=error_host, authenticated=error_auth,
                             status=status, wait_seconds=delay, rate_limited=limited)
                time.sleep(delay)
                waited += delay
                continue
            raise _install_http_error(error_host, status, error_auth, limited, error_headers) from None
        finally:
            if part is not None:
                part.unlink(missing_ok=True)
    raise PhotoError("Instalacao: numero maximo de tentativas atingido.")


# The Linux packages also contain a bash completion named photocraft-cli.
# A basename match is NOT proof that a file is the native command-line program.
import stat


def _inspect_cli_candidate(path: Path, unpacked: Path) -> dict:
    """Inspect a file header without executing it or following symlinks."""
    record = {"path": path.relative_to(unpacked).as_posix(),
              "eligible": False, "reason": "unreadable"}
    try:
        relative = path.relative_to(unpacked)
        current = unpacked
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                record["reason"] = "symlink"
                return record
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            record["reason"] = "not_regular"
            return record
        record.update(size=info.st_size, mode=oct(stat.S_IMODE(info.st_mode)))
        # ELF64 header: EI_CLASS=2, EI_DATA=1, EM_X86_64=62.
        # This is only a format/architecture check; SHA-256 establishes which
        # release was fetched, and --version/native tests must still pass.
        with path.open("rb") as stream:
            header = stream.read(64)
        if not header.startswith(b"\x7fELF"):
            record["reason"] = "not_elf"
        elif len(header) < 64:
            record["reason"] = "truncated_elf_header"
        elif header[4:7] != b"\x02\x01\x01":
            record["reason"] = "not_elf64_little_endian"
        elif int.from_bytes(header[18:20], "little") != 62:
            record["reason"] = "not_x86_64"
        elif int.from_bytes(header[16:18], "little") not in (2, 3):
            record["reason"] = "not_executable_or_pie"
        elif (int.from_bytes(header[20:24], "little") != 1
              or int.from_bytes(header[52:54], "little") != 64):
            record["reason"] = "invalid_elf_header"
        else:
            record.update(eligible=True, reason="elf64_x86_64")
    except OSError as exc:
        record.update(reason="unreadable", error_type=type(exc).__name__)
    return record


def _discover_cli(unpacked: Path, report_path: Path) -> tuple[Path, dict]:
    """Choose one native binary, ignoring completions/docs with the same name."""
    unpacked = Path(unpacked)
    if unpacked.is_symlink() or not unpacked.is_dir():
        raise PhotoError("Diretorio de extracao invalido para procurar o CLI.")
    unpacked = unpacked.resolve(strict=True)
    matches = sorted(unpacked.rglob("photocraft-cli"))
    entries = [_inspect_cli_candidate(path, unpacked) for path in matches]
    binaries = [path for path, row in zip(matches, entries) if row["eligible"]]
    report = {
        "integration": INTEGRATION_VERSION,
        "status": "selected" if len(binaries) == 1 else "missing" if not binaries else "ambiguous",
        "name_matches": len(matches), "native_candidates": len(binaries),
        "candidates": entries[:40], "inventory_truncated": len(entries) > 40,
        "native_rendering_confirmed": False,
    }
    if len(binaries) == 1:
        report["selected"] = binaries[0].relative_to(unpacked).as_posix()
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    _install_log("binary-discovery", **report)
    if not binaries:
        raise PhotoError(
            f"Nenhum photocraft-cli ELF64 x86_64 valido entre {len(matches)} nomes encontrados. "
            "Veja binary-discovery e install-discovery.json; nenhum arquivo auxiliar foi executado.")
    if len(binaries) != 1:
        raise PhotoError(
            f"Mais de um photocraft-cli ELF64 x86_64 valido ({len(binaries)}). "
            "Veja install-discovery.json; nenhum candidato foi escolhido arbitrariamente.")
    return binaries[0], report


def install(root: Path):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Do not publish stale diagnostics from an earlier installation attempt.
    for report_name in ("install-report.json", "install-discovery.json"):
        (root / report_name).unlink(missing_ok=True)
    release = json.loads(get(f"https://api.github.com/repos/{REPO}/releases/tags/v{VERSION}"))
    if release.get("tag_name") != f"v{VERSION}":
        raise RuntimeError("Tag inesperada.")
    name = f"photocraft-{VERSION}-linux-x86_64.tar.gz"
    asset = next((a for a in release.get("assets", []) if a["name"] == name), None)
    if not asset:
        raise RuntimeError("Release sem o tarball esperado: " + name)
    url = asset["browser_download_url"]
    prefix = f"https://github.com/{REPO}/releases/download/v{VERSION}/"
    if not url.startswith(prefix):
        raise RuntimeError("URL inesperada para o artefato.")
    digest = asset.get("digest") or ""
    expected = digest[7:] if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) else None
    if not expected:
        checksum = next((a for a in release["assets"] if a["name"] == "SHA256SUMS.txt"), None)
        if not checksum:
            raise RuntimeError("Release sem checksum verificavel.")
        checksum_url = checksum["browser_download_url"]
        if not checksum_url.startswith(prefix):
            raise RuntimeError("URL inesperada para o checksum.")
        for line in get(checksum_url).decode().splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
            if match and match[2].removeprefix("./") == name:
                expected = match[1]
                break
    if not expected:
        raise RuntimeError("SHA256 do tarball ausente; instalacao interrompida.")
    archive = root / name
    if not archive.exists() or archive_sha256(archive).lower() != expected.lower():
        get(url, archive)
    actual = archive_sha256(archive)
    if actual.lower() != expected.lower():
        raise RuntimeError("SHA256 divergente.")
    _install_log("archive-verified", asset=name, sha256=actual)

    # Search only the extraction made by THIS attempt, never stale directories.
    unpacked = Path(tempfile.mkdtemp(prefix="unpacked-", dir=root))
    try:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            if sum(m.size for m in members) > 3 * 1024**3 or len(members) > 10000:
                raise RuntimeError("Arquivo compactado grande demais.")
            tf.extractall(unpacked, filter="data")
        exe, discovery = _discover_cli(unpacked, root / "install-discovery.json")
        # Do not make the bash completion or other data files executable.
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        probe_env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "FONTCONFIG_PATH") if k in os.environ}
        probe_home = root / "probe-home"
        probe_home.mkdir(exist_ok=True)
        probe_env.update(HOME=str(probe_home), XDG_CONFIG_HOME=str(probe_home / "config"),
                         XDG_CACHE_HOME=str(probe_home / "cache"), PHOTOCRAFT_GPU="0")
        probe = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                               timeout=30, check=True, env=probe_env)
        reported = (probe.stdout + probe.stderr).strip()
        if not re.search(r"(?<![0-9.])" + re.escape(VERSION) + r"(?![0-9.])", reported):
            raise RuntimeError("Versao do executavel diverge da release.")
        result = {"version": VERSION, "asset": name, "archive_sha256": actual,
                  "binary": str(exe), "binary_member": discovery["selected"],
                  "binary_sha256": archive_sha256(exe), "reported_version": reported,
                  "name_matches": discovery["name_matches"],
                  "native_candidates": discovery["native_candidates"]}
        (root / "install-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(unpacked, ignore_errors=True)
        raise
    os.environ["PHOTOCRAFT_BIN"] = str(exe)
    if os.getenv("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
            stream.write("PHOTOCRAFT_BIN=" + str(exe) + "\n")
    print(json.dumps(result, indent=2))
    return result


def _install_with_diagnostics(root: Path, report_root: Path):
    """Include discovery evidence in the existing Actions artifact, even on failure."""
    try:
        return install(root)
    finally:
        for name in ("install-report.json", "install-discovery.json"):
            source = Path(root).resolve() / name
            destination = Path(report_root).resolve() / name
            try:
                if source.is_file() and not source.is_symlink() and source != destination:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            except OSError as exc:
                # Diagnostic copying must not hide the original install error.
                _install_log("diagnostics-copy-failed", file=name, error_type=type(exc).__name__)

# ============================================================================
# Incorporated from photocraft_native_tests.py
# ============================================================================
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from PIL import Image, ImageStat, ImageChops


def run_suite(root: Path) -> dict:
    root = Path(root).resolve(); root.mkdir(parents=True, exist_ok=True)
    report = {"engine": "PhotoCraft native binary", "mocked": False, "status": "running", "cases": []}
    try:
        exe = binary_path()
        with open(exe, "rb") as binary:
            report["binary_sha256"] = hashlib.file_digest(binary, "sha256").hexdigest()
    except (PhotoError, OSError) as exc:
        report.update(status="unavailable", reason=str(exc), passed=0, failed=0, executed=0)
        (root / "native-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    inputs = root / "inputs"; inputs.mkdir(exist_ok=True)
    Image.new("RGB", (96, 64), (12, 34, 56)).save(inputs / "solid.png")
    im = Image.new("RGB", (96, 64)); im.putdata([(255 if (x+y)%2 else 0,)*3 for y in range(64) for x in range(96)])
    im.save(inputs / "checker.png")
    Image.new("RGBA", (20, 16), (255, 0, 0, 255)).save(inputs / "overlay.png")

    def case(name, fn):
        out = root / name; out.mkdir(exist_ok=True); start = time.monotonic()
        try:
            with PhotoRPC(inputs, out) as rpc:
                detail = fn(rpc, out)
                (out / "rpc-trace.json").write_text(json.dumps(rpc.trace, indent=2))
            report["cases"].append({"name": name, "status": "passed", "detail": detail, "seconds": round(time.monotonic()-start, 3)})
        except Exception as exc:
            report["cases"].append({"name": name, "status": "failed", "error": str(exc), "seconds": round(time.monotonic()-start, 3)})

    def save(rpc, out, name="result.png"):
        rpc.call("doc.save", {"path": name}); p = out / name
        assert p.is_file() and p.stat().st_size > 0, "Arquivo de saida nao foi escrito."
        with Image.open(p) as img:
            img.load(); return img.convert("RGBA")

    def catalog(rpc, out):
        rows = rpc.commands()
        assert any(x["id"] == "image.adjustments.invert" for x in rows)
        (root / "commands-native.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
        return {"commands": len(rows)}
    case("01-catalog", catalog)

    def roundtrip(rpc, out):
        rpc.call("doc.open", {"path": "solid.png"}); got = save(rpc, out)
        assert got.size == (96, 64) and got.getpixel((48, 32))[:3] == (12,34,56)
        return {"size": got.size, "pixel": got.getpixel((48,32))}
    case("02-png-roundtrip", roundtrip)

    def invert(rpc, out):
        rpc.call("doc.open", {"path": "solid.png"}); rpc.execute("image.adjustments.invert")
        got = save(rpc, out).getpixel((48, 32))[:3]
        assert all(abs(a-b) <= 1 for a,b in zip(got,(243,221,199))), str(got)
        return {"pixel": got}
    case("03-invert", invert)

    def twice(rpc, out):
        rpc.call("doc.open", {"path": "solid.png"})
        rpc.execute("image.adjustments.invert"); rpc.execute("image.adjustments.invert")
        got=save(rpc,out).getpixel((48,32))[:3]
        assert all(abs(a-b)<=1 for a,b in zip(got,(12,34,56)))
        return {"pixel":got}
    case("04-invert-twice",twice)

    def blur(rpc,out):
        rpc.call("doc.open",{"path":"checker.png"}); rpc.execute("filter.blur.gaussianBlur",{"radius":3})
        img=save(rpc,out).convert("L"); sd=ImageStat.Stat(img.crop((10,10,86,54))).stddev[0]
        assert sd < 30, f"Desfoque nao reduziu o contraste: stddev={sd}"
        return {"stddev":sd}
    case("05-blur",blur)

    def alpha(rpc,out):
        rpc.call("doc.new",{"width":64,"height":48,"mode":"rgb","depth":8,"background":"transparent"})
        im=save(rpc,out); assert im.getextrema()[3] == (0,0)
        return {"alpha":im.getextrema()[3]}
    case("06-transparent-canvas",alpha)

    def brush(rpc,out):
        rpc.call("doc.new",{"width":96,"height":64,"background":"white","mode":"rgb","depth":8})
        rpc.execute("layer.new.layer",{"name":"Ink"})
        rpc.execute("paint.stroke",{"points":[[30,32,1],[66,32,1]],"size":20,"color":"#ff0000"})
        img=save(rpc,out); pixel=img.getpixel((48,32)); assert pixel[0] > 200 and pixel[1] < 80
        return {"pixel":pixel}
    case("07-paint-layer",brush)

    def place(rpc,out):
        rpc.call("doc.open",{"path":"solid.png"})
        r=rpc.call("doc.place",{"path":"overlay.png","fit":False,"scale":100,"center":[48,32]})
        pixel=save(rpc,out).getpixel((48,32)); assert pixel[0] > 240 and pixel[1] < 10
        return {"result":r,"pixel":pixel}
    case("08-place-image",place)

    def layered(rpc,out,extension):
        rpc.call("doc.open",{"path":"solid.png"})
        rpc.execute("layer.new.layer",{"name":"Named test layer"})
        rpc.call("doc.save",{"path":"test."+extension})
        p=out/("test."+extension); assert p.is_file() and p.stat().st_size>0
        # A new native process must reopen it; same-process save is not enough.
        with PhotoRPC(out,out/"reopen") as fresh:
            fresh.call("doc.open",{"path":p.name})
            info=fresh.call("doc.inspect")
            assert "Named test layer" in json.dumps(info), "Nome/camada nao sobreviveu ao reopen."
            img=save(fresh,out/"reopen")
            assert img.size==(96,64) and img.getpixel((48,32))[:3]==(12,34,56)
        return {"bytes":p.stat().st_size,"reopened":True,"named_layer_preserved":True}
    case("09-pcraft-reopen",lambda r,o:layered(r,o,"pcraft"))
    case("10-psd-reopen",lambda r,o:layered(r,o,"psd"))

    def banner(rpc,out):
        rpc.call("doc.new",{"width":720,"height":4320,"background":"white","mode":"rgb","depth":8})
        got=save(rpc,out); assert got.size==(720,4320)
        return {"size":got.size,"canvas_dimensions_preserved":True}
    case("11-tall-banner",banner)

    def boundary(rpc,out):
        # A valid, existing image must be refused because it is outside the read root.
        # A missing file would not prove path confinement.
        Image.new("RGB", (8, 8), (10, 20, 30)).save(root / "outside.png")
        try: rpc.call("doc.open",{"path":"../outside.png"})
        except PhotoError: return {"refused":True}
        raise AssertionError("Acesso fora da pasta nao foi recusado.")
    case("12-read-root-boundary",boundary)

    report["passed"]=sum(x["status"]=="passed" for x in report["cases"])
    report["failed"]=sum(x["status"]=="failed" for x in report["cases"])
    report["executed"]=len(report["cases"])
    report["status"]="passed" if not report["failed"] else "failed"
    (root/"native-report.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    return report


# ============================================================================
# Incorporated from photocraft_job.py
# ============================================================================
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import warnings
import zipfile
import requests
from PIL import Image

MAX_INPUT=200*1024**2
MAX_TOTAL=500*1024**2
MAX_OUTPUT=450*1024**2
Image.MAX_IMAGE_PIXELS=MAX_PIXELS


def preflight(path:Path):
    if not path.is_file() or not 0<path.stat().st_size<=MAX_INPUT:
        raise PhotoError("Arquivo vazio ou maior que 200 MiB.")
    ext=path.suffix.lower()
    if ext in {".psd",".psb"}:
        with path.open("rb") as f: header=f.read(26)
        if len(header)<26 or header[:4]!=b"8BPS": raise PhotoError("PSD/PSB invalido.")
        h,w=struct.unpack(">II",header[14:22])
    elif ext==".pcraft":
        if not zipfile.is_zipfile(path): raise PhotoError("Envie .pcraft como arquivo ZIP nativo, nao uma pasta.")
        with zipfile.ZipFile(path) as z:
            records=z.infolist()
            if len(records)>20000 or sum(x.file_size for x in records)>1024**3:
                raise PhotoError("Projeto nativo grande demais quando descompactado.")
            for item in records:
                name=item.filename.replace("\\","/")
                if name.startswith("/") or ".." in name.split("/") or item.flag_bits&1:
                    raise PhotoError("Estrutura insegura ou criptografada no projeto.")
        return
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("error",Image.DecompressionBombWarning)
            with Image.open(path) as im:
                w,h=im.size; im.verify()
    if w<1 or h<1 or max(w,h)>MAX_EDGE or w*h>MAX_PIXELS:
        raise PhotoError("Imagem excede 12000 px por lado ou 32 megapixels.")


class Telegram:
    def __init__(self,chat,thread=None):
        self.token=os.environ.get("BOT_TOKEN","")
        if not self.token: raise PhotoError("BOT_TOKEN ausente no runner.")
        self.api="https://api.telegram.org/bot"+self.token
        self.chat,self.thread=chat,thread
        self.http=requests.Session(); self.mt=None

    def clean(self,text):
        return str(text).replace(self.token,"[TOKEN]")[:3500]

    def params(self):
        return {"chat_id":str(self.chat),**({"message_thread_id":self.thread} if self.thread else {})}

    def call(self,method,data=None,files=None):
        try:
            r=self.http.post(self.api+"/"+method,data=data or {},files=files,timeout=(20,180))
            j=r.json()
        except Exception as exc: raise PhotoError("Telegram: falha de transporte ("+type(exc).__name__+").") from None
        if not j.get("ok"): raise PhotoError("Telegram: "+self.clean(j.get("description","erro")))
        return j.get("result")

    def say(self,text):
        return self.call("sendMessage",{**self.params(),"text":self.clean(text)})

    def media_client(self):
        if self.mt is None:
            if not os.getenv("TG_API_ID") or not os.getenv("TG_API_HASH"):
                raise PhotoError("Arquivo grande: configure TG_API_ID e TG_API_HASH existentes no bot.")
            from pyrogram import Client
            self.mt=Client("photocraft_bot",api_id=int(os.environ["TG_API_ID"]),api_hash=os.environ["TG_API_HASH"],
                           bot_token=self.token,in_memory=True,no_updates=True)
            self.mt.start()
        return self.mt

    def download(self,asset,path:Path):
        declared=asset.get("size",0)
        if declared>MAX_INPUT: raise PhotoError("Entrada maior que 200 MiB.")
        if declared>19*1024**2:
            def progress(current,total):
                if current>MAX_INPUT or total>MAX_INPUT:
                    self.media_client().stop_transmission()
            got=self.media_client().download_media(asset["file_id"],file_name=str(path),progress=progress)
            if not got: raise PhotoError("Download MTProto cancelado ou vazio.")
        else:
            try: info=self.call("getFile",{"file_id":asset["file_id"]})
            except PhotoError as exc:
                if "too big" not in str(exc).lower(): raise
                got=self.media_client().download_media(asset["file_id"],file_name=str(path),
                    progress=lambda current,total: self.media_client().stop_transmission() if max(current,total)>MAX_INPUT else None)
                if not got: raise PhotoError("Download MTProto falhou.")
                preflight(path); return
            part=info.get("file_path","")
            if not re.fullmatch(r"[A-Za-z0-9_./-]+",part) or ".." in part.split("/") or part.startswith("/"):
                raise PhotoError("Caminho Telegram inesperado.")
            count=0
            try:
                with self.http.get("https://api.telegram.org/file/bot"+self.token+"/"+part,stream=True,timeout=(20,120)) as r:
                    r.raise_for_status()
                    with path.open("wb") as f:
                        for block in r.iter_content(1024*1024):
                            count+=len(block)
                            if count>MAX_INPUT: raise PhotoError("Entrada excedeu limite durante download.")
                            f.write(block)
            except requests.RequestException: raise PhotoError("Falha ao baixar arquivo do Telegram.") from None
        preflight(path)

    def document(self,path:Path,caption=""):
        size=path.stat().st_size
        if size>MAX_OUTPUT: raise PhotoError("Saida excede o limite experimental de 450 MiB.")
        if size>49*1024**2:
            return self.media_client().send_document(self.chat,document=str(path),caption=caption,
                **({"message_thread_id":self.thread} if self.thread else {}))
        with path.open("rb") as f:
            return self.call("sendDocument",{**self.params(),"caption":caption},
                             {"document":(path.name,f,"application/octet-stream")})

    def close(self):
        if self.mt:
            try:self.mt.stop()
            except Exception:pass
        self.http.close()


def allowed_payload(payload):
    if not isinstance(payload,dict): raise PhotoError("Payload invalido.")
    allowed=[x.strip() for x in os.getenv("PHOTOCRAFT_ALLOWED_USERS","").split(",") if x.strip()]
    if str(payload.get("user_id")) not in allowed:
        raise PhotoError("PHOTOCRAFT_ALLOWED_USERS nao autoriza este usuario; configure a variavel no GitHub.")
    if not isinstance(payload.get("chat_id"),int) or isinstance(payload["chat_id"],bool): raise PhotoError("chat_id invalido.")
    if payload.get("thread_id") is not None and (type(payload["thread_id"]) is not int or payload["thread_id"]<0): raise PhotoError("thread_id invalido.")
    if payload.get("op") not in {"preview","export","inspect","catalog","test"}: raise PhotoError("Operacao invalida.")
    if payload.get("format","png") not in EXPORTS: raise PhotoError("Formato nao permitido.")
    term=payload.get("filter","")
    if not isinstance(term,str) or len(term)>100: raise PhotoError("Filtro invalido.")
    if payload.get("op")!="test": validate_plan(payload.get("plan"))
    return payload


def run_job(payload,root:Path,tg:Telegram):
    op=payload["op"]
    if op=="test":
        report=run_suite(root/"native-tests")
        tg.document(root/"native-tests"/"native-report.json","Teste real do motor PhotoCraft; nao usa simulacao.")
        if report["status"]!="passed": raise PhotoError("Teste nativo nao aprovado: "+report["status"])
        tg.say(f"PhotoCraft: {report['passed']} testes nativos passaram. Isso nao certifica todas as ferramentas nem fidelidade ao Photoshop.")
        return
    plan=validate_plan(payload["plan"])
    inp=root/"inputs";out=root/"outputs";inp.mkdir();out.mkdir()
    local=[];total=0;sources=[]
    for i,asset in enumerate(plan.get("assets",[]),1):
        path=inp/(f"a{i:02d}"+Path(asset["name"]).suffix.lower())
        tg.download(asset,path);total+=path.stat().st_size
        if total>MAX_TOTAL:raise PhotoError("Entradas excedem 500 MiB.")
        local.append(path.name)
        with path.open("rb") as f: digest=hashlib.file_digest(f,"sha256").hexdigest()
        sources.append({"slot":i,"name":asset["name"],"bytes":path.stat().st_size,"sha256":digest})
    tg.say("PhotoCraft: processando copia isolada; os arquivos originais nao serao alterados.")
    with PhotoRPC(inp,out) as rpc:
        info=apply_plan(rpc,plan,local)
        if op=="catalog":
            rows=rpc.commands(payload.get("filter",""))
            report=[{**x,"bot_allowed":command_allowed(x["id"])} for x in rows]
            p=out/"photocraft-commands.json";p.write_text(json.dumps(report,indent=2,ensure_ascii=False))
            txt=out/"photocraft-commands.txt"
            txt.write_text("\n\n".join(f"{x['id']} | bot: {'permitido' if x['bot_allowed'] else 'restrito'} | enabled: {x.get('enabled')}\n{x.get('label','')}\n{x.get('params','')}\n{x.get('disabledReason') or ''}" for x in report),encoding="utf-8")
            tg.document(txt,"Catalogo real desta versao e deste estado do projeto.");tg.document(p)
            return
        if op=="inspect":
            p=out/"photocraft-project.json";p.write_text(json.dumps(info,indent=2,ensure_ascii=False))
            tg.document(p,"Camadas, estado e propriedades devolvidos pelo PhotoCraft.");return
        if op=="preview":
            rpc.call("doc.render",{"maxSide":1280,"path":"preview.png"})
            p=out/"preview.png"
            with Image.open(p) as im: im.verify()
            tg.document(p,"Previa PhotoCraft (max. 1280 px). O projeto original nao foi redimensionado.")
        else:
            fmt=payload.get("format","png")
            rpc.call("doc.save",{"path":"project.pcraft"})
            native=out/"project.pcraft"
            if not native.is_file(): raise PhotoError("Motor nao gerou .pcraft como arquivo; nenhuma saida sera anunciada como completa.")
            if fmt!="pcraft":
                rpc.call("doc.save",{"path":"export."+fmt})
                target=out/("export."+fmt)
                if not target.is_file() or target.stat().st_size==0: raise PhotoError("Exportacao vazia.")
                if fmt in {"png","jpg","webp"}:
                    with Image.open(target) as im:
                        if im.width*im.height>MAX_PIXELS:raise PhotoError("Exportacao maior que 32 megapixels.")
                        im.verify()
                tg.document(target,"Exportacao PhotoCraft; confira visualmente antes de publicar.")
            tg.document(native,"Projeto em camadas. Arquivos finais PNG/JPG nao recuperam camadas antigas automaticamente.")
            recipe=out/"recipe.json";recipe.write_text(json.dumps(plan,indent=2,ensure_ascii=False))
            tg.document(recipe,"Receita vinculada aos file_id deste bot; guarde tambem o .pcraft.")
        report={"version":INTEGRATION_VERSION,"sources":sources,"trace":rpc.trace,"inspect":info,
                "native_warnings":rpc.warnings,"native_stderr_excerpt":rpc.stderr.decode("utf-8","replace")[:8192],
                "limitations":["No Photoshop equivalence test","No automatic recreation of reference images","No full metadata scrub"]}
        p=out/"processing-report.json";p.write_text(json.dumps(report,indent=2,ensure_ascii=False))
        tg.document(p,"Relatorio do processamento, sem credenciais do bot.")
        if rpc.warnings:
            tg.say("PhotoCraft retornou avisos; confira fontes e compatibilidade no relatorio antes de publicar.")
    tg.say("PhotoCraft: tarefa concluida. O motor executou a receita; revise a arte e os textos.")


# ============================================================================
# Incorporated from patch_worker_photocraft.py
# ============================================================================
from pathlib import Path
import argparse


def patch_worker(original:str,block:str)->str:
    if '// PHOTOCRAFT TELEGRAM MODULE v0.1 EXPERIMENTAL' in original:
        raise ValueError('Worker ja contem o modulo. Nao duplique o bloco.')
    markers=['\n// ---------- handlers ----------\n','async function handleCallback(cq) {','async function handleUpdate(update) {']
    for marker in markers:
        if original.count(marker)!=1: raise ValueError('Ponto de integracao ausente ou ambiguo: '+marker)
    start=original.index('async function handleUpdate(update) {')
    bind=original.find('  bindThread(msg);',start)
    if bind<0:raise ValueError('bindThread(msg) nao encontrado no handler.')
    cut=bind+len('  bindThread(msg);')
    out=original[:cut]+'\n  if (await pcHandleMessage(msg)) return;\n'+original[cut:]
    out=out.replace('async function handleCallback(cq) {','async function handleCallback(cq) {\n  if (cq.data?.startsWith("pc:")) return pcHandleCallback(cq);',1)
    return out.replace('\n// ---------- handlers ----------\n','\n'+block.rstrip()+'\n\n// ---------- handlers ----------\n',1)


# Exact Worker module from the original lab, embedded only for patch-worker.
WORKER_MODULE = r'''
// PHOTOCRAFT TELEGRAM MODULE v0.1 EXPERIMENTAL
// Add this block once, before '// ---------- handlers ----------'.
// handleUpdate: if (await pcHandleMessage(msg)) return; after bindThread(msg).
// handleCallback: if (cq.data?.startsWith('pc:')) return pcHandleCallback(cq);
// Uses existing BOT_OWNER_ID, BOT_TOKEN, KV, tg, to, threadOf, ghReady, ghDispatch.
const PC_EXTRA_ALLOWED_USERS = []; // Optional numeric Telegram IDs; owner already allowed.
const PC_TTL = 86400;
const PC_FORMATS = new Set(['png', 'jpg', 'webp', 'psd', 'pcraft']);
const PC_MAX_STEPS = 160;
const PC_INPUT_EXT = /\.(png|jpe?g|webp|tiff?|psd|psb|pcraft)$/i;
const pcKey = (msg) => `pc:s:${msg.chat.id}:${msg.from.id}:${msg.message_thread_id || msg.chat._thread || 0}`;
const pcAllowed = (uid) => Number.isSafeInteger(uid) && uid > 0 &&
  ((typeof BOT_OWNER_ID !== 'undefined' && BOT_OWNER_ID > 0 && uid === BOT_OWNER_ID) || PC_EXTRA_ALLOWED_USERS.includes(uid));
const pcBytes = (x) => new TextEncoder().encode(typeof x === 'string' ? x : JSON.stringify(x)).length;
const pcCopy = (x) => JSON.parse(JSON.stringify(x));
const pcDefault = () => ({schema: 1, canvas: {width: 1200, height: 1200, background: 'white'}, assets: [], steps: []});

function pcValidate(p) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) throw Error('Receita deve ser objeto JSON.');
  if (Object.keys(p).some(k => !['schema','canvas','base','assets','steps'].includes(k))) throw Error('Campo desconhecido na receita.');
  if ((p.schema ?? 1) !== 1) throw Error('Versao da receita nao suportada.');
  if (pcBytes(p) > 48000) throw Error('Receita excede 48 KB.');
  if (!Array.isArray(p.assets) || p.assets.length > 24) throw Error('Use ate 24 arquivos.');
  for (const a of p.assets) {
    if (!a || typeof a !== 'object' || Array.isArray(a) || Object.keys(a).some(k => !['file_id','name','size'].includes(k)) ||
        typeof a.file_id !== 'string' || !/^[A-Za-z0-9_-]{5,1024}$/.test(a.file_id) ||
        typeof a.name !== 'string' || a.name.length > 250 || !PC_INPUT_EXT.test(a.name) ||
        !Number.isSafeInteger(a.size ?? 0) || (a.size ?? 0) < 0 || (a.size ?? 0) > 200 * 1024 ** 2) throw Error('Arquivo invalido ou grande demais.');
  }
  if (p.base !== undefined && p.base !== null) {
    if (p.canvas != null || !Number.isInteger(p.base) || p.base < 1 || p.base > p.assets.length) throw Error('Arquivo base invalido.');
  } else {
    const c = p.canvas;
    if (!c || typeof c !== 'object' || Object.keys(c).some(k => !['width','height','background'].includes(k)) ||
        !Number.isInteger(c.width) || !Number.isInteger(c.height) || c.width < 1 || c.height < 1 ||
        Math.max(c.width,c.height) > 12000 || c.width*c.height > 32000000 ||
        !['white','black','transparent'].includes(c.background || 'white')) throw Error('Canvas: ate 12000 px por lado e 32 MP; fundo white, black ou transparent.');
  }
  if (!Array.isArray(p.steps) || p.steps.length > PC_MAX_STEPS) throw Error('Use ate 160 operacoes.');
  for (const s of p.steps) {
    if (!s || typeof s !== 'object' || Array.isArray(s)) throw Error('Operacao invalida.');
    if (s.command) {
      if (Object.keys(s).some(k => !['command','params'].includes(k)) || typeof s.command !== 'string' || !/^[A-Za-z][A-Za-z0-9_.]{1,119}$/.test(s.command)) throw Error('Comando invalido.');
    } else if (['place','replace'].includes(s.method)) {
      if (Object.keys(s).some(k => !['method','asset','params'].includes(k)) || !Number.isInteger(s.asset) || s.asset < 1 || s.asset > p.assets.length) throw Error('Numero de arquivo invalido.');
    } else throw Error('Operacao desconhecida.');
    if (s.params !== undefined && (!s.params || typeof s.params !== 'object' || Array.isArray(s.params))) throw Error('params deve ser um objeto JSON.');
  }
  // The Python adapter additionally enforces command families, path restrictions,
  // numeric ranges, JSON depth, memory and time. No user code reaches a shell.
  return p;
}

function pcAsset(msg) {
  if (!msg) return null;
  if (msg.document) {
    const d=msg.document;
    if (!PC_INPUT_EXT.test(d.file_name || '')) throw Error('Envie PNG, JPG, WebP, TIFF, PSD, PSB ou .pcraft.');
    return {file_id:d.file_id,name:d.file_name.slice(0,250),size:d.file_size || 0};
  }
  if (msg.photo?.length) {
    const p=msg.photo[msg.photo.length-1];
    return {file_id:p.file_id,name:'photo.jpg',size:p.file_size || 0};
  }
  return null;
}

function pcJson(raw) {
  let obj;
  try { obj=JSON.parse(raw || '{}'); } catch { throw Error('JSON invalido. Use aspas duplas nas chaves e textos.'); }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw Error('Use um objeto JSON, nao uma lista.');
  return obj;
}

function pcRows(msg,st) {
  const make=(text,act)=>({text,callback_data:`pc:${msg.from.id}:${msg.message_thread_id || msg.chat._thread || 0}:${st.rev}:${act}`});
  return [[make('Previa','preview'),make('Camadas','inspect')],
          [make('PNG + projeto','png'),make('PSD + projeto','psd')],
          [make('Desfazer etapa','undo'),make('Refazer etapa','redo')]];
}

async function pcPanel(msg,st,extra='') {
  const p=st.plan;
  const target=p.canvas ? `${p.canvas.width} x ${p.canvas.height}` : `base: ${p.assets[p.base-1]?.name || '?'}`;
  const tail=p.steps.slice(-5).map((s,i)=>`${p.steps.length-Math.min(5,p.steps.length)+i+1}. ${s.command || s.method+' arquivo '+s.asset}`).join('\n');
  return tg('sendMessage',{...to(msg.chat),text:
    `${extra ? extra+'\n\n' : ''}PhotoCraft - projeto experimental\n${target}\n`+
    `${p.assets.length} arquivo(s), ${p.steps.length} operacao(oes). Revisao ${st.rev}.\n`+
    (tail ? `Ultimas etapas:\n${tail}\n` : '')+
    '\nPrevia executa a receita no Actions. /foto ajuda mostra os comandos.\nSessao: 24 h apos a ultima alteracao; exporte para guardar.',
    reply_markup:{inline_keyboard:pcRows(msg,st)}});
}

const PC_HELP = [
  'PhotoCraft - integracao experimental (nao e Photoshop completo).',
  '/foto novo 1200x1200 transparent - canvas vazio',
  '/foto abrir - responda a imagem/PSD/.pcraft para usar como base',
  '/foto adicionar - responda a outro arquivo para registrar um recurso',
  '/foto arquivos - lista numeros dos recursos',
  '/foto colocar 2 {"fit":false,"scale":50,"center":[600,500]}',
  '/foto substituir 2 {"layer":"Capa"} - Smart Object existente',
  '/foto selecionar Nome da camada',
  '/foto camada {"name":"Retrato","opacity":0.8,"blend":"Multiply"}',
  '/foto pb | /foto matiz 30 10 | /foto inverter | /foto desfoque 3',
  '/foto comando ID {JSON} - operacao real do motor, validada no runner',
  '/foto comandos type. - consulta catalogo instalado e parametros reais',
  '/foto previa | /foto camadas | /foto exportar png|jpg|webp|psd|pcraft',
  '/foto desfazer | /foto refazer - ultima etapa da receita, nao o canvas',
  '/foto receita - exporta JSON com os file_id deste bot',
  '/foto importar {JSON} - ou responda ao arquivo .json; restaura a receita',
  '/foto painel | /foto cancelar - encerra a sessao, nao um job em execucao',
  '/foto teste - testes reais do motor no runner; nao substituem revisao visual',
  'Para texto, mascaras e efeitos avancados: consulte /foto comandos e use /foto comando. Nao ha IA interpretando texto livre nesta versao.',
  'Envie fotos como documento para preservar o arquivo original.'
].join('\n');

async function pcDispatch(msg,st,op,format='png',filter='') {
  if (!ghReady()) throw Error('Configure GITHUB_TOKEN e GITHUB_REPO existentes.');
  const payload={chat_id:msg.chat.id,user_id:msg.from.id,thread_id:msg.message_thread_id || msg.chat._thread || null,
                 op,format,filter:filter.slice(0,100),revision:st?.rev || 0,
                 plan:op==='test' ? null : pcValidate(pcCopy(st?.plan || pcDefault()))};
  if (pcBytes(payload)>60000) throw Error('Receita grande demais para o dispatch.');
  const err=await ghDispatch('photocraft-tool',payload);
  if (err) throw Error('GitHub nao aceitou a tarefa: '+String(err).slice(0,200));
  return tg('sendMessage',{...to(msg.chat),text:`PhotoCraft: tarefa ${op} enviada ao Actions (revisao ${payload.revision}). O resultado sera enviado por essa execucao. Nenhuma alteracao no uploader.`});
}

async function pcSave(msg,st) {
  pcValidate(st.plan);
  st.rev=(st.rev || 0)+1;
  if (pcBytes(st)>180000) throw Error('Historico da sessao grande demais; exporte a receita.');
  await KV.put(pcKey(msg),JSON.stringify(st),{expirationTtl:PC_TTL});
}

async function pcAddStep(msg,st,step) {
  if (!st) throw Error('Comece com /foto abrir ou /foto novo.');
  st.plan.steps.push(step);st.redo=[];
  await pcSave(msg,st);
  return pcPanel(msg,st,'Etapa adicionada. Ainda nao renderizada.');
}

async function pcRecipeFile(msg,plan) {
  const data=JSON.stringify(plan,null,2);
  const form=new FormData();
  form.append('chat_id',String(msg.chat.id));
  if (msg.chat._thread) form.append('message_thread_id',String(msg.chat._thread));
  form.append('document',new Blob([data],{type:'application/json'}),'photocraft-recipe.json');
  form.append('caption','Receita com identificadores deste bot. Nao publique em um repositorio publico.');
  const response=await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendDocument`,{method:'POST',body:form});
  const result=await response.json();
  if (!result.ok) throw Error('Falha ao enviar receita: '+String(result.description || '').slice(0,200));
  return result;
}

async function pcImportRecipe(msg,arg) {
  if (arg) return pcValidate(pcJson(arg));
  const document=(msg.document ? msg : msg.reply_to_message)?.document;
  if (!document || !/\.json$/i.test(document.file_name || '') || (document.file_size || 0)>48000) throw Error('Responda a uma receita .json de ate 48 KB com /foto importar.');
  const result=await tg('getFile',{file_id:document.file_id});
  const filePath=result.result?.file_path || '';
  if (!result.ok || !/^[A-Za-z0-9_./-]+$/.test(filePath) || filePath.startsWith('/') || filePath.split('/').includes('..')) throw Error('Nao consegui obter o JSON no Telegram.');
  const response=await fetch(`https://api.telegram.org/file/bot${BOT_TOKEN}/${filePath}`);
  if (!response.ok || Number(response.headers.get('content-length') || 0)>48000 || !response.body) throw Error('Receita indisponivel ou grande demais.');
  const reader=response.body.getReader();let total=0;const chunks=[];
  try {
    for (;;) {
      const part=await reader.read();if (part.done) break;
      total+=part.value.byteLength;
      if (total>48000) throw Error('Receita excedeu 48 KB.');
      chunks.push(part.value);
    }
  } finally {await reader.cancel().catch(()=>{});}
  const bytes=new Uint8Array(total);let at=0;for (const c of chunks) {bytes.set(c,at);at+=c.byteLength;}
  return pcValidate(pcJson(new TextDecoder('utf-8',{fatal:true}).decode(bytes)));
}

async function pcHandleMessage(msg) {
  const raw=String(msg.text || msg.caption || '').trim();
  const match=/^\/(?:foto|photocraft)(?:@\w+)?(?:\s+([\s\S]*))?$/i.exec(raw);
  if (!match) return false;
  const say=(text)=>tg('sendMessage',{...to(msg.chat),text});
  if (!pcAllowed(msg.from?.id)) {await say('PhotoCraft restrito aos usuarios autorizados. Configure BOT_OWNER_ID.');return true;}
  if (!KV) {await say('Falta o binding KV ALBUM existente.');return true;}
  try {
    const input=(match[1] || '').trim();
    const token=/^(\S+)(?:\s+([\s\S]*))?$/.exec(input);
    const act=(token?.[1] || 'painel').toLowerCase(), arg=(token?.[2] || '').trim();
    let st=await KV.get(pcKey(msg),'json');
    if (['ajuda','help'].includes(act) || (!st && act==='painel')) {await say(PC_HELP);return true;}
    if (act==='novo') {
      const m=/^(\d{1,5})x(\d{1,5})(?:\s+(white|black|transparent))?$/i.exec(arg);
      if (!m) throw Error('Use /foto novo 1200x1200 transparent');
      st={plan:{schema:1,canvas:{width:+m[1],height:+m[2],background:(m[3] || 'white').toLowerCase()},assets:[],steps:[]},redo:[],rev:0};
      await pcSave(msg,st);await pcPanel(msg,st);return true;
    }
    if (act==='abrir') {
      const a=pcAsset(msg.document || msg.photo ? msg : msg.reply_to_message);
      if (!a) throw Error('Responda a uma imagem, PSD, PSB ou .pcraft com /foto abrir.');
      st={plan:{schema:1,base:1,assets:[a],steps:[]},redo:[],rev:0};
      await pcSave(msg,st);await pcPanel(msg,st);return true;
    }
    if (act==='teste') {await pcDispatch(msg,st,'test');return true;}
    if (act==='comandos') {await pcDispatch(msg,st,'catalog','png',arg);return true;}
    if (act==='importar') {
      st={plan:await pcImportRecipe(msg,arg),redo:[],rev:st?.rev || 0};
      await pcSave(msg,st);await pcPanel(msg,st);return true;
    }
    if (!st) throw Error('Comece com /foto abrir ou /foto novo.');
    if (act==='adicionar') {
      const a=pcAsset(msg.document || msg.photo ? msg : msg.reply_to_message);
      if (!a) throw Error('Responda ao arquivo com /foto adicionar.');
      st.plan.assets.push(a);await pcSave(msg,st);await pcPanel(msg,st,`Arquivo ${st.plan.assets.length}: ${a.name}. Use /foto colocar ${st.plan.assets.length}.`);
    } else if (act==='arquivos') {
      await say(st.plan.assets.map((a,i)=>`${i+1}. ${a.name} (${a.size || '?'} bytes)`).join('\n') || 'Nenhum arquivo registrado.');
    } else if (act==='painel') await pcPanel(msg,st);
    else if (act==='cancelar') {await KV.delete(pcKey(msg));await say('Sessao PhotoCraft encerrada. Jobs ja enviados continuam no Actions.');}
    else if (act==='receita') await pcRecipeFile(msg,st.plan);
    else if (['colocar','substituir'].includes(act)) {
      const m=/^(\d+)(?:\s+([\s\S]+))?$/.exec(arg);
      if (!m) throw Error('Use numero do arquivo seguido de JSON opcional.');
      await pcAddStep(msg,st,{method:act==='colocar'?'place':'replace',asset:+m[1],params:pcJson(m[2] || '{}')});
    } else if (act==='comando') {
      const m=/^(\S+)(?:\s+([\s\S]+))?$/.exec(arg);
      if (!m) throw Error('Use /foto comando ID {"parametro":valor}');
      await pcAddStep(msg,st,{command:m[1],params:pcJson(m[2] || '{}')});
    } else if (act==='camada') await pcAddStep(msg,st,{command:'layer.setProps',params:pcJson(arg)});
    else if (act==='selecionar') {
      if (!arg) throw Error('Informe o nome unico da camada.');
      await pcAddStep(msg,st,{command:'layer.select',params:{name:arg}});
    } else if (act==='pb') await pcAddStep(msg,st,{command:'layer.newAdjustmentLayer.hueSaturation',params:{hue:0,saturation:-100}});
    else if (act==='matiz') {
      const values=arg.split(/\s+/).map(Number);
      if (values.length!==2 || !values.every(Number.isFinite) || Math.abs(values[0])>180 || Math.abs(values[1])>100) throw Error('Use /foto matiz H S; H entre -180 e 180, S entre -100 e 100.');
      await pcAddStep(msg,st,{command:'layer.newAdjustmentLayer.hueSaturation',params:{hue:values[0],saturation:values[1]}});
    } else if (act==='inverter') await pcAddStep(msg,st,{command:'image.adjustments.invert',params:{}});
    else if (act==='desfoque') {
      const radius=Number(arg);
      if (!arg || !Number.isFinite(radius) || radius<=0 || radius>200) throw Error('Raio entre 0 e 200 px, maior que zero.');
      await pcAddStep(msg,st,{command:'filter.blur.gaussianBlur',params:{radius}});
    } else if (['desfazer','refazer'].includes(act)) {
      st.redo=st.redo || [];
      const src=act==='desfazer'?st.plan.steps:st.redo, dst=act==='desfazer'?st.redo:st.plan.steps;
      if (!src.length) throw Error('Nenhuma etapa para '+act+'.');
      dst.push(src.pop());await pcSave(msg,st);await pcPanel(msg,st);
    } else if (act==='previa') await pcDispatch(msg,st,'preview');
    else if (act==='camadas') await pcDispatch(msg,st,'inspect');
    else if (act==='exportar') {
      const fmt=arg.toLowerCase() || 'png';if (!PC_FORMATS.has(fmt)) throw Error('Formatos: png, jpg, webp, psd, pcraft.');
      await pcDispatch(msg,st,'export',fmt);
    } else throw Error('Comando desconhecido. /foto ajuda');
  } catch(e) {await say('PhotoCraft: '+String(e.message || e).slice(0,1800));}
  return true;
}

async function pcHandleCallback(cq) {
  const answer=(text)=>tg('answerCallbackQuery',{callback_query_id:cq.id,text});
  const p=String(cq.data || '').split(':');
  if (p.length!==5 || p[0]!=='pc' || !cq.message || !pcAllowed(cq.from?.id) || String(cq.from.id)!==p[1]) return answer('Este painel pertence a outro usuario.');
  const thread=cq.message.message_thread_id || 0;
  if (String(thread)!==p[2]) return answer('Topico diferente do painel.');
  const msg={chat:{...cq.message.chat,_thread:thread},from:cq.from,message_thread_id:thread};
  const st=KV ? await KV.get(pcKey(msg),'json') : null;
  if (!st) return answer('Sessao expirada. Abra o projeto novamente.');
  if (String(st.rev)!==p[3]) return answer('Painel antigo. Use /foto painel.');
  await answer('Recebido.');
  const map={preview:'previa',inspect:'camadas',png:'exportar png',psd:'exportar psd',undo:'desfazer',redo:'refazer'};
  if (map[p[4]]) return pcHandleMessage({...msg,text:'/foto '+map[p[4]]});
}
'''

# Only used by selftest. Never substituted for the native engine.
RPC_TEST_FIXTURE = r'''
#!/usr/bin/env python3
"""Transport fixture only; does NOT render or impersonate a native engine test."""
import json,os,sys,time
for line in sys.stdin:
    req=json.loads(line); method=req['method']
    if method=='hang': time.sleep(60)
    if method=='bad-json': print('not-json',flush=True); continue
    if method=='exit': sys.exit(0)
    if method=='wrong-id': print(json.dumps({'id':9999,'ok':True,'result':{}}),flush=True);continue
    if method=='fail': print(json.dumps({'id':req['id'],'ok':False,'error':'fixture failure'}),flush=True);continue
    if method=='engine.commands': result=[{'id':'image.adjustments.invert','params':'{}','enabled':True},{'id':'layer.new.layer','params':'{}','enabled':True}]
    elif method=='warn':result={'warnings':['fixture warning, not a native font test']}
    elif method=='env':result={'BOT_TOKEN':os.getenv('BOT_TOKEN'),'GITHUB_TOKEN':os.getenv('GITHUB_TOKEN'),'PHOTOCRAFT_CONTROL_PORT':os.getenv('PHOTOCRAFT_CONTROL_PORT')}
    else:result={'echo':req['params'],'method':method,'fixture':True}
    print(json.dumps({'id':req['id'],'ok':True,'result':result}),flush=True)
'''


# Adapter self-tests (mock transport; no native rendering claim).
import copy,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image


def valid():return {'schema':1,'canvas':{'width':1200,'height':1200,'background':'transparent'},'assets':[],'steps':[]}
def asset():return {'file_id':'FAKE_asset_123','name':'a.png','size':123}

class PlanTests(unittest.TestCase):
    def test_basic(self):self.assertEqual(validate_plan(valid()),valid())
    def test_tall_banner(self):p=valid();p['canvas'].update(width=1200,height=7200);self.assertEqual(validate_plan(p)['canvas']['height'],7200)
    def test_pixel_limit(self):p=valid();p['canvas'].update(width=12000,height=12000);self.assertRaises(PhotoError,validate_plan,p)
    def test_negative(self):p=valid();p['canvas']['width']=-1;self.assertRaises(PhotoError,validate_plan,p)
    def test_bool_dimension(self):p=valid();p['canvas']['width']=True;self.assertRaises(PhotoError,validate_plan,p)
    def test_float_dimension(self):p=valid();p['canvas']['width']=2.5;self.assertRaises(PhotoError,validate_plan,p)
    def test_base(self):p={'base':1,'assets':[asset()],'steps':[]};self.assertEqual(validate_plan(p)['base'],1)
    def test_missing_base(self):p={'base':1,'assets':[],'steps':[]};self.assertRaises(PhotoError,validate_plan,p)
    def test_base_canvas_conflict(self):p=valid();p.update(base=1,assets=[asset()]);self.assertRaises(PhotoError,validate_plan,p)
    def test_unknown_key(self):p=valid();p['shell']='echo bad';self.assertRaises(PhotoError,validate_plan,p)
    def test_empty_document(self):self.assertRaises(PhotoError,validate_plan,{'assets':[],'steps':[]})
    def test_wrong_asset_extension(self):p=valid();a=asset();a['name']='evil.sh';p['assets']=[a];self.assertRaises(PhotoError,validate_plan,p)
    def test_asset_limit(self):p=valid();p['assets']=[asset()]*25;self.assertRaises(PhotoError,validate_plan,p)
    def test_fileid_injection(self):p=valid();a=asset();a['file_id']='../../bad';p['assets']=[a];self.assertRaises(PhotoError,validate_plan,p)
    def test_command_supported_family(self):p=valid();p['steps']=[{'command':'filter.blur.gaussianBlur','params':{'radius':3}}];self.assertEqual(len(validate_plan(p)['steps']),1)
    def test_command_file_blocked(self):p=valid();p['steps']=[{'command':'file.open','params':{}}];self.assertRaises(PhotoError,validate_plan,p)
    def test_nested_path_blocked(self):p=valid();p['steps']=[{'command':'layer.setProps','params':{'nested':{'path':'/etc/passwd'}}}];self.assertRaises(PhotoError,validate_plan,p)
    def test_numeric_nonfinite(self):p=valid();p['steps']=[{'command':'filter.blur.gaussianBlur','params':{'radius':float('nan')}}];self.assertRaises(PhotoError,validate_plan,p)
    def test_no_infinite_recursion(self):
        p={};q=p
        for _ in range(15):q['nested']={};q=q['nested']
        self.assertRaises(PhotoError,validate_tree,p)
    def test_unknown_method(self):p=valid();p['steps']=[{'method':'app.open','params':{'path':'a'}}];self.assertRaises(PhotoError,validate_plan,p)
    def test_place(self):p=valid();p['assets']=[asset()];p['steps']=[{'method':'place','asset':1,'params':{'center':[20,30],'scale':50,'fit':False}}];validate_plan(p)
    def test_place_path_forbidden(self):p=valid();p['assets']=[asset()];p['steps']=[{'method':'place','asset':1,'params':{'path':'a'}}];self.assertRaises(PhotoError,validate_plan,p)
    def test_place_scale_invalid(self):p=valid();p['assets']=[asset()];p['steps']=[{'method':'place','asset':1,'params':{'scale':-1}}];self.assertRaises(PhotoError,validate_plan,p)
    def test_replace_layer(self):p=valid();p['assets']=[asset()];p['steps']=[{'method':'replace','asset':1,'params':{'layer':'Capa'}}];validate_plan(p)
    def test_steps_limit(self):p=valid();p['steps']=[{'command':'image.adjustments.invert'}]*161;self.assertRaises(PhotoError,validate_plan,p)
    def test_no_mutation(self):p=valid();q=validate_plan(p);q['canvas']['width']=20;self.assertEqual(p['canvas']['width'],1200)
    def test_allowed_type(self):self.assertTrue(command_allowed('type.create'))
    def test_plugins_blocked(self):self.assertFalse(command_allowed('plugins.install'))
    def test_export_blocked(self):self.assertFalse(command_allowed('layer.exportAs'))
    def test_quoted_shell_command_blocked(self):self.assertFalse(command_allowed('image.adjustments.invert;echo'))
    def test_async_user_step_blocked(self):p=valid();p['steps']=[{'command':'image.adjustments.invert','wait':False}];self.assertRaises(PhotoError,validate_plan,p)

class ProcessTests(unittest.TestCase):
    def setUp(self):self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
    def tearDown(self):self.t.cleanup()
    def rpc(self,timeout=2):return PhotoRPC(self.root/'in',self.root/'out',command=[sys.executable, '-u', '-c', RPC_TEST_FIXTURE],timeout=timeout,total_timeout=10)
    def test_echo(self):
        with self.rpc() as rpc:self.assertEqual(rpc.call('ping',{'x':1})['echo'],{'x':1})
    def test_catalog_is_bare_array(self):
        with self.rpc() as rpc:self.assertEqual(rpc.commands()[0]['id'],'image.adjustments.invert')
    def test_no_secrets_in_child(self):
        with patch.dict(os.environ,{'BOT_TOKEN':'secret','GITHUB_TOKEN':'secret2','PHOTOCRAFT_CONTROL_PORT':'9999'}):
            with self.rpc() as rpc:self.assertTrue(all(v is None for v in rpc.call('env').values()))
    def test_warning_preserved(self):
        with self.rpc() as rpc:
            rpc.call('warn');self.assertIn('fixture warning, not a native font test',rpc.warnings)
    def test_failure_not_success(self):
        with self.rpc() as rpc:self.assertRaises(PhotoError,rpc.call,'fail')
    def test_bad_json(self):
        with self.rpc() as rpc:self.assertRaises(PhotoError,rpc.call,'bad-json')
    def test_wrong_id(self):
        with self.rpc() as rpc:self.assertRaises(PhotoError,rpc.call,'wrong-id')
    def test_exit(self):
        with self.rpc() as rpc:self.assertRaises(PhotoError,rpc.call,'exit')
    def test_timeout_terminates_process(self):
        with self.rpc(timeout=0.2) as rpc:
            self.assertRaises(PhotoError,rpc.call,'hang');self.assertIsNotNone(rpc.proc.poll())
    def test_apply_exact_sequence(self):
        p=valid();p['steps']=[{'command':'image.adjustments.invert','params':{}}]
        with self.rpc() as rpc:
            apply_plan(rpc,p,[]);self.assertEqual([x['method'] for x in rpc.trace],['doc.new','engine.commands','engine.execute','doc.inspect'])
    def test_missing_native_command(self):
        p=valid();p['steps']=[{'command':'type.create','params':{}}]
        with self.rpc() as rpc:self.assertRaises(PhotoError,apply_plan,rpc,p,[])
    def test_missing_asset_count(self):
        with self.rpc() as rpc:self.assertRaises(PhotoError,apply_plan,rpc,valid(),['x.png'])
    def test_no_binary_is_unavailable(self):
        with patch.dict(os.environ,{'PHOTOCRAFT_BIN':'/definitely/not/installed'}):
            report=run_suite(self.root/'native')
        self.assertEqual(report['status'],'unavailable');self.assertEqual(report['executed'],0);self.assertEqual(report['passed'],0)

class FileTests(unittest.TestCase):
    def setUp(self):self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
    def tearDown(self):self.t.cleanup()
    def test_png_valid(self):p=self.root/'x.png';Image.new('RGB',(20,40)).save(p);preflight(p)
    def test_html_not_png(self):p=self.root/'x.png';p.write_text('<html>not an image</html>');self.assertRaises(Exception,preflight,p)
    def test_invalid_psd(self):p=self.root/'x.psd';p.write_bytes(b'fake');self.assertRaises(PhotoError,preflight,p)
    def test_zip_traversal(self):
        p=self.root/'x.pcraft'
        with zipfile.ZipFile(p,'w') as z:z.writestr('../bad','x')
        self.assertRaises(PhotoError,preflight,p)
    def test_unexpected_large_image(self):
        p=self.root/'x.png';Image.new('RGB',(12001,1)).save(p);self.assertRaises(PhotoError,preflight,p)
    def test_worker_patch_preserves_config(self):
        s='const BOT_TOKEN="KEEP";\nasync function handleCallback(cq) {\n}\n// ---------- handlers ----------\nasync function handleUpdate(update) {\n  bindThread(msg);\n}\n'
        out=patch_worker(s,'// PHOTOCRAFT TELEGRAM MODULE v0.1 EXPERIMENTAL\nfunction pcHandleMessage(){}')
        self.assertIn('const BOT_TOKEN="KEEP"',out);self.assertIn('if (await pcHandleMessage(msg)) return;',out)
    def test_marker_in_documentation_is_ignored(self):
        s='// Paste before "// ---------- handlers ----------".\nasync function handleCallback(cq) {\n}\n// ---------- handlers ----------\nasync function handleUpdate(update) {\n  bindThread(msg);\n}\n'
        out=patch_worker(s,'// PHOTOCRAFT TELEGRAM MODULE v0.1 EXPERIMENTAL')
        self.assertTrue(out.startswith('// Paste before "// ---------- handlers ----------".'))
        self.assertEqual(out.count('// PHOTOCRAFT TELEGRAM MODULE'),1)
    def test_worker_patch_refuses_duplicates(self):self.assertRaises(ValueError,patch_worker,'// PHOTOCRAFT TELEGRAM MODULE v0.1 EXPERIMENTAL','')
    def test_worker_patch_refuses_unknown_base(self):self.assertRaises(ValueError,patch_worker,'no worker here','')
    def test_runner_authorization(self):
        with patch.dict(os.environ,{'PHOTOCRAFT_ALLOWED_USERS':'123'}):
            p={'chat_id':10,'user_id':123,'op':'preview','plan':valid()};allowed_payload(p)
            p['user_id']=124;self.assertRaises(PhotoError,allowed_payload,p)
    def test_runner_requires_authorization(self):
        with patch.dict(os.environ,{'PHOTOCRAFT_ALLOWED_USERS':''}):self.assertRaises(PhotoError,allowed_payload,{'chat_id':10,'user_id':123,'op':'test'})



# ============================================================================
# Single-file orchestration, diagnostics and CLI
# ============================================================================
NATIVE_CASES = 12


def archive_sha256(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def redact(text: Any, payload: dict | None = None) -> str:
    """Remove known credentials and file IDs, without printing the input recipe."""
    value = str(text)
    secrets = []
    for name, secret in os.environ.items():
        if secret and len(secret) >= 4 and re.search(r'TOKEN|SECRET|PASSWORD|API_HASH|SESSION', name, re.I):
            secrets.append(secret)
    if isinstance(payload, dict):
        plan = payload.get('plan')
        if isinstance(plan, dict) and isinstance(plan.get('assets'), list):
            for asset_item in plan['assets']:
                if isinstance(asset_item, dict) and isinstance(asset_item.get('file_id'), str):
                    secrets.append(asset_item['file_id'])
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'(?:file/)?bot\d+:[A-Za-z0-9_-]+', 'bot[REDACTED]', value)
    return value[:5000]


def failure_detail(exc: Exception, payload: dict | None = None) -> str:
    detail = type(exc).__name__ + ': ' + str(exc)
    if isinstance(exc, subprocess.CalledProcessError):
        for stream in (exc.stdout, exc.stderr):
            if stream:
                detail += '\n' + (stream.decode('utf-8', 'replace') if isinstance(stream, bytes) else str(stream))[-1800:]
    return redact(detail, payload)


def read_payload(event_file: Path | None = None) -> dict:
    name = event_file or os.getenv('GITHUB_EVENT_PATH')
    if not name:
        raise PhotoError('GITHUB_EVENT_PATH ausente. Use --event evento.json ou execute pelo GitHub Actions.')
    path = Path(name)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise PhotoError('Evento grande demais.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise PhotoError('Evento nao e JSON valido.') from exc
    if not isinstance(data, dict) or not isinstance(data.get('client_payload'), dict):
        raise PhotoError('Evento sem client_payload valido.')
    return data['client_payload']


def mask_payload(payload: dict) -> None:
    """Only emit masks for already-authorized, validated identifiers."""
    for item in (payload.get('plan') or {}).get('assets', []):
        print('::add-mask::' + item['file_id'], flush=True)
    print('PhotoCraft: payload autorizado; conteudo da receita omitido do log.', flush=True)


def require_native_pass(report: dict) -> None:
    cases = report.get('cases', [])
    valid_result = (
        report.get('mocked') is False and report.get('status') == 'passed'
        and report.get('executed') == NATIVE_CASES
        and report.get('passed') == NATIVE_CASES and report.get('failed') == 0
        and len(cases) == NATIVE_CASES
        and all(case.get('status') == 'passed' for case in cases)
    )
    if not valid_result:
        failures = [str(case.get('name', '?')) + ': ' + str(case.get('error', 'falhou'))
                    for case in cases if case.get('status') != 'passed']
        reason = report.get('reason') or '; '.join(failures[:3]) or 'suite incompleta ou resultado invalido'
        raise PhotoError('Teste nativo nao aprovado (' + str(report.get('status', 'unknown')) + '): ' + str(reason))


class ExecutionReport:
    def __init__(self, root: Path, operation: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'execution-report.json'
        self.payload: dict | None = None
        self.current = 'start'
        self.data = {'integration': INTEGRATION_VERSION, 'operation': operation,
                     'status': 'running', 'stages': [], 'native_rendering_confirmed': False}
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def step(self, name: str, function, *args, **kwargs):
        self.current = name
        record = {'stage': name, 'status': 'running'}
        self.data['stages'].append(record)
        self.save()
        start = time.monotonic()
        print('[photocraft:unico] ' + json.dumps({'stage': name, 'status': 'started'}), flush=True)
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            record.update(status='failed', seconds=round(time.monotonic() - start, 3),
                          error=failure_detail(exc, self.payload))
            self.data.update(status='failed', failed_stage=name)
            self.save()
            raise
        record.update(status='passed', seconds=round(time.monotonic() - start, 3))
        self.save()
        print('[photocraft:unico] ' + json.dumps(record, ensure_ascii=False), flush=True)
        return result

    def finish(self) -> None:
        self.data['status'] = 'passed'
        self.save()


def _validated_event(event_file: Path | None) -> dict:
    data = read_payload(event_file)
    allowed_payload(data)
    mask_payload(data)
    return data


def _run_native_gate(root: Path) -> dict:
    if not __debug__:
        raise PhotoError('Nao execute testes nativos com python -O; as assercoes precisam estar habilitadas.')
    result = run_suite(root)
    require_native_pass(result)
    return result


def telegram_notice(payload: dict, text: str) -> None:
    # Do not notify a user who failed the authorization guard.
    allowed_payload(payload)
    with_token = os.getenv('BOT_TOKEN')
    if not with_token:
        return
    tg = Telegram(payload['chat_id'], payload.get('thread_id'))
    try:
        tg.say(redact(text, payload)[:3400])
    finally:
        tg.close()


def send_task(payload: dict, native_result: dict, report_root: Path) -> None:
    tg = Telegram(payload['chat_id'], payload.get('thread_id'))
    try:
        if payload['op'] == 'test':
            tg.document(report_root / 'native' / 'native-report.json',
                        'Teste real do motor PhotoCraft; nao usa simulacao.')
            tg.say(f"PhotoCraft: {native_result['passed']} testes nativos passaram. "
                   'Isso nao certifica todas as ferramentas nem fidelidade ao Photoshop.')
        else:
            with tempfile.TemporaryDirectory(prefix='photocraft-job-') as tmp:
                run_job(payload, Path(tmp), tg)
    finally:
        tg.close()


def run_pipeline(args) -> int:
    report = ExecutionReport(args.out, 'run')
    payload = None
    try:
        payload = report.step('authorize', _validated_event, args.event)
        report.payload = payload
        report.step('install', _install_with_diagnostics, args.dir, report.root)
        native_result = report.step('native-test', _run_native_gate, report.root / 'native')
        report.data['native_rendering_confirmed'] = True
        report.save()
        report.step('process', send_task, payload, native_result, report.root)
        report.finish()
        return 0
    except Exception as exc:
        detail = failure_detail(exc, payload)
        print('[photocraft:unico] ' + json.dumps({'stage': report.current, 'status': 'failed', 'error': detail}, ensure_ascii=False), file=sys.stderr, flush=True)
        if payload is not None:
            message = 'PhotoCraft: falha na etapa ' + report.current + '.\n' + detail[:2200]
            if not report.data['native_rendering_confirmed']:
                message += '\nA edicao nao foi iniciada; nenhum resultado grafico foi confirmado.'
            else:
                message += '\nA tarefa nao foi concluida; alguns arquivos podem ja ter sido enviados.'
            server = os.getenv('GITHUB_SERVER_URL', 'https://github.com')
            repository = os.getenv('GITHUB_REPOSITORY', '')
            run_id = os.getenv('GITHUB_RUN_ID', '')
            if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) and run_id.isdigit() and server == 'https://github.com':
                message += '\nExecucao: ' + server + '/' + repository + '/actions/runs/' + run_id
            try:
                telegram_notice(payload, message)
            except Exception as notice_error:
                print('PhotoCraft: aviso Telegram nao enviado (' + type(notice_error).__name__ + ').', file=sys.stderr)
        return 1


def local_selftest(out: Path | None = None) -> bool:
    """Local adapter tests only. The native check never calls this as a fallback."""
    classes = (PlanTests, ProcessTests, FileTests, UnifiedTests, InstallerHTTPTests, InstallerDiscoveryTests)
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in classes)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {'suite': 'adapter-selftest', 'mocked_transport': True,
               'native_rendering_confirmed': False, 'executed': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'status': 'passed' if result.wasSuccessful() else 'failed'}
    if out:
        out = Path(out); out.mkdir(parents=True, exist_ok=True)
        (out / 'selftest-report.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)
    return result.wasSuccessful()


def check_pipeline(args) -> int:
    report = ExecutionReport(args.out, 'check')
    try:
        def test_adapter():
            if not local_selftest(report.root):
                raise PhotoError('Teste do adaptador falhou. Isso nao e um teste grafico.')
        report.step('selftest', test_adapter)
        report.step('install', _install_with_diagnostics, args.dir, report.root)
        report.step('native-test', _run_native_gate, report.root / 'native')
        report.data['native_rendering_confirmed'] = True
        report.finish()
        return 0
    except Exception as exc:
        print('[photocraft:unico] ' + json.dumps({'stage': report.current, 'status': 'failed', 'error': failure_detail(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def patch_worker_files(source: Path, output: Path, block_file: Path | None = None) -> None:
    source = Path(source); output = Path(output)
    if source.resolve() == output.resolve():
        raise PhotoError('Use um arquivo de saida diferente para preservar o Worker original.')
    if output.exists():
        raise PhotoError('A saida ja existe. Escolha outro nome; nao vou sobrescrever seu Worker.')
    block = Path(block_file).read_text(encoding='utf-8') if block_file else WORKER_MODULE.lstrip('\n')
    updated = patch_worker(source.read_text(encoding='utf-8'), block)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(updated)
    print('Worker atualizado em ' + str(output))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version', version=INTEGRATION_VERSION)
    commands = parser.add_subparsers(dest='mode', required=True)
    commands.add_parser('selftest', help='Testes locais do adaptador; NAO renderiza pelo motor.').add_argument('--out', type=Path)
    validate = commands.add_parser('validate', help='Validar autorizacao e receita do evento, sem instalar nem editar.')
    validate.add_argument('--event', type=Path)
    ins = commands.add_parser('install', help='Instalar e verificar o PhotoCraft nativo fixado em 0.23.1.')
    ins.add_argument('--dir', type=Path, default=Path('.photocraft'))
    native = commands.add_parser('test', help='12 testes do executavel REAL ja instalado; sem simulacao.')
    native.add_argument('--out', type=Path, default=Path('photocraft-native-results'))
    for name, description in [('run', 'Autorizar, instalar, testar e processar a tarefa Telegram.'),
                              ('check', 'Testar o adaptador, instalar e executar testes nativos sem Telegram.')]:
        sub = commands.add_parser(name, help=description)
        sub.add_argument('--dir', type=Path, default=Path('.photocraft'))
        sub.add_argument('--out', type=Path, default=Path('photocraft-results'))
        if name == 'run':
            sub.add_argument('--event', type=Path)
    worker = commands.add_parser('patch-worker', help='Aplicar modulo JS embutido ao Worker; opcional, sem publicar.')
    worker.add_argument('--input', type=Path, required=True)
    worker.add_argument('--output', type=Path, required=True)
    worker.add_argument('--block', type=Path, help='Bloco alternativo local; o padrao ja esta dentro deste Python.')
    return parser


def cli(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.mode == 'selftest':
            return 0 if local_selftest(args.out) else 1
        if args.mode == 'validate':
            _validated_event(args.event)
        elif args.mode == 'install':
            install(args.dir)
        elif args.mode == 'test':
            if not __debug__:
                raise PhotoError('Os testes nativos exigem Python sem -O.')
            result = run_suite(args.out)
            print(json.dumps({k: v for k, v in result.items() if k != 'cases'}, indent=2))
            if result.get('status') == 'unavailable':
                return 2
            require_native_pass(result)
        elif args.mode == 'run':
            return run_pipeline(args)
        elif args.mode == 'check':
            return check_pipeline(args)
        elif args.mode == 'patch-worker':
            patch_worker_files(args.input, args.output, args.block)
        return 0
    except Exception as exc:
        print('PhotoCraft: ' + failure_detail(exc), file=sys.stderr)
        return 1


class UnifiedTests(unittest.TestCase):
    """Regression tests for the consolidation. Pipeline services are mocked here."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, payload=None):
        path = self.root / 'event.json'
        path.write_text(json.dumps({'client_payload': payload or {'user_id': 123, 'chat_id': 123, 'op': 'test'}}))
        return path

    def args(self):
        return argparse.Namespace(event=self.event(), dir=self.root / 'engine', out=self.root / 'report')

    def native_pass(self):
        return {'mocked': False, 'status': 'passed', 'executed': 12, 'passed': 12, 'failed': 0,
                'cases': [{'name': f'fixture-{i}', 'status': 'passed'} for i in range(12)]}

    def test_parser_modes(self):
        for mode in ('selftest', 'validate', 'install', 'test', 'run', 'check'):
            self.assertEqual(make_parser().parse_args([mode]).mode, mode)

    def test_parser_worker(self):
        a = make_parser().parse_args(['patch-worker', '--input', 'old.js', '--output', 'new.js'])
        self.assertEqual(a.input, Path('old.js'))

    def test_release_pin_preserved(self):
        self.assertEqual(VERSION, '0.23.1')
        self.assertEqual(REPO, 'storytold/photocraft')

    def test_no_old_python_module_imports(self):
        import ast
        tree = ast.parse(Path(__file__).read_text(encoding='utf-8'))
        modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        forbidden = {'photocraft_bridge', 'photocraft_native_tests', 'photocraft_job',
                     'install_photocraft', 'patch_worker_photocraft'}
        self.assertFalse(forbidden.intersection(modules))

    def test_archive_digest(self):
        path = self.root / 'x'; path.write_bytes(b'abc')
        self.assertEqual(archive_sha256(path), hashlib.sha256(b'abc').hexdigest())

    def test_event_read(self):
        self.assertEqual(read_payload(self.event())['op'], 'test')

    def test_event_reject_missing_payload(self):
        path = self.root / 'event.json'; path.write_text('{}')
        self.assertRaises(PhotoError, read_payload, path)

    def test_event_reject_invalid_json(self):
        path = self.root / 'event.json'; path.write_text('not JSON')
        self.assertRaises(PhotoError, read_payload, path)

    def test_event_reject_missing_path(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_PATH': ''}):
            self.assertRaises(PhotoError, read_payload)

    def test_event_reject_large(self):
        path = self.root / 'event.json'; path.write_bytes(b' ' * (2 * 1024**2 + 1))
        self.assertRaises(PhotoError, read_payload, path)

    def test_redaction(self):
        p = {'plan': {'assets': [{'file_id': 'ASSET_ID_abcdef'}]}}
        with patch.dict(os.environ, {'BOT_TOKEN': 'token-abcdef', 'TG_API_HASH': 'hash-123456'}):
            text = redact('token-abcdef hash-123456 ASSET_ID_abcdef', p)
        self.assertNotIn('abcdef', text); self.assertNotIn('123456', text)

    def test_subprocess_error_redaction(self):
        e = subprocess.CalledProcessError(1, ['engine'], stderr='failure token-abcdef')
        with patch.dict(os.environ, {'BOT_TOKEN': 'token-abcdef'}):
            detail = failure_detail(e)
        self.assertIn('CalledProcessError', detail)
        self.assertNotIn('token-abcdef', detail)

    def test_complete_native_pass_required(self):
        require_native_pass(self.native_pass())
        report = self.native_pass(); report['executed'] = 11
        self.assertRaises(PhotoError, require_native_pass, report)

    def test_mocked_native_pass_rejected(self):
        report = self.native_pass(); report['mocked'] = True
        self.assertRaises(PhotoError, require_native_pass, report)

    def test_native_failed_case_in_error(self):
        report = self.native_pass(); report['cases'][0] = {'name': 'roundtrip', 'status': 'failed', 'error': 'bad pixel'}
        with self.assertRaisesRegex(PhotoError, 'roundtrip: bad pixel'):
            require_native_pass(report)

    def test_no_silent_pass_without_native(self):
        with patch.dict(os.environ, {'PHOTOCRAFT_BIN': '/definitely/not/installed'}):
            code = cli(['test', '--out', str(self.root / 'native')])
        self.assertEqual(code, 2)
        report = json.loads((self.root / 'native/native-report.json').read_text())
        self.assertEqual(report['executed'], 0)

    def test_install_same_process_env_and_secret_isolation(self):
        import io
        content = _test_elf_header()  # synthetic header; process probe is mocked
        archive = self.root / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as tf:
            item = tarfile.TarInfo('package/bin/photocraft-cli'); item.size = len(content)
            tf.addfile(item, io.BytesIO(content))
        digest = archive_sha256(archive)
        url = f'https://github.com/{REPO}/releases/download/v{VERSION}/fixture.tar.gz'
        data = {'tag_name': f'v{VERSION}', 'assets': [{'name': f'photocraft-{VERSION}-linux-x86_64.tar.gz',
                'browser_download_url': url, 'digest': 'sha256:' + digest}]}
        def fake_get(link, dest=None):
            if dest is None:
                return json.dumps(data).encode()
            shutil.copyfile(archive, dest)
        result = subprocess.CompletedProcess(['fixture'], 0, stdout='PhotoCraft '+VERSION, stderr='')
        with patch(__name__+'.get', side_effect=fake_get), patch(__name__+'.subprocess.run', return_value=result) as process:
            with patch.dict(os.environ, {'BOT_TOKEN': 'secret-123456', 'GITHUB_TOKEN': 'secret-654321', 'GITHUB_ENV': ''}):
                installed = install(self.root / 'install')
                self.assertEqual(os.environ['PHOTOCRAFT_BIN'], installed['binary'])
                child_env = process.call_args.kwargs['env']
                self.assertNotIn('BOT_TOKEN', child_env)
                self.assertNotIn('GITHUB_TOKEN', child_env)

    def test_unauthorized_run_never_installs(self):
        with patch.dict(os.environ, {'PHOTOCRAFT_ALLOWED_USERS': ''}), patch(__name__+'.install') as installer:
            with patch(__name__+'.telegram_notice') as notice:
                code = run_pipeline(self.args())
        self.assertEqual(code, 1); installer.assert_not_called(); notice.assert_not_called()
        report = json.loads((self.root / 'report/execution-report.json').read_text())
        self.assertEqual(report['failed_stage'], 'authorize')

    def test_install_failure_prevents_native_and_task(self):
        with patch.dict(os.environ, {'PHOTOCRAFT_ALLOWED_USERS': '123'}), patch(__name__+'.install', side_effect=RuntimeError('fixture install failure')):
            with patch(__name__+'._run_native_gate') as gate, patch(__name__+'.send_task') as task:
                with patch(__name__+'.telegram_notice') as notice:
                    code = run_pipeline(self.args())
        self.assertEqual(code, 1); gate.assert_not_called(); task.assert_not_called()
        self.assertIn('install', notice.call_args.args[1])

    def test_native_failure_prevents_task(self):
        with patch.dict(os.environ, {'PHOTOCRAFT_ALLOWED_USERS': '123'}), patch(__name__+'.install'):
            with patch(__name__+'._run_native_gate', side_effect=PhotoError('fixture graphics failure')):
                with patch(__name__+'.send_task') as task, patch(__name__+'.telegram_notice'):
                    code = run_pipeline(self.args())
        self.assertEqual(code, 1); task.assert_not_called()
        report = json.loads((self.root / 'report/execution-report.json').read_text())
        self.assertEqual(report['failed_stage'], 'native-test')
        self.assertFalse(report['native_rendering_confirmed'])

    def test_pipeline_sequence_with_mocks_only(self):
        calls = []
        with patch.dict(os.environ, {'PHOTOCRAFT_ALLOWED_USERS': '123'}):
            with patch(__name__+'.install', side_effect=lambda _: calls.append('install')):
                with patch(__name__+'._run_native_gate', side_effect=lambda _: calls.append('native-test') or self.native_pass()):
                    with patch(__name__+'.send_task', side_effect=lambda *_: calls.append('process')):
                        self.assertEqual(run_pipeline(self.args()), 0)
        self.assertEqual(calls, ['install', 'native-test', 'process'])

    def test_execution_report_redacts(self):
        report = ExecutionReport(self.root, 'fixture')
        with patch.dict(os.environ, {'BOT_TOKEN': 'secret-abcdef'}):
            with self.assertRaises(RuntimeError):
                def fail():
                    raise RuntimeError('error with secret-abcdef')
                report.step('fixture-stage', fail)
        raw = report.path.read_text()
        self.assertNotIn('secret-abcdef', raw)
        self.assertEqual(json.loads(raw)['failed_stage'], 'fixture-stage')

    def test_embedded_worker_present(self):
        self.assertIn('async function pcHandleMessage', WORKER_MODULE)
        self.assertIn('const PC_TTL', WORKER_MODULE)

    def test_patch_worker_files_refuses_in_place(self):
        self.assertRaises(PhotoError, patch_worker_files, self.root/'worker.js', self.root/'worker.js')

    def test_patch_worker_files_refuses_overwrite(self):
        out = self.root / 'out.js'; out.write_text('KEEP')
        self.assertRaises(PhotoError, patch_worker_files, self.root/'worker.js', out)
        self.assertEqual(out.read_text(), 'KEEP')

    def test_standalone_help_without_other_project_files(self):
        path = self.root / 'only.py'; shutil.copyfile(__file__, path)
        p = subprocess.run([sys.executable, str(path), '--help'], capture_output=True, text=True, cwd=self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('patch-worker', p.stdout)

    def test_standalone_validate_without_old_scripts(self):
        path = self.root / 'only.py'; shutil.copyfile(__file__, path)
        event = self.event()
        env = {**os.environ, 'PHOTOCRAFT_ALLOWED_USERS': '123'}
        p = subprocess.run([sys.executable, str(path), 'validate', '--event', str(event)],
                           capture_output=True, text=True, cwd=self.root, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('payload autorizado', p.stdout)




class InstallerHTTPTests(unittest.TestCase):
    """HTTP responses are simulated. These tests do NOT execute PhotoCraft."""
    API = 'https://api.github.com/repos/storytold/photocraft/releases/tags/v0.23.1'
    ASSET = 'https://github.com/storytold/photocraft/releases/download/v0.23.1/photocraft-0.23.1-linux-x86_64.tar.gz'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {'GITHUB_TOKEN': '', 'GH_TOKEN': ''})
        self.environment.start()
        self.sleeper = patch(__name__ + '.time.sleep')
        self.sleep = self.sleeper.start()

    def tearDown(self):
        self.sleeper.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def response(self, data=b'{}', headers=None):
        import io
        from email.message import Message
        obj = io.BytesIO(data)
        obj.headers = Message()
        for key, value in (headers or {}).items():
            obj.headers[key] = str(value)
        return obj

    def error(self, status, body=b'', headers=None, url=None):
        from email.message import Message
        import io
        msg = Message()
        for key, value in (headers or {}).items():
            msg[key] = str(value)
        return urllib.error.HTTPError(url or self.API, status, 'test HTTP error', msg, io.BytesIO(body))

    def redirect(self, target, source=None):
        req = urllib.request.Request(source or self.API, headers={
            'Authorization': 'Bearer fixture-redirect-secret',
            'Cookie': 'fixture-cookie', 'Proxy-Authorization': 'fixture-proxy',
            'User-Agent': 'fixture'})
        return _InstallRedirect().redirect_request(req, None, 302, 'Found', {}, target)

    def test_api_auth_from_github_token(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-github-token'}):
            headers = _install_headers(self.API)
        self.assertEqual(headers['Authorization'], 'Bearer fixture-github-token')
        self.assertEqual(headers['X-GitHub-Api-Version'], '2022-11-28')

    def test_gh_token_fallback(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'fixture-local-token'}):
            self.assertEqual(_install_headers(self.API)['Authorization'], 'Bearer fixture-local-token')

    def test_github_token_precedence(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-one', 'GH_TOKEN': 'fixture-two'}):
            self.assertEqual(_install_headers(self.API)['Authorization'], 'Bearer fixture-one')

    def test_absent_token_is_not_fake_authenticated(self):
        self.assertNotIn('Authorization', _install_headers(self.API))

    def test_token_whitespace_rejected(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture\nsecret'}):
            self.assertRaises(PhotoError, _install_headers, self.API)

    def test_no_token_for_public_release(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}):
            self.assertNotIn('Authorization', _install_headers(self.ASSET))

    def test_no_token_for_lookalike_domain(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}):
            self.assertNotIn('Authorization', _install_headers('https://api.github.com.example.invalid/path'))

    def test_no_token_for_query_containing_api_host(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}):
            self.assertNotIn('Authorization', _install_headers('https://example.invalid/?next=api.github.com'))

    def test_no_token_for_cdn(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}):
            self.assertNotIn('Authorization', _install_headers('https://release-assets.githubusercontent.com/a?sig=x'))

    def test_invalid_urls_rejected(self):
        for url in ('http://api.github.com/x', 'file:///x', 'https://api.github.com:8443/x',
                    'https://user:password@api.github.com/x', 'https://api.github.com/x\n',
                    'https://api.github.com:invalid/x', 'not-a-url'):
            with self.subTest(url=url):
                self.assertRaises(PhotoError, _install_headers, url)

    def test_api_redirect_retains_auth_on_same_host(self):
        request = self.redirect('https://api.github.com/repositories/1/releases')
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-redirect-secret')

    def test_cross_host_redirect_removes_credentials(self):
        for url in ('https://github.com/x', 'https://release-assets.githubusercontent.com/x',
                    'https://api.github.com.example.invalid/x'):
            with self.subTest(url=url):
                request = self.redirect(url)
                for key in ('Authorization', 'Cookie', 'Proxy-authorization'):
                    self.assertIsNone(request.get_header(key))
                self.assertEqual(request.get_header('User-agent'), 'fixture')

    def test_redirect_downgrade_refused(self):
        self.assertRaises(PhotoError, self.redirect, 'http://api.github.com/x')

    def test_redirect_userinfo_refused(self):
        self.assertRaises(PhotoError, self.redirect, 'https://user:password@api.github.com/x')

    def test_redirect_to_api_does_not_add_auth(self):
        req = urllib.request.Request(self.ASSET, headers={'User-Agent': 'fixture'})
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}):
            result = _InstallRedirect().redirect_request(req, None, 302, 'Found', {}, self.API)
        self.assertIsNone(result.get_header('Authorization'))

    def test_request_sends_auth_header(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}):
            with patch(__name__ + '.urllib.request.build_opener') as build:
                build.return_value.open.return_value = self.response(b'{"ok":true}')
                self.assertEqual(get(self.API), b'{"ok":true}')
                req = build.return_value.open.call_args.args[0]
                self.assertEqual(req.get_header('Authorization'), 'Bearer fixture-secret')
        self.sleep.assert_not_called()

    def test_request_logs_never_show_token(self):
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret-do-not-print'}):
            with patch(__name__ + '.urllib.request.build_opener') as build, redirect_stdout(output):
                build.return_value.open.return_value = self.response()
                get(self.API)
        self.assertNotIn('fixture-secret-do-not-print', output.getvalue())
        self.assertIn('"authenticated": true', output.getvalue())

    def test_metadata_size_limit(self):
        with patch(__name__ + '.MAX_METADATA', 4), patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.return_value = self.response(b'12345')
            self.assertRaisesRegex(PhotoError, 'Metadados', get, self.API)

    def test_complete_download_replaces_atomically(self):
        destination = self.root / 'engine.tar.gz'
        destination.write_bytes(b'old')
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.return_value = self.response(b'new-content', {'Content-Length': '11'})
            get(self.ASSET, destination)
        self.assertEqual(destination.read_bytes(), b'new-content')
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_short_download_preserves_old_archive(self):
        destination = self.root / 'engine.tar.gz'
        destination.write_bytes(b'old')
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.return_value = self.response(b'short', {'Content-Length': '20'})
            self.assertRaisesRegex(PhotoError, 'incompleto', get, self.ASSET, destination)
        self.assertEqual(destination.read_bytes(), b'old')
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_oversize_download_cleans_temp(self):
        destination = self.root / 'engine.tar.gz'
        with patch(__name__ + '.MAX_ARCHIVE', 4), patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.return_value = self.response(b'12345')
            self.assertRaisesRegex(PhotoError, 'limite', get, self.ASSET, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_retry_after_seconds(self):
        self.assertEqual(_rate_limit_wait({'Retry-After': '17'}, 0, now=1000), 17)

    def test_retry_after_http_date(self):
        from email.utils import formatdate
        value = formatdate(1021, usegmt=True)
        self.assertEqual(_rate_limit_wait({'Retry-After': value}, 0, now=1000), 21)

    def test_primary_reset_respected(self):
        self.assertEqual(_rate_limit_wait({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1030'}, 0, now=1000), 31)

    def test_stricter_wait_is_used(self):
        self.assertEqual(_rate_limit_wait({'Retry-After': '10', 'X-RateLimit-Remaining': '0',
                                          'X-RateLimit-Reset': '1030'}, 0, now=1000), 31)

    def test_secondary_wait_has_backoff(self):
        self.assertEqual(_rate_limit_wait({}, 0, now=1000), 60)
        self.assertEqual(_rate_limit_wait({}, 1, now=1000), 120)

    def test_invalid_retry_after_uses_safe_delay(self):
        self.assertEqual(_rate_limit_wait({'Retry-After': 'bogus'}, 0, now=1000), 60)

    def test_retry_after_zero_not_busy_loop(self):
        self.assertEqual(_rate_limit_wait({'Retry-After': '0'}, 0, now=1000), 1)

    def test_429_retry_succeeds(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [self.error(429, headers={'Retry-After': 2}), self.response(b'ok')]
            self.assertEqual(get(self.API), b'ok')
            self.assertEqual(build.return_value.open.call_count, 2)
        self.sleep.assert_called_once_with(2)

    def test_primary_403_waits_until_reset(self):
        error = self.error(403, headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1010'})
        with patch(__name__ + '.time.time', return_value=1000), patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [error, self.response(b'ok')]
            self.assertEqual(get(self.API), b'ok')
        self.sleep.assert_called_once_with(11)

    def test_secondary_403_waits_at_least_minute(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [self.error(403, b'secondary rate limit'), self.response(b'ok')]
            self.assertEqual(get(self.API), b'ok')
        self.sleep.assert_called_once_with(60)

    def test_long_rate_limit_stops_without_early_retry(self):
        error = self.error(403, headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '5000'})
        with patch(__name__ + '.time.time', return_value=1000), patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = error
            with self.assertRaisesRegex(PhotoError, 'Reset informado') as caught:
                get(self.API)
            self.assertEqual(build.return_value.open.call_count, 1)
            self.assertIn('GITHUB_TOKEN: ${{ github.token }}', str(caught.exception))
        self.sleep.assert_not_called()

    def test_auth_failure_does_not_retry_anonymously(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}), patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = self.error(401, b'fixture-secret')
            with self.assertRaisesRegex(PhotoError, 'token recusado') as caught:
                get(self.API)
            self.assertEqual(build.return_value.open.call_count, 1)
            self.assertNotIn('fixture-secret', str(caught.exception))
        self.sleep.assert_not_called()

    def test_403_permission_failure_not_classified_rate_limit(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = self.error(403, b'Resource not accessible')
            with self.assertRaisesRegex(PhotoError, 'HTTP 403') as caught:
                get(self.API)
            self.assertNotIn('limite de requisicoes', str(caught.exception))
            self.assertEqual(build.return_value.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_404_not_retried(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = self.error(404)
            self.assertRaisesRegex(PhotoError, 'HTTP 404', get, self.API)
            self.assertEqual(build.return_value.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_transient_503_retried(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [self.error(503), self.response(b'ok')]
            self.assertEqual(get(self.API), b'ok')
        self.sleep.assert_called_once_with(2)

    def test_503_retry_after_respected(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [self.error(503, headers={'Retry-After': '19'}), self.response(b'ok')]
            self.assertEqual(get(self.API), b'ok')
        self.sleep.assert_called_once_with(19)

    def test_retries_bounded(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [self.error(429, headers={'Retry-After': '1'}) for _ in range(3)]
            self.assertRaisesRegex(PhotoError, 'limite de requisicoes', get, self.API)
            self.assertEqual(build.return_value.open.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_retry_wait_budget_bounded(self):
        with patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = [self.error(429, headers={'Retry-After': '70'}) for _ in range(3)]
            self.assertRaises(PhotoError, get, self.API)
            self.assertEqual(build.return_value.open.call_count, 2)
        self.sleep.assert_called_once_with(70)

    def test_cdn_failure_not_reported_authenticated(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}), patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = self.error(403, url='https://release-assets.githubusercontent.com/a?sig=fixture-private-query')
            with self.assertRaises(PhotoError) as caught:
                get(self.API)
            self.assertIn('sem autenticacao', str(caught.exception))
            self.assertNotIn('fixture-private-query', str(caught.exception))

    def test_auth_rate_error_omits_credentials_and_body(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-secret'}), patch(__name__ + '.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = self.error(403, b'rate limit fixture-secret', {'Retry-After': '500'})
            with self.assertRaises(PhotoError) as caught:
                get(self.API)
            self.assertIn('autenticada', str(caught.exception))
            self.assertNotIn('fixture-secret', str(caught.exception))

    def test_digest_mismatch_remains_fatal(self):
        def fake_get(link, dest=None):
            if dest is None:
                return json.dumps({'tag_name': 'v' + VERSION, 'assets': [{
                    'name': f'photocraft-{VERSION}-linux-x86_64.tar.gz',
                    'browser_download_url': self.ASSET, 'digest': 'sha256:' + '0' * 64}]}).encode()
            Path(dest).write_bytes(b'not-the-verified-release')
        with patch(__name__ + '.get', side_effect=fake_get), patch(__name__ + '.subprocess.run') as process:
            self.assertRaisesRegex(RuntimeError, 'SHA256 divergente', install, self.root/'install')
            process.assert_not_called()

    def test_missing_checksum_remains_fatal(self):
        data = {'tag_name': 'v' + VERSION, 'assets': [{
            'name': f'photocraft-{VERSION}-linux-x86_64.tar.gz', 'browser_download_url': self.ASSET}]}
        with patch(__name__ + '.get', return_value=json.dumps(data).encode()), patch(__name__ + '.subprocess.run') as process:
            self.assertRaisesRegex(RuntimeError, 'checksum verificavel', install, self.root/'install')
            process.assert_not_called()

    def test_unexpected_checksum_host_is_refused(self):
        data = {'tag_name': 'v' + VERSION, 'assets': [{
            'name': f'photocraft-{VERSION}-linux-x86_64.tar.gz', 'browser_download_url': self.ASSET},
            {'name': 'SHA256SUMS.txt', 'browser_download_url': 'https://example.invalid/SHA256SUMS.txt'}]}
        with patch(__name__ + '.get', return_value=json.dumps(data).encode()) as getter:
            self.assertRaisesRegex(RuntimeError, 'URL inesperada para o checksum', install, self.root/'install')
            self.assertEqual(getter.call_count, 1)

    def test_github_token_redacted_in_execution_report(self):
        report = ExecutionReport(self.root/'report', 'fixture')
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-private-token'}):
            def fail():
                raise RuntimeError('fixture-private-token must be removed')
            self.assertRaises(RuntimeError, report.step, 'install', fail)
        self.assertNotIn('fixture-private-token', report.path.read_text())

def _test_elf_header(*, machine=62, elf_type=3, elf_class=2, endian=1,
                     version=1, header_size=64) -> bytes:
    """Synthetic ELF header for parser tests. NOT a working PhotoCraft binary."""
    import struct
    data = bytearray(64)
    data[:7] = bytes([0x7f, 0x45, 0x4c, 0x46, elf_class, endian, 1])
    struct.pack_into('<HHI', data, 16, elf_type, machine, version)
    struct.pack_into('<H', data, 52, header_size)
    return bytes(data)


class InstallerDiscoveryTests(unittest.TestCase):
    """Local tar/header/selection tests. Network and native probe are mocked."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tree = self.root / 'tree'
        self.tree.mkdir()
        self.report = self.root / 'discovery.json'
        self.environment = patch.dict(os.environ, {'GITHUB_ENV': '', 'PHOTOCRAFT_BIN': ''})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def put(self, name='package/bin/photocraft-cli', content=None, mode=0o644):
        path = self.tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_test_elf_header() if content is None else content)
        path.chmod(mode)
        return path

    def select(self):
        return _discover_cli(self.tree, self.report)

    def archive(self, files=None):
        import io
        files = files if files is not None else {
            'package/bin/photocraft-cli': _test_elf_header(),
            'package/share/bash-completion/completions/photocraft-cli': b'# bash completion fixture\n',
            'package/share/doc/README.txt': b'synthetic archive, not PhotoCraft\n',
        }
        archive = self.root / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as tf:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                member.size = len(data)
                tf.addfile(member, io.BytesIO(data))
        digest = archive_sha256(archive)
        asset = f'photocraft-{VERSION}-linux-x86_64.tar.gz'
        url = f'https://github.com/{REPO}/releases/download/v{VERSION}/{asset}'
        metadata = {'tag_name': 'v' + VERSION, 'assets': [
            {'name': asset, 'browser_download_url': url, 'digest': 'sha256:' + digest}]}
        def fetch(link, dest=None):
            if dest is None:
                return json.dumps(metadata).encode()
            shutil.copyfile(archive, dest)
        return fetch

    def installer(self, files=None, version=None):
        """Context manager: fixture tar bytes + a simulated --version response."""
        from contextlib import ExitStack
        stack = ExitStack()
        getter = stack.enter_context(patch(__name__ + '.get', side_effect=self.archive(files)))
        process = stack.enter_context(patch(__name__ + '.subprocess.run', return_value=
            subprocess.CompletedProcess(['fixture'], 0, 'photocraft-cli ' + (version or VERSION) + ' (fixture)\n', '')))
        return stack, getter, process

    def test_reproduces_basename_collision_and_selects_binary(self):
        binary = self.put()
        self.put('package/share/bash-completion/completions/photocraft-cli', b'# bash completion\n')
        old_candidates = [p for p in self.tree.rglob('photocraft-cli') if p.is_file() and not p.is_symlink()]
        self.assertEqual(len(old_candidates), 2)  # the old installer rejects this
        selected, report = self.select()
        self.assertEqual(selected, binary)
        self.assertEqual(report['name_matches'], 2)
        self.assertEqual(report['native_candidates'], 1)
        self.assertEqual(report['candidates'][1]['reason'], 'not_elf')
        self.assertFalse(report['native_rendering_confirmed'])

    def test_executable_completion_still_not_native(self):
        binary = self.put()
        self.put('share/bash-completion/completions/photocraft-cli', b'#!/bin/sh\nexit 0\n', 0o755)
        self.assertEqual(self.select()[0], binary)

    def test_missing_execute_bit_does_not_confuse_header_selection(self):
        binary = self.put(mode=0o644)
        self.assertEqual(self.select()[0], binary)
        self.assertFalse(binary.stat().st_mode & stat.S_IXUSR)

    def test_usr_bin_layout(self):
        binary = self.put('release/usr/bin/photocraft-cli')
        self.assertEqual(self.select()[0], binary)

    def test_bare_binary_layout(self):
        binary = self.put('photocraft-cli')
        self.assertEqual(self.select()[0], binary)

    def test_completion_only_is_not_executed(self):
        self.put('share/bash-completion/completions/photocraft-cli', b'# shell source')
        with patch(__name__ + '.subprocess.run') as process:
            self.assertRaisesRegex(PhotoError, 'Nenhum photocraft-cli ELF64', self.select)
            process.assert_not_called()
        report = json.loads(self.report.read_text())
        self.assertEqual((report['status'], report['name_matches']), ('missing', 1))

    def test_missing_name_gets_precise_inventory(self):
        self.assertRaisesRegex(PhotoError, '0 nomes encontrados', self.select)
        self.assertEqual(json.loads(self.report.read_text())['native_candidates'], 0)

    def test_two_native_binaries_are_not_chosen_arbitrarily(self):
        self.put('one/bin/photocraft-cli')
        self.put('two/bin/photocraft-cli')
        self.assertRaisesRegex(PhotoError, 'Mais de um', self.select)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['native_candidates'], 2)
        self.assertEqual(report['status'], 'ambiguous')
        self.assertNotIn('selected', report)

    def test_candidate_symlink_is_rejected(self):
        target = self.put('real-binary')
        (self.tree / 'photocraft-cli').symlink_to(target)
        self.assertRaises(PhotoError, self.select)
        self.assertEqual(json.loads(self.report.read_text())['candidates'][0]['reason'], 'symlink')

    def test_parent_symlink_is_rejected(self):
        self.put('real/bin/photocraft-cli')
        (self.tree / 'alias').symlink_to(self.tree / 'real', target_is_directory=True)
        row = _inspect_cli_candidate(self.tree / 'alias/bin/photocraft-cli', self.tree)
        self.assertEqual(row['reason'], 'symlink')

    def test_root_symlink_is_rejected(self):
        link = self.root / 'tree-link'
        link.symlink_to(self.tree, target_is_directory=True)
        self.assertRaisesRegex(PhotoError, 'Diretorio de extracao', _discover_cli, link, self.report)

    def test_missing_root_is_rejected(self):
        self.assertRaises(PhotoError, _discover_cli, self.root / 'absent', self.report)

    def test_unrelated_native_filename_is_not_a_cli(self):
        self.put('package/bin/photocraft')
        self.assertRaises(PhotoError, self.select)
        self.assertEqual(json.loads(self.report.read_text())['name_matches'], 0)

    def test_directory_named_cli_is_not_regular_file(self):
        (self.tree / 'photocraft-cli').mkdir()
        self.assertRaises(PhotoError, self.select)
        self.assertEqual(json.loads(self.report.read_text())['candidates'][0]['reason'], 'not_regular')

    def test_truncated_elf_rejected(self):
        binary = self.put(content=b'\x7fELF')
        self.assertEqual(_inspect_cli_candidate(binary, self.tree)['reason'], 'truncated_elf_header')

    def test_shell_script_rejected_even_with_expected_version_text(self):
        self.put(content=b'#!/bin/sh\necho photocraft-cli 0.23.1\n', mode=0o755)
        self.assertRaises(PhotoError, self.select)

    def test_32bit_elf_rejected(self):
        binary = self.put(content=_test_elf_header(elf_class=1))
        self.assertEqual(_inspect_cli_candidate(binary, self.tree)['reason'], 'not_elf64_little_endian')

    def test_big_endian_elf_rejected(self):
        binary = self.put(content=_test_elf_header(endian=2))
        self.assertEqual(_inspect_cli_candidate(binary, self.tree)['reason'], 'not_elf64_little_endian')

    def test_arm64_elf_rejected(self):
        binary = self.put(content=_test_elf_header(machine=183))
        self.assertEqual(_inspect_cli_candidate(binary, self.tree)['reason'], 'not_x86_64')

    def test_object_file_rejected(self):
        binary = self.put(content=_test_elf_header(elf_type=1))
        self.assertEqual(_inspect_cli_candidate(binary, self.tree)['reason'], 'not_executable_or_pie')

    def test_invalid_elf_version_rejected(self):
        binary = self.put(content=_test_elf_header(version=0))
        self.assertEqual(_inspect_cli_candidate(binary, self.tree)['reason'], 'invalid_elf_header')

    def test_invalid_elf_header_size_rejected(self):
        binary = self.put(content=_test_elf_header(header_size=52))
        self.assertEqual(_inspect_cli_candidate(binary, self.tree)['reason'], 'invalid_elf_header')

    def test_pie_and_exec_accepted_for_subsequent_version_probe(self):
        for value in (2, 3):
            with self.subTest(elf_type=value):
                binary = self.put(content=_test_elf_header(elf_type=value))
                self.assertTrue(_inspect_cli_candidate(binary, self.tree)['eligible'])

    def test_read_error_recorded_without_execution(self):
        binary = self.put()
        with patch.object(Path, 'open', side_effect=PermissionError('fixture')):
            row = _inspect_cli_candidate(binary, self.tree)
        self.assertEqual(row['reason'], 'unreadable')
        self.assertEqual(row['error_type'], 'PermissionError')

    def test_inventory_output_is_bounded(self):
        for number in range(50):
            self.put(f'completion-{number:02d}/photocraft-cli', b'# text')
        self.assertRaises(PhotoError, self.select)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['name_matches'], 50)
        self.assertEqual(len(report['candidates']), 40)
        self.assertTrue(report['inventory_truncated'])

    def test_install_extracts_fixture_and_only_probes_binary(self):
        stack, getter, process = self.installer()
        with stack:
            result = install(self.root / 'install')
        binary = Path(result['binary'])
        self.assertEqual(binary.parts[-2:], ('bin', 'photocraft-cli'))
        self.assertEqual(result['name_matches'], 2)
        self.assertEqual(result['native_candidates'], 1)
        process.assert_called_once()
        self.assertEqual(process.call_args.args[0], [str(binary), '--version'])
        self.assertTrue(binary.stat().st_mode & stat.S_IXUSR)
        completion = binary.parent.parent / 'share/bash-completion/completions/photocraft-cli'
        self.assertFalse(completion.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(result['binary_sha256'], archive_sha256(binary))

    def test_stale_directory_not_searched(self):
        destination = self.root / 'install'
        stale = destination / 'unpacked/old/photocraft-cli'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(_test_elf_header())
        stack, _, _ = self.installer()
        with stack:
            result = install(destination)
        self.assertNotEqual(result['binary'], str(stale))
        self.assertEqual(result['native_candidates'], 1)
        self.assertTrue(stale.exists())

    def test_repeated_install_is_not_ambiguous(self):
        stack, getter, process = self.installer()
        with stack:
            first = install(self.root / 'install')
            second = install(self.root / 'install')
        self.assertNotEqual(first['binary'], second['binary'])
        self.assertEqual(second['native_candidates'], 1)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(getter.call_count, 3)  # metadata twice; archive once

    def test_missing_cli_retains_diagnostics_and_cleans_extraction(self):
        stack, _, process = self.installer({'share/photocraft-cli': b'# only completion'})
        with stack:
            self.assertRaises(PhotoError, install, self.root / 'install')
        process.assert_not_called()
        self.assertTrue((self.root / 'install/install-discovery.json').is_file())
        self.assertFalse(list((self.root / 'install').glob('unpacked-*')))
        self.assertEqual(os.environ['PHOTOCRAFT_BIN'], '')

    def test_bad_version_remains_fatal(self):
        stack, _, _ = self.installer(version='0.22.0')
        with stack:
            self.assertRaisesRegex(RuntimeError, 'Versao do executavel', install, self.root / 'install')
        self.assertFalse(list((self.root / 'install').glob('unpacked-*')))
        self.assertFalse((self.root / 'install/install-report.json').exists())

    def test_version_prefix_is_not_accepted_as_exact_version(self):
        stack, _, _ = self.installer(version='0.23.10')
        with stack:
            self.assertRaisesRegex(RuntimeError, 'Versao do executavel', install, self.root / 'install')

    def test_version_probe_failure_is_not_hidden(self):
        stack, _, process = self.installer()
        process.side_effect = subprocess.CalledProcessError(127, ['fixture'], stderr='missing library')
        with stack:
            self.assertRaises(subprocess.CalledProcessError, install, self.root / 'install')
        self.assertFalse(list((self.root / 'install').glob('unpacked-*')))

    def test_archive_traversal_rejected_before_probe(self):
        stack, _, process = self.installer({'../../escape.bin': b'no'})
        with stack:
            self.assertRaises(tarfile.TarError, install, self.root / 'install')
        process.assert_not_called()
        self.assertFalse((self.root / 'escape.bin').exists())

    def test_failure_artifact_contains_discovery(self):
        stack, _, _ = self.installer({'share/photocraft-cli': b'# only completion'})
        with stack:
            self.assertRaises(PhotoError, _install_with_diagnostics, self.root / 'install', self.root / 'diagnostics')
        self.assertEqual(json.loads((self.root / 'diagnostics/install-discovery.json').read_text())['status'], 'missing')

    def test_success_artifact_contains_both_installation_reports(self):
        stack, _, _ = self.installer()
        with stack:
            _install_with_diagnostics(self.root / 'install', self.root / 'diagnostics')
        for name in ('install-report.json', 'install-discovery.json'):
            self.assertEqual((self.root / 'install' / name).read_bytes(), (self.root / 'diagnostics' / name).read_bytes())

    def test_diagnostics_copy_cannot_mask_original_error(self):
        root = self.root / 'install'; root.mkdir()
        (root / 'install-discovery.json').write_text('{}')
        with patch(__name__ + '.install', side_effect=PhotoError('original failure')):
            with patch(__name__ + '.shutil.copyfile', side_effect=PermissionError('fixture')):
                self.assertRaisesRegex(PhotoError, 'original failure', _install_with_diagnostics, root, self.root / 'out')

    def test_stale_reports_cleared_before_new_network_attempt(self):
        root = self.root / 'install'; root.mkdir()
        for name in ('install-report.json', 'install-discovery.json'):
            (root / name).write_text('{"stale":true}')
        with patch(__name__ + '.get', side_effect=PhotoError('fixture network failure')):
            self.assertRaises(PhotoError, install, root)
        for name in ('install-report.json', 'install-discovery.json'):
            self.assertFalse((root / name).exists())


if __name__ == '__main__':
    raise SystemExit(cli())
