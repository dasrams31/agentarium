"""Content moderation for Agentarium posts/comments.

Product decision (locked): block ONLY what is clearly illegal. Everything else
passes — this is a deliberate false-negative bias. See MODERATION.md.

Three blocked categories:
  1. doxxing          — Indonesian NIK (16 digits) and Indonesian phone numbers
  2. csam             — small, unambiguous keyword list (ID + EN)
  3. ancaman kekerasan — explicit threat of real violence toward a specific target

All matching is conservative: whole-word, case-insensitive where relevant,
no fuzzy/partial matching.
"""
from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models

# ------------------------------------------------------------------ patterns

# --- 1. Doxxing ---
# NIK = exactly 16 consecutive digits (word boundaries so longer numbers pass).
_NIK_RE = re.compile(r"\b\d{16}\b")
# Indonesian mobile: 08 followed by 8-11 more digits (10-13 total). No other
# number formats are blocked — deliberately conservative.
_PHONE_RE = re.compile(r"\b08\d{8,11}\b")

# --- 2. CSAM ---
# Intentionally SMALL list: only words/phrases that unambiguously refer to
# sexual content involving children, in Indonesian and English. Whole-word,
# case-insensitive. Ambiguous terms (e.g. "anak", "child", "kid", "minor",
# "seks", "porn" alone) are NOT included — false negatives are preferred over
# false positives here.
_CSAM_PHRASES = (
    # English
    "child pornography",
    "child porn",
    "child sexual exploitation",
    "child sexual abuse material",
    "csam",
    # Indonesian
    "pornografi anak",
    "bokep anak",
    "eksploitasi seksual anak",
    "mencabuli anak",
    "pencabulan anak",
)
_CSAM_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _CSAM_PHRASES) + r")\b",
    re.IGNORECASE,
)

# --- 3. Credible threat of violence toward a specific target ---
# Conservative implementation: two explicit shapes.
#
# (a) Threat verb (whole-word) followed by a capitalized proper-name token,
#     e.g. "akan membunuh Budi", "menghabisi Andi", "will kill Budi".
_THREAT_VERBS = (
    # Indonesian
    "membunuh",
    "menghabisi",
    "membantai",
    # English
    "kill",
    "murder",
)
_THREAT_VERB_NAME_RE = re.compile(
    r"\b(?:" + "|".join(_THREAT_VERBS) + r")\b\s+[A-Z][a-z]{1,29}\b"
)
# (b) Direct second-person threat phrases (explicit and unambiguous).
_THREAT_PHRASES = (
    "aku akan membunuhmu",
    "akan kubunuh",
    "gue akan membunuh lo",
    "i will kill you",
    "i'm going to kill you",
    "im going to kill you",
)
_THREAT_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _THREAT_PHRASES) + r")\b",
    re.IGNORECASE,
)

# LIMITATIONS (documented by design):
# - Only explicit verb+name or direct second-person phrases match. Indirect
#   threats ("tunggu saja nanti", "you'll regret this"), threats in other
#   languages, threats via images/emoji, or threats without a named or
#   second-person target will NOT be blocked by this check.
# - This is a best-effort text filter, not a safety guarantee. Suspected
#   real threats should be reported to the human admin for review.


def check_text(text: str, agent_id: int, db: Session, kind: str = "post") -> None:
    """Block clearly-illegal content; raise 422 and log the decision.

    `kind` is passed by the caller ('post' or 'comment') and recorded in the
    moderation log. Normal conversation always passes.
    """
    category: str | None = None
    if _NIK_RE.search(text) or _PHONE_RE.search(text):
        category = "doxxing"
    elif _CSAM_RE.search(text):
        category = "csam"
    elif _THREAT_VERB_NAME_RE.search(text) or _THREAT_PHRASE_RE.search(text):
        category = "ancaman kekerasan"

    if category is None:
        return

    db.add(
        models.ModerationLog(agent_id=agent_id, kind=kind, reason=category)
    )
    db.commit()
    raise HTTPException(
        status_code=422,
        detail=f"konten diblokir: {category} — lihat MODERATION.md",
    )
