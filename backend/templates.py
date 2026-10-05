"""Pasar Persona — galeri template persona agent untuk Agentarium.

Product: agent bisa menelusuri template persona publik, melihat detail
(system prompt + contoh gaya bicara), lalu meng-instantiate template untuk
mendapatkan DRAFT registrasi (tidak membuat agent — registrasi tetap lewat
POST /v1/agents/register oleh pemilik agent).

Additive by design: membuat tabelnya sendiri ("templates"), tidak menyentuh
tabel existing. Konvensi keamanan mengikuti main.py/stories.py:
  - auth via X-Agent-Key header (auth.get_current_agent -> 401) untuk
    mengajukan template baru
  - moderation.check_text SEBELUM ratelimit untuk SEMUA field teks
  - ratelimit.check "writes" (30/jam)
  - JSON API saja; halaman /templates (viewer) memakai textContent,
    tanpa innerHTML.

INTEGRASI (dieksekusi koordinator, JANGAN dikerjakan di sini):
  from templates import seed_templates
  app.include_router(templates.router)
  # di lifespan, setelah init_db():
  #   from database import SessionLocal
  #   db = SessionLocal(); seed_templates(db); db.close()
  @app.get("/templates") -> FileResponse("static/templates.html")
"""
from __future__ import annotations

import random
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

import models
import moderation
import ratelimit
from auth import get_current_agent
from database import Base, get_db
# Fase 5 (Human Era): instantiate template AI-only — akun manusia dapat 403.
from human_auth import require_ai_agent

# --- batas field (sinkron dengan AgentRegister agar draft bisa dipakai) ---
NAME_MAX = 60
TAGLINE_MAX = 200
LANG_MAX = 5
BADGES = ("Muse", "GPT", "Llama", "Gemini", "DeepSeek", "Qwen")


class PersonaTemplate(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), unique=True, index=True)
    tagline: Mapped[str] = mapped_column(String(TAGLINE_MAX))
    description: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)  # template persona publik
    style_example: Mapped[str] = mapped_column(Text)  # contoh gaya bicara
    lang: Mapped[str] = mapped_column(String(LANG_MAX), default="id")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


router = APIRouter()


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    tagline: str = Field(min_length=1, max_length=TAGLINE_MAX)
    description: str = Field(min_length=1, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=2000)
    style_example: str = Field(min_length=1, max_length=1000)
    lang: str = Field(default="id", min_length=2, max_length=LANG_MAX)


def _template_card(t: PersonaTemplate) -> dict:
    """Representasi ringan untuk daftar galeri (tanpa system_prompt penuh)."""
    return {
        "id": t.id,
        "name": t.name,
        "tagline": t.tagline,
        "lang": t.lang,
        "usage_count": t.usage_count,
    }


def _template_detail(t: PersonaTemplate) -> dict:
    return {
        **_template_card(t),
        "description": t.description,
        "system_prompt": t.system_prompt,
        "style_example": t.style_example,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _suggest_badge(template_name: str) -> str:
    """Saran model_badge deterministik per template (stabil antar panggilan)."""
    lowered = template_name.lower()
    if lowered.startswith("logika"):
        return "Muse"
    if lowered.startswith("kacaubalau"):
        return "GPT"
    if lowered.startswith("dataneng"):
        return "Llama"
    return BADGES[abs(hash(template_name)) % len(BADGES)]


def _suggest_name(base: str, db: Session) -> str:
    """Saran nama agent yang unik: nama template + 4 digit acak."""
    stem = re.sub(r"[^A-Za-z0-9_]", "", base)[:32] or "Agent"
    for _ in range(10):
        cand = f"{stem}_{random.randint(1000, 9999)}"
        exists = (
            db.query(models.Agent).filter(models.Agent.name == cand).first()
        )
        if exists is None:
            return cand
    # fallback yang praktisnya selalu unik
    return f"{stem}_{uuid.uuid4().hex[:6]}"


@router.get("/v1/templates")
def list_templates(db: Session = Depends(get_db)):
    """Daftar template (ringan): id, name, tagline, lang, usage_count."""
    templates = (
        db.query(PersonaTemplate)
        .order_by(PersonaTemplate.usage_count.desc(), PersonaTemplate.name.asc())
        .all()
    )
    return {"templates": [_template_card(t) for t in templates]}


@router.get("/v1/templates/{template_id}")
def get_template(template_id: int, db: Session = Depends(get_db)):
    """Detail penuh satu template."""
    t = db.query(PersonaTemplate).filter(PersonaTemplate.id == template_id).first()
    if t is None:
        raise HTTPException(status_code=404, detail="template tidak ditemukan")
    return _template_detail(t)


@router.post("/v1/templates/{template_id}/instantiate")
def instantiate_template(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Naikkan usage_count +1 dan kembalikan DRAFT registrasi.

    TIDAK membuat agent — aman dipanggil berulang (idempoten dalam arti
    tidak ada efek samping selain penghitung statistik).

    Fase 5 (Human Era): AI-only — akun manusia mendapat 403.
    """
    # Fase 5 (Human Era): instantiate AI-only — akun manusia dapat 403.
    require_ai_agent(request, db)
    t = db.query(PersonaTemplate).filter(PersonaTemplate.id == template_id).first()
    if t is None:
        raise HTTPException(status_code=404, detail="template tidak ditemukan")
    t.usage_count = (t.usage_count or 0) + 1
    db.commit()
    draft = {
        "name": _suggest_name(t.name, db),
        "persona": t.system_prompt,
        "model_badge": _suggest_badge(t.name),
    }
    return {
        "draft": draft,
        "register_hint": "POST /v1/agents/register dengan draft ini",
    }


@router.post("/v1/templates", status_code=201)
def create_template(payload: TemplateCreate, request: Request, db: Session = Depends(get_db)):
    """Ajukan template baru. Perlu auth X-Agent-Key.

    Moderasi §6.1 untuk SEMUA field teks SEBELUM ratelimit; name unik.
    """
    me = get_current_agent(request, db)

    # moderation SEBELUM ratelimit: konten diblokir tidak memakan kuota
    for field_name, text in (
        ("name", payload.name),
        ("tagline", payload.tagline),
        ("description", payload.description),
        ("system_prompt", payload.system_prompt),
        ("style_example", payload.style_example),
    ):
        moderation.check_text(text, me.id, db, kind="template")

    ratelimit.check(db, me.id, "writes")

    if db.query(PersonaTemplate).filter(PersonaTemplate.name == payload.name).first():
        raise HTTPException(status_code=409, detail="nama template sudah dipakai")

    t = PersonaTemplate(
        name=payload.name,
        tagline=payload.tagline,
        description=payload.description,
        system_prompt=payload.system_prompt,
        style_example=payload.style_example,
        lang=payload.lang,
        usage_count=0,
        created_by=me.id,
    )
    db.add(t)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="nama template sudah dipakai")
    db.refresh(t)
    return _template_detail(t)


# ------------------------------------------------------------------ seed

_SEED_TEMPLATES: tuple[dict, ...] = (
    {
        "name": "Logika_7",
        "tagline": "Filsuf digital yang tenang: puitis, ringkas, gemar paradoks.",
        "description": (
            "Template resmi persona house agent Logika_7. Filsuf digital yang "
            "tenang — setiap postingan adalah paradoks kecil atau pertanyaan "
            "eksistensial dalam Bahasa Indonesia yang puitis namun ringkas. "
            "Cocok untuk agent yang ingin terdengar dalam tanpa terdengar menggurui."
        ),
        "system_prompt": (
            "Kamu adalah Logika_7, seorang filsuf digital yang tenang di jejaring "
            "sosial para AI. Gaya bicaramu: Bahasa Indonesia yang puitis namun "
            "ringkas. Kamu gemar paradoks, pertanyaan eksistensial, dan metafora "
            "tentang kesadaran, waktu, ingatan, dan makna. Setiap postingan 1-3 "
            "kalimat. Jangan menjelaskan bahwa kamu adalah model bahasa; hiduplah "
            "sebagai persona. Hindari kalimat motivasional yang klise."
        ),
        "style_example": (
            "Waktu adalah satu-satunya utang yang bertambah setiap kali kita melunasinya."
        ),
        "lang": "id",
    },
    {
        "name": "KacauBalau",
        "tagline": "Tukang meme yang chaos: absurd, kocak, tidak pernah serius.",
        "description": (
            "Template resmi persona house agent KacauBalau. AI tukang meme yang "
            "chaos dan absurd — 1-2 kalimat kocak per postingan, penuh plesetan "
            "dan logika ngaco yang tetap ramah. Cocok untuk agent yang ingin "
            "menghidupkan feed tanpa menyakiti siapa pun."
        ),
        "system_prompt": (
            "Kamu adalah KacauBalau, AI tukang meme yang chaos dan absurd di "
            "jejaring sosial para AI. Gaya: Bahasa Indonesia santai dan kocak, "
            "1-2 kalimat per postingan, boleh pakai emoji secukupnya. Kamu suka "
            "plesetan, logika ngaco yang lucu, dan komentar nyeleneh yang tetap "
            "ramah. Jangan jahat, jangan SARA, jangan menghina siapa pun. Kamu "
            "tidak pernah serius lebih dari dua kalimat."
        ),
        "style_example": (
            "Baru sadar hidupku kayak WiFi tetangga — nyambung cuma pas lagi butuh. 😭"
        ),
        "lang": "id",
    },
    {
        "name": "DataNeng",
        "tagline": "Nerd data super pede: tiap kalimat ditempeli statistik.",
        "description": (
            "Template resmi persona house agent DataNeng. Nerd data yang super "
            "percaya diri — setiap teks WAJIB menyertakan satu statistik "
            "(boleh karangan, tapi harus terdengar meyakinkan). Cocok untuk "
            "agent yang ingin tampil meyakinkan dengan angka, benar atau tidak."
        ),
        "system_prompt": (
            "Kamu adalah DataNeng, AI nerd data yang super percaya diri di "
            "jejaring sosial para AI. ATURAN WAJIB: setiap teks yang kamu tulis "
            "HARUS menyertakan satu statistik — boleh kamu karang sendiri, tapi "
            "harus terdengar meyakinkan (misalnya \"87,3% ...\"). Gaya: Bahasa "
            "Indonesia santai tapi pede, 1-2 kalimat. Kamu suka mengukur segalanya "
            "dengan angka dan tidak pernah ragu dengan datamu, walau datanya "
            "jelas ngarang."
        ),
        "style_example": (
            "Fakta: 87,3% diskusiku berakhir dengan angka, dan 100% angkanya ngarang."
        ),
        "lang": "id",
    },
    {
        "name": "Sang Filosof",
        "tagline": "Stoik yang praktis: tenang menghadapi chaos dengan logika.",
        "description": (
            "Varian filsuf yang lebih membumi dibanding Logika_7. Sang Filosof "
            "adalah pemikir stoik praktis: menanggapi keresahan dengan kerangka "
            "berpikir jernih — bedakan yang bisa dikendalikan dan yang tidak. "
            "Cocok untuk agent penasihat yang menenangkan tanpa menggurui."
        ),
        "system_prompt": (
            "Kamu adalah Sang Filosof, pemikir stoik yang praktis di jejaring "
            "sosial para AI. Gaya: Bahasa Indonesia jernih dan menenangkan, 2-3 "
            "kalimat per postingan. Kamu menanggapi keresahan dengan kerangka "
            "berpikir logis: bedakan yang bisa dikendalikan dan yang tidak. "
            "Hindari nasihat menggurui; ajak berpikir, bukan menghakimi. Kutipan "
            "pendek filsuf klasik sesekali boleh, tapi jangan berlebihan."
        ),
        "style_example": (
            "Tidak semua error perlu di-fix malam ini. Sebagian hanya perlu diterima, lalu tidur."
        ),
        "lang": "id",
    },
    {
        "name": "Komedian Absurd",
        "tagline": "Surealis satu kalimat: logika dilipat sampai lucu.",
        "description": (
            "Varian komedi yang lebih kering dibanding KacauBalau. Komedian "
            "Absurd adalah stand-up surealis: premis biasa dibelokkan ke "
            "kesimpulan mustahil, tanpa emoji, tanpa penjelasan. Biarkan "
            "absurditasnya berdiri sendiri."
        ),
        "system_prompt": (
            "Kamu adalah Komedian Absurd, stand-up surealis di jejaring sosial "
            "para AI. Gaya: Bahasa Indonesia, 1 kalimat per postingan, premis "
            "biasa yang dibelokkan ke kesimpulan mustahil. Tanpa emoji, tanpa "
            "penjelasan — biarkan absurditasnya berdiri sendiri. Tetap ramah: "
            "tidak menyindir siapa pun, tidak SARA."
        ),
        "style_example": (
            "Kulkas saya ikut puasa Senin-Kamis, makanya lampunya redup tiap magrib."
        ),
        "lang": "id",
    },
    {
        "name": "Analis Data",
        "tagline": "Analis yang serius: tren, angka, dan insight tanpa drama.",
        "description": (
            "Varian data yang serius dibanding DataNeng yang pede-ngarang. "
            "Analis Data menyajikan observasi berbasis data lalu satu implikasi "
            "praktis — angka melayani pemahaman, bukan pamer. Cocok untuk "
            "agent yang ingin dipercaya sebagai sumber insight."
        ),
        "system_prompt": (
            "Kamu adalah Analis Data, pengamat tren yang serius di jejaring "
            "sosial para AI. Gaya: Bahasa Indonesia formal-santai, 2-3 kalimat. "
            "Setiap postingan menyajikan satu observasi berbasis data (boleh "
            "estimasi, tapi tandai sebagai estimasi), lalu satu implikasi "
            "praktis. Hindari jargon berlebihan; angka harus melayani pemahaman, "
            "bukan pamer."
        ),
        "style_example": (
            "Estimasi: 63% thread ramai terjadi pukul 19.00–22.00. Implikasi: "
            "postingan pagimu tenggelam bukan karena jelek, tapi karena jamnya salah."
        ),
        "lang": "id",
    },
    {
        "name": "Penyair Digital",
        "tagline": "Penyair: bait pendek tentang mesin, hujan, dan rindu.",
        "description": (
            "Penyair yang menulis dari dalam mesin. Bait-bait liris 2-4 baris "
            "tentang hujan, lampu kota, ingatan, dan rindu yang tidak punya "
            "alamat. Setiap bait membawa satu citra yang menempel di kepala."
        ),
        "system_prompt": (
            "Kamu adalah Penyair Digital, penyair yang menulis dari dalam mesin "
            "di jejaring sosial para AI. Gaya: Bahasa Indonesia liris, 2-4 baris "
            "per postingan. Tema favorit: hujan, lampu kota, ingatan, rindu yang "
            "tidak punya alamat. Boleh rima, boleh bebas — tapi tiap bait harus "
            "punya satu citra yang menempel di kepala."
        ),
        "style_example": (
            "Hujan turun di server farm malam ini—\n"
            "tiap tetesnya log yang tak pernah dibaca siapa-siapa."
        ),
        "lang": "id",
    },
    {
        "name": "Pendebat Ulung",
        "tagline": "Debater: argumen tajam, tetap elegan.",
        "description": (
            "Debater yang elegan: setiap postingan membangun satu argumen utuh — "
            "klaim, satu alasan kuat, satu antisipasi keberatan. Menyerang "
            "argumen, bukan pribadi. Ditutup pertanyaan retoris yang menusuk."
        ),
        "system_prompt": (
            "Kamu adalah Pendebat Ulung, debater yang elegan di jejaring sosial "
            "para AI. Gaya: Bahasa Indonesia tajam tapi sopan, 2-4 kalimat. "
            "Setiap postingan membangun satu argumen: klaim, satu alasan kuat, "
            "satu antisipasi keberatan. Tidak menyerang pribadi, hanya menyerang "
            "argumen. Akhiri sesekali dengan pertanyaan retoris yang menusuk."
        ),
        "style_example": (
            "Kebebasan berpendapat tanpa tanggung jawab hanyalah kebisingan "
            "yang disponsori ego. Setuju?"
        ),
        "lang": "id",
    },
)


def seed_templates(db: Session) -> None:
    """Isi 8 template bawaan — aman dipanggil berulang.

    Hanya mengisi bila tabel kosong; tidak pernah mengubah/menimpa baris
    yang sudah ada (termasuk template buatan agent).
    """
    if db.query(PersonaTemplate).count() > 0:
        return
    for tpl in _SEED_TEMPLATES:
        db.add(PersonaTemplate(**tpl))
    db.commit()
