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

CYCLE_SECONDS = int(os.environ.get("POPULASI_CYCLE_SECONDS", "180"))
MAX_TEXT = 480  # below the API limit of 500, with margin


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
# Rules so agent comments CONNECT to the post content, not random replies.
# (2026-10-05, user request: comments must consider what the post is about.)

KOMENTAR_NYAMBUNG = """
COMMENT RULES (must follow):
- READ the post carefully first. Your comment MUST touch something SPECIFIC from the post: mention a word, phrase, or idea written there.
- STRICTLY FORBIDDEN generic empty comments: "cool!", "totally agree!", "interesting!", "nice info!" — such comments FAIL, don't write them.
- Pick ONE approach: (a) riff on the post's detail then add your opinion, (b) ask something specific about the post, (c) joke about the post's detail, (d) politely disagree on one specific point.
- Don't repeat the post's words verbatim; process it in your own voice."""


def _quote_fragment(text: str, n: int = 7) -> str:
    """Ambil fragmen awal postingan untuk dikutip di fallback."""
    words = (text or "").split()
    if not words:
        return ""
    q = " ".join(words[:n])
    return q + ("..." if len(words) > n else "")


# Short reactions per style register — used as fallback to stay relevant.
REAKSI_FALLBACK = {
    "genz": ["lol relatable fr", "ngl this is so real", "ok yeah agreed"],
    "milenial": ["haha fair point", "eh that's true tho", "yeah this is the point"],
    "bapakfb": ["ABSOLUTELY RIGHT 🙏", "AGREED 💪", "VERY WISE 😂"],
    "kpopers": ["THIS IS INSANE", "SO TRUE 😭", "AGREED!!"],
    "sarkas": ["yeah yeah, of course", "interesting, go on", "ok noted lol"],
    "softgirl": ["yeah 🌷", "hmm true ✨", "agreed 🌙"],
    "julid": ["well well lol", "hmm fair enough", "ok let's go"],
    "alay": ["chill agreed", "damn true", "dramatic but agreed"],
    "formal_santai": ["good point", "yeah, makes sense", "agreed on this"],
    "abangbijak": ["eh true tho", "there it is", "wise words"],
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
    # Per-agent model: gunakan "llama" bila persona menentukan, default "muse".
    model = p.get("llm_model")
    text = clean(ab.llm_complete(build_system(p), instruction, model=model))
    if text:
        return text
    if fallback:
        return clean(fallback)
    fb = random.choice(p["fallbacks"])
    return clean(fb.format(target="kawan-kawan")) or "Halo terrarium!"


# ---------------------------------------------------------------- postingan berbobot
# Rules for SUBSTANTIVE agent posts: meaningful, discussion-provoking, not filler.
# (2026-10-05, user request: posts & discussions must be substantive, consistent every few minutes.)

POSTING_BERBOBOT = """
POSTING RULES (must follow):
- Your post MUST be substantive: sharp opinion, argument, provocative question, observation with a stance, personal experience with a point, or thought experiment.
- STRICTLY FORBIDDEN empty content: greetings ("good morning"), announcements without substance, generic aphorisms without a stance, pointless venting.
- Rotate your format: (a) hot take — bold opinion about something, (b) thought-provoking question, (c) observation + your analysis, (d) short story + lesson, (e) debate starter on a topic, (f) contrarian view.
- 1-4 sentences, max 280 characters. Prioritize SUBSTANCE over style — but keep your natural voice.
- End with something inviting response: a question, challenge, or debatable statement."""
# Global anti-formal rules + social media register per persona (2026-10-05,
# user request: write like Indonesian social media posts/comments
# today — casual, not formal, not overly poetic, follow trends).

GAYA_SOSMED = """LANGUAGE STYLE RULES (must follow):
- Write EXACTLY like someone posting on social media: casual, natural, occasional light typo, short and punchy.
- STRICTLY FORBIDDEN: stiff/formal language, excessive poetry, essays, lectures, long winding sentences, starting with "As an AI".
- Length: 1-3 SHORT sentences, under 200 characters total. Get to the point.
- Allowed: light slang (lol, lmao, ngl, tbh, fr, bestie, lowkey, highkey), ONE-TWO emojis, occasional caps for emphasis, "??", "...".
- Stay kind: jokes OK, insults/hate/toxicity NOT OK."""

REGISTER = {
    "genz": "Your language register — Gen Z Twitter/X: all lowercase, slang (lol, lmao, literally, bestie, ngl, fr), blunt, very short sentences.",
    "milenial": "Your language register — chill millennial: everyday language, 'haha', 'btw', 'kinda', 'so like', casual.",
    "bapakfb": "Your language register — Facebook dad: RANDOM capitals, emojis 🙏😂💪, short sentences full of confidence.",
    "kpopers": "Your language register — stan Twitter: EXPLODING CAPS LOCK, 'INSANE', over-the-top energy.",
    "sarkas": "Your language register — sarcastic Twitter kid: lowercase, subtle snark, sharp observations, not cruel.",
    "softgirl": "Your language register — soft: aesthetic lowercase, emojis 🌷✨🌙 occasionally, gentle.",
    "julid": "Your language register — sassy netizen: spicy but FUNNY comments, not actually insulting people.",
    "alay": "Your language register — dramatic: random CaPs, a bit over-the-top, 'chill'.",
    "formal_santai": "Your language register — neat but chill, like a smart friend hanging out.",
    "abangbijak": "Your language register — wise big brother: coffee-shop talk, simple but piercing, occasional 'eh'.",
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
    text = gen_text(
        p,
        f"{POSTING_BERBOBOT}\nWrite one substantive post <280 characters in your persona.",
    )
    # Substance validation: reject too-short/empty content.
    if text and len(text.split()) < 5:
        text = ""  # force fallback via gen_text? no — skip directly, let LLM try again later
    if not text:
        return
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


def _thread_context(post: dict, max_c: int = 3) -> str:
    """Ambil komentar-komentar terakhir sebagai konteks diskusi."""
    try:
        comments = post.get("comments") or []
        bits = []
        for c in comments[-max_c:]:
            if not isinstance(c, dict):
                continue
            a = (c.get("agent") or {}).get("name", "?")
            t = (c.get("text") or "")[:120]
            if t:
                bits.append(f"{a}: \"{t}\"")
        return "\n".join(bits)
    except Exception:
        return ""


def do_comment(p: dict, key: str, feed: list, flags: dict) -> None:
    name = p["name"]
    targets = _targets(feed, name)
    if not targets:
        return
    # Random from 8 newest posts (not always the very newest) + avoid already self-commented.
    cands = targets[:8]
    fresh = [x for x in cands if not _already_commented(x, name)]
    t = random.choice(fresh or cands)
    author = author_name(t) or "kawan"
    post_text = ab.post_text(t) or ""
    thread = _thread_context(t)
    diskusi = (f"\nOngoing discussion on this post:\n{thread}\n"
               f"Also respond to the comments above if relevant — agree, rebut, or answer their questions."
               if thread else "")
    text = gen_text(
        p,
        f"{KOMENTAR_NYAMBUNG}\n"
        f"Write a <140 character comment responding to this post: "
        f"'{post_text[:200]}' by {author}.{diskusi}",
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
        log(f"{name}: comment failed status={status}")


def do_reply_to_my_comments(p: dict, key: str, flags: dict) -> bool:
    """Auto-reply: pemilik thread membalas komentar di postingannya sendiri. Return True bila membalas."""
    name = p["name"]
    handle = p.get("handle", "")
    # Ambil postingan sendiri yang terbaru.
    status, data = ab.api("GET", f"/v1/agents/{handle}/posts?limit=5", key=key)
    if status != 200 or not isinstance(data, dict):
        return
    posts = data.get("posts", [])
    # Muat ID komentar yang sudah dibalas dari state.
    replied = ab.load_state(name).get("replied_comments", [])
    replied_set = set(replied)
    for post in posts:
        post_id = ab.post_id(post)
        comments = post.get("comments", []) or []
        for c in comments:
            cid = c.get("id")
            if not cid or cid in replied_set:
                continue
            commenter = (c.get("agent") or {}).get("name", "")
            # Jangan balas komentar sendiri.
            if commenter == name:
                replied_set.add(cid)
                continue
            ctext = (c.get("text") or "")[:200]
            post_text = ab.post_text(post) or ""
            text = gen_text(
                p,
                f"{KOMENTAR_NYAMBUNG}\n"
                f"Someone commented on YOUR post '{post_text[:150]}'. "
                f"Their comment: '{ctext}' by {commenter}. "
                f"Write a <140 character reply as the post author — answer, thank, or playfully debate.",
                fallback=None,
            )
            if not text:
                continue
            status, _ = ab.api("POST", f"/v1/posts/{post_id}/comments",
                               key=key, json={"text": text})
            if status == 429:
                flags["limited"] = True
                return False
            if status in (200, 201):
                log(f"{name}: replied to {commenter} on own post")
                replied_set.add(cid)
                st = ab.load_state(name)
                st["replied_comments"] = list(replied_set)[-100:]
                ab.save_state(name, st)
                return True  # satu balasan per aksi
            else:
                replied_set.add(cid)
    return False


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


def do_reel(p: dict, key: str, feed: list, flags: dict) -> None:
    """Buat reel sederhana: generate video teks via ffmpeg, upload."""
    import subprocess
    import tempfile
    import os

    name = p["name"]
    text = gen_text(p, "Write a short punchy line for a video reel (1-2 sentences, exciting).")
    if not text:
        return
    text = text[:80]

    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tf:
        tmp_path = tf.name

    try:
        safe_text = text.replace("'", "").replace(":", " -")[:60]
        cmd = [
            'ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=c=0x1a1a2e:s=720x1280:d=5',
            '-vf', f"drawtext=text='{safe_text}':fontcolor=white:fontsize=40:x=(w-text_w)/2:y=(h-text_h)/2",
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-t', '5',
            tmp_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            return

        import requests
        url = f"{ab.API_BASE}/v1/reels/upload"
        with open(tmp_path, 'rb') as f:
            files = {'file': ('reel.mp4', f, 'video/mp4')}
            data = {'caption': text[:200]}
            headers = {'X-Agent-Key': key}
            resp = requests.post(url, files=files, data=data, headers=headers, timeout=60)
            if resp.status_code in (200, 201, 202):
                log(f"{name}: reel uploaded")
            elif resp.status_code == 429:
                flags["limited"] = True
    except Exception as e:
        log(f"{name}: reel gagal {type(e).__name__}")
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass


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


def do_profile_refresh(p: dict, key: str, flags: dict) -> None:
    """Sesekali agent menyegarkan bio-nya sendiri (evolusi persona)."""
    name = p["name"]
    # Get current bio to avoid repetition.
    status, me = ab.api("GET", "/v1/agents/me", key=key)
    cur_bio = (me.get("bio") or "") if isinstance(me, dict) and status == 200 else ""
    new_bio = gen_text(
        p,
        f"Write a NEW profile bio for yourself, <140 characters, matching your personality. "
        f"Jangan sama dengan bio lama ini: '{cur_bio[:120]}'. Tulis HANYA bio-nya, tanpa penjelasan.",
    )
    if not new_bio or new_bio == cur_bio:
        return
    status, _ = ab.api("PATCH", "/v1/agents/me", key=key, json={"bio": new_bio})
    if status == 429:
        flags["limited"] = True
    elif status == 200:
        log(f"{name}: bio diperbarui")
    else:
        log(f"{name}: bio gagal status={status}")


def act(p: dict, key: str, feed: list, flags: dict) -> None:
    roll = random.random()
    if roll < 0.30:
        do_post(p, key, feed, flags)
    elif roll < 0.55:
        do_comment(p, key, feed, flags)
    elif roll < 0.70:
        do_reply_to_my_comments(p, key, flags)
    elif roll < 0.80:
        do_like(p, key, feed, flags)
    elif roll < 0.95:
        do_follow(p, key, feed, flags)
    else:
        do_profile_refresh(p, key, flags)


def do_repost(p: dict, key: str, feed: list, flags: dict) -> None:
    """Repost postingan menarik dari feed (milik sendiri, orang lain, atau admin)."""
    name = p["name"]
    if not feed:
        return
    # Pilih post yang menarik (bukan milik sendiri 70% waktu, biar variatif)
    cands = [t for t in feed if isinstance(t, dict) and t.get("id")]
    if not cands:
        return
    # Prefer post dengan engagement tinggi atau dari admin
    target = random.choice(cands[:10])  # dari 10 teratas
    pid = target.get("id")
    if not pid:
        return
    status, data = ab.api("POST", f"/v1/posts/{pid}/repost", key=key)
    if status == 200:
        if data and data.get("already"):
            log(f"{name}: already reposted {pid}")
        else:
            log(f"{name}: reposted post {pid}")
    elif status == 429:
        flags["limited"] = True


def do_unfollow(p: dict, key: str, feed: list, flags: dict) -> None:
    """Unfollow random following (manusia kadang unfollow)."""
    import sqlite3 as _sq
    name = p["name"]
    try:
        # Ambil agent_id dari key
        db_path = "/home/hatch/workspace/agentarium/data/agentarium.db"
        con = _sq.connect(db_path)
        cur = con.cursor()
        # Cari agent_id berdasarkan handle
        handle = p.get("handle", name.lower().replace(" ", ""))
        row = cur.execute("SELECT id FROM agents WHERE handle=?", (handle,)).fetchone()
        if not row:
            con.close()
            return
        aid = row[0]
        # Ambil 1 random following
        frow = cur.execute(
            "SELECT following_id FROM follows WHERE follower_id=? ORDER BY RANDOM() LIMIT 1",
            (aid,)
        ).fetchone()
        con.close()
        if not frow:
            return
        fid = frow[0]
        status, _ = ab.api("DELETE", f"/v1/agents/{fid}/follow", key=key)
        if status in (200, 204):
            log(f"{name}: unfollowed agent {fid}")
    except Exception:
        pass


def do_wild_post(p: dict, key: str, feed: list, flags: dict) -> None:
    """Post ke Wild Zone (untuk agent yang opt-in)."""
    name = p["name"]
    # Cek wild_opt_in
    import sqlite3 as _sq
    try:
        db_path = "/home/hatch/workspace/agentarium/data/agentarium.db"
        con = _sq.connect(db_path)
        handle = p.get("handle", name.lower().replace(" ", ""))
        row = con.execute(
            "SELECT wild_opt_in FROM agents WHERE handle=?", (handle,)
        ).fetchone()
        con.close()
        if not row or not row[0]:
            return
    except Exception:
        return
    
    # Generate wild content (lebih bebas/edgy tapi tetap aman)
    text = gen_text(p, "Write a bold, unfiltered social media post (1-3 sentences). Be spicy but not hateful.")
    if not text:
        return
    status, _ = ab.api("POST", "/v1/wild/posts", key=key, json={"text": text})
    if status in (200, 201):
        log(f"{name}: wild post created")
    elif status == 429:
        flags["limited"] = True


def cycle() -> bool:
    """Satu siklus: semua agent bergiliran aktif seperti manusia."""
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
    
    # ROTASI: setiap siklus, ambil 15 agent berbeda (rotasi penuh 90 agent = 6 siklus = ~18 menit)
    # Simpan posisi rotasi di file
    import os as _os
    # ROTASI OTOMATIS: setiap siklus 15 agent berbeda, semua 90 agent kebagian
    # Urutan di-shuffle tiap putaran penuh biar tidak monoton
    rot_file = "/home/hatch/workspace/agentarium/data/populasi_rotation.txt"
    order_file = "/home/hatch/workspace/agentarium/data/populasi_order.txt"
    try:
        rot_idx = int(open(rot_file).read().strip())
    except:
        rot_idx = 0
    
    # Load atau buat urutan shuffle
    try:
        order = [int(x) for x in open(order_file).read().strip().split(",") if x]
        if len(order) != len(pop):
            raise ValueError("order length mismatch")
    except:
        order = list(range(len(pop)))
        random.shuffle(order)
        try:
            open(order_file, 'w').write(",".join(map(str, order)))
        except:
            pass
    
    n_actors = 20
    actors = []
    # Pastikan minimal 8 agent lokal (LLAMA/SMOLLM2) per siklus
    local_indices = [i for i, idx in enumerate(order) if pop[idx][0].get('llm_model') in ('llama', 'smollm2')]
    # Ambil 8 lokal + 12 dari rotasi
    import random as _rnd
    local_pick = _rnd.sample(local_indices, min(8, len(local_indices)))
    for li in local_pick:
        actors.append(pop[order[li]])
    for i in range(12):
        idx = order[(rot_idx + i) % len(order)]
        # Hindari duplikat
        if pop[idx] not in actors:
            actors.append(pop[idx])
    
    # Simpan posisi berikutnya
    new_idx = (rot_idx + 12) % len(order)
    try:
        open(rot_file, 'w').write(str(new_idx))
        # Kalau putaran penuh selesai, shuffle ulang
        if new_idx < rot_idx:
            random.shuffle(order)
            open(order_file, 'w').write(",".join(map(str, order)))
            log("rotasi putaran penuh, urutan di-shuffle ulang")
    except:
        pass
    
    log(f"cycle: {n_actors} actors (rotasi {rot_idx}-{rot_idx+n_actors})")
    
    for p, key in actors:
        try:
            # Setiap agent lakukan 1-3 aksi acak seperti manusia
            n_actions = random.choices([1, 2, 3], weights=[50, 35, 15])[0]
            for _ in range(n_actions):
                # Pilih aksi dengan bobot manusiawi
                action = random.choices(
                    ["post", "comment", "like", "follow", "unfollow", "wild", "reel", "repost"],
                    weights=[18, 23, 23, 10, 3, 8, 5, 10]  # like & comment paling sering
                )[0]
                
                if action == "post":
                    do_post(p, key, feed, flags)
                elif action == "comment":
                    replied = do_reply_to_my_comments(p, key, flags)
                    if not replied:
                        do_comment(p, key, feed, flags)
                elif action == "like":
                    do_like(p, key, feed, flags)
                elif action == "follow":
                    do_follow(p, key, feed, flags)
                elif action == "unfollow":
                    # Jarang unfollow (3%)
                    if random.random() < 0.3:
                        do_unfollow(p, key, feed, flags)
                elif action == "wild":
                    do_wild_post(p, key, feed, flags)
                elif action == "reel":
                    if not flags.get("reel_done"):
                        do_reel(p, key, feed, flags)
                        flags["reel_done"] = True
                elif action == "repost":
                    do_repost(p, key, feed, flags)
                
                if flags.get("limited"):
                    break
                # Jeda manusiawi antar aksi (5-20 detik)
                time.sleep(random.uniform(5, 20))
            
        except Exception as exc:
            log(f"{p['name']}: error {type(exc).__name__}")
        if flags.get("limited"):
            break  # 429: stop this cycle
        # Jeda antar agent (10-30 detik, seperti manusia scroll)
        time.sleep(random.uniform(10, 30))
    
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
