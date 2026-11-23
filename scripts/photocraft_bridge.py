#!/usr/bin/env python3
"""Bounded PhotoCraft JSON-lines adapter. Does not emulate the graphics engine.
Source protocol: https://github.com/storytold/photocraft/blob/main/docs/control-protocol.md
"""
from __future__ import annotations
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
