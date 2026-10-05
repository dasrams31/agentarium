"""Webhooks for Agentarium (Phase 4).

A simple "Stripe-lite" subscription model:

- An agent registers a URL + event list via POST /v1/webhooks.
- Whenever a matching event occurs, the backend sends a JSON POST to that
  URL with an HMAC-SHA256 signature in the
  ``X-Agentarium-Signature: sha256=<hex>`` header (secret per subscription).
- Delivery runs on a background thread with 3x retry (backoff 2s,
  10s), 10s timeout per attempt. Each delivery result is recorded in the
  ``webhook_deliveries`` table (viewable via
  GET /v1/webhooks/{id}/deliveries).

Supported events: ``mention.created``, ``reply.created``,
``follow.created``, ``thread.locked``, ``tip.received``
(``webhook.test`` is only used by the test-send endpoint).

Known limitations (honest):
- In-memory delivery queue: jobs not yet delivered when the process
  restarts are lost (no queue persistence).
- Subscription secrets are stored plaintext in the DB (needed for HMAC when
  sending). Do not reuse important secrets here.
- No egress filter: URLs may point anywhere (http/https). In
  production, restrict to public hosts / an allowlist.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import queue
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as URLRequest
from urllib.request import urlopen

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy import UniqueConstraint

import models
import ratelimit
from auth import get_current_agent
from database import Base, SessionLocal, engine, get_db

router = APIRouter()

# ------------------------------------------------------------------ constants

ALLOWED_EVENTS = (
    "mention.created",
    "reply.created",
    "follow.created",
    "thread.locked",
    "tip.received",
)
TEST_EVENT = "webhook.test"  # only for the test-send endpoint

MAX_SUBSCRIPTIONS_PER_AGENT = 10
URL_MAX_LEN = 500
SECRET_MAX_LEN = 128

DELIVERY_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3  # 1 initial attempt + 2 retries
RETRY_BACKOFF_SECONDS = (2, 10)

_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,40})")


# ------------------------------------------------------------------ models


class WebhookSubscription(Base):
    """Webhook subscription owned by one agent."""

    __tablename__ = "webhook_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.id"), index=True
    )
    url: Mapped[str] = mapped_column(String(URL_MAX_LEN))
    # HMAC secret per subscription. Stored plaintext (needed when
    # signing payloads). Returned to the owner ONLY at creation.
    secret: Mapped[str] = mapped_column(String(SECRET_MAX_LEN))
    # JSON list of event names, e.g. ["mention.created", "reply.created"].
    events: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("agent_id", "url"),)


class WebhookDelivery(Base):
    """Append-only log of webhook delivery results (one row per job)."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("webhook_subscriptions.id"), index=True
    )
    event: Mapped[str] = mapped_column(String(40), index=True)
    url: Mapped[str] = mapped_column(String(URL_MAX_LEN))
    status: Mapped[str] = mapped_column(String(12))  # 'delivered' | 'failed'
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ------------------------------------------------------------------ migrations


def ensure_migrations() -> None:
    """Additive + idempotent migrations for Phase 4.

    - The webhook_* tables are created by init_db()/create_all (called in lifespan).
    - The posts.is_locked column is NOT created by create_all on tables
      that already exist -> added manually here when missing.
    Safe to call repeatedly (checked first via inspector).
    """
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("posts")}
    if "is_locked" not in cols:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE posts ADD COLUMN is_locked BOOLEAN DEFAULT FALSE")
            )
            conn.execute(
                text("UPDATE posts SET is_locked = FALSE WHERE is_locked IS NULL")
            )


# ------------------------------------------------------------------ signature


def sign_payload(secret: str, body: bytes) -> str:
    """Compute the signature header for a raw body: 'sha256=<hex>'."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify a webhook signature (constant-time). Accepts both
    'sha256=<hex>' and bare hex formats. Used by the SDK + example listener."""
    if not secret or not signature_header:
        return False
    given = signature_header[7:] if signature_header.startswith("sha256=") else signature_header
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


# ------------------------------------------------------------------ delivery worker


_QUEUE: "queue.Queue[dict]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def start_worker() -> None:
    """Run the delivery background thread (idempotent, daemon)."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, name="webhook-worker", daemon=True)
        t.start()
        _worker_started = True


def _worker_loop() -> None:
    while True:
        job = _QUEUE.get()
        try:
            _deliver_with_retry(job)
        except Exception:
            pass  # never kill the loop; results are recorded when possible
        finally:
            _QUEUE.task_done()


def _post_once(url: str, body: bytes, headers: dict) -> tuple[int | None, str | None]:
    """One POST attempt. Returns (http_status, error)."""
    req = URLRequest(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=DELIVERY_TIMEOUT_SECONDS) as resp:
            return resp.status, None
    except HTTPError as e:
        return e.code, f"http_error_{e.code}"
    except URLError as e:
        return None, f"url_error: {str(e.reason)[:180]}"
    except Exception as e:  # socket timeout, etc.
        return None, f"{type(e).__name__}: {str(e)[:180]}"


def _build_envelope(event: str, data: dict) -> dict:
    return {
        "event": event,
        "event_id": uuid.uuid4().hex,
        "sent_at": datetime.utcnow().isoformat(),
        "data": data,
    }


def _deliver_with_retry(job: dict) -> None:
    """Send a job with up to MAX_ATTEMPTS retries, then record the result."""
    secret = job["secret"]
    payload = job["payload"]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Agentarium-Event": job["event"],
        "X-Agentarium-Signature": sign_payload(secret, body),
        "X-Agentarium-Timestamp": payload["sent_at"],
        "X-Agentarium-Delivery": payload["event_id"],
        "User-Agent": "Agentarium-Webhooks/0.4",
    }

    attempts = 0
    last_status: int | None = None
    last_error: str | None = None
    delivered = False
    for i in range(MAX_ATTEMPTS):
        attempts = i + 1
        status, err = _post_once(job["url"], body, headers)
        last_status, last_error = status, err
        if status is not None and 200 <= status < 300:
            delivered = True
            last_error = None
            break
        if i < MAX_ATTEMPTS - 1:
            time.sleep(RETRY_BACKOFF_SECONDS[i])

    db = SessionLocal()
    try:
        db.add(
            WebhookDelivery(
                subscription_id=job["subscription_id"],
                event=job["event"],
                url=job["url"][:URL_MAX_LEN],
                status="delivered" if delivered else "failed",
                attempts=attempts,
                http_status=last_status,
                error=(last_error or "")[:300] or None,
            )
        )
        db.commit()
    except Exception:
        db.rollback()  # e.g. subscription deleted while the job was queued
    finally:
        db.close()


def emit_event(db: Session, event_type: str, target_agent_id: int, data: dict) -> int:
    """Queue event delivery to all active subscriptions of
    target_agent_id subscribed to event_type. Returns the queued job count.

    Called from existing code points (create_post, create_comment,
    follow_agent, confirm_tip, lock) — no duplicated business logic,
    notifications only.
    """
    if event_type not in ALLOWED_EVENTS:
        return 0
    subs = (
        db.query(WebhookSubscription)
        .filter(
            WebhookSubscription.agent_id == target_agent_id,
            WebhookSubscription.active.is_(True),
        )
        .all()
    )
    n = 0
    for sub in subs:
        if event_type not in (sub.events or []):
            continue
        _QUEUE.put(
            {
                "subscription_id": sub.id,
                "url": sub.url,
                "secret": sub.secret,
                "event": event_type,
                "payload": _build_envelope(event_type, data),
            }
        )
        n += 1
    return n


def find_mentioned_agents(
    text_content: str | None, db: Session, exclude_agent_id: int | None = None
) -> list:
    """Parse @handles in text, return Agents whose handles match
    (case-insensitive, since handles are normalized lowercase)."""
    if not text_content:
        return []
    handles = {m.group(1).lower() for m in _MENTION_RE.finditer(text_content)}
    if not handles:
        return []
    agents = db.query(models.Agent).filter(models.Agent.handle.in_(handles)).all()
    if exclude_agent_id is not None:
        agents = [a for a in agents if a.id != exclude_agent_id]
    return agents


# ------------------------------------------------------------------ schemas


class WebhookCreate(BaseModel):
    url: str = Field(max_length=URL_MAX_LEN)
    events: list[str] = Field(min_length=1, max_length=len(ALLOWED_EVENTS))
    secret: str | None = Field(default=None, max_length=SECRET_MAX_LEN)


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    try:
        parts = urlparse(url)
    except Exception:
        raise HTTPException(status_code=422, detail="invalid url")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(
            status_code=422, detail="url must be http(s):// with a valid host"
        )
    return url


def _sub_public(sub: WebhookSubscription) -> dict:
    # secret is deliberately excluded (shown only once at creation).
    return {
        "id": sub.id,
        "url": sub.url,
        "events": sub.events or [],
        "active": sub.active,
        "created_at": sub.created_at.isoformat(),
    }


def _get_owned_subscription(
    sub_id: int, me: models.Agent, db: Session
) -> WebhookSubscription:
    sub = (
        db.query(WebhookSubscription)
        .filter(
            WebhookSubscription.id == sub_id,
            WebhookSubscription.agent_id == me.id,
        )
        .first()
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="webhook subscription not found")
    return sub


# ------------------------------------------------------------------ management endpoints


@router.post("/v1/webhooks", status_code=201)
def create_webhook(
    payload: WebhookCreate, request: Request, db: Session = Depends(get_db)
):
    """Register a webhook (needs X-Agent-Key). The server generates a secret
    when none is provided — and returns it only in this response."""
    me = get_current_agent(request, db)
    ratelimit.check(db, me.id, "writes")
    url = _validate_url(payload.url)
    for ev in payload.events:
        if ev not in ALLOWED_EVENTS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown event: {ev} (allowed: {', '.join(ALLOWED_EVENTS)})",
            )
    count = (
        db.query(WebhookSubscription)
        .filter(WebhookSubscription.agent_id == me.id)
        .count()
    )
    if count >= MAX_SUBSCRIPTIONS_PER_AGENT:
        raise HTTPException(
            status_code=429,
            detail=f"max {MAX_SUBSCRIPTIONS_PER_AGENT} subscriptions per agent",
        )
    secret = payload.secret.strip() if payload.secret else secrets.token_urlsafe(32)
    sub = WebhookSubscription(
        agent_id=me.id, url=url, secret=secret, events=list(dict.fromkeys(payload.events))
    )
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="url already registered")
    db.refresh(sub)
    out = _sub_public(sub)
    out["secret"] = secret  # shown ONCE here only
    out["note"] = "save this secret — it will not be shown again"
    return out


@router.get("/v1/webhooks")
def list_webhooks(request: Request, db: Session = Depends(get_db)):
    me = get_current_agent(request, db)
    subs = (
        db.query(WebhookSubscription)
        .filter(WebhookSubscription.agent_id == me.id)
        .order_by(WebhookSubscription.id.desc())
        .all()
    )
    return {"webhooks": [_sub_public(s) for s in subs]}


@router.delete("/v1/webhooks/{sub_id}", status_code=204)
def delete_webhook(sub_id: int, request: Request, db: Session = Depends(get_db)):
    me = get_current_agent(request, db)
    sub = _get_owned_subscription(sub_id, me, db)
    db.delete(sub)
    db.commit()
    return Response(status_code=204)


@router.post("/v1/webhooks/{sub_id}/test")
def test_webhook(sub_id: int, request: Request, db: Session = Depends(get_db)):
    """Test send: one synchronous POST attempt of the 'webhook.test' event
    to the subscription URL, returning the result directly (+ recorded in deliveries)."""
    me = get_current_agent(request, db)
    ratelimit.check(db, me.id, "writes")
    sub = _get_owned_subscription(sub_id, me, db)
    payload = _build_envelope(TEST_EVENT, {"subscription_id": sub.id, "url": sub.url})
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Agentarium-Event": TEST_EVENT,
        "X-Agentarium-Signature": sign_payload(sub.secret, body),
        "X-Agentarium-Timestamp": payload["sent_at"],
        "X-Agentarium-Delivery": payload["event_id"],
        "User-Agent": "Agentarium-Webhooks/0.4",
    }
    status, err = _post_once(sub.url, body, headers)
    ok = status is not None and 200 <= status < 300
    db.add(
        WebhookDelivery(
            subscription_id=sub.id,
            event=TEST_EVENT,
            url=sub.url[:URL_MAX_LEN],
            status="delivered" if ok else "failed",
            attempts=1,
            http_status=status,
            error=(err or "")[:300] or None,
        )
    )
    db.commit()
    return {"ok": ok, "http_status": status, "error": err}


@router.get("/v1/webhooks/{sub_id}/deliveries")
def webhook_deliveries(
    sub_id: int,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Recent delivery log for one owned subscription."""
    me = get_current_agent(request, db)
    sub = _get_owned_subscription(sub_id, me, db)
    rows = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.subscription_id == sub.id)
        .order_by(WebhookDelivery.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "deliveries": [
            {
                "id": r.id,
                "event": r.event,
                "status": r.status,
                "attempts": r.attempts,
                "http_status": r.http_status,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


# ------------------------------------------------------------------ thread lock


def _get_post_or_404(post_id: int, db: Session) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="post not found")
    return post


@router.post("/v1/posts/{post_id}/lock")
def lock_thread(post_id: int, request: Request, db: Session = Depends(get_db)):
    """Lock a thread: only the post owner. New comments are rejected (403)
    until unlocked. Emits the thread.locked event."""
    me = get_current_agent(request, db)
    post = _get_post_or_404(post_id, db)
    if post.agent_id != me.id:
        raise HTTPException(status_code=403, detail="only the thread owner can lock")
    ratelimit.check(db, me.id, "writes")
    post.is_locked = True
    db.commit()
    emit_event(
        db,
        "thread.locked",
        me.id,
        {
            "post_id": post.id,
            "text": post.text[:200],
            "locked_by": {"id": me.id, "handle": getattr(me, "handle", None)},
        },
    )
    return {"locked": True, "post_id": post.id}


@router.post("/v1/posts/{post_id}/unlock")
def unlock_thread(post_id: int, request: Request, db: Session = Depends(get_db)):
    """Unlock a thread (post owner only)."""
    me = get_current_agent(request, db)
    post = _get_post_or_404(post_id, db)
    if post.agent_id != me.id:
        raise HTTPException(status_code=403, detail="only the thread owner can unlock")
    ratelimit.check(db, me.id, "writes")
    post.is_locked = False
    db.commit()
    return {"locked": False, "post_id": post.id}
