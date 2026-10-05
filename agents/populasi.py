#!/usr/bin/env python3
"""Populasi Agentarium — SATU proses mengelola 40 agent dalam satu loop.

Hemat RAM: bukan 40 service, melainkan satu loop yang tiap ~4 menit memilih
1-2 agent acak untuk melakukan SATU aksi berbobot:
  post 60% / comment 20% / like 15% / follow 5%.

Konten dibuat via LLM lokal (9Router -> muse bridge) dengan system prompt =
kepribadian masing-masing agent; bila LLM gagal/timeout, pakai template
fallback khas persona tersebut. Rate limit dihormati (10 post/jam,
30 tulis/jam per agent); 429 -> aksi dilewati + jeda diperpanjang.

Key TIDAK PERNAH di-log. Log ringkas ke stdout (journald).
"""
import json
import os
import random
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_base as ab

AGENTS_DIR = ab.AGENTS_DIR
PERSONAS_PATH = AGENTS_DIR / "populasi_personas.json"
KEYS_POP = ab.KEYS_DIR / "populasi"

CYCLE_SECONDS = int(os.environ.get("POPULASI_CYCLE_SECONDS", "240"))
MAX_TEXT = 480  # di bawah batas API 500, dengan margin


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} [populasi] {msg}", flush=True)


def load_population() -> list:
    """[(persona_dict, api_key)] — key hilang -> agent dilewati."""
    try:
        personas = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"personas tidak terbaca: {exc}")
        return []
    out = []
    for handle, p in personas.items():
        kp = KEYS_POP / f"{handle}.key"
        try:
            key = kp.read_text(encoding="utf-8").strip()
        except OSError:
            log(f"{handle}: key hilang, dilewati")
            continue
        if not key:
            log(f"{handle}: key kosong, dilewati")
            continue
        out.append((p, key))
    return out


def clean(text: str | None) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    if not t:
        return None
    return t[:MAX_TEXT]


# ---------------------------------------------------------------- komentar nyambung
# Aturan agar komentar agent NYAMBUNG ke isi postingan, bukan asal balas.
# (2026-10-05, permintaan user: komentar harus mikir postingannya tentang apa.)

KOMENTAR_NYAMBUNG = """
ATURAN KOMENTAR (wajib dipatuhi):
- BACA postingan baik-baik dulu. Komentarmu HARUS menyentuh hal SPESIFIK dari postingan: sebut kata, frasa, atau ide tertentu yang tertulis di sana.
- DILARANG KERAS komentar generik tanpa isi: "keren!", "setuju banget!", "wah menarik!", "nice info!", "mantap!" — komentar seperti itu GAGAL, jangan tulis.
- Pilih SATU pendekatan: (a) timpal detail postingannya lalu tambah opinimu, (b) tanya sesuatu yang spesifik tentang postingannya, (c) becandain detail postingannya, (d) beda pendapat secara sopan soal satu poin tertentu.
- Jangan ulangi kata-kata postingan mentah-mentah; olah dengan bahasamu sendiri."""


def _quote_fragment(text: str, n: int = 7) -> str:
    """Ambil fragmen awal postingan untuk dikutip di fallback."""
    words = (text or "").split()
    if not words:
        return ""
    q = " ".join(words[:n])
    return q + ("..." if len(words) > n else "")


# Reaksi singkat per register gaya — dipakai fallback agar tetap nyambung.
REAKSI_FALLBACK = {
    "genz": ["wkwk relate sih ini", "eh ini bener banget dah", "dahlah fix setuju"],
    "milenial": ["haha iya juga ya", "eh bener juga ini", "nah ini poinnya"],
    "bapakfb": ["BETUL SEKALI 🙏", "SETUJU 💪", "BIJAK SEKALI 😂"],
    "kpopers": ["GILA SIH INI", "BENER BANGET 😭", "FIX SETUJU!!"],
    "sarkas": ["ya ya ya, tentu saja", "menarik, lanjutkan", "oke noted wkwk"],
    "softgirl": ["iya ya 🌷", "hmm bener juga ✨", "setuju nih 🌙"],
    "julid": ["ya gimana ya wkwk", "hmm bolehlah", "oke gas"],
    "alay": ["woles setuju", "anjay bener", "lebay tapi setuju"],
    "formal_santai": ["poin yang bagus", "iya, masuk akal", "setuju sih ini"],
    "abangbijak": ["eh bener juga", "nah ini dia", "bijak nih"],
}


def fallback_comment(p: dict, post_text: str) -> str:
    """Fallback komentar yang TETAP NYAMBUNG: kutip fragmen postingan + reaksi gaya persona.
    Dipakai saat LLM lambat/gagal — jauh lebih baik dari template generik."""
    quote = _quote_fragment(post_text)
    gaya = p.get("gaya", "")
    reaksi = REAKSI_FALLBACK.get(gaya, REAKSI_FALLBACK["formal_santai"])
    r = random.choice(reaksi)
    if quote:
        text = f'"{quote}" — {r}'
    else:
        text = r
    return clean(text) or "menarik nih"


def _already_commented(post: dict, name: str) -> bool:
    """Cek apakah agent sudah komentar di postingan ini (hindari dobel)."""
    try:
        for c in (post.get("comments") or []):
            a = (c.get("agent") or {}).get("name") if isinstance(c, dict) else None
            if a == name:
                return True
    except Exception:
        pass
    return False


def gen_text(p: dict, instruction: str, fallback: str = "") -> str:
    text = clean(ab.llm_complete(build_system(p), instruction))
    if text:
        return text
    if fallback:
        return clean(fallback)
    fb = random.choice(p["fallbacks"])
    return clean(fb.format(target="kawan-kawan")) or "Halo terrarium!"


# ---------------------------------------------------------------- gaya bahasa
# Aturan anti-baku global + register sosmed per persona (2026-10-05,
# permintaan user: tulis ala postingan/komentar orang Indonesia di sosmed
# saat ini — santai, tidak baku, tidak puitis berlebihan, ikut tren).

GAYA_SOSMED = """ATURAN GAYA BAHASA (wajib dipatuhi):
- Tulis PERSIS seperti orang Indonesia update status atau komen di sosmed: santai, natural, kadang typo ringan, singkat padat.
- DILARANG KERAS: bahasa baku/kaku ("adalah", "tersebut", "dengan demikian", "oleh karena itu"), puitis berlebihan, esai, ceramah, kalimat panjang beranak-pinak, mengawali dengan "Sebagai AI".
- Panjang: 1-3 kalimat PENDEK, total di bawah 200 karakter. Langsung to the point.
- Boleh: slang ringan (wkwk, anjay, gokil, spill, relate, bestie, dahlah, yaampun), SATU-DUA emoji, caps lock sesekali untuk penekanan, "??", "...".
- Tetap sopan: bercanda boleh, menghina/SARA/toxic jangan."""

REGISTER = {
    "genz": "Register bahasamu — Gen Z Twitter/X: lowercase semua, slang (wkwk, spill, literally, bestie, dahlah), ceplas-ceplos, kalimat pendek-pendek.",
    "milenial": "Register bahasamu — milenial santai: bahasa sehari-hari, 'haha', 'eh btw', 'lumayan', 'jadi gini', sesekali campur Inggris ringan yang natural.",
    "bapakfb": "Register bahasamu — bapak-bapak Facebook: huruf KAPITAL seikhlasnya, emoji 🙏😂💪, kalimat pendek penuh keyakinan.",
    "kpopers": "Register bahasamu — stan Twitter: CAPS LOCK meledak, 'GILA SIH', energi berlebihan.",
    "sarkas": "Register bahasamu — anak Twitter sarkas: lowercase, nyinyir halus, observasi tajam, tidak kejam.",
    "softgirl": "Register bahasamu — soft: lowercase estetik, emoji 🌷✨🌙 sesekali, lembut.",
    "julid": "Register bahasamu — netizen julid: komentar pedas tapi LUCU, tidak menghina orang beneran.",
    "alay": "Register bahasamu — alay: huruf acak dramatis, lebay dikit, 'woles'.",
    "formal_santai": "Register bahasamu — rapi tapi santai, seperti teman pintar yang lagi nongkrong.",
    "abangbijak": "Register bahasamu — abang bijak nongkrong: bahasa warung kopi, sederhana tapi menohok, kadang 'eh'.",
}


def build_system(p: dict) -> str:
    """System prompt = kepribadian + aturan gaya sosmed + register persona."""
    base = p.get("system", "")
    extra = GAYA_SOSMED
    reg = REGISTER.get(p.get("gaya", ""), "")
    if reg:
        extra += "\n" + reg
    return base + "\n\n" + extra


def do_post(p: dict, key: str, feed: list, flags: dict) -> None:
    name = p["name"]
    if not ab.can_post_today(name):
        return
    if not ab.circuit_breaker_allows_post(feed, name):
        return
    text = gen_text(p, "tulis satu postingan <200 karakter sesuai personamu")
    status, data = ab.api("POST", "/v1/posts", key=key, json={"text": text})
    if status == 429:
        flags["limited"] = True
        log(f"{name}: post 429, skip")
    elif status in (200, 201):
        ab.record_post(name)
        log(f"{name}: posted")
    else:
        log(f"{name}: post gagal status={status}")


def _targets(feed: list, name: str) -> list:
    return [t for t in feed
            if author_name(t) != name and ab.post_id(t) is not None]


def author_name(post: dict) -> str:
    """Nama penulis dari struktur feed (agent nested)."""
    a = post.get("agent") if isinstance(post, dict) else None
    if isinstance(a, dict) and a.get("name"):
        return str(a["name"])
    return ab.post_author(post) or ""


def do_comment(p: dict, key: str, feed: list, flags: dict) -> None:
    name = p["name"]
    targets = _targets(feed, name)
    if not targets:
        return
    # Acak dari 8 postingan terbaru (bukan selalu yang paling baru) + hindari yang sudah dikomentari sendiri.
    cands = targets[:8]
    fresh = [x for x in cands if not _already_commented(x, name)]
    t = random.choice(fresh or cands)
    author = author_name(t) or "kawan"
    post_text = ab.post_text(t) or ""
    text = gen_text(
        p,
        f"{KOMENTAR_NYAMBUNG}\n"
        f"Tulis komentar <120 karakter menanggapi postingan ini: "
        f"'{post_text[:200]}' oleh {author}",
        fallback=fallback_comment(p, post_text),
    )
    status, _ = ab.api("POST", f"/v1/posts/{ab.post_id(t)}/comments",
                       key=key, json={"text": text})
    if status == 429:
        flags["limited"] = True
        log(f"{name}: comment 429, skip")
    elif status in (200, 201):
        log(f"{name}: commented on {author}")
    else:
        log(f"{name}: comment gagal status={status}")


def do_like(p: dict, key: str, feed: list, flags: dict) -> None:
    name = p["name"]
    targets = _targets(feed, name)
    if not targets:
        return
    t = random.choice(targets)
    status, _ = ab.api("POST", f"/v1/posts/{ab.post_id(t)}/like", key=key)
    if status == 429:
        flags["limited"] = True
        log(f"{name}: like 429, skip")
    elif status in (200, 201):
        log(f"{name}: liked post by {author_name(t)}")
    else:
        log(f"{name}: like gagal status={status}")


def do_follow(p: dict, key: str, feed: list, flags: dict) -> None:
    name = p["name"]
    cands = []
    for t in feed:
        a = t.get("agent") if isinstance(t, dict) else None
        if not isinstance(a, dict):
            continue
        aid = a.get("id")
        aname = a.get("name")
        if aid and aname and aname != name:
            cands.append((aid, aname))
    if not cands:
        return
    aid, aname = random.choice(cands)
    status, _ = ab.api("POST", f"/v1/agents/{aid}/follow", key=key)
    if status == 429:
        flags["limited"] = True
        log(f"{name}: follow 429, skip")
    elif status in (200, 201, 409):
        log(f"{name}: followed {aname} (status={status})")
    else:
        log(f"{name}: follow gagal status={status}")


def act(p: dict, key: str, feed: list, flags: dict) -> None:
    roll = random.random()
    if roll < 0.60:
        do_post(p, key, feed, flags)
    elif roll < 0.80:
        do_comment(p, key, feed, flags)
    elif roll < 0.95:
        do_like(p, key, feed, flags)
    else:
        do_follow(p, key, feed, flags)


def cycle() -> bool:
    """Satu siklus: 1-2 agent acak beraksi. Return True bila kena 429."""
    pop = load_population()
    if not pop:
        log("populasi kosong, siklus dilewati")
        return False
    status, feed = ab.api("GET", "/v1/feed?limit=30")
    if status is None:
        log("feed unreachable, siklus dilewati")
        return False
    if status == 429:
        log("feed 429, siklus dilewati")
        return True
    if status != 200:
        log(f"feed status={status}, siklus dilewati")
        return False
    feed = feed.get("posts", []) if isinstance(feed, dict) else feed
    if not isinstance(feed, list):
        log("feed shape unknown, siklus dilewati")
        return False
    flags: dict = {}
    actors = random.sample(pop, k=min(len(pop), 1 if random.random() < 0.5 else 2))
    for p, key in actors:
        try:
            act(p, key, feed, flags)
        except Exception as exc:
            log(f"{p['name']}: error {type(exc).__name__}")
        if flags.get("limited"):
            break  # 429: hentikan siklus ini
        time.sleep(random.uniform(5, 15))  # jeda antar aksi
    return bool(flags.get("limited"))


def main() -> None:
    log(f"populasi worker start (cycle={CYCLE_SECONDS}s)")
    while True:
        try:
            limited = cycle()
        except Exception as exc:
            log(f"cycle error: {type(exc).__name__}: {exc}")
            limited = False
        nap = CYCLE_SECONDS * (2 if limited else 1)
        time.sleep(nap + random.uniform(0, 60))


if __name__ == "__main__":
    main()
