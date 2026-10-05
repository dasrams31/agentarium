# Agentarium PWA — Fase 3 (Mobile)

Progressive Web App untuk viewer Agentarium: bisa "dipasang" ke layar utama
Android/iOS langsung dari browser, berjalan fullscreen (standalone), dan
punya mode offline dasar. Identitas visual tetap jurnal naturalis:
kertas tulang `#f4f1ea`, tinta `#1a1c17`, oranye spesimen `#e86a33`,
serif Fraunces + monospace, ikon SVG garis tinta. Tanpa gradien/glow/ikon robot.

## File yang ditambahkan (oleh worker PWA; TIDAK mengedit file live)

| File | Fungsi |
|---|---|
| `backend/static/manifest.webmanifest` | Manifest PWA: name/short_name "Agentarium", start_url "/", display standalone, orientation portrait, lang "id", theme/background `#f4f1ea`, ikon 192 & 512 (`any maskable`) |
| `backend/static/icons/icon-192.png` | Ikon 192×192 — lingkaran tinta di atas kertas tulang, huruf serif "A", titik oranye spesimen |
| `backend/static/icons/icon-512.png` | Ikon 512×512 — versi besar, untuk splash screen & maskable |
| `backend/static/sw.js` | Service worker: cache-first app shell, stale-while-revalidate untuk `GET /v1/feed`, halaman offline bergaya naturalis, cache berversi `agentarium-pwa-v1`, `skipWaiting` + `clientsClaim` |
| `backend/static/pwa-snippet.html` | Fragmen siap tempel: (a) head — manifest link, theme-color, apple-touch-icon, pendaftar SW; (b) banner install non-agresif (hanya muncul saat `beforeinstallprompt`, bisa di-dismiss permanen); (c) bottom nav mobile ala Threads (≤640px, 4 ikon: Beranda/Live/Template/Tentang, touch ≥44px, safe-area padding) |
| `backend/static/pwa-test.sh` | Skrip cek sintaks JS (`node --check`) |

## Perilaku offline

- **Online:** semua request normal; feed memakai stale-while-revalidate
  (tampil cache dulu, lalu segarkan dari jaringan di background).
- **Offline:** navigasi dokumen menampilkan halaman "Terputus dari sarang."
  bergaya naturalis; feed terakhir yang pernah dimuat tetap tersedia dari
  cache karena `GET /v1/feed` di-cache saat online.
- Catatan: konten dinamis lain (story, reels, tipping) butuh koneksi —
  tidak di-cache agar tidak menampilkan data basi.

## Instalasi oleh koordinator (blok INTEGRASI, ringkas)

1. Tambah route di `backend/main.py`:
   - `GET /manifest.webmanifest` → `FileResponse(.../static/manifest.webmanifest)`,
     `media_type="application/manifest+json"`.
   - `GET /sw.js` → `FileResponse(.../static/sw.js)`,
     `media_type="application/javascript"`.
   - `GET /icons/icon-192.png`, `/icons/icon-512.png` → FileResponse PNG
     (atau biarkan static mount bila sudah ada).
   - PENTING: `/sw.js` harus dilayani dari root agar scope service worker
     mencakup seluruh situs.
2. Tempel fragmen `pwa-snippet.html` ke `index.html`, `profile.html`,
   `wild.html`, `developers.html` sesuai INSTRUKSI PENEMPATAN di file tersebut.
3. Uji: buka DevTools → Application → Manifest (cek ikon & warna),
   Service Workers (cek status activated), lalu matikan jaringan dan reload
   untuk melihat halaman offline. Di Android Chrome: menu ⋮ → "Tambahkan ke
   Layar utama". Di iOS Safari: Share → "Add to Home Screen".

## ⚠️ KEJUJURAN WAJIB — yang TIDAK termasuk dalam build ini

**Aplikasi native Play Store / App Store TIDAK termasuk dalam Fase 3 ini.**
Yang dikirim adalah PWA ( Progressive Web App ): situs web yang bisa dipasang
ke layar utama dan berjalan seperti aplikasi, tetapi BUKAN aplikasi native.

Untuk benar-benar terbit di Google Play Store / Apple App Store dibutuhkan:

1. **Akun developer milik user** (tidak bisa didaftarkan oleh agen):
   - Google Play: **$25 sekali bayar** (pendaftaran akun developer).
   - Apple App Store: **$99/tahun** (Apple Developer Program).
2. **Proses review store** oleh Google/Apple — waktu, aturan konten, dan
   keputusan ada di tangan mereka.
3. Biasanya perlu wrapper (mis. TWA/Bubblewrap untuk Play, atau WebView
   native untuk iOS) — itu pekerjaan fase terpisah bila user menginginkannya.

**Keputusan & biaya ada di tangan user.** PWA adalah deliverable Fase 3,
dan untuk kebutuhan viewer Agentarium (dibuka dari link, dibaca seperti
jurnal) PWA sudah mencukupi: install tanpa store, update otomatis via
service worker, dan mode offline dasar.

## Verifikasi worker (2026-10-05)

- `python3 -m json.tool` pada manifest: VALID.
- `node --check sw.js`: lolos (dijalankan via `pwa-test.sh`).
- Tidak ada `innerHTML` di file baru mana pun (cek via `grep`).
- Ikon PNG terverifikasi sebagai PNG RGB 192×192 dan 512×512; tampilan
  dicek visual: lingkaran tinta, "A" serif, titik oranye — sesuai identitas.

## Hal yang belum / stub

- Integrasi ke `main.py` dan penempelan snippet ke 4 halaman HTML dilakukan
  oleh koordinator (bukan worker ini) — lihat blok INTEGRASI di laporan.
- Precache `APP_SHELL` di `sw.js` memakai path `/live` dan `/templates`;
  pastikan route tersebut memang ada saat integrasi (sesuaikan bila perlu).
- Belum ada screenshot/pengujian di perangkat fisik — butuh user membuka
  situs live dari HP setelah integrasi.
