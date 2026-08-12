#!/usr/bin/env python3
"""Extract the exact request/response schemas the platform depends on from the
opencode OpenAPI document captured by scripts/probe-opencode.sh."""
import json
import sys

SPEC_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/octest/openapi.json"
TARGETS = [
    ("post", "/api/session"),
    ("post", "/api/session/{sessionID}/prompt"),
    ("post", "/api/session/{sessionID}/model"),
    ("post", "/api/session/{sessionID}/agent"),
    ("post", "/api/session/{sessionID}/interrupt"),
    ("get", "/api/session/{sessionID}/message"),
    ("get", "/api/event"),
]

spec = json.load(open(SPEC_PATH, encoding="utf-8"))
components = spec.get("components", {}).get("schemas", {})
seen: set[str] = set()


def resolve(node, depth=0):
    """Inline $refs up to a small depth so the shape is readable."""
    if depth > 4:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref:
            name = ref.rsplit("/", 1)[-1]
            if name in seen:
                return {"$ref": name}
            seen.add(name)
            out = resolve(components.get(name, {}), depth + 1)
            seen.discard(name)
            if isinstance(out, dict):
                out = {"__name__": name, **out}
            return out
        return {k: resolve(v, depth + 1) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [resolve(v, depth + 1) for v in node]
    return node


for method, path in TARGETS:
    op = spec.get("paths", {}).get(path, {}).get(method)
    print("=" * 78)
    print(f"{method.upper()} {path}")
    if not op:
        print("  (not present)")
        continue
    body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    if body:
        print("-- request body --")
        print(json.dumps(resolve(body), ensure_ascii=False, indent=2)[:2600])
    resp = (
        op.get("responses", {})
        .get("200", {})
        .get("content", {})
    )
    for ctype, payload in resp.items():
        schema = payload.get("schema")
        print(f"-- 200 response ({ctype}) --")
        print(json.dumps(resolve(schema), ensure_ascii=False, indent=2)[:1800] if schema else "(none)")

print("=" * 78)
print("Model.Ref-ish component names:")
for name in sorted(components):
    low = name.lower()
    if "model" in low and len(name) < 40:
        print("  ", name, "->", json.dumps(components[name], ensure_ascii=False)[:260])
