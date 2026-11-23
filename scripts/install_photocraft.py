#!/usr/bin/env python3
"""Install a pinned official Linux tarball, verifying its release digest/checksum.
Run in a disposable CI runner. The release is never silently upgraded.
"""
from __future__ import annotations
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


def get(url, dest=None):
    req=urllib.request.Request(url,headers={"User-Agent":"PhotoCraft-Telegram-Integration/0.1","Accept":"application/vnd.github+json" if "api.github.com" in url else "*/*"})
    with urllib.request.urlopen(req,timeout=90) as r:
        if dest is None: return r.read(8*1024*1024)
        size=0
        with open(dest,"wb") as f:
            while chunk:=r.read(1024*1024):
                size+=len(chunk)
                if size>MAX_ARCHIVE: raise RuntimeError("Download excedeu o limite.")
                f.write(chunk)


def install(root:Path):
    root=root.resolve(); root.mkdir(parents=True,exist_ok=True)
    release=json.loads(get(f"https://api.github.com/repos/{REPO}/releases/tags/v{VERSION}"))
    if release.get("tag_name")!=f"v{VERSION}": raise RuntimeError("Tag inesperada.")
    name=f"photocraft-{VERSION}-linux-x86_64.tar.gz"
    asset=next((a for a in release.get("assets",[]) if a["name"]==name),None)
    if not asset: raise RuntimeError("Release sem o tarball esperado: "+name)
    url=asset["browser_download_url"]
    if not url.startswith(f"https://github.com/{REPO}/releases/download/v{VERSION}/"):
        raise RuntimeError("URL inesperada para o artefato.")
    digest=asset.get("digest") or ""
    expected=digest[7:] if re.fullmatch(r"sha256:[0-9a-fA-F]{64}",digest) else None
    if not expected:
        checksum=next((a for a in release["assets"] if a["name"]=="SHA256SUMS.txt"),None)
        if not checksum: raise RuntimeError("Release sem checksum verificavel.")
        for line in get(checksum["browser_download_url"]).decode().splitlines():
            m=re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)",line.strip())
            if m and m[2].removeprefix("./")==name: expected=m[1]; break
    if not expected: raise RuntimeError("SHA256 do tarball ausente; instalacao interrompida.")
    archive=root/name
    if not archive.exists() or hashlib.file_digest(open(archive,"rb"),"sha256").hexdigest().lower()!=expected.lower(): get(url,archive)
    actual=hashlib.file_digest(open(archive,"rb"),"sha256").hexdigest()
    if actual.lower()!=expected.lower(): raise RuntimeError("SHA256 divergente.")
    with tarfile.open(archive,"r:gz") as tf:
        members=tf.getmembers()
        if sum(m.size for m in members)>3*1024**3 or len(members)>10000: raise RuntimeError("Arquivo compactado grande demais.")
        tf.extractall(root/"unpacked",filter="data")
    candidates=[p for p in (root/"unpacked").rglob("photocraft-cli") if p.is_file() and not p.is_symlink()]
    if len(candidates)!=1: raise RuntimeError("Nao encontrei exatamente um photocraft-cli.")
    exe=candidates[0]; exe.chmod(exe.stat().st_mode|0o100)
    p=subprocess.run([str(exe),"--version"],capture_output=True,text=True,timeout=30,check=True)
    if VERSION not in p.stdout+p.stderr: raise RuntimeError("Versao do executavel diverge da release.")
    result={"version":VERSION,"asset":name,"archive_sha256":actual,"binary":str(exe),"reported_version":(p.stdout+p.stderr).strip()}
    (root/"install-report.json").write_text(json.dumps(result,indent=2))
    if os.getenv("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"],"a") as f: f.write("PHOTOCRAFT_BIN="+str(exe)+"\n")
    print(json.dumps(result,indent=2))
    return result

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--dir",type=Path,required=True);a=p.parse_args();install(a.dir)
