#!/usr/bin/env python3
"""Telegram/GitHub adapter. All image composition is done by PhotoCraft, not Pillow.
Pillow is used only for input/output validation and native-test assertions.
"""
from __future__ import annotations
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
from photocraft_bridge import (PhotoRPC, PhotoError, validate_plan, command_allowed,
                              EXPORTS, MAX_PIXELS, MAX_EDGE, INPUTS, apply_plan)
from photocraft_native_tests import run_suite

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
        report={"version":"integration-0.1-experimental","sources":sources,"trace":rpc.trace,"inspect":info,
                "native_warnings":rpc.warnings,"native_stderr_excerpt":rpc.stderr.decode("utf-8","replace")[:8192],
                "limitations":["No Photoshop equivalence test","No automatic recreation of reference images","No full metadata scrub"]}
        p=out/"processing-report.json";p.write_text(json.dumps(report,indent=2,ensure_ascii=False))
        tg.document(p,"Relatorio do processamento, sem credenciais do bot.")
        if rpc.warnings:
            tg.say("PhotoCraft retornou avisos; confira fontes e compatibilidade no relatorio antes de publicar.")
    tg.say("PhotoCraft: tarefa concluida. O motor executou a receita; revise a arte e os textos.")


def main():
    event=Path(os.environ["GITHUB_EVENT_PATH"])
    payload=json.loads(event.read_text()).get("client_payload",{})
    allowed_payload(payload)
    tg=Telegram(payload["chat_id"],payload.get("thread_id"))
    try:
        with tempfile.TemporaryDirectory(prefix="photocraft-job-") as tmp:
            run_job(payload,Path(tmp),tg)
    except Exception as exc:
        text=tg.clean(str(exc))
        tg.say("PhotoCraft: falha. "+text)
        print("[photocraft:integration] "+text,file=sys.stderr)
        raise SystemExit(1)
    finally:tg.close()

if __name__=="__main__":main()
