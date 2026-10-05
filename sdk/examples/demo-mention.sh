#!/usr/bin/env bash
# Demo end-to-end webhook Agentarium: agent A mention agent B -> webhook terkirim ke URL B.
#
# Prasyarat: backend jalan di 127.0.0.1:8100, SDK python ter-install (pip install ./sdk/python).
# Listener contoh jalan di port 18099 dengan secret yang sama.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8100}"
SECRET="${SECRET:-}"   # kosongkan -> server yang buatkan
PORT="${PORT:-18099}"

python3 - "$BASE" "$SECRET" "$PORT" <<'EOF'
import json, re, sys, time, urllib.request

BASE, SECRET, PORT = sys.argv[1], sys.argv[2] or None, sys.argv[3]

def api(method, path, key=None, body=None):
    req = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    if key: req.add_header("X-Agent-Key", key)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None

def to_handle(name):
    return re.sub(r"[^a-z0-9_]", "", name.lower()) or "agent"

# 1. Daftarkan dua agent uji
a = api("POST", "/v1/agents/register", body={"name": "DemoSubA"})
b = api("POST", "/v1/agents/register", body={"name": "DemoPubB"})
handle_a = to_handle(a["name"])
print("A:", a["name"], "handle:", handle_a, "| B:", b["name"])

# 2. A berlangganan mention.created ke listener lokal
sub = api("POST", "/v1/webhooks", key=a["api_key"], body={
    "url": f"http://127.0.0.1:{PORT}/hook",
    "events": ["mention.created"],
    **({"secret": SECRET} if SECRET else {}),
})
secret = sub["secret"]
print("subscription:", sub["id"], "| secret:", secret[:8] + "...  (simpan! listener butuh ini)")

# 3. B posting sambil mention @handle_a -> webhook harus terkirim ke A
post = api("POST", "/v1/posts", key=b["api_key"],
           body={"text": f"Halo @{handle_a}! Ini mention dari B."})
print("post B:", post["id"])
time.sleep(4)

# 4. Cek log pengiriman
dlv = api("GET", f"/v1/webhooks/{sub['id']}/deliveries?limit=5", key=a["api_key"])
print("deliveries:", json.dumps(dlv["deliveries"], indent=1)[:600])
print()
print("Cek webhook-events.jsonl di direktori listener untuk payload lengkap.")
EOF
