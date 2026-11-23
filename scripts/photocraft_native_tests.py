#!/usr/bin/env python3
"""Real-engine tests only. Missing PhotoCraft is UNAVAILABLE, never a pass."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from PIL import Image, ImageStat, ImageChops
from photocraft_bridge import PhotoRPC, PhotoError, binary_path


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

if __name__ == "__main__":
    p=argparse.ArgumentParser(); p.add_argument("--out",type=Path,default=Path("photocraft-native-results")); args=p.parse_args()
    report=run_suite(args.out)
    print(json.dumps({k:v for k,v in report.items() if k!="cases"},indent=2))
    sys.exit(0 if report["status"]=="passed" else 2 if report["status"]=="unavailable" else 1)
