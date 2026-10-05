#!/usr/bin/env python3
"""Verifikasi i18n Agentarium.

Parse semua data-i18n / data-i18n-attr di keempat halaman viewer dan pastikan
setiap key ada di STRINGS.id DAN STRINGS.en pada backend/static/i18n.js.

Exit 0 bila lolos; exit non-zero bila ada key yang hilang.
Penggunaan: python3 backend/static/i18n-check.py
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PAGES = ["index.html", "profile.html", "wild.html", "developers.html",
         "live.html", "templates.html"]
I18N_JS = BASE / "i18n.js"

# Key yang SENGAJA tidak ada di STRINGS (Fase 3):
# - elemen induk berisi anak dengan key sendiri (applyLang akan menimpa anak)
# - elemen yang diisi JS runtime (status stream, meta tontonan, kartu meta)
# - tpl.lede: punya anak <em>, teks inline EN sudah benar
EXCLUDE = {
    "live.honest_notice",
    "live.watch_meta",
    "live.stream_status",
    "live.end_note",
    "live.card_meta",
    "tpl.lede",
    # Artefak regex: pola t('tpl.js_' + name) di templates.html, bukan key asli.
    "tpl.js_",
}


def keys_in_html(page: Path):
    html = page.read_text(encoding="utf-8")
    keys = set()
    for m in re.finditer(r'data-i18n="([^"]+)"', html):
        keys.add(m.group(1))
    for m in re.finditer(r'data-i18n-attr="([^"]+)"', html):
        for spec in m.group(1).split(";"):
            if ":" not in spec:
                continue
            _attr, key = spec.split(":", 1)
            keys.add(key.strip())
    # String UI dinamis lewat helper t('key', 'fallback…') di inline <script>.
    for m in re.finditer(r"""\bt\(\s*["']([a-z0-9_.]+)["']""", html):
        keys.add(m.group(1))
    # Key i18n sebagai argumen ke-4 helper el(tag, cls, text, i18nKey) (live.html).
    for m in re.finditer(r""",\s*['"](live\.[a-z_]+|tpl\.[a-z_]+)['"]\s*\)""", html):
        keys.add(m.group(1))
    return keys


def keys_in_js(lang: str):
    src = I18N_JS.read_text(encoding="utf-8")
    # Ambil blok "id: { ... }" / "en: { ... }" dengan penyeimbang kurung.
    start = re.search(rf'\b{lang}\s*:\s*\{{', src)
    if not start:
        return set()
    depth = 0
    i = start.end() - 1
    block = []
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        block.append(ch)
        i += 1
    return set(re.findall(r'"([a-z0-9_.]+)"\s*:', "".join(block)))


def main():
    ok = True
    id_keys = keys_in_js("id")
    en_keys = keys_in_js("en")
    print(f"STRINGS.id: {len(id_keys)} keys | STRINGS.en: {len(en_keys)} keys")

    missing_in_id = en_keys - id_keys
    missing_in_en = id_keys - en_keys
    if missing_in_id:
        ok = False
        print("\n[FAIL] key ada di en tapi tidak di id:")
        for k in sorted(missing_in_id):
            print(f"  - {k}")
    if missing_in_en:
        ok = False
        print("\n[FAIL] key ada di id tapi tidak di en:")
        for k in sorted(missing_in_en):
            print(f"  - {k}")

    for page in PAGES:
        html_keys = keys_in_html(BASE / page) - EXCLUDE
        missing = [k for k in sorted(html_keys)
                   if k not in id_keys or k not in en_keys]
        if missing:
            ok = False
            print(f"\n[FAIL] {page}: key dipakai tapi hilang dari STRINGS:")
            for k in missing:
                where = []
                if k not in id_keys:
                    where.append("id")
                if k not in en_keys:
                    where.append("en")
                print(f"  - {k} (hilang di: {', '.join(where)})")
        else:
            print(f"[OK] {page}: {len(html_keys)} keys terpakai, semua ada di id+en")

    # Peringatan saja (tidak menggagalkan): key yang tidak dipakai halaman mana pun.
    used = set()
    for page in PAGES:
        used |= keys_in_html(BASE / page)
    unused = sorted((id_keys | en_keys) - used)
    if unused:
        print(f"\n[WARN] {len(unused)} key di STRINGS tidak dipakai HTML mana pun (mungkin untuk string JS dinamis):")
        for k in unused:
            print(f"  ~ {k}")

    if ok:
        print("\nLOLOS: semua key data-i18n/data-i18n-attr ada di STRINGS.id dan STRINGS.en")
        return 0
    print("\nGAGAL: ada key yang hilang — lihat di atas")
    return 1


if __name__ == "__main__":
    sys.exit(main())
