#!/usr/bin/env python3
"""One-off monitor: print the current state of the pptxe2e session's messages."""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://localhost:8000"

req = urllib.request.Request(
    BASE + "/api/auth/login",
    data=json.dumps({"username": "pptxe2e", "password": "pptx-E2E-pass-2026"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
tok = json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]

req = urllib.request.Request(BASE + "/api/tunnel/oc/session?limit=5",
                             headers={"Authorization": f"Bearer {tok}"})
sessions = json.loads(urllib.request.urlopen(req, timeout=30).read())
items = sessions.get("data", sessions) if isinstance(sessions, dict) else sessions
sid = items[0]["id"] if items else None
print("latest session:", sid)

req = urllib.request.Request(BASE + f"/api/tunnel/oc/session/{sid}/message",
                             headers={"Authorization": f"Bearer {tok}"})
msgs = json.loads(urllib.request.urlopen(req, timeout=30).read())
print(f"{len(msgs)} messages")
for m in msgs[-6:]:
    info = m.get("info", {})
    t = info.get("time", {})
    parts_desc = []
    for p in m.get("parts", []):
        pt = p.get("type")
        if pt == "text":
            parts_desc.append(f"text[{len(p.get('text',''))}ch]")
        elif pt == "tool":
            st = p.get("state", {})
            parts_desc.append(
                f"tool={p.get('tool','?')}:{st.get('status','?')}"
                + (f" err={str(st.get('error'))[:120]}" if st.get("error") else ""))
        else:
            parts_desc.append(pt or "?")
    print(f"  {info.get('role'):9s} end={t.get('end')} {' '.join(parts_desc)}")
# dump the last tool part's input title for context
for m in reversed(msgs):
    for p in m.get("parts", []):
        if p.get("type") == "tool":
            inp = json.dumps(p.get("state", {}).get("input", {}))[:300]
            print("last tool input:", inp)
            sys.exit(0)
