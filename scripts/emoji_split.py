#!/usr/bin/env python3
import io
import json
import os
import sys
import tempfile

import requests
from PIL import Image

BOT_TOKEN = os.environ["BOT_TOKEN"]
PAYLOAD = json.loads(os.environ["PAYLOAD"])
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg(method, data=None, files=None):
    r = requests.post(f"{TG}/{method}", data=data, files=files, timeout=60)
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
    im = im.convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    im.thumbnail((100, 100), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    x = (100 - im.width) // 2
    y = (100 - im.height) // 2
    canvas.paste(im, (x, y), im)
    return canvas


def split_logo(im, max_parts=7):
    im = im.convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    if w < 120:
        return [im]
    parts = max(3, min(max_parts, w // 70))
    slice_w = w // parts
    tiles = []
    for i in range(parts):
        left = i * slice_w
        right = w if i == parts - 1 else (i + 1) * slice_w
        tile = im.crop((left, 0, right, h))
        tiles.append(tile)
    return tiles


def upload_emoji(im, set_name, owner_id, emoji="⭐"):
    im = make_emoji_image(im)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        im.save(tmp, format="PNG")
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            files = {"png_sticker": ("emoji.png", f, "image/png")}
            data = {
                "user_id": str(owner_id),
                "name": set_name,
                "title": "Era Logos",
                "sticker_type": "custom_emoji",
                "emojis": emoji,
            }
            r = tg("addStickerToSet", data=data, files=files)
            if not r.get("ok"):
                f.seek(0)
                r = tg("createNewStickerSet", data=data, files=files)
            if not r.get("ok"):
                raise RuntimeError(r.get("description", "falha emoji"))
    finally:
        os.unlink(tmp_path)

    info = tg("getStickerSet", data={"name": set_name})
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
        for tile in split_logo(im):
            result["logo_ids"].append(upload_emoji(tile, set_name, owner_id))

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
