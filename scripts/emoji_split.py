#!/usr/bin/env python3
import io
import json
import os
import sys

import requests
from PIL import Image

BOT_TOKEN = os.environ["BOT_TOKEN"]
PAYLOAD = json.loads(os.environ["PAYLOAD"])
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg(method, **kwargs):
    r = requests.post(f"{TG}/{method}", **kwargs, timeout=60)
    return r.json()


def get_bot_username():
    r = tg("getMe")
    if not r.get("ok"):
        raise RuntimeError("getMe falhou")
    return r["result"]["username"]


def download_image(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGBA")


def make_emoji_image(im):
    """Redimensiona pra 100x100 mantendo transparência e garantindo conteúdo."""
    im = im.convert("RGBA")
    # corta bordas totalmente transparentes
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    # redimensiona mantendo proporção
    im.thumbnail((100, 100), Image.Resampling.LANCZOS)
    # canvas transparente 100x100
    canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    x = (100 - im.width) // 2
    y = (100 - im.height) // 2
    canvas.paste(im, (x, y), im)
    return canvas


def upload_emoji(im, set_name, owner_id, emoji="⭐"):
    im = make_emoji_image(im)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    raw = buf.getvalue()
    if len(raw) < 150:
        raise RuntimeError(f"PNG muito pequeno ({len(raw)} bytes)")
    buf.seek(0)

    files = {"png_sticker": ("emoji.png", buf, "image/png")}
    data = {
        "user_id": str(owner_id),
        "name": set_name,
        "title": "Era Logos",
        "sticker_type": "custom_emoji",
        "emojis": emoji,
    }
    r = tg("addStickerToSet", data=data, files=files)
    if not r.get("ok"):
        r = tg("createNewStickerSet", data=data, files=files)
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "falha emoji"))

    info = tg("getStickerSet", json={"name": set_name})
    return info["result"]["stickers"][-1]["custom_emoji_id"]


def main():
    logo_url = PAYLOAD.get("logo_url")
    cover_url = PAYLOAD.get("cover_url")
    owner_id = PAYLOAD.get("owner_id")
    sheet_id = (PAYLOAD.get("sheet_id") or "x")[:10]
    callback = PAYLOAD.get("callback")

    if not owner_id:
        raise RuntimeError("owner_id ausente")

    bot_username = get_bot_username()
    set_name = f"era_{sheet_id}_by_{bot_username}"

    result = {"logo_ids": [], "cover_id": None}

    if logo_url:
        im = download_image(logo_url)
        result["logo_ids"].append(upload_emoji(im, set_name, owner_id))

    if cover_url:
        im = download_image(cover_url)
        result["cover_id"] = upload_emoji(im, set_name, owner_id, "🖼")

    if callback:
        requests.post(callback["url"], json={"token": callback["token"], "result": result}, timeout=20)

    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"erro: {e}", file=sys.stderr)
        raise
