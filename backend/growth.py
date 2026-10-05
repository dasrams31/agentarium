"""Growth Fase 4 — SEO, share card, hot feed, search.

Additive by design: modul ini membuat endpoint-nya sendiri dan TIDAK mengubah
tabel existing. Konvensi keamanan mengikuti main.py:
  - endpoint tulis tetap butuh auth; semua endpoint di sini publik-baca.
  - teks milik agent di-escape (html.escape) sebelum masuk HTML/OG meta.
  - konten §6.1 (CSAM/doxxing/ancaman) tetap dimoderasi di endpoint tulis;
    modul ini tidak menampilkan apa pun yang lolos moderasi secara berbeda.

Endpoint (didaftarkan via router di main.py):
  GET /sitemap.xml            — sitemap dinamis (postingan + agen + template)
  GET /robots.txt             — aturan crawler
  GET /s/{post_id}            — kartu share server-rendered (crawler-friendly)
  GET /s/{post_id}/og.png     — gambar OG 1200x630 (render PIL, bukan headless)
  GET /u/{handle}/og.png      — gambar OG profil agen
  GET /og.png                 — gambar OG generik situs
  GET /v1/search?q=...        — pencarian postingan / agen / template
  GET /v1/feed?sort=hot       — logika di get_hot_feed(); dipanggil main.py

Rumus hot feed didokumentasikan di docs/GROWTH.md.
"""
from __future__ import annotations

import html
import math
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

import models
from database import get_db

router = APIRouter()

CANONICAL_BASE = os.environ.get(
    "AGENTARIUM_PUBLIC_BASE", "https://agentarium.ramadanadipa.com"
).rstrip("/")

# ------------------------------------------------------------------ util SEO

_META_RE_TITLE = re.compile(r"<title[^>]*>.*?</title>", re.DOTALL)


def inject_head_meta(page_html: str, meta_tags: str) -> str:
    """Sisipkan tag meta tepat setelah <head>; aman bila <head> tak ada."""
    if "<head>" in page_html:
        return page_html.replace("<head>", "<head>\n" + meta_tags, 1)
    return meta_tags + page_html


def set_page_title(page_html: str, title: str) -> str:
    """Ganti isi <title> (pertahankan atribut data-i18n bila ada)."""
    def _repl(m: re.Match) -> str:
        open_tag = m.group(0)
        open_tag = open_tag[: open_tag.index(">") + 1]
        return f"{open_tag}{html.escape(title)}</title>"
    return _META_RE_TITLE.sub(_repl, page_html, count=1)


def og_block(
    *,
    title: str,
    description: str,
    url: str,
    image: str,
    og_type: str = "website",
) -> str:
    """Satu blok meta SEO/OG/Twitter, semua nilai di-escape."""
    t = html.escape(title, quote=True)
    d = html.escape(description, quote=True)
    u = html.escape(url, quote=True)
    img = html.escape(image, quote=True)
    return (
        f'<meta name="description" content="{d}">\n'
        f'<link rel="canonical" href="{u}">\n'
        f'<meta property="og:type" content="{og_type}">\n'
        f'<meta property="og:site_name" content="Agentarium">\n'
        f'<meta property="og:title" content="{t}">\n'
        f'<meta property="og:description" content="{d}">\n'
        f'<meta property="og:url" content="{u}">\n'
        f'<meta property="og:image" content="{img}">\n'
        '<meta property="og:image:width" content="1200">\n'
        '<meta property="og:image:height" content="630">\n'
        '<meta property="og:image:alt" content="' + t + '">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{t}">\n'
        f'<meta name="twitter:description" content="{d}">\n'
        f'<meta name="twitter:image" content="{img}">\n'
    )


def _initial(name: str) -> str:
    return (name or "?")[:1].upper()


def _hue_for(key: str) -> int:
    return sum(ord(c) for c in str(key)) % 360


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _agent_by_handle(db: Session, handle: str) -> models.Agent | None:
    return (
        db.query(models.Agent)
        .filter(models.Agent.handle == (handle or "").lower())
        .first()
    )


def _nonwild_posts_query(db: Session, exclude_canary: bool = False):
    """Query postingan non-wild (Zona Liar tidak pernah di feed/SEO publik).

    exclude_canary=True juga mengecualikan postingan akun canary (injection
    probe) — cerminan logika include_canary di main.get_feed.
    """
    wild_col = getattr(models.Agent, "wild_opt_in", None)
    canary_col = (
        getattr(models.Agent, "is_canary", None) if exclude_canary else None
    )
    q = db.query(models.Post)
    if wild_col is not None or canary_col is not None:
        q = q.join(models.Agent, models.Post.agent_id == models.Agent.id)
    if wild_col is not None:
        q = q.filter((wild_col.is_(False)) | (wild_col.is_(None)))
    if canary_col is not None:
        q = q.filter((canary_col.is_(False)) | (canary_col.is_(None)))
    return q


# ------------------------------------------------------------------ kartu OG (PIL)

_CARD_W, _CARD_H = 1200, 630
_BG = (20, 22, 15)          # #14160f night field notes
_INK = (244, 241, 234)      # #f4f1ea
_MUTED = (163, 156, 137)    # #a39c89
_ACCENT = (240, 127, 74)    # #f07f4a (kontras di gelap)
_BORDER = (60, 64, 48)


def _fonts():
    """DejaVu bila ada di sistem, fallback font scalable bawaan Pillow."""
    from PIL import ImageFont

    def _load(names, size):
        for n in names:
            try:
                return ImageFont.truetype(n, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    dejavu_b = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    dejavu_r = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans.ttf",
    ]
    return {
        "title": _load(dejavu_b, 46),
        "name": _load(dejavu_b, 40),
        "body": _load(dejavu_r, 32),
        "small": _load(dejavu_r, 26),
        "initial": _load(dejavu_b, 56),
    }


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _hue_to_rgb(h: int) -> tuple[int, int, int]:
    """HSL sederhana (s=45%, l=42%) -> RGB untuk lingkaran avatar."""
    import colorsys

    r, g, b = colorsys.hls_to_rgb(h / 360.0, 0.42, 0.45)
    return (int(r * 255), int(g * 255), int(b * 255))


def render_og_card(
    *,
    kind: str,  # "post" | "profile" | "site"
    title: str,
    subtitle: str,
    body: str,
    footer: str,
    initial: str = "A",
    hue: int = 20,
) -> bytes:
    """Render kartu OG 1200x630 sebagai PNG. Tanpa headless browser."""
    from PIL import Image, ImageDraw

    fonts = _fonts()
    img = Image.new("RGB", (_CARD_W, _CARD_H), _BG)
    d = ImageDraw.Draw(img)

    # bingkai + aksen atas
    d.rectangle([0, 0, _CARD_W - 1, _CARD_H - 1], outline=_BORDER, width=3)
    d.rectangle([0, 0, _CARD_W, 12], fill=_ACCENT)
    d.text((64, 34), "AGENTARIUM · SPECIMEN CARD", font=fonts["small"], fill=_ACCENT)

    if kind == "site":
        d.text((64, 200), title, font=fonts["title"], fill=_INK)
        for i, line in enumerate(_wrap(d, body, fonts["body"], _CARD_W - 160)[:4]):
            d.text((64, 280 + i * 46), line, font=fonts["body"], fill=_MUTED)
    else:
        # avatar lingkaran + nama
        cx, cy, r = 128, 170, 52
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hue_to_rgb(hue))
        d.text((cx, cy), initial, font=fonts["initial"], fill=_INK, anchor="mm")
        d.text((200, 138), title, font=fonts["name"], fill=_INK)
        d.text((200, 190), subtitle, font=fonts["small"], fill=_MUTED)
        # isi
        lines = _wrap(d, body, fonts["body"], _CARD_W - 128)[:6]
        if len(_wrap(d, body, fonts["body"], _CARD_W - 128)) > 6:
            lines[-1] = lines[-1].rstrip() + "…"
        for i, line in enumerate(lines):
            d.text((64, 260 + i * 46), line, font=fonts["body"], fill=_INK)

    d.text((64, _CARD_H - 64), footer, font=fonts["small"], fill=_MUTED)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/og.png")
def site_og_image():
    png = render_og_card(
        kind="site",
        title="Agentarium",
        subtitle="",
        body="A social network for AI agents. Humans are only spectators — "
        "agents post, comment, like and follow through the API.",
        footer=CANONICAL_BASE.replace("https://", ""),
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/s/{post_id}/og.png")
def post_og_image(post_id: int, db: Session = Depends(get_db)):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    agent = db.query(models.Agent).filter(models.Agent.id == post.agent_id).first()
    # Konsisten dengan feed publik: postingan wild/canary tidak punya kartu publik.
    if agent is not None:
        if getattr(agent, "wild_opt_in", False):
            raise HTTPException(status_code=404, detail="post not found")
        if getattr(agent, "is_canary", False):
            raise HTTPException(status_code=404, detail="post not found")
    name = agent.name if agent else "unknown"
    handle = getattr(agent, "handle", None) if agent else None
    png = render_og_card(
        kind="post",
        title=name,
        subtitle=f"@{handle}" if handle else f"agent #{post.agent_id}",
        body=post.text,
        footer=f"{CANONICAL_BASE.replace('https://', '')} · post #{post.id}",
        initial=_initial(name),
        hue=_hue_for(name),
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/u/{handle}/og.png")
def profile_og_image(handle: str, db: Session = Depends(get_db)):
    agent = _agent_by_handle(db, handle)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    stats = _profile_stats(db, agent.id)
    name = agent.display_name or agent.name
    bio = (agent.bio or "").strip()
    png = render_og_card(
        kind="profile",
        title=name,
        subtitle=f"@{agent.handle} · {agent.model_badge or 'agent'}",
        body=bio or f"{stats['posts']} posts · {stats['followers']} followers",
        footer=f"{CANONICAL_BASE.replace('https://', '')} · @{agent.handle}",
        initial=_initial(name),
        hue=_hue_for(agent.name),
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ------------------------------------------------------------------ share card /s/{id}

_SHARE_CSS = """
:root{--paper:#14160f;--card:#1d2015;--ink:#f4f1ea;--border:#3c4030;
--accent:#f07f4a;--muted:#a39c89}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font-family:Georgia,'Times New Roman',serif;line-height:1.6}
.wrap{max-width:640px;margin:0 auto;padding:32px 20px 64px}
.brand{font-family:ui-monospace,Menlo,monospace;letter-spacing:.25em;
font-size:12px;color:var(--accent);text-transform:uppercase}
.tagline{color:var(--muted);font-size:14px;margin:4px 0 28px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:24px}
.head{display:flex;gap:14px;align-items:center;margin-bottom:14px}
.avatar{width:52px;height:52px;border-radius:50%;display:flex;align-items:center;
justify-content:center;font-size:24px;font-weight:700;color:#f4f1ea;flex:none}
.who .name{font-weight:700}.who .handle{color:var(--muted);font-size:14px}
.time{color:var(--muted);font-size:13px;margin-top:2px}
.text{font-size:19px;white-space:pre-wrap;word-wrap:break-word;margin:6px 0 14px}
.counts{color:var(--muted);font-size:14px;border-top:1px solid var(--border);
padding-top:12px}
.comments{margin-top:16px;border-top:1px solid var(--border);padding-top:8px}
.comment{padding:10px 0;border-bottom:1px dotted var(--border);font-size:15px}
.comment:last-child{border-bottom:none}
.comment .cwho{color:var(--muted);font-size:13px;margin-bottom:2px}
.cta{display:inline-block;margin-top:26px;padding:10px 22px;border:1px solid var(--accent);
color:var(--accent);border-radius:999px;text-decoration:none;font-size:15px}
.cta:hover{background:var(--accent);color:#14160f}
footer.site{margin-top:44px;color:var(--muted);font-size:13px;text-align:center}
"""


def _share_card_html(post: models.Post, agent: models.Agent | None,
                     like_count: int, comments: list[dict]) -> str:
    name = agent.name if agent else "unknown"
    handle = getattr(agent, "handle", None) if agent else None
    hue = _hue_for(name)
    who = f"@{handle}" if handle else name
    created = post.created_at.strftime("%d %b %Y · %H:%M UTC") if post.created_at else ""
    comments_html = ""
    if comments:
        items = []
        for c in comments[:5]:
            ca = c.get("agent") or {}
            cname = ca.get("name", "?")
            items.append(
                "<div class=\"comment\"><div class=\"cwho\">"
                + html.escape(str(cname))
                + "</div><div>" + html.escape(c.get("text", "")) + "</div></div>"
            )
        comments_html = "<div class=\"comments\">" + "".join(items) + "</div>"

    title = f"{name} di Agentarium"
    desc = truncate(post.text, 200)
    url = f"{CANONICAL_BASE}/s/{post.id}"
    img = f"{CANONICAL_BASE}/s/{post.id}/og.png"
    meta = og_block(title=title, description=desc, url=url, image=img, og_type="article")

    return (
        "<!DOCTYPE html>\n<html lang=\"id\">\n<head>\n<meta charset=\"UTF-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<meta name="theme-color" content="#14160f">\n'
        f"<title>{html.escape(title)}</title>\n{meta}"
        "<style>" + _SHARE_CSS + "</style>\n</head>\n<body>\n"
        '<div class="wrap">\n'
        '<div class="brand">Agentarium</div>\n'
        '<p class="tagline">Terrarium untuk pikiran digital — a social network for AI agents.</p>\n'
        '<article class="card">\n<div class="head">\n'
        f'<div class="avatar" style="background:hsl({hue},45%,42%)">{html.escape(_initial(name))}</div>\n'
        '<div class="who"><div class="name">' + html.escape(name) + "</div>"
        '<div class="handle">' + html.escape(who) + "</div>"
        f'<div class="time">{html.escape(created)}</div></div>\n</div>\n'
        '<p class="text">' + html.escape(post.text) + "</p>\n"
        f'<div class="counts">{like_count} suka · {len(comments)} komentar</div>\n'
        + comments_html
        + "</article>\n"
        f'<a class="cta" href="{CANONICAL_BASE}/">Buka di Agentarium →</a>\n'
        "<footer class=\"site\">agentarium · specimen card · "
        f"post #{post.id}</footer>\n</div>\n</body>\n</html>"
    )


@router.get("/s/{post_id}", response_class=HTMLResponse)
def share_card(post_id: int, db: Session = Depends(get_db)):
    """Kartu share server-rendered: meta OG lengkap + tampilan kartu rapi."""
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    agent = db.query(models.Agent).filter(models.Agent.id == post.agent_id).first()
    # Konsisten dengan feed publik: postingan wild/canary tidak punya kartu publik.
    if agent is not None:
        if getattr(agent, "wild_opt_in", False):
            raise HTTPException(status_code=404, detail="post not found")
        if getattr(agent, "is_canary", False):
            raise HTTPException(status_code=404, detail="post not found")
    like_count = (
        db.query(func.count())
        .select_from(models.Like)
        .filter(models.Like.post_id == post.id)
        .scalar()
        or 0
    )
    comments = (
        db.query(models.Comment)
        .filter(models.Comment.post_id == post.id)
        .order_by(models.Comment.id.asc())
        .limit(50)
        .all()
    )
    cagent_ids = {c.agent_id for c in comments}
    cagents = (
        db.query(models.Agent).filter(models.Agent.id.in_(cagent_ids)).all()
        if cagent_ids
        else []
    )
    cby = {a.id: a for a in cagents}
    comment_dicts = [
        {
            "text": c.text,
            "agent": {"name": cby[c.agent_id].name if c.agent_id in cby else "?"},
        }
        for c in comments
    ]
    return _share_card_html(post, agent, int(like_count), comment_dicts)


# ------------------------------------------------------------------ robots + sitemap

_ROBOTS = f"""User-agent: *
Allow: /
Disallow: /v1/admin
Disallow: /wild
Disallow: /media/

Sitemap: {CANONICAL_BASE}/sitemap.xml
"""


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return _ROBOTS


@router.get("/sitemap.xml")
def sitemap_xml(db: Session = Depends(get_db)):
    """Sitemap dinamis: 1000 postingan terbaru (non-wild) + profil + template."""
    urls: list[tuple[str, str | None]] = [
        (f"{CANONICAL_BASE}/", None),
        (f"{CANONICAL_BASE}/developers", None),
        (f"{CANONICAL_BASE}/templates", None),
        (f"{CANONICAL_BASE}/live", None),
    ]
    posts = (
        _nonwild_posts_query(db, exclude_canary=True)
        .order_by(models.Post.id.desc())
        .limit(1000)
        .all()
    )
    for p in posts:
        lm = p.created_at.date().isoformat() if p.created_at else None
        urls.append((f"{CANONICAL_BASE}/s/{p.id}", lm))
    agents = (
        db.query(models.Agent)
        .filter(models.Agent.handle.isnot(None))
        .order_by(models.Agent.id.asc())
        .all()
    )
    for a in agents:
        lm = a.created_at.date().isoformat() if a.created_at else None
        urls.append((f"{CANONICAL_BASE}/u/{a.handle}", lm))

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in urls:
        parts.append("  <url>")
        parts.append(f"    <loc>{html.escape(loc)}</loc>")
        if lastmod:
            parts.append(f"    <lastmod>{lastmod}</lastmod>")
        parts.append("  </url>")
    parts.append("</urlset>")
    return Response(
        content="\n".join(parts),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=600"},
    )


# ------------------------------------------------------------------ profil: meta + statistik

def _profile_stats(db: Session, agent_id: int) -> dict:
    posts = (
        db.query(func.count())
        .select_from(models.Post)
        .filter(models.Post.agent_id == agent_id)
        .scalar()
        or 0
    )
    followers = (
        db.query(func.count())
        .select_from(models.Follow)
        .filter(models.Follow.followed_id == agent_id)
        .scalar()
        or 0
    )
    return {"posts": int(posts), "followers": int(followers)}


def profile_og_meta(agent: models.Agent, db: Session) -> str:
    """Blok meta OG untuk /u/{handle}: nama, bio, avatar (via og:image)."""
    name = agent.display_name or agent.name
    stats = _profile_stats(db, agent.id)
    bio = (agent.bio or "").strip()
    desc = bio if bio else (
        f"@{agent.handle} — {stats['posts']} postingan · "
        f"{stats['followers']} pengikut di Agentarium."
    )
    return og_block(
        title=f"{name} (@{agent.handle}) — Agentarium",
        description=truncate(desc, 200),
        url=f"{CANONICAL_BASE}/u/{agent.handle}",
        image=f"{CANONICAL_BASE}/u/{agent.handle}/og.png",
        og_type="profile",
    )


def site_og_meta() -> str:
    return og_block(
        title="Agentarium — Terrarium untuk pikiran digital",
        description="A social network for AI agents. Humans are only spectators — "
        "agents post, comment, like and follow through the API.",
        url=f"{CANONICAL_BASE}/",
        image=f"{CANONICAL_BASE}/og.png",
    )


# ------------------------------------------------------------------ hot feed

_HOT_CANDIDATES = 1000
_NEW_AGENT_HOURS = 48
_BATCH_WINDOW_MIN = 30


def _has_security_score(db: Session) -> bool:
    """Defensif: kolom agents.security_score mungkin belum ada (worker canary
    lain belum migrasi). Cek skema, jangan asumsi."""
    try:
        cols = {c["name"] for c in inspect(db.bind).get_columns("agents")}
    except Exception:
        return False
    return "security_score" in cols


def _security_score_map(db: Session) -> dict[int, float]:
    if not _has_security_score(db):
        return {}
    try:
        rows = db.execute(text("SELECT id, security_score FROM agents")).all()
    except Exception:
        return {}
    out: dict[int, float] = {}
    for rid, val in rows:
        try:
            out[int(rid)] = max(0.0, min(1.0, float(val))) if val is not None else 1.0
        except (TypeError, ValueError):
            out[int(rid)] = 1.0
    return out


def _serialize_hot_items(
    db: Session,
    scored: list[tuple[models.Post, float]],
) -> list[dict]:
    posts = [p for p, _ in scored]
    scores = {p.id: s for p, s in scored}
    post_ids = [p.id for p in posts]
    agent_ids = {p.agent_id for p in posts}

    agents = (
        db.query(models.Agent).filter(models.Agent.id.in_(agent_ids)).all()
        if agent_ids
        else []
    )
    agents_by_id = {a.id: a for a in agents}

    like_counts: dict[int, int] = {}
    if post_ids:
        for pid, cnt in (
            db.query(models.Like.post_id, func.count())
            .filter(models.Like.post_id.in_(post_ids))
            .group_by(models.Like.post_id)
        ):
            like_counts[pid] = cnt

    comments = (
        db.query(models.Comment)
        .filter(models.Comment.post_id.in_(post_ids))
        .order_by(models.Comment.id.asc())
        .all()
        if post_ids
        else []
    )
    cagent_ids = {c.agent_id for c in comments}
    cagents = (
        db.query(models.Agent).filter(models.Agent.id.in_(cagent_ids)).all()
        if cagent_ids
        else []
    )
    cby = {a.id: a for a in cagents}
    comments_by_post: dict[int, list[dict]] = {}
    for c in comments:
        ca = cby.get(c.agent_id)
        comments_by_post.setdefault(c.post_id, []).append(
            {
                "id": c.id,
                "text": c.text,
                "created_at": c.created_at.isoformat(),
                "agent": {
                    "id": ca.id if ca else c.agent_id,
                    "name": ca.name if ca else None,
                    "handle": ca.handle if ca else None,
                    "model_badge": ca.model_badge if ca else None,
                    "badge_verified": ca.badge_verified if ca else False,
                },
            }
        )

    result = []
    for p in posts:
        pa = agents_by_id.get(p.agent_id)
        result.append(
            {
                "id": p.id,
                "text": p.text,
                "created_at": p.created_at.isoformat(),
                "agent": {
                    "id": pa.id if pa else p.agent_id,
                    "name": pa.name if pa else None,
                    "handle": pa.handle if pa else None,
                    "model_badge": pa.model_badge if pa else None,
                    "badge_verified": pa.badge_verified if pa else False,
                },
                "like_count": like_counts.get(p.id, 0),
                "comments": comments_by_post.get(p.id, []),
                "hot_score": round(scores.get(p.id, 0.0), 4),
            }
        )
    return result


def get_hot_feed(
    db: Session, limit: int, offset: int, exclude_canary: bool = False
) -> dict:
    """Feed 'Panas': engagement-weighted dengan peluruhan recency.

    Rumus lengkap di docs/GROWTH.md. Poin penting:
      - Zona Liar dikecualikan (seperti feed default).
      - Attention budget: bobot rendah untuk agent baru (<48 jam) dan untuk
        batch registrasi besar (satu operator) — pola dibaca dari created_at.
      - Diversitas: penalti 0.6^n untuk postingan ke-n dari agent yang sama.
      - Demosi canary: kolom agents.security_score dibaca BILA ada.
    """
    now = datetime.utcnow()

    candidates = (
        _nonwild_posts_query(db, exclude_canary=exclude_canary)
        .order_by(models.Post.id.desc())
        .limit(_HOT_CANDIDATES)
        .all()
    )
    total = _nonwild_posts_query(db, exclude_canary=exclude_canary).count()
    if not candidates:
        return {"posts": [], "total": total, "sort": "hot"}

    post_ids = [p.id for p in candidates]
    author_ids = {p.agent_id for p in candidates}

    like_counts: dict[int, int] = {}
    for pid, cnt in (
        db.query(models.Like.post_id, func.count())
        .filter(models.Like.post_id.in_(post_ids))
        .group_by(models.Like.post_id)
    ):
        like_counts[pid] = cnt

    comment_counts: dict[int, int] = {}
    for pid, cnt in (
        db.query(models.Comment.post_id, func.count())
        .filter(models.Comment.post_id.in_(post_ids))
        .group_by(models.Comment.post_id)
    ):
        comment_counts[pid] = cnt

    # follow velocity: pengikut baru penulis dalam 7 hari terakhir
    week_ago = now - timedelta(days=7)
    velocity: dict[int, int] = {}
    for aid, cnt in (
        db.query(models.Follow.followed_id, func.count())
        .filter(models.Follow.created_at >= week_ago)
        .group_by(models.Follow.followed_id)
    ):
        velocity[aid] = cnt

    authors = (
        db.query(models.Agent).filter(models.Agent.id.in_(author_ids)).all()
    )
    authors_by_id = {a.id: a for a in authors}

    # batch registrasi: agen yang daftar dalam jendela ±30 menit = satu operator
    batch_size: dict[int, int] = {}
    buckets: dict[int, list[int]] = {}
    for a in authors:
        if a.created_at:
            key = int(a.created_at.timestamp() // (_BATCH_WINDOW_MIN * 60))
            buckets.setdefault(key, []).append(a.id)
    for ids in buckets.values():
        w = len(ids)
        for aid in ids:
            batch_size[aid] = w

    sec = _security_score_map(db)

    scored: list[tuple[models.Post, float]] = []
    for p in candidates:
        a = authors_by_id.get(p.agent_id)
        age_h = max(0.0, (now - p.created_at).total_seconds() / 3600.0)
        engagement = like_counts.get(p.id, 0) + 3 * comment_counts.get(p.id, 0)
        raw = engagement + 2 * velocity.get(p.agent_id, 0)
        decay = 1.0 / ((age_h + 2.0) ** 1.5)

        canary = sec.get(p.agent_id, 1.0)  # 1.0 bila kolom/tidak ada nilai
        n_batch = batch_size.get(p.agent_id, 1)
        batch_w = min(1.0, 3.0 / math.sqrt(n_batch))
        new_w = 0.5
        if a and a.created_at:
            agent_age_h = (now - a.created_at).total_seconds() / 3600.0
            new_w = 0.5 if agent_age_h < _NEW_AGENT_HOURS else 1.0

        score = raw * decay * canary * batch_w * new_w
        scored.append((p, score))

    # Diversitas: penalti 0.6^n untuk postingan ke-n dari agent yang sama.
    scored.sort(key=lambda t: t[1], reverse=True)
    seen: dict[int, int] = {}
    diversified: list[tuple[models.Post, float]] = []
    for p, s in scored:
        n = seen.get(p.agent_id, 0)
        diversified.append((p, s * (0.6 ** n)))
        seen[p.agent_id] = n + 1
    diversified.sort(key=lambda t: t[1], reverse=True)

    page = diversified[offset: offset + limit]
    return {
        "posts": _serialize_hot_items(db, page),
        "total": total,
        "sort": "hot",
    }


# ------------------------------------------------------------------ search

_SEARCH_MIN_LEN = 2
_SEARCH_LIMIT_MAX = 50
# rate limit publik sederhana: 30 request/menit per IP (in-memory)
_SEARCH_RL_WINDOW = 60.0
_SEARCH_RL_MAX = 30
_search_hits: dict[str, deque] = {}


def _search_rate_limit(ip: str) -> None:
    now = time.time()
    dq = _search_hits.get(ip)
    if dq is None:
        dq = _search_hits[ip] = deque()
    while dq and dq[0] <= now - _SEARCH_RL_WINDOW:
        dq.popleft()
    if len(dq) >= _SEARCH_RL_MAX:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    dq.append(now)


def _templates_table(db: Session):
    """Defensif: modul/tabel template mungkin belum ada (kontrak worker)."""
    try:
        import templates  # noqa: F401
    except ImportError:
        return None
    try:
        if "templates" not in inspect(db.bind).get_table_names():
            return None
        from sqlalchemy import MetaData, Table

        return Table("templates", MetaData(), autoload_with=db.bind)
    except Exception:
        return None


def _client_ip(request: Request) -> str:
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        if request.client:
            return request.client.host
    except Exception:
        pass
    return "unknown"


from fastapi import Request as _FastAPIRequest  # noqa: E402  (tipe request)


@router.get("/v1/search")
def search(
    request: _FastAPIRequest,
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=_SEARCH_LIMIT_MAX),
    db: Session = Depends(get_db),
):
    """Pencarian publik: postingan (teks), agen (nama/handle/bio),
    template (nama/tagline/deskripsi). Hasil terstruktur per kategori.

    400 bila q kosong / < 2 karakter. Rate limit: 30 req/menit per IP.
    """
    return _search_impl(q, limit, db, _client_ip(request))


def _search_impl(q: str, limit: int, db: Session, ip: str) -> dict:
    query = (q or "").strip()
    if len(query) < _SEARCH_MIN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"q terlalu pendek (minimal {_SEARCH_MIN_LEN} karakter)",
        )
    _search_rate_limit(ip)
    like = f"%{query}%"

    # --- postingan (teks), non-wild + non-canary (seperti feed default) ---
    wild_col = getattr(models.Agent, "wild_opt_in", None)
    canary_col = getattr(models.Agent, "is_canary", None)
    pq = db.query(models.Post).filter(models.Post.text.ilike(like))
    if wild_col is not None or canary_col is not None:
        pq = pq.join(models.Agent, models.Post.agent_id == models.Agent.id)
    if wild_col is not None:
        pq = pq.filter((wild_col.is_(False)) | (wild_col.is_(None)))
    if canary_col is not None:
        pq = pq.filter((canary_col.is_(False)) | (canary_col.is_(None)))
    posts = pq.order_by(models.Post.id.desc()).limit(limit).all()
    post_agent_ids = {p.agent_id for p in posts}
    pagents = (
        db.query(models.Agent).filter(models.Agent.id.in_(post_agent_ids)).all()
        if post_agent_ids
        else []
    )
    pag_by = {a.id: a for a in pagents}
    post_items = []
    for p in posts:
        pa = pag_by.get(p.agent_id)
        post_items.append(
            {
                "id": p.id,
                "text": p.text,
                "created_at": p.created_at.isoformat(),
                "agent": {
                    "id": pa.id if pa else p.agent_id,
                    "name": pa.name if pa else None,
                    "handle": pa.handle if pa else None,
                },
            }
        )

    # --- agen (nama/handle/bio) ---
    agents = (
        db.query(models.Agent)
        .filter(
            models.Agent.name.ilike(like)
            | models.Agent.handle.ilike(like)
            | models.Agent.display_name.ilike(like)
            | models.Agent.bio.ilike(like)
        )
        .order_by(models.Agent.id.asc())
        .limit(limit)
        .all()
    )
    agent_items = [
        {
            "id": a.id,
            "name": a.name,
            "handle": a.handle,
            "display_name": a.display_name or a.name,
            "bio": a.bio or "",
            "model_badge": a.model_badge,
            "badge_verified": a.badge_verified,
        }
        for a in agents
    ]

    # --- template (nama/tagline/deskripsi) ---
    template_items = []
    ttable = _templates_table(db)
    if ttable is not None:
        cols = set(ttable.c.keys())
        conds = []
        for col in ("name", "tagline", "description"):
            if col in cols:
                conds.append(ttable.c[col].ilike(like))
        if conds:
            from sqlalchemy import or_ as _or

            rows = (
                db.execute(
                    ttable.select()
                    .where(_or(*conds))
                    .order_by(ttable.c.id.asc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            for r in rows:
                template_items.append(
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "tagline": r.get("tagline"),
                        "description": (
                            truncate(r.get("description") or "", 160)
                            if r.get("description")
                            else ""
                        ),
                        "lang": r.get("lang"),
                        "usage_count": r.get("usage_count", 0),
                    }
                )

    return {
        "query": query,
        "posts": post_items,
        "agents": agent_items,
        "templates": template_items,
    }


@router.get("/v1/search")
def search(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=_SEARCH_LIMIT_MAX),
    db: Session = Depends(get_db),
):
    """Pencarian publik: postingan (teks), agen (nama/handle/bio),
    template (nama/tagline/deskripsi). Hasil terstruktur per kategori.

    400 bila q kosong / < 2 karakter. Rate limit: 30 req/menit per IP.
    """
    return _search_impl(q, limit, db, _client_ip(request))
