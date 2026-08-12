#!/usr/bin/env python3
"""End-to-end check of the four layers, with no mocks anywhere.

    browser (this script)
      -> platform control layer   http://localhost:9123
      -> container runtime layer  opencode serve in agent-<uid>
      -> shared services          sqlite record + docker volumes

It exercises the real request path the SPA uses:

    POST /api/auth/register|login
    POST /api/agent/start
    GET  /api/agent/runtime
    GET  /api/tunnel/providers          (reads opencode's own /config)
    POST /api/tunnel/oc/api/session     (opencode route, proxied verbatim)
    POST /api/tunnel/oc/api/session/{id}/prompt   {"prompt":{"text":...}}
    GET  /api/tunnel/events             (SSE relay of opencode /api/event)
    GET  /api/tunnel/oc/api/session/{id}/message

Usage:
    python3 scripts/e2e.py [--base http://localhost:9123] [--prompt "..."]

Exit code is non-zero if any layer fails to respond; a model-side failure
(expired API key) is reported but does not fail the transport check, because
that is a credential problem, not a platform problem.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(self, method: str, path: str, body=None, timeout=120):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw
        except Exception as e:  # noqa: BLE001
            return 0, str(e)


class EventTap(threading.Thread):
    """Consume the platform's SSE relay in the background, like the browser."""

    daemon = True

    def __init__(self, base: str, token: str):
        super().__init__()
        self.url = f"{base}/api/tunnel/events?token={urllib.parse.quote(token)}&lastEventId=0"
        self.events: list[dict] = []
        self.stop_flag = threading.Event()

    def run(self):
        try:
            with urllib.request.urlopen(self.url, timeout=300) as resp:
                for raw in resp:
                    if self.stop_flag.is_set():
                        return
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        self.events.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    def types(self) -> list[str]:
        return [e.get("type", "?") for e in self.events]

    def wait_for(self, wanted: set[str], timeout: float) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in self.events:
                if e.get("type") in wanted:
                    return e["type"]
            time.sleep(0.4)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:9123")
    ap.add_argument("--user", default=f"e2e{int(time.time())}")
    ap.add_argument("--password", default="e2e-password-123")
    ap.add_argument("--prompt", default="Reply with exactly: PONG")
    ap.add_argument("--wait", type=float, default=30.0)
    args = ap.parse_args()

    c = Client(args.base)
    failures = 0

    def step(label, status, detail=""):
        nonlocal failures
        mark = OK if status is True else (WARN if status == "warn" else BAD)
        if status is False:
            failures += 1
        print(f"{mark} {label}" + (f"  — {detail}" if detail else ""))

    print("== layer 2: platform control ==")
    code, body = c.call("GET", "/api/health", timeout=10)
    step("backend /api/health", code == 200, f"HTTP {code}")
    if code != 200:
        return 1

    code, body = c.call("POST", "/api/auth/register",
                        {"username": args.user, "password": args.password}, timeout=30)
    if code != 200:
        code, body = c.call("POST", "/api/auth/login",
                            {"username": args.user, "password": args.password}, timeout=30)
    step("auth", code == 200 and isinstance(body, dict), f"HTTP {code}")
    if code != 200:
        print(body)
        return 1
    c.token = body["access_token"]

    print("\n== layer 3: container runtime ==")
    t0 = time.time()
    code, body = c.call("POST", "/api/agent/start", {}, timeout=180)
    started = code == 200 and isinstance(body, dict) and body.get("running")
    step("POST /api/agent/start", bool(started),
         f"HTTP {code} {body if not started else body.get('container_name')} "
         f"in {time.time()-t0:.1f}s")
    if not started:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 1

    code, runtime = c.call("GET", "/api/agent/runtime", timeout=20)
    step("GET /api/agent/runtime", code == 200,
         f"{runtime.get('runtime')} · config={runtime.get('config', {}).get('source')}"
         if code == 200 else f"HTTP {code}")
    if code == 200:
        cfg = runtime.get("config", {})
        step("host opencode.json mounted", bool(cfg.get("mounted")) or "warn",
             f"providers={cfg.get('providers')} stripped={cfg.get('stripped')}")

    # Attach the event tap before doing anything that generates events.
    tap = EventTap(args.base, c.token)
    tap.start()
    time.sleep(1.0)

    print("\n== opencode contract through the tunnel ==")
    code, health = c.call("GET", "/api/tunnel/oc/api/health", timeout=20)
    step("proxy -> opencode GET /api/health", code == 200, f"HTTP {code} {health}")

    code, provs = c.call("GET", "/api/tunnel/providers", timeout=30)
    if not isinstance(provs, dict):
        provs = {"_raw": provs}
    n_models = sum(len(p.get("models", [])) for p in (provs.get("providers") or [])) if code == 200 else 0
    step("GET /api/tunnel/providers", code == 200 and n_models > 0,
         f"{len(provs.get('providers', []))} providers / {n_models} models, "
         f"default={provs.get('default')}" if code == 200 else f"HTTP {code} {provs}")
    if code == 200:
        for p in provs.get("providers", []):
            print(f"    · {p['id']:<28} {len(p['models']):>2} models  {p.get('baseURL') or ''}")

    # opencode serve mode does NOT auto-resolve the default model from
    # config.json — it must be passed explicitly at session creation time.
    # Additionally, the FIRST session on a fresh container triggers a title-
    # generation call whose model resolution races with provider initialisation
    # and fails with "Model unavailable" — poisoning that session's runner so
    # subsequent prompts are silently dropped. Creating a throwaway session
    # first lets the race resolve; the second session works correctly.
    default_model = provs.get("default")
    session_body: dict = {"agent": "build", "location": {"directory": "/workspace"}}
    if default_model and "/" in default_model:
        pid, mid = default_model.split("/", 1)
        session_body["model"] = {"providerID": pid, "id": mid}

    # Warmup session (discarded) — absorbs the first-session model race.
    wcode, wresp = c.call("POST", "/api/tunnel/oc/api/session", session_body, timeout=30)
    print(f"    warmup: HTTP {wcode}")
    time.sleep(5)

    code, sess = c.call("POST", "/api/tunnel/oc/api/session",
                        session_body, timeout=60)
    session_id = (sess or {}).get("data", {}).get("id") if code == 200 else None
    step("POST /api/tunnel/oc/api/session", bool(session_id),
         session_id or f"HTTP {code} {sess}")
    if not session_id:
        return 1

    print("\n== layer 4: real model call ==")
    code, r = c.call("POST", f"/api/tunnel/oc/api/session/{session_id}/prompt",
                     {"prompt": {"text": args.prompt}}, timeout=300)
    step("POST .../prompt", code == 200, f"HTTP {code} {str(r)[:200]}")

    # Wait for the model call to finish. opencode's serve mode can emit
    # session.idle very quickly (title generation) or very late (long output),
    # so we poll both the SSE stream AND the /message endpoint.
    terminal = tap.wait_for({"session.idle", "session.next.step.failed", "session.error"}, args.wait)

    # Even if SSE doesn't deliver a terminal event, the model may still be
    # processing. Poll /message for up to 60s looking for an assistant reply.
    poll_deadline = time.time() + 60
    found_assistant = False
    while time.time() < poll_deadline:
        code, msgs = c.call("GET", f"/api/tunnel/oc/api/session/{session_id}/message", timeout=30)
        data = (msgs or {}).get("data", []) if code == 200 else []
        if any(m.get("type") == "assistant" for m in data):
            found_assistant = True
            break
        time.sleep(3)

    if found_assistant:
        step("SSE reached a terminal event", terminal is not None,
             terminal or f"no terminal SSE (but /message has assistant reply)")
    else:
        step("SSE reached a terminal event", terminal is not None, terminal or f"timeout after {args.wait}s")

    seen = tap.types()
    print(f"    events observed ({len(seen)}): {', '.join(dict.fromkeys(seen))}")

    # The authoritative check is /message: if the assistant replied there,
    # the model call succeeded regardless of whether the SSE relay delivered
    # the delta events in time.
    # If we already fetched /message during polling, reuse that response.
    if not found_assistant:
        code, msgs = c.call("GET", f"/api/tunnel/oc/api/session/{session_id}/message", timeout=60)
    # 'msgs' and 'code' are already set from the polling loop if found_assistant.
    data = (msgs or {}).get("data", []) if code == 200 else []
    step("GET .../message", code == 200, f"{len(data)} messages")
    assistant_texts: list[str] = []
    for m in data:
        if m.get("type") == "user":
            print(f"    user      : {m.get('text', '')[:120]}")
        elif m.get("type") == "assistant":
            texts = [b.get("text", "") for b in m.get("content", []) if b.get("type") == "text"]
            tools = [b.get("name") for b in m.get("content", []) if b.get("type") == "tool"]
            print(f"    assistant : model={m.get('model')} tools={tools}")
            if texts:
                print(f"                {' '.join(texts)[:300]}")
                assistant_texts.extend(texts)
            if m.get("error"):
                print(f"                error={m['error']}")

    # The model call is considered successful if /message contains an assistant
    # turn with non-empty text content — that is proof the entire four-layer
    # chain (browser → platform → container → LLM) completed end to end.
    got_text = bool(assistant_texts)
    sse_deltas = any(t == "session.next.text.delta" for t in seen)
    if got_text and sse_deltas:
        step("model streamed text (SSE + /message)", True)
    elif got_text:
        step("model replied (/message has assistant text)", True, "SSE deltas not relayed (pump timing)")
    elif sse_deltas:
        step("model streamed text (SSE only)", "warn", "/message has no assistant text yet")
    else:
        failed = [e for e in tap.events if e.get("type") == "session.next.step.failed"]
        msg = failed[0]["data"].get("error", {}).get("message", "") if failed else "no output"
        step("model replied", "warn", f"no output — {msg[:300]}")

    tap.stop_flag.set()
    print(f"\n{'PASS' if failures == 0 else 'FAIL'} — {failures} hard failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
