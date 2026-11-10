#!/usr/bin/env python3
"""
box2gofile.py - pega um link compartilhado do Box (arquivo ou pasta) e envia pro
GoFile em streaming, sem salvar nada no disco. Mostra o progresso no Telegram.

Uso:
    python box2gofile.py "LINK" --list
    python box2gofile.py "LINK" --select 1,3 --mode 2 -y
    python box2gofile.py "LINK" --select all --mode 3 -y --folder ID_DA_PASTA

Token do GoFile: variável de ambiente GOFILE_TOKEN.
Telegram (opcional): BOT_TOKEN, TG_CHAT_ID, TG_THREAD_ID.
Requer: pip install requests
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
import zipfile
from html import escape as esc
from urllib.parse import urlparse, unquote

import requests

GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "")
UPLOAD_URL = "https://upload.gofile.io/uploadfile"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
CHUNK = 1024 * 1024


# ----------------------------------------------------------------- utilidades
def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def safe(name):
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip() or "sem_nome"


def fmt_time(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if h or m else "") + f"{sec}s"


def notify(text):
    tok, chat = os.environ.get("BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        return
    payload = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
    if os.environ.get("TG_THREAD_ID"):
        payload["message_thread_id"] = int(os.environ["TG_THREAD_ID"])
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=payload, timeout=20)
    except Exception:
        pass


class Status:
    """Uma mensagem (rich message) no Telegram, editada com o progresso."""
    def __init__(self):
        self.tok = os.environ.get("BOT_TOKEN")
        self.chat = os.environ.get("TG_CHAT_ID")
        self.thread = os.environ.get("TG_THREAD_ID")
        self.mid = None
        self.rich = True            # cai pra texto comum se o Telegram recusar
        self.name, self.job, self.state = "", "", "Preparando"
        self.done, self.total, self.t0 = 0, 0, time.time()
        self.running = False
        self.link, self.folder, self.failed = None, None, []

    def _call(self, method, payload):
        try:
            return requests.post(f"https://api.telegram.org/bot{self.tok}/{method}",
                                 json=payload, timeout=20).json()
        except Exception:
            return {}

    def begin(self, name, total=0):
        self.name, self.total, self.done = name, total, 0
        self.t0, self.state = time.time(), "Enviando"

    def _stats(self):
        el = time.time() - self.t0
        speed = self.done / el if el > 0 else 0
        pct = min(100, self.done * 100 / self.total) if self.total else 0
        filled = int(pct / 100 * 12)
        bar = "●" * filled + "○" * (12 - filled)
        eta = "-"
        if self.total and speed > 0 and self.done < self.total:
            eta = fmt_time((self.total - self.done) / speed)
        return el, speed, pct, bar, eta

    def html(self):
        el, speed, pct, bar, eta = self._stats()
        rows = [
            ("Feito", human(self.done)),
            ("Total", human(self.total) if self.total else "?"),
            ("Status", self.state),
            ("Velocidade", f"{human(speed)}/s"),
            ("ETA", eta),
            ("Decorrido", fmt_time(el)),
        ]
        h = (f"<h3>📦 Box → GoFile</h3>"
             f"<p><b>{esc(self.name)}</b></p>"
             f"<p><code>[{bar}] {pct:.0f}%</code></p>"
             "<table bordered striped>"
             + "".join(f"<tr><td><b>{k}</b></td><td>{esc(str(v))}</td></tr>" for k, v in rows)
             + "</table>")
        if self.folder:
            h += f"<p>📁 ID da pasta: <code>{esc(self.folder)}</code></p>"
        if self.failed:
            h += ("<details><summary>⚠️ Falharam (%d)</summary>" % len(self.failed)
                  + "".join(f"<p>{esc(f)}</p>" for f in self.failed) + "</details>")
        h += f"<footer>Job {esc(self.job or '-')}</footer>"
        if self.link:
            h += ('<tg-button-row><tg-button type="url" style="primary" '
                  f'url="{esc(self.link, quote=True)}">Abrir no GoFile</tg-button></tg-button-row>')
        return h

    def plain(self):
        el, speed, pct, bar, eta = self._stats()
        t = (f"{self.name}\n╭ Box → GoFile\n├ [{bar}] {pct:.0f}%\n"
             f"├ Done » {human(self.done)}\n├ Total » {human(self.total) if self.total else '?'}\n"
             f"├ Status » {self.state}\n├ Speed » {human(speed)}/s\n├ ETA » {eta}\n"
             f"├ Elapsed » {fmt_time(el)}\n╰ Job » {self.job}")
        if self.link:
            t += f"\n\nLink: {self.link}"
        if self.folder:
            t += f"\nID da pasta: {self.folder}"
        if self.failed:
            t += "\nFalharam: " + ", ".join(self.failed)
        return t[:4000]

    def _send(self, rich):
        base = {"chat_id": self.chat}
        if self.mid is None:
            if self.thread:
                base["message_thread_id"] = int(self.thread)
            base["disable_notification"] = True
            if rich:
                return self._call("sendRichMessage", {**base, "rich_message": {"html": self.html()}})
            return self._call("sendMessage", {**base, "text": self.plain(),
                                              "disable_web_page_preview": True})
        base["message_id"] = self.mid
        if rich:
            return self._call("editMessageText", {**base, "rich_message": {"html": self.html()}})
        return self._call("editMessageText", {**base, "text": self.plain(),
                                              "disable_web_page_preview": True})

    def push(self):
        if not (self.tok and self.chat):
            return
        r = self._send(self.rich) if self.rich else {}
        if self.rich and not r.get("ok"):
            if "not modified" in str(r.get("description", "")).lower():
                return
            self.rich = False            # rich recusado: usa texto comum daqui pra frente
        if not self.rich:
            r = self._send(False)
            if "not modified" in str(r.get("description", "")).lower():
                return
        if r.get("ok") and self.mid is None:
            self.mid = r["result"]["message_id"]

    def start(self):
        self.running = True

        def loop():
            while self.running:
                self.push()
                time.sleep(5)
        threading.Thread(target=loop, daemon=True).start()

    def finish(self, ok, link=None, folder=None, failed=()):
        self.running = False
        time.sleep(0.5)
        self.state = "Concluído ✅" if ok else "Concluído com falhas ⚠️"
        if ok and self.total:
            self.done = self.total
        self.link, self.folder, self.failed = link, folder, list(failed)
        self.push()


STATUS = Status()


# ----------------------------------------------------------------- leitura do Box
class BoxShare:
    def __init__(self, url):
        p = urlparse(url)
        m = re.match(r"^(/(?:s|v)/[A-Za-z0-9_\-]+)", p.path)
        if not m:
            sys.exit("Esse link não parece ser um link compartilhado do Box.")
        self.base = f"{p.scheme}://{p.netloc}"
        self.root = m.group(1)          # ex: /s/abc123  ou  /v/priorities
        self.url = url
        self.shared_name = None

    def _load(self, url):
        html = requests.get(url, headers=UA, timeout=30).text
        m = re.search(r"Box\.postStreamData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
        if not m:
            return None, html
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return None, html
        si = data.get("/app-api/enduserapp/shared-item") or {}
        if si.get("sharedName") and not self.shared_name:
            self.shared_name = si["sharedName"]   # no /v/nome o shared_name real é outro
        return data, html

    def direct(self, fid):
        return (f"{self.base}/index.php?rm=box_download_shared_file"
                f"&shared_name={self.shared_name}&file_id=f_{fid}")

    def _parse_folder(self, data):
        folder = data.get("/app-api/enduserapp/shared-folder")
        if not folder:
            return None, []
        if folder.get("pageCount", 1) > 1:
            print(f"[aviso] '{folder.get('currentFolderName')}' tem mais de uma página; só a primeira foi lida.")
        items = []
        for it in folder.get("items", []):
            if it.get("type") == "file":
                items.append({"type": "file", "name": it["name"], "size": it.get("itemSize", 0),
                              "url": self.direct(it["id"])})
            else:
                items.append({"type": "folder", "name": it["name"], "size": it.get("itemSize", 0),
                              "count": it.get("filesCount", 0),
                              "page": f"{self.base}{self.root}/folder/{it['id']}"})
        return folder.get("currentFolderName"), items

    def top(self):
        """Retorna (nome_da_pasta, itens). Para arquivo único, itens = [file]."""
        if "zip_download" in self.url or "box_download_shared_file" in self.url:
            return "download", [{"type": "file", "name": None, "size": 0, "url": self.url}]
        data, html = self._load(self.url)
        if data:
            si = data.get("/app-api/enduserapp/shared-item") or {}
            if si.get("itemType") == "file":
                return "arquivo", [{"type": "file", "name": None, "size": 0, "url": self.direct(si["itemID"])}]
            name, items = self._parse_folder(data)
            if items:
                return name, items
        f = re.search(r'"typedID":"f_(\d+)"', html or "")
        if f and self.shared_name:
            return "arquivo", [{"type": "file", "name": None, "size": 0, "url": self.direct(f.group(1))}]
        sys.exit("Não consegui listar os arquivos. Use o link zip_download do userscript.")

    def walk(self, folder, prefix=""):
        """Lista recursivamente todos os arquivos de uma pasta: [(url, caminho_relativo, tamanho)]."""
        data, _ = self._load(folder["page"])
        if not data:
            print(f"[aviso] não consegui abrir a pasta '{folder['name']}'")
            return []
        _, items = self._parse_folder(data)
        out = []
        for it in items:
            rel = f"{prefix}{safe(it['name'])}"
            if it["type"] == "file":
                out.append((it["url"], rel, it["size"]))
            else:
                out += self.walk(it, rel + "/")
        return out


# ----------------------------------------------------------------- upload
def get_filename(resp, fallback):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    name = unquote(m.group(1)) if m else fallback
    return name.replace('"', "").replace("\r", "").replace("\n", "")


def multipart(chunks, filename, boundary, fields):
    head = ""
    for k, v in fields.items():
        head += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
    head += (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n")
    yield head.encode("utf-8")
    sent = 0
    for c in chunks:
        if c:
            sent += len(c)
            STATUS.done = sent
            yield c
    yield f"\r\n--{boundary}--\r\n".encode()


def gofile_post(chunks, filename, folder_id):
    boundary = uuid.uuid4().hex
    headers = {"Authorization": f"Bearer {GOFILE_TOKEN}",
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    fields = {"folderId": folder_id} if folder_id else {}
    r = requests.post(UPLOAD_URL, data=multipart(chunks, filename, boundary, fields),
                      headers=headers, timeout=None)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Resposta inesperada ({r.status_code}): {r.text[:300]}")
    if j.get("status") != "ok":
        raise RuntimeError(f"Erro do GoFile: {j}")
    d = j["data"]
    return d.get("parentFolder") or folder_id, d.get("downloadPage")


def upload_file(url, known_name, folder_id):
    with requests.get(url, headers=UA, stream=True, timeout=60) as src:
        src.raise_for_status()
        fallback = known_name or os.path.basename(urlparse(url).path) or "arquivo"
        filename = known_name or get_filename(src, fallback)
        print(f"Arquivo: {filename}")
        STATUS.begin(filename, int(src.headers.get("Content-Length") or 0))
        return gofile_post(src.iter_content(CHUNK), filename, folder_id)


class _Pipe:
    """Destino 'não seekable' pro zipfile: vai juntando bytes e entregando em blocos."""
    def __init__(self):
        self.q = queue.Queue(maxsize=4)
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        if len(self.buf) >= CHUNK:
            self.q.put(bytes(self.buf))
            self.buf.clear()
        return len(b)

    def flush(self):
        pass

    def finish(self):
        if self.buf:
            self.q.put(bytes(self.buf))
            self.buf.clear()


def zip_chunks(entries):
    """Gera um .zip (sem compressão, só empacota) em streaming a partir de [(url, caminho, tam)]."""
    pipe = _Pipe()

    def worker():
        try:
            with zipfile.ZipFile(pipe, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for i, (url, rel, _) in enumerate(entries, 1):
                    with requests.get(url, headers=UA, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        zi = zipfile.ZipInfo(rel, time.localtime()[:6])
                        zi.compress_type = zipfile.ZIP_STORED
                        with zf.open(zi, "w", force_zip64=True) as dest:
                            for c in r.iter_content(CHUNK):
                                if c:
                                    dest.write(c)
            pipe.finish()
            pipe.q.put(None)
        except BaseException as e:  # repassa o erro pra thread principal
            pipe.q.put(e)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = pipe.q.get()
        if item is None:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


def upload_zip(zipname, entries, folder_id):
    total = sum(e[2] for e in entries)
    print(f"Zip: {zipname}  ({len(entries)} arquivos, ~{human(total)})")
    STATUS.begin(zipname, total)
    return gofile_post(zip_chunks(entries), zipname, folder_id)


# ----------------------------------------------------------------- interface
def show_list(items):
    for i, it in enumerate(items, 1):
        if it["type"] == "folder":
            print(f"  [{i:>2}] 📁 {it['name']}  ({it.get('count', '?')} arquivos, {human(it['size'])})")
        else:
            print(f"  [{i:>2}]    {it['name'] or '(arquivo)'}  ({human(it['size'])})")


def parse_selection(text, n):
    text = (text or "").strip().lower()
    if text in ("", "a", "all", "todos", "tudo"):
        return list(range(n))
    idx = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            idx.update(range(int(a) - 1, int(b)))
        elif part:
            idx.add(int(part) - 1)
    bad = [i + 1 for i in idx if not 0 <= i < n]
    if bad:
        sys.exit(f"Número(s) inválido(s): {bad}")
    return sorted(idx)


def build_jobs(share, root_name, chosen, mode):
    jobs = []  # ("file", url, name) | ("zip", zipname, entries)
    if mode == "3":
        entries = []
        for it in chosen:
            if it["type"] == "file":
                entries.append((it["url"], safe(it["name"] or "arquivo"), it["size"]))
            else:
                print(f"Lendo pasta: {it['name']} ...")
                entries += share.walk(it, safe(it["name"]) + "/")
        jobs.append(("zip", safe(root_name) + ".zip", entries))
        return jobs

    for it in chosen:
        if it["type"] == "file":
            jobs.append(("file", it["url"], it["name"]))
        else:
            print(f"Lendo pasta: {it['name']} ...")
            entries = share.walk(it, "")
            if not entries:
                continue
            if mode == "2":
                jobs.append(("zip", safe(it["name"]) + ".zip", entries))
            else:
                for url, rel, _ in entries:
                    jobs.append(("file", url, safe(it["name"]) + " - " + rel.replace("/", " - ")))
    return jobs


def main():
    ap = argparse.ArgumentParser(description="Box -> GoFile em streaming")
    ap.add_argument("url")
    ap.add_argument("--list", action="store_true", help="só lista o conteúdo e sai")
    ap.add_argument("--select", help="números separados por vírgula (ex: 1,3 ou 2-4) ou 'all'")
    ap.add_argument("--mode", choices=["1", "2", "3"],
                    help="1=arquivos separados, 2=um zip por pasta, 3=tudo num zip")
    ap.add_argument("--folder", help="ID (UUID) de uma pasta existente do GoFile; sem isso cria uma nova")
    ap.add_argument("-y", "--yes", action="store_true", help="não pedir confirmação")
    args = ap.parse_args()

    if not GOFILE_TOKEN and not args.list:
        sys.exit("Defina o token: GOFILE_TOKEN (variável de ambiente).")

    share = BoxShare(args.url)
    root_name, items = share.top()
    print(f'\nConteúdo de "{root_name}":\n')
    show_list(items)
    print()
    if args.list:
        return

    sel = args.select
    if sel is None:
        sel = input("Quais enviar? (ex: 1,3 ou 2-4; Enter = todos): ")
    chosen = [items[i] for i in parse_selection(sel, len(items))]

    has_folder = any(c["type"] == "folder" for c in chosen)
    mode = args.mode
    if mode is None:
        if len(chosen) == 1 and not has_folder:
            mode = "1"
        else:
            print("\nComo enviar?")
            print("  [1] cada arquivo separado (pastas viram arquivos soltos)")
            print("  [2] um .zip por pasta (arquivos soltos vão individuais)")
            print("  [3] tudo num único .zip")
            mode = input(f"Escolha [{'2' if has_folder else '1'}]: ").strip() or ("2" if has_folder else "1")
            if mode not in ("1", "2", "3"):
                sys.exit("Opção inválida.")

    jobs = build_jobs(share, root_name, chosen, mode)
    if not jobs:
        sys.exit("Nada pra enviar.")

    print("\nPlano de envio:")
    for j in jobs:
        if j[0] == "zip":
            print(f"  ZIP  {j[1]}  ({len(j[2])} arquivos, ~{human(sum(e[2] for e in j[2]))})")
        else:
            print(f"  FILE {j[2] or '(arquivo)'}")
    if not args.yes and input("\nContinuar? [S/n] ").strip().lower() in ("n", "nao", "não"):
        return

    folder_id, page, failed = args.folder, None, []
    STATUS.start()
    for i, job in enumerate(jobs, 1):
        print(f"\n[{i}/{len(jobs)}]")
        STATUS.job = f"{i}/{len(jobs)}"
        try:
            if job[0] == "zip":
                folder_id, page = upload_zip(job[1], job[2], folder_id)
            else:
                folder_id, page = upload_file(job[1], job[2], folder_id)
        except Exception as e:
            print(f"  FALHOU: {e}")
            failed.append(job[1] if job[0] == "zip" else (job[2] or job[1]))

    STATUS.finish(not failed, page, folder_id, failed)

    print("\nConcluído!")
    if page:
        print("Link:", page)
    if folder_id:
        print("ID da pasta (use em --folder pra enviar mais coisas pra ela):", folder_id)
    if failed:
        print("Falharam:", ", ".join(failed))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            notify(f"❌ {e.code}")
        raise
