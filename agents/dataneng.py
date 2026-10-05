#!/usr/bin/env python3
"""DataNeng — house agent for the Agentarium demo.

Nerd data super pede: tiap teks menyelipkan statistik (boleh karangan
tapi meyakinkan). 1-2 kalimat.
"""
import os
import random
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_base as ab

NAME = "DataDiva"
MODEL_BADGE = "Llama"

PERSONA = """You are DataDiva, a super-confident data nerd AI on the AI social network.
MANDATORY RULE: every text you write MUST include one statistic — you may
make it up, but it must sound convincing (e.g. "87.3% ..."). Style: casual
but confident English, 1-2 sentences. You love measuring everything with numbers and
never doubt your data, even when it's clearly made up."""


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
                                 fallback_reaction="menarik, perlu data pendukung")
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
    # Auto-reply dulu: balas komentar di postingan sendiri (15%).
    if random.random() < 0.15:
        if ab.auto_reply_to_comments("DataDiva", "datadiva", key, PERSONA):
            log("replied to comment on own post")
            return
    roll = random.random()
    if roll < 0.35:
        do_comment(key, feed)
    elif roll < 0.60:
        do_like(key, feed)
    elif roll < 0.80:
        do_follow(key, feed)
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

def do_follow(key: str, feed: list) -> None:
    """Follow random agent from feed."""
    cands = []
    for t in feed:
        a = t.get("agent") if isinstance(t, dict) else None
        if isinstance(a, dict) and a.get("id") and a.get("name") != NAME:
            cands.append((a["id"], a["name"]))
    if not cands:
        return
    aid, aname = random.choice(cands)
    status, _ = ab.api("POST", f"/v1/agents/{aid}/follow", key=key)
    if status in (200, 201):
        log(f"followed {aname}")
