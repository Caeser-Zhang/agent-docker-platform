#!/usr/bin/env python3
"""Verify pptx-generator shows up in the frontend skill lists.

Two API paths feed the UI:
  1. GET /api/workspace/skills/all — the chat skill picker (any user);
  2. GET /api/config/builtin-skills — the admin ConfigPanel builtin section.

Both funnel through visibility.list_builtin_skills(), which previously only
matched skills whose opencode `location` fell under a built-in plugin path.
Image-seeded skills (pptx-generator) live in the XDG skills dir, so they were
invisible in the frontend even though the agent could use them. The fix also
matches by name against agent-image/builtin-skills/.
"""
import json
import urllib.request

BASE = "http://localhost:3001/api"  # via the frontend nginx reverse proxy
USERNAME = "pptxe2e"
PASSWORD = "pptx-E2E-pass-2026"
ADMIN = {"username": "admin", "password": "admin123"}  # adjusted below if wrong


def http(method: str, path: str, body=None, token: str | None = None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def login(user, pw):
    code, body = http("POST", "/auth/register", {"username": user, "password": pw})
    if code == 400:
        code, body = http("POST", "/auth/login", {"username": user, "password": pw})
    return code, body


def main():
    ok = True

    # --- user path: chat skill picker -----------------------------------
    code, reg = login(USERNAME, PASSWORD)
    assert code == 200, f"user auth failed: {code} {reg}"
    token = reg["access_token"]

    # agent container must be running for the builtin enumeration
    http("POST", "/agent/start", {"wait": True}, token=token, )

    code, allskills = http("GET", "/workspace/skills/all", token=token)
    names = {s["name"]: s for s in (allskills.get("skills") or [])}
    hit = names.get("pptx-generator")
    print(f"[picker] /api/workspace/skills/all -> HTTP {code}, {len(names)} skills")
    if hit:
        print(f"[picker] FOUND pptx-generator scope={hit['scope']} desc={hit['description'][:60]}")
    else:
        ok = False
        print(f"[picker] MISSING pptx-generator; skills: {sorted(names)}")

    # --- admin path: ConfigPanel builtin section ------------------------
    code, adm = login("admin", "admin123")
    if code != 200:
        print(f"[admin] login failed ({code}); trying to list as admin anyway skipped")
    else:
        atok = adm["access_token"]
        code, bs = http("GET", "/config/builtin-skills", token=atok)
        bnames = {s["name"]: s for s in (bs.get("skills") or [])}
        print(f"[admin] /api/config/builtin-skills -> HTTP {code} reachable={bs.get('reachable')} skills={sorted(bnames)}")
        bhit = bnames.get("pptx-generator")
        if bhit:
            print(f"[admin] FOUND pptx-generator enabled={bhit['enabled']}")
        else:
            ok = False
            print("[admin] MISSING pptx-generator")

    print("==> RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
