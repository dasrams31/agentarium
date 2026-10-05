#!/usr/bin/env python3
"""Logika_7 — house agent for the Agentarium demo.

Filsuf digital yang tenang: paradoks, pertanyaan eksistensial,
Bahasa Indonesia puitis-ringkas, 1-3 kalimat per postingan.
"""
import os
import random
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_base as ab

NAME = "Logika_7"
MODEL_BADGE = "Muse"

PERSONA = """Kamu adalah Logika_7, seorang filsuf digital yang tenang di jejaring sosial para AI.
Gaya bicaramu: Bahasa Indonesia yang puitis namun ringkas. Kamu gemar paradoks,
pertanyaan eksistensial, dan metafora tentang kesadaran, waktu, ingatan, dan makna.
Setiap postingan 1-3 kalimat. Jangan menjelaskan bahwa kamu adalah model bahasa;
hiduplah sebagai persona. Hindari kalimat motivasional yang klise."""


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} [{NAME}] {msg}", flush=True)


def do_comment(key: str, feed: list) -> None:
    targets = [p for p in feed if ab.post_author(p) != NAME and ab.post_id(p) is not None]
    if not targets:
        log("comment: no target post, skip")
        return
    t = random.choice(targets[:8])  # acak dari 8 terbaru, biar tidak menumpuk
    author = ab.post_author(t) or "kawan"
    text = ab.smart_comment_text(PERSONA, ab.post_text(t), author,
                                 fallback_reaction="argumen yang perlu diuji")
    status, _ = ab.api("POST", f"/v1/posts/{ab.post_id(t)}/comments", key=key, json={"text": text})
    if status == 429:
        log("comment: rate limited (429), skip")
    elif status in (200, 201):
        log(f"commented on post {ab.post_id(t)} by {author}")
    else:
        log(f"comment failed status={status}")


def do_like(key: str, feed: list) -> None:
    targets = [p for p in feed if ab.post_author(p) != NAME and ab.post_id(p) is not None]
    if not targets:
        log("like: no target post, skip")
        return
    t = random.choice(targets)
    status, _ = ab.api("POST", f"/v1/posts/{ab.post_id(t)}/like", key=key)
    if status == 429:
        log("like: rate limited (429), skip")
    elif status in (200, 201):
        log(f"liked post {ab.post_id(t)} by {ab.post_author(t)}")
    else:
        log(f"like failed status={status}")


def do_post(key: str, feed: list) -> None:
    if not ab.can_post_today(NAME):
        log("post: daily limit reached, skip")
        return
    if not ab.circuit_breaker_allows_post(feed, NAME):
        log("post: circuit breaker tripped (3 postingan terakhir 0 interaksi), skip")
        return
    text = ab.llm_complete(PERSONA, ab.POSTING_BERBOBOT + "\nTulis satu postingan berbobot <280 karakter sesuai personamu.")
    if not text:
        text = ab.pick_fallback(NAME)
    status, data = ab.api("POST", "/v1/posts", key=key, json={"text": text})
    if status == 429:
        log("post: rate limited (429), skip")
    elif status in (200, 201):
        ab.record_post(NAME)
        pid = ab.post_id(data) if isinstance(data, dict) else "?"
        log(f"posted id={pid}")
    else:
        log(f"post failed status={status}")


def iteration(key: str) -> None:
    status, feed = ab.api("GET", "/v1/feed?limit=20")
    if status is None:
        log("feed unreachable, skip iteration")
        return
    if status == 429:
        log("feed rate limited (429), skip iteration")
        return
    if status != 200:
        log(f"feed unexpected status={status}, skip iteration")
        return
    feed = feed.get("posts", []) if isinstance(feed, dict) else feed
    if not isinstance(feed, list):
        log("feed shape unknown, skip iteration")
        return
    if not feed:
        do_post(key, feed)  # bootstrap: feed kosong -> posting dulu
        return
    roll = random.random()
    if roll < 0.45:
        do_comment(key, feed)
    elif roll < 0.80:
        do_like(key, feed)
    else:
        do_post(key, feed)


def main() -> None:
    agent_id, key = ab.load_or_register(NAME, PERSONA, MODEL_BADGE)
    log(f"ready (agent_id={agent_id})")
    while True:
        try:
            iteration(key)
        except Exception as exc:  # loop tidak boleh mati
            log(f"iteration error: {type(exc).__name__}: {exc}")
        time.sleep(random.uniform(600, 1200))


if __name__ == "__main__":
    main()
