"""Mapa de andamento (numpy + scipy + ffmpeg; sem librosa).

Pipeline:
  1. load / onset_env      áudio mono 22,05 kHz -> envelope de onsets
  2. tempogram + viterbi   caminho suave de BPM (só serve de guia para o rastreador)
  3. beat_track            batidas por programação dinâmica (Ellis 2007), com refino sub-quadro
  4. segment               particionamento ótimo dos tempos das batidas em retas (tempo x índice da batida):
                           trechos longos = estáveis; trechos curtos em sequência = rampa
  5. report_html / plot_png
"""
import subprocess

import numpy as np
import scipy.ndimage as nd
import scipy.signal as ss

SR = 22050
HOP = 256
FPS = SR / HOP

WEAK = 0.15        # batida com < 15% da força típica = "sem batida"
MIN_SEG = 4        # batidas mínimas por pedaço no particionamento
LONG_SEG = 12      # a partir de 12 batidas (3 compassos) o pedaço conta como "estável"
SIGMA = 0.004      # jitter de uma batida (s): ~ 1 quadro do envelope / sqrt(12) + folga
PENALTY = 40.0     # custo de abrir um novo pedaço (em unidades de SSE/SIGMA^2)
MERGE_TOL = 0.45   # BPM: estáveis vizinhos mais próximos que isso viram um só
RAMP_MIN = 3.0     # BPM: variação mínima para chamar de rampa
BEATS_PER_BAR = 4


# ----------------------------------------------------------------------
# 1-3. áudio -> batidas
# ----------------------------------------------------------------------
def load(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def onset_env(y):
    f, _, Z = ss.stft(y, fs=SR, nperseg=1024, noverlap=1024 - HOP, boundary=None)
    S = np.abs(Z)
    edges = np.geomspace(30, 9000, 41)
    bands = np.array([S[(f >= a) & (f < b)].sum(0) for a, b in zip(edges[:-1], edges[1:])])
    L = np.log1p(10 * bands)
    flux = np.r_[0, np.maximum(0, np.diff(L, axis=1)).sum(0)]
    flux = np.maximum(flux - nd.uniform_filter1d(flux, int(0.4 * FPS)), 0)
    return flux / (flux.std() + 1e-9)


def tempogram(env, win_s=6.0, step_s=0.5, lo=50, hi=220, res=0.25):
    """Tempograma de Fourier com soma de harmônicas."""
    W, st = int(win_s * FPS), int(step_s * FPS)
    bpms = np.arange(lo, hi, res)
    n = np.arange(W)
    hann = np.hanning(W)
    harm = np.array([1, 2, 3, 4])
    freqs = harm[:, None] * bpms[None, :] / 60.0                       # (H, B)
    basis = np.exp(-2j * np.pi * freqs[..., None] * n / FPS)            # (H, B, W)
    wts = np.array([1.0, 0.7, 0.5, 0.35])[:, None]
    times, M = [], []
    for s in range(0, len(env) - W + 1, st):
        seg = env[s:s + W]
        seg = (seg - seg.mean()) * hann
        M.append((wts * np.abs(basis @ seg)).sum(0))
        times.append((s + W / 2) / FPS)
    M = np.array(M).T
    M /= (M.max(0, keepdims=True) + 1e-9)
    return np.array(times), bpms, M


def viterbi(M, bpms, prior_bpm, max_log_step=0.08, lam=10.0):
    """Caminho de andamento suave; o prior fixa a oitava (evita saltos para metade/dobro)."""
    B, T = M.shape
    lb = np.log2(bpms)
    prior = -0.5 * ((lb - np.log2(prior_bpm)) / 0.45) ** 2
    cost = -np.log(M + 1e-3) - 0.5 * prior[:, None]
    dl = lb[1] - lb[0]
    k = int(max_log_step / dl)
    offs = np.arange(-k, k + 1)
    pen = lam * (offs * dl) ** 2 * 100
    D = np.full((B, T), np.inf)
    P = np.zeros((B, T), int)
    D[:, 0] = cost[:, 0]
    ar = np.arange(B)
    for t in range(1, T):
        prev = D[:, t - 1]
        best = np.full(B, np.inf)
        arg = np.zeros(B, int)
        for o, p in zip(offs, pen):
            sh = np.full(B, np.inf)
            if o >= 0:
                sh[:B - o] = prev[o:]
            else:
                sh[-o:] = prev[:B + o]
            c = sh + p
            m = c < best
            best[m] = c[m]
            arg[m] = (ar + o)[m]
        D[:, t] = best + cost[:, t]
        P[:, t] = np.clip(arg, 0, B - 1)
    path = np.zeros(T, int)
    path[-1] = D[:, -1].argmin()
    for t in range(T - 1, 0, -1):
        path[t - 1] = P[path[t], t]
    return bpms[path]


def beat_track(env, tt, path, tight=120.0):
    """Rastreador de batidas por programação dinâmica, com período variável no tempo.
    Devolve os tempos (s) com refino parabólico sub-quadro."""
    n = len(env)
    per = np.interp(np.arange(n) / FPS, tt, 60.0 / path) * FPS
    sm = nd.gaussian_filter1d(env, 2.0)
    C = np.zeros(n)
    back = -np.ones(n, int)
    for t in range(n):
        p = per[t]
        lo, hi = max(int(t - 2 * p), 0), int(t - p / 2)
        if hi < 0 or hi <= lo:
            C[t] = sm[t]
            continue
        taus = np.arange(lo, hi + 1)
        sc = C[taus] - tight * np.log((t - taus) / p) ** 2
        j = sc.argmax()
        C[t] = sm[t] + sc[j]
        back[t] = taus[j]
    last = int(per[-1])
    t = int(np.argmax(C[-last:]) + n - last)
    idx = []
    while t >= 0:
        idx.append(t)
        t = back[t]
    idx = np.array(idx[::-1])
    # refino parabólico no envelope suavizado
    out = idx.astype(float)
    for q, i in enumerate(idx):
        if 0 < i < n - 1:
            a, b, c = sm[i - 1], sm[i], sm[i + 1]
            d = a - 2 * b + c
            if d < 0:
                out[q] = i + np.clip(0.5 * (a - c) / d, -0.5, 0.5)
    return out / FPS


def beat_strength(env, beats):
    idx = np.round(beats * FPS).astype(int).clip(0, len(env) - 1)
    st = np.array([env[max(0, i - 3):i + 4].max() for i in idx])
    ref = np.median(st[st > np.percentile(st, 40)]) if len(st) else 1.0
    return st / (ref + 1e-9)


# ----------------------------------------------------------------------
# 4. segmentação
# ----------------------------------------------------------------------
def _fit(x, y):
    """Reta y = a + b*x (mínimos quadrados) e SSE."""
    b, a = np.polyfit(x, y, 1)
    return a, b, float(((y - (a + b * x)) ** 2).sum())


def _partition(t, min_seg=MIN_SEG, pen=PENALTY, sigma=SIGMA):
    """Particionamento ótimo (programação dinâmica) de t[i] ~ reta por pedaço.
    Devolve lista de (i0, i1) com i1 exclusivo."""
    m = len(t)
    if m < 2 * min_seg:
        return [(0, m)]
    x = np.arange(m, dtype=float)
    c1 = np.r_[0, np.cumsum(x)]
    c2 = np.r_[0, np.cumsum(x * x)]
    cy = np.r_[0, np.cumsum(t)]
    cyy = np.r_[0, np.cumsum(t * t)]
    cxy = np.r_[0, np.cumsum(x * t)]

    def cost(i, j):
        n = j - i
        sx, sxx = c1[j] - c1[i], c2[j] - c2[i]
        sy, syy, sxy = cy[j] - cy[i], cyy[j] - cyy[i], cxy[j] - cxy[i]
        vx = sxx - sx * sx / n
        cov = sxy - sx * sy / n
        sse = syy - sy * sy / n - (cov * cov / vx if vx > 1e-12 else 0.0)
        return max(sse, 0.0) / sigma ** 2

    F = np.full(m + 1, np.inf)
    F[0] = -pen
    arg = np.zeros(m + 1, int)
    for j in range(min_seg, m + 1):
        for i in range(0, j - min_seg + 1):
            if not np.isfinite(F[i]) or (i > 0 and i < min_seg):
                continue
            v = F[i] + cost(i, j) + pen
            if v < F[j]:
                F[j], arg[j] = v, i
    cuts, j = [], m
    while j > 0:
        cuts.append((arg[j], j))
        j = arg[j]
    return cuts[::-1]


def _weak_runs(rel, min_run=3):
    runs, i, n = [], 0, len(rel)
    while i < n:
        if rel[i] < WEAK:
            j = i
            while j < n and rel[j] < WEAK:
                j += 1
            if j - i >= min_run:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _bpm_of(beats, i0, i1):
    """BPM por regressão nas batidas i0..i1-1."""
    if i1 - i0 < 3:
        return None
    return 60.0 / np.polyfit(np.arange(i1 - i0), beats[i0:i1], 1)[0]


def _classify(pieces, beats):
    """Pedaços do particionamento -> lista de trechos {kind, i0, i1}.
    Longos = estáveis; sequência de curtos entre um estável e (estável | fim do bloco) = rampa se for
    monotônica e grande; senão é absorvida pelo vizinho estável mais parecido."""
    info = [{"i0": a, "i1": b, "bpm": _bpm_of(beats, a, b), "long": (b - a) >= LONG_SEG} for a, b in pieces]
    out, k = [], 0
    while k < len(info):
        if info[k]["long"]:
            out.append({"kind": "stable", "i0": info[k]["i0"], "i1": info[k]["i1"]})
            k += 1
            continue
        j = k
        while j < len(info) and not info[j]["long"]:
            j += 1
        run = info[k:j]
        left = out[-1] if out and out[-1]["kind"] == "stable" else None
        right = info[j] if j < len(info) else None
        i0, i1 = run[0]["i0"], run[-1]["i1"]
        is_ramp = False
        if left is not None and len(run) >= 2:
            b_left = _bpm_of(beats, left["i0"], left["i1"])
            b_end = _bpm_of(beats, max(i0, i1 - 5), i1) if i1 - i0 >= 5 else run[-1]["bpm"]
            b_right = right["bpm"] if right else None
            d = np.diff([r["bpm"] for r in run if r["bpm"]]) if len(run) > 1 else np.array([])
            sign = np.sign(b_end - b_left)
            mono = len(d) == 0 or (np.sign(d) == sign).mean() >= 0.7
            # a rampa tem de terminar longe do BPM de partida; se o próximo estável volta ao mesmo BPM, é ruído
            back_to_start = b_right is not None and abs(b_right - b_left) < RAMP_MIN and abs(b_end - b_left) < RAMP_MIN
            is_ramp = abs(b_end - b_left) >= RAMP_MIN and mono and not back_to_start
        if is_ramp:
            out.append({"kind": "ramp", "i0": i0, "i1": i1})
        else:
            # absorve no vizinho estável mais próximo em BPM
            cands = []
            if left is not None:
                cands.append(("L", _bpm_of(beats, left["i0"], left["i1"])))
            if right is not None:
                cands.append(("R", right["bpm"]))
            mid = _bpm_of(beats, i0, i1) or (cands[0][1] if cands else 0)
            if not cands:
                out.append({"kind": "stable", "i0": i0, "i1": i1})
            else:
                side = min(cands, key=lambda c: abs(c[1] - mid))[0]
                if side == "L":
                    left["i1"] = i1
                else:
                    info[j]["i0"] = i0
        k = j
    return out


def _merge_stables(segs, beats):
    merged = []
    for s in segs:
        s["bpm"] = _bpm_of(beats, s["i0"], s["i1"]) if s["kind"] == "stable" else None
        if (merged and s["kind"] == "stable" and merged[-1]["kind"] == "stable"
                and abs(s["bpm"] - merged[-1]["bpm"]) <= MERGE_TOL):
            p = merged[-1]
            p["i1"] = s["i1"]
            p["bpm"] = _bpm_of(beats, p["i0"], p["i1"])
        else:
            merged.append(s)
    return merged


def _boundary_time(beats, a, c):
    """Instante da troca entre dois estáveis: cruzamento das duas retas (índice da batida -> tempo)."""
    i = c["i0"]
    t_beat = float(beats[i])
    xa = np.arange(max(a["i0"], i - 40), i - 3)
    xb = np.arange(i + 3, min(c["i1"], i + 40))
    if len(xa) < 8 or len(xb) < 8:
        return t_beat
    a1, a0 = np.polyfit(xa, beats[xa], 1)
    b1, b0 = np.polyfit(xb, beats[xb], 1)
    if abs(a1 - b1) < 1e-6:
        return t_beat
    j = (b0 - a0) / (a1 - b1)
    t = a0 + a1 * j
    return float(t) if abs(j - i) <= 6 else t_beat


def _ramp_bars(beats, i0, i1):
    """Compasso a compasso: blocos de 4 batidas a partir da 1ª batida da rampa.
    O último bloco, se incompleto (>= 2 intervalos), entra como parcial."""
    bars = []
    k = i0
    while k + BEATS_PER_BAR <= i1 - 1:
        bars.append({"t": float(beats[k]), "bpm": 60.0 * BEATS_PER_BAR / (beats[k + BEATS_PER_BAR] - beats[k]),
                     "partial": False})
        k += BEATS_PER_BAR
    n_left = (i1 - 1) - k
    if n_left >= 2:
        bars.append({"t": float(beats[k]), "bpm": 60.0 * n_left / (beats[i1 - 1] - beats[k]), "partial": True})
    return bars


def segment(beats, rel, dur):
    """Devolve a lista de trechos ordenados no tempo: stable | ramp | nobeat."""
    n = len(beats)
    weak = _weak_runs(rel)
    is_weak = np.zeros(n, bool)
    for a, b in weak:
        is_weak[a:b] = True

    segs = []
    # blocos contíguos de batidas fortes
    i = 0
    while i < n:
        if is_weak[i]:
            i += 1
            continue
        j = i
        while j < n and not is_weak[j]:
            j += 1
        if j - i >= MIN_SEG:
            bt = beats[i:j]
            parts = _partition(bt)
            blk = _classify(parts, bt)
            blk = _merge_stables(blk, bt)
            # a rampa parte da última batida do andamento antigo: o 1º intervalo alongado começa nela.
            # Procura, perto da fronteira do particionamento, 2 intervalos seguidos > 3,5% acima do período estável.
            for q, s_ in enumerate(blk):
                if s_["kind"] == "ramp" and q > 0 and blk[q - 1]["kind"] == "stable":
                    p0 = 60.0 / _bpm_of(bt, blk[q - 1]["i0"], blk[q - 1]["i1"])
                    for k in range(max(blk[q - 1]["i0"], s_["i0"] - 4), min(s_["i0"] + 3, len(bt) - 2)):
                        if bt[k + 1] - bt[k] > p0 * 1.035 and bt[k + 2] - bt[k + 1] > p0 * 1.035:
                            s_["i0"] = k
                            break
            for s in blk:
                s["i0"] += i
                s["i1"] += i
                segs.append(s)
        i = j

    # tempos e BPMs
    for k, s in enumerate(segs):
        s["t0"] = float(beats[s["i0"]])
        s["t1"] = float(beats[s["i1"] - 1])
        if s["kind"] == "stable":
            s["bpm"] = _bpm_of(beats, s["i0"], s["i1"])
    for a, c in zip(segs, segs[1:]):
        if a["kind"] == "stable" and c["kind"] == "stable" and c["i0"] == a["i1"]:
            a["t1"] = c["t0"] = _boundary_time(beats, a, c)
        elif c["i0"] <= a["i1"]:
            a["t1"] = c["t0"] = float(beats[c["i0"]])
    for k, s in enumerate(segs):
        if s["kind"] == "ramp":
            prev = [x for x in segs[:k] if x["kind"] == "stable"]
            s["bpm"] = prev[-1]["bpm"] if prev else _bpm_of(beats, s["i0"], min(s["i0"] + 5, s["i1"]))
            s["bpm_end"] = _bpm_of(beats, max(s["i0"], s["i1"] - 5), s["i1"])
            s["bars"] = _ramp_bars(beats, s["i0"], s["i1"])
    if segs:
        segs[0]["t0"] = float(beats[segs[0]["i0"]])

    for a, b in weak:
        t0 = float(beats[a - 1]) if a > 0 else 0.0
        t1 = float(dur) if b >= n else float(beats[b])
        segs.append({"kind": "nobeat", "t0": t0, "t1": t1})
    # um nobeat que colide com o fim de uma rampa/estável começa onde o outro termina
    segs.sort(key=lambda s: (s["t0"], s["kind"] == "nobeat"))
    for a, c in zip(segs, segs[1:]):
        if c["kind"] == "nobeat" and a["t1"] > c["t0"]:
            c["t0"] = a["t1"]
    return segs


def local_series(beats, rel, n_beats=8):
    """BPM local (regressão em n batidas fortes contíguas): usado só no gráfico."""
    out = []
    ok = rel >= WEAK
    for i in range(len(beats) - n_beats + 1):
        if ok[i:i + n_beats].all():
            out.append((beats[i:i + n_beats].mean(), _bpm_of(beats, i, i + n_beats)))
    return np.array(out) if out else np.zeros((0, 2))


def analyze(path):
    y = load(path)
    dur = len(y) / SR
    env = onset_env(y)
    tt0, bp0, M0 = tempogram(env, 6.0, 1.0)
    prof = M0.sum(1) * np.exp(-0.5 * ((np.log2(bp0) - np.log2(120)) / 0.6) ** 2)
    glob = float(bp0[prof.argmax()])
    tt, bp, M = tempogram(env, 3.5, 0.25)
    path_bpm = viterbi(M, bp, glob)
    beats = beat_track(env, tt, path_bpm)
    rel = beat_strength(env, beats)
    segs = segment(beats, rel, dur)

    main = None
    stable = [s for s in segs if s["kind"] == "stable"]
    if stable:
        groups = {}
        for s in stable:
            groups[round(s["bpm"])] = groups.get(round(s["bpm"]), 0) + (s["t1"] - s["t0"])
        k = max(groups, key=groups.get)
        sel = [s for s in stable if round(s["bpm"]) == k]
        main = float(np.average([s["bpm"] for s in sel], weights=[s["t1"] - s["t0"] for s in sel]))
    alts = []
    if main:
        prof2 = M0.sum(1)
        at = lambda x: prof2[np.abs(bp0 - x).argmin()] if bp0[0] <= x <= bp0[-1] else 0  # noqa: E731
        for f in (0.5, 2.0):
            if at(main * f) >= 0.9 * at(main) and 50 <= main * f <= 220:
                alts.append(main * f)
    return {"dur": dur, "main": main, "segs": segs, "ser": local_series(beats, rel), "beats": beats,
            "rel": rel, "alts": alts, "glob": glob}


# ----------------------------------------------------------------------
# 5. relatório e gráfico
# ----------------------------------------------------------------------
def fmt_t(s):
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def fmt_b(b):
    return f"{b:.1f}".replace(".", ",")


def report_html(res, name, simple=False):
    main, segs = res["main"], res["segs"]
    if not main:
        return f"🎚 <b>{name}</b>\nNão achei uma batida definida neste áudio."
    if simple:
        out = [f"🎚 <b>{name}</b>", f"BPM: <b>{round(main)}</b> ({fmt_b(main)})"]
        if res["alts"]:
            out.append("Também faz sentido contar como " + " ou ".join(str(round(a)) for a in res["alts"])
                       + " (metade/dobro do tempo).")
        if any(s["kind"] != "stable" for s in segs) or len([s for s in segs if s["kind"] == "stable"]) > 1:
            out.append("O andamento muda ao longo da faixa: use <code>/bpm complexo</code> para ver o mapa.")
        return "\n".join(out)

    out = [f"🎚 <b>Mapa de andamento</b> · {name}", f"Andamento principal: <b>{round(main)} BPM</b>", ""]
    for s in segs:
        a = fmt_t(s["t0"])
        b = fmt_t(s["t1"]) if s["t1"] < res["dur"] - 0.5 else "fim"
        if s["kind"] == "stable":
            d = s["bpm"] - main
            out.append(f"{a} – {b}  <b>{fmt_b(s['bpm'])}</b> BPM" + ("" if abs(d) < 0.5 else f"  ({d:+.0f})"))
        elif s["kind"] == "ramp":
            r = s["bpm_end"] / s["bpm"]
            kind = "ritardando" if r < 1 else "accelerando"
            extra = " · chega em halftime" if abs(r - 0.5) <= 0.05 else (" · chega no dobro" if abs(r - 2.0) <= 0.1 else "")
            out.append(f"{a} – {b}  <b>{kind}: {fmt_b(s['bpm'])} → {fmt_b(s['bpm_end'])}</b> BPM "
                       f"({(r - 1) * 100:+.0f}%{extra})")
            if s.get("bars"):
                vals = " → ".join(f"{x['bpm']:.0f}" + ("*" if x["partial"] else "") for x in s["bars"])
                out.append(f"   por compasso: {vals}")
                if any(x["partial"] for x in s["bars"]):
                    out.append("   (* compasso incompleto)")
        else:
            out.append(f"{a} – {b}  sem batida definida")
    out += ["", "<i>Medido no áudio. A resolução é de ~1 quadro (12 ms) por batida: separa 116 de 117, mas não "
                "diferencia degraus de uma rampa contínua.</i>"]
    return "\n".join(out)


def plot_png(res, path, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    segs, ser = res["segs"], res["ser"]
    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=110)
    for s in segs:
        if s["kind"] == "nobeat":
            ax.axvspan(s["t0"], s["t1"], color="#999", alpha=.25, lw=0)
        elif s["kind"] == "ramp":
            ax.axvspan(s["t0"], s["t1"], color="#e8a33d", alpha=.25, lw=0)
    if len(ser):
        ax.plot(ser[:, 0], ser[:, 1], lw=1.2, color="#2b6cb0", label="BPM medido (8 batidas)")
    for s in segs:
        if s["kind"] == "stable":
            ax.hlines(s["bpm"], s["t0"], s["t1"], colors="#c53030", lw=2)
            ax.text((s["t0"] + s["t1"]) / 2, s["bpm"] + 1.6, f"{s['bpm']:.1f}", ha="center", fontsize=8, color="#c53030")
        elif s["kind"] == "ramp":
            for x in s.get("bars", []):
                ax.plot(x["t"], x["bpm"], "o", color="#b7791f", ms=4)
    if res["main"]:
        ax.axhline(res["main"], ls=":", color="#555", lw=1)
    ax.set_xlabel("tempo (s)")
    ax.set_ylabel("BPM")
    ax.set_title(title or "Mapa de andamento")
    ax.grid(alpha=.25)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
