#!/usr/bin/env python3
"""
Corta o logo da era em faixas e sobe cada uma como custom emoji.
Capa = 1 emoji só.
Devolve os IDs pro Worker.
"""
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
        raise RuntimeError("getMe falhou: " + str(r))
    return r["result"]["username"]


def download_image(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGBA")


def trim(im):
    bbox = im.getbbox()
    return im.crop(bbox) if bbox else im


def split_logo(im, max_parts=8):
    im = trim(im)
    w, h = im.size
    if w < 90:
        return [im]
    parts = max(2, min(max_parts, w // 65))
    slice_w = w // parts
    tiles = []
    for i in range(parts):
        left = i * slice_w
        right = w if i == parts - 1 else (i + 1) * slice_w
        tile = im.crop((left, 0, right, h))
        tile.thumbnail((100, 100), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        canvas.paste(tile, ((100 - tile.width) // 2, (100 - tile.height) // 2), tile)
        tiles.append(canvas)
    return tiles


def make_cover(im):
    im = trim(im)
    im.thumbnail((100, 100), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    canvas.paste(im, ((100 - im.width) // 2, (100 - im.height) // 2), im)
    return canvas


def upload_emoji(im, set_name, owner_id, emoji="⭐"):
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)
    files = {"png_sticker": ("e.png", buf, "image/png")}
    data = {
        "user_id": owner_id,
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
    sheet_id = PAYLOAD.get("sheet_id") or "x"
    callback = PAYLOAD.get("callback")

    if not owner_id:
        raise RuntimeError("owner_id ausente")

    bot_username = get_bot_username()
    set_name = f"era_{sheet_id[:10]}_by_{bot_username}"

    result = {"logo_ids": [], "cover_id": None}

    if logo_url:
        tiles = split_logo(download_image(logo_url))
        for tile in tiles:
            result["logo_ids"].append(upload_emoji(tile, set_name, owner_id))

    if cover_url:
        cover = make_cover(download_image(cover_url))
        result["cover_id"] = upload_emoji(cover, set_name, owner_id, "🖼")

    if callback:
        requests.post(
            callback["url"],
            json={"token": callback["token"], "result": result},
            timeout=20,
        )
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"erro: {e}", file=sys.stderr)
        raise
