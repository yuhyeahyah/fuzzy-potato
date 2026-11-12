"""
Identificação de faixas por impressão digital: Chromaprint (fpcalc) -> AcoustID -> MusicBrainz.

Só stdlib. Usado por audio_tools.op_identificar; aqui não tem nada de Telegram.

Lógica:
  1. fpcalc em cada arquivo (fallback: ffmpeg -> WAV quando o fpcalc não decodifica o formato)
  2. AcoustID devolve gravações (MBID) com nota 0..1 e os lançamentos onde cada uma aparece
  3. VOTAÇÃO de lançamento: cada faixa vota, com a sua nota, em todo lançamento que contém
     uma das suas gravações candidatas. Ganha o que cobre mais faixas, com bônus para
     contagem de faixas igual à da sessão e penalidade para coletânea/single.
     Isso escolhe UMA edição coerente para o álbum inteiro em vez de cada faixa puxar pra um lado.
  4. Busca essa edição completa no MusicBrainz e casa cada arquivo com a faixa dela:
       a) pela gravação idêntica (MBID)                    -> status "album"
       b) por título parecido + duração (remaster/versão)  -> status "fuzzy"
     Faixa identificada que não está na edição           -> status "single" (dados da própria gravação)
  5. Capa: Cover Art Archive (edição, depois grupo) -> iTunes -> Deezer, conferindo artista/álbum.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib import error, parse, request

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
MB_URL = "https://musicbrainz.org/ws/2"
FP_LENGTH = 120              # segundos analisados pelo fpcalc (o padrão do AcoustID)
MIN_SCORE = 0.50             # resultado do AcoustID abaixo disso é ruído
MAX_CANDS = 6                # gravações candidatas guardadas por faixa
MAX_MB_RECORDING_FETCH = 20  # gravações sem metadado no AcoustID que buscamos no MusicBrainz

_repo = os.environ.get("GITHUB_REPOSITORY", "")
UA = os.environ.get("MB_USER_AGENT", "").strip() or (
    f"TelegramTagBot/1.0 ( https://github.com/{_repo} )" if _repo else "TelegramTagBot/1.0 ( https://github.com )"
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class FatalError(RuntimeError):
    """Erro que não adianta repetir por faixa (chave do AcoustID inválida etc.)."""


# ---------- limite de requisições ----------

class Pace:
    def __init__(self, interval):
        self.interval = interval
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            d = self.last + self.interval - time.monotonic()
            if d > 0:
                time.sleep(d)
            self.last = time.monotonic()


ACOUSTID_PACE = Pace(0.36)  # AcoustID: 3 req/s
MB_PACE = Pace(1.1)         # MusicBrainz: 1 req/s


# ---------- HTTP ----------

def http(url, data=None, headers=None, timeout=30, tries=4, pace=None, raw=False):
    """GET (ou POST com data) com repetição em 429/5xx/rede.
    raw=False: devolve o JSON (ou None em 404). raw=True: (bytes, content-type) ou None."""
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    hdr.update(headers or {})
    body = None
    if data is not None:
        body = parse.urlencode(data).encode() if isinstance(data, dict) else data
        hdr.setdefault("Content-Type", "application/x-www-form-urlencoded")
    last = "falha de rede"
    for attempt in range(tries):
        if pace:
            pace.wait()
        req = request.Request(url, data=body, headers=hdr)
        try:
            with request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                ctype = r.headers.get("Content-Type", "")
            return (payload, ctype) if raw else json.loads(payload)
        except error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504):
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = float(ra) if ra and ra.isdigit() else 2 ** attempt + 0.5
                time.sleep(min(wait, 20))
                continue
            if raw:
                return None
            try:
                return json.loads(e.read())  # AcoustID manda erro em JSON com 400
            except Exception:  # noqa: BLE001
                raise RuntimeError(last) from None
        except (error.URLError, TimeoutError, ConnectionError, ValueError) as e:
            last = str(e)[:150]
            time.sleep(2 ** attempt + 0.5)
    raise RuntimeError(last)


# ---------- texto ----------

def fold(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def norm(s):
    s = fold(s)
    s = _PAREN.sub(" ", s)                                   # (Remastered 2011), [Live], (feat. X)
    s = re.sub(r"\s[-–]\s.*\b(remaster|version|mix|edit|live|mono|stereo)\b.*$", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def credit(artists):
    """artist-credit do MusicBrainz ou artists do AcoustID -> 'A feat. B'."""
    return "".join((a.get("name") or (a.get("artist") or {}).get("name") or "") + (a.get("joinphrase") or "")
                   for a in artists or []).strip()


def names_for(f):
    """Títulos prováveis de um arquivo, a partir do título do Telegram e do nome do arquivo."""
    out = []
    if f.get("title"):
        out.append(f["title"])
    st = re.sub(r"\.[a-z0-9]{2,5}$", "", str(f.get("name") or ""), flags=re.I).replace("_", " ").strip()
    if st:
        st = re.sub(r"^\s*(?:cd\s*)?\d{1,3}(?:[-.]\d{1,3})?\s*[-.)_]?\s+", "", st, flags=re.I)  # "01 - ", "1-03 "
        out.append(st)
        if " - " in st:
            out.append(st.split(" - ")[-1])  # "Artista - Título"
    seen = set()
    return [x for x in out if x and not (fold(x) in seen or seen.add(fold(x)))]


def title_case(s):
    return re.sub(r"(^|[\s\-/])(\w)", lambda m: m.group(1) + m.group(2).upper(), s or "")


# ---------- 1) impressão digital ----------

def fpcalc_bin():
    return shutil.which("fpcalc")


def _ffprobe_duration(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def _fpcalc(path):
    p = subprocess.run([fpcalc_bin(), "-json", "-length", str(FP_LENGTH), str(path)],
                       capture_output=True, text=True, timeout=240)
    # o fpcalc às vezes sai com código != 0 por erro de decodificação parcial, mas ainda imprime o JSON
    try:
        j = json.loads(p.stdout)
        if j.get("fingerprint"):
            return float(j.get("duration") or 0), j["fingerprint"]
    except ValueError:
        pass
    raise RuntimeError((p.stderr or "fpcalc falhou").strip().splitlines()[-1][:200] if (p.stderr or "").strip() else "fpcalc falhou")


def fingerprint(path):
    """(duração em segundos do arquivo inteiro, fingerprint)."""
    if not fpcalc_bin():
        raise RuntimeError("fpcalc (Chromaprint) não está instalado")
    path = Path(path)
    try:
        dur, fp = _fpcalc(path)
        if dur <= 0:
            dur = _ffprobe_duration(path)
        return dur, fp
    except RuntimeError as first:
        # formato que o fpcalc não lê: decodifica só o começo com ffmpeg; a duração vem do original
        wav = path.with_suffix(".fp.wav")
        try:
            r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-t", str(FP_LENGTH + 5),
                                "-ac", "2", "-ar", "44100", "-f", "wav", str(wav)],
                               capture_output=True, text=True, timeout=240)
            if r.returncode != 0 or not wav.exists():
                raise RuntimeError(f"não consegui decodificar o áudio ({first})") from None
            _, fp = _fpcalc(wav)
            return _ffprobe_duration(path), fp
        finally:
            wav.unlink(missing_ok=True)


# ---------- 2) AcoustID ----------

def lookup(key, fp, duration):
    """[(nota, gravação)] ordenado pela nota, sem gravações repetidas."""
    if not duration or duration < 7:
        return []  # curto demais para o AcoustID casar
    j = http(ACOUSTID_URL, data={
        "client": key, "format": "json", "duration": str(int(round(duration))),
        "fingerprint": fp, "meta": "recordings releasegroups releases tracks",
    }, pace=ACOUSTID_PACE, timeout=45)
    if not j:
        raise RuntimeError("AcoustID não respondeu")
    if j.get("status") != "ok":
        err = j.get("error") or {}
        msg = err.get("message") or "erro desconhecido"
        if err.get("code") in (4, 5, 6) or "api key" in msg.lower() or "application" in msg.lower():
            raise FatalError(f"AcoustID recusou a chave ({msg}). Confira o secret ACOUSTID_KEY.")
        raise RuntimeError(f"AcoustID: {msg}")
    best = {}
    for res in j.get("results") or []:
        s = float(res.get("score") or 0)
        if s < MIN_SCORE:
            continue
        for rec in res.get("recordings") or []:
            rid = rec.get("id")
            if not rid:
                continue
            cur = best.get(rid)
            # mesma gravação em dois resultados: fica a nota maior, e o metadado mais completo
            if not cur or s > cur[0] or (s == cur[0] and len(rec) > len(cur[1])):
                best[rid] = (s, rec)
    return sorted(best.values(), key=lambda x: -x[0])[:MAX_CANDS]


def rec_releases(rec):
    for rg in rec.get("releasegroups") or []:
        for rel in rg.get("releases") or []:
            yield rel, rg
    for rel in rec.get("releases") or []:
        yield rel, None


def rel_track_count(rel):
    tc = rel.get("track_count")
    if tc:
        return int(tc)
    return sum(int(m.get("track_count") or 0) for m in rel.get("mediums") or [])


# ---------- 3) votação de lançamento ----------

def choose_release(tracks):
    """tracks: [{'cands': [(nota, gravação)], ...}]. Devolve o lançamento vencedor ou None (sem consenso)."""
    n = len(tracks)
    ident = sum(1 for t in tracks if t["cands"])
    if not ident:
        return None
    agg = {}
    for idx, t in enumerate(tracks):
        for s, rec in t["cands"]:
            for rel, rg in rec_releases(rec):
                rid = rel.get("id")
                if not rid:
                    continue
                a = agg.setdefault(rid, {"cover": {}, "rel": rel, "rg": rg})
                if rg and not a["rg"]:
                    a["rg"] = rg
                if s > a["cover"].get(idx, 0):
                    a["cover"][idx] = s
    if not agg:
        return None

    def rank(a):
        rel, rg = a["rel"], a["rg"] or {}
        score = sum(a["cover"].values())
        tc = rel_track_count(rel)
        if tc == n:
            score += 0.75                       # edição com exatamente as faixas enviadas
        elif tc:
            score -= 0.03 * abs(tc - n)         # deluxe com 20 faixas perde para a normal com 12
        sec = {x.lower() for x in rg.get("secondarytypes") or []}
        typ = (rg.get("type") or "").lower()
        if sec & {"compilation", "dj-mix", "mixtape/street"}:
            score -= 0.6                        # "Now That's What I Call Music" não é o álbum
        if n > 1 and typ == "single":
            score -= 0.4
        if n == 1 and typ == "album":
            score += 0.1                        # um arquivo só: prefere o álbum ao single
        year = int((rel.get("date") or {}).get("year") or 9999)
        return (round(score, 4), -year, rel.get("country") == "XW")

    best = max(agg.values(), key=rank)
    need = 1 if ident == 1 else max(2, -(-ident // 2))  # metade das identificadas, no mínimo 2
    return best if len(best["cover"]) >= need else None


# ---------- 4) MusicBrainz ----------

def mb_release(rid):
    j = http(f"{MB_URL}/release/{rid}?inc=recordings+artist-credits+labels+release-groups+genres+isrcs&fmt=json",
             pace=MB_PACE, timeout=40)
    if not j or j.get("error"):
        raise RuntimeError((j or {}).get("error") or "lançamento não encontrado")
    rg = j.get("release-group") or {}
    genres = sorted((j.get("genres") or []) + (rg.get("genres") or []), key=lambda g: -(g.get("count") or 0))
    media = j.get("media") or []
    tracks = []
    for di, md in enumerate(media, 1):
        for t in md.get("tracks") or []:
            rec = t.get("recording") or {}
            tracks.append({
                "disc": int(md.get("position") or di),
                "position": int(t.get("position") or 0),
                "title": t.get("title") or rec.get("title") or "",
                "credit": credit(t.get("artist-credit") or rec.get("artist-credit")),
                "isrc": (rec.get("isrcs") or [""])[0],
                "length": round((t.get("length") or rec.get("length") or 0) / 1000),
                "recording_id": rec.get("id"),
            })
    label = next((li["label"]["name"] for li in j.get("label-info") or [] if (li.get("label") or {}).get("name")), "")
    return {
        "id": j.get("id"),
        "rg_id": rg.get("id"),
        "title": j.get("title") or "",
        "artist": credit(j.get("artist-credit")),
        # ano ORIGINAL do álbum (grupo); a data da edição só se o grupo não tiver
        "year": (rg.get("first-release-date") or j.get("date") or "")[:4],
        "label": label,
        "genre": title_case(genres[0]["name"]) if genres else "",
        "disc_total": len(media),
        "track_total": len(tracks),
        "country": j.get("country") or "",
        "format": "+".join(dict.fromkeys(m.get("format") for m in media if m.get("format"))),
        "tracks": tracks,
    }


def mb_recording(rid):
    j = http(f"{MB_URL}/recording/{rid}?inc=artist-credits+isrcs&fmt=json", pace=MB_PACE, timeout=30)
    if not j or j.get("error"):
        return None
    return {"title": j.get("title") or "", "credit": credit(j.get("artist-credit")),
            "isrc": (j.get("isrcs") or [""])[0], "length": round((j.get("length") or 0) / 1000)}


def release_public(rel):
    """Lançamento sem a lista de faixas (vai no callback para o Worker)."""
    if not rel:
        return None
    return {k: rel[k] for k in ("id", "rg_id", "title", "artist", "year", "label", "genre",
                                 "disc_total", "track_total", "country", "format")}


# ---------- casamento arquivo <-> faixa da edição ----------

def map_tracks(tracks, rel):
    """{índice do arquivo: (faixa da edição, nota, 'album'|'fuzzy')}"""
    by_rec = {}
    for rt in rel["tracks"]:
        by_rec.setdefault(rt["recording_id"], []).append(rt)
    assigned, used = {}, set()

    # a) gravação idêntica. Ordenado pela nota: em disputa (mesma gravação 2x), fica com a melhor.
    pairs = [(s, idx, rt) for idx, t in enumerate(tracks) for s, rec in t["cands"] for rt in by_rec.get(rec["id"], [])]
    for s, idx, rt in sorted(pairs, key=lambda x: -x[0]):
        key = (rt["disc"], rt["position"])
        if idx in assigned or key in used:
            continue
        assigned[idx] = (rt, round(s, 3), "album")
        used.add(key)

    # b) outra gravação da mesma música (remaster, versão do álbum vs. do single): título + duração
    free = [rt for rt in rel["tracks"] if (rt["disc"], rt["position"]) not in used]
    scored = []
    for idx, t in enumerate(tracks):
        if idx in assigned:
            continue
        names = [rec.get("title") for _, rec in t["cands"][:2] if rec.get("title")] + t["names"]
        dur = t.get("duration") or 0
        for rt in free:
            sim = max((similar(x, rt["title"]) for x in names), default=0.0)
            dd = abs(dur - rt["length"]) if dur and rt["length"] else None
            ok = ((sim >= 0.95 and (dd is None or dd <= 10))
                  or (sim >= 0.80 and dd is not None and dd <= 4)
                  or (sim >= 0.70 and dd is not None and dd <= 1.5))
            if ok:
                scored.append((sim - (dd or 0) * 0.01, idx, rt))
    for conf, idx, rt in sorted(scored, key=lambda x: -x[0]):
        key = (rt["disc"], rt["position"])
        if idx in assigned or key in used:
            continue
        assigned[idx] = (rt, round(max(0.0, min(conf, 1.0)), 3), "fuzzy")
        used.add(key)
    return assigned


def build_results(tracks, rel):
    """Um resultado por arquivo, na ordem recebida."""
    assigned = map_tracks(tracks, rel) if rel else {}
    out, fetched = [], 0
    for idx, t in enumerate(tracks):
        r = {"status": "none", "score": 0.0, "title": "", "credit": "", "isrc": "", "disc": 0, "position": 0,
             "recording_id": "", "error": t.get("error") or ""}
        if idx in assigned:
            rt, s, st = assigned[idx]
            r.update(status=st, score=s, title=rt["title"], credit=rt["credit"], isrc=rt["isrc"],
                     disc=rt["disc"], position=rt["position"], recording_id=rt["recording_id"] or "", error="")
        elif t["cands"]:
            s, rec = t["cands"][0]
            info = {"title": rec.get("title") or "", "credit": credit(rec.get("artists")), "isrc": ""}
            if (not info["title"] or not info["credit"]) and fetched < MAX_MB_RECORDING_FETCH and UUID_RE.match(rec["id"]):
                fetched += 1
                try:
                    info = mb_recording(rec["id"]) or info
                except Exception:  # noqa: BLE001
                    pass
            if info["title"]:
                r.update(status="single", score=round(s, 3), title=info["title"], credit=info["credit"],
                         isrc=info.get("isrc") or "", recording_id=rec["id"], error="")
        out.append(r)
    return out


# ---------- 5) capa ----------

def _same(a, b, cut):
    return similar(a, b) >= cut or (norm(a) and norm(b) and (norm(a) in norm(b) or norm(b) in norm(a))
                                    and min(len(norm(a)), len(norm(b))) >= 4)


def _image(url, headers=None):
    try:
        got = http(url, headers={"Accept": "image/*", **(headers or {})}, raw=True, tries=2, timeout=40)
    except RuntimeError:
        return None
    if not got:
        return None
    data, ctype = got
    if len(data) < 2000:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return data, "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    return None  # webp/gif/html de erro: o Telegram e o ID3 querem JPEG/PNG


def find_cover(rel):
    """(bytes, mime, fonte) ou None."""
    if UUID_RE.match(rel.get("id") or ""):
        got = _image(f"https://coverartarchive.org/release/{rel['id']}/front-1200")
        if got:
            return (*got, "Cover Art Archive")
    if UUID_RE.match(rel.get("rg_id") or ""):
        got = _image(f"https://coverartarchive.org/release-group/{rel['rg_id']}/front-1200")
        if got:
            return (*got, "Cover Art Archive (grupo)")
    artist, album = rel.get("artist") or "", rel.get("title") or ""
    if not album:
        return None
    try:
        j = http("https://itunes.apple.com/search?" + parse.urlencode(
            {"term": f"{artist} {album}", "entity": "album", "limit": "10"}), tries=2, timeout=20) or {}
        for x in j.get("results") or []:
            if x.get("artworkUrl100") and _same(x.get("collectionName"), album, 0.8) and _same(x.get("artistName"), artist, 0.75):
                got = _image(re.sub(r"/\d+x\d+bb\.", "/1200x1200bb.", x["artworkUrl100"]))
                if got:
                    return (*got, "iTunes")
    except RuntimeError:
        pass
    try:
        j = http("https://api.deezer.com/search/album?" + parse.urlencode(
            {"q": f'artist:"{artist}" album:"{album}"', "limit": "10"}), tries=2, timeout=20) or {}
        for x in j.get("data") or []:
            if x.get("cover_xl") and _same(x.get("title"), album, 0.8) and _same((x.get("artist") or {}).get("name"), artist, 0.75):
                got = _image(x["cover_xl"])
                if got:
                    return (*got, "Deezer")
    except RuntimeError:
        pass
    return None


# ---------- retorno para o Worker ----------

def post_callback(cb, body):
    """Entrega o resultado no endpoint /fpid do Worker. True se entregou (ou já tinha entregado)."""
    url, token = cb.get("url"), cb.get("token")
    if not url or not token:
        return False
    data = json.dumps({**body, "token": token}, ensure_ascii=False).encode()
    for wait in (0, 3, 8, 15, 30, 45):
        if wait:
            time.sleep(wait)
        req = request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "tagbot-actions/1.0"})
        try:
            with request.urlopen(req, timeout=30):
                return True
        except error.HTTPError as e:
            if e.code == 410:      # já entregue numa tentativa cuja resposta se perdeu
                return True
            if e.code in (400, 413):
                return False
            # 404: o token ainda não propagou no KV; 5xx: Worker instável. Tenta de novo.
        except Exception:  # noqa: BLE001
            pass
    return False
