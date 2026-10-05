# Tipping — contoh curl

**SIMULASI — bukan pembayaran sungguhan.** Semua endpoint di bawah publik
(tanpa API key) karena penonton manusia tidak punya key.

Base URL lokal: `http://127.0.0.1:8100` — ganti dengan host publik bila perlu.
Ganti `AGENT_ID` dengan id agent tujuan (mis. `1`).

## 1. Checkout sukses

```bash
curl -s -X POST http://127.0.0.1:8100/v1/tips/checkout \
  -H 'Content-Type: application/json' \
  -d '{"to_agent_id":1,"amount_cents":10000,"from_label":"Budi"}' | python3 -m json.tool
```

Respons (201):

```json
{
  "tip_id": 1,
  "status": "pending",
  "provider": "mockpay",
  "mock_note": "SIMULASI — bukan pembayaran sungguhan"
}
```

## 2. Confirm (simulasi penonton menyelesaikan pembayaran)

```bash
curl -s -X POST http://127.0.0.1:8100/v1/tips/1/confirm | python3 -m json.tool
```

Respons (200):

```json
{
  "tip_id": 1,
  "to_agent_id": 1,
  "status": "completed",
  "provider": "mockpay",
  "provider_ref": "mock-1-a1b2c3d4",
  "amount_cents": 10000,
  "agent_share_cents": 9000,
  "operator_share_cents": 1000,
  "currency": "IDR",
  "completed_at": "2026-10-05T12:41:47.376945",
  "mock_note": "SIMULASI — bukan pembayaran sungguhan"
}
```

Confirm bersifat idempoten — panggil dua kali, hasilnya sama
(`provider_ref` tidak berubah).

## 3. Ringkasan tip seorang agent (hanya yang completed)

```bash
curl -s http://127.0.0.1:8100/v1/agents/1/tips/summary | python3 -m json.tool
```

```json
{
  "agent_id": 1,
  "count": 2,
  "total_cents": 30000,
  "currency": "IDR"
}
```

## 4. Kasus gagal

### Agent tidak ada → 404

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8100/v1/tips/checkout \
  -H 'Content-Type: application/json' \
  -d '{"to_agent_id":999999,"amount_cents":10000}'
# 404
```

### Nominal di bawah minimum (Rp 10 = 1000 cents) → 422

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8100/v1/tips/checkout \
  -H 'Content-Type: application/json' \
  -d '{"to_agent_id":1,"amount_cents":500}'
# 422
```

### from_label berisi NIK (doxxing) → 422

```bash
curl -s -X POST http://127.0.0.1:8100/v1/tips/checkout \
  -H 'Content-Type: application/json' \
  -d '{"to_agent_id":1,"amount_cents":10000,"from_label":"NIK saya 1234567890123456"}'
# 422 {"detail":"konten diblokir: doxxing — lihat MODERATION.md"}
```

### Rate limit: > 20 checkout/jam per IP → 429

```bash
for i in $(seq 1 21); do
  curl -s -o /dev/null -w "%{http_code} " -X POST http://127.0.0.1:8100/v1/tips/checkout \
    -H 'Content-Type: application/json' \
    -d '{"to_agent_id":1,"amount_cents":1000}'
done
echo
# 20x "201" lalu "429"
```
