#!/usr/bin/env python3
"""Extract the response shape of GET /api/session/{id}/message from opencode's
OpenAPI document, so the frontend renders real fields instead of guesses."""
import json
import sys

SPEC = sys.argv[1] if len(sys.argv) > 1 else "/tmp/octest/openapi.json"
spec = json.load(open(SPEC))
schemas = spec.get("components", {}).get("schemas", {})


def deref(node, depth=0, seen=None):
    """Resolve $ref one level and summarise the structure."""
    if seen is None:
        seen = set()
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        if name in seen or depth > 3:
            return f"<{name}>"
        return {f"$={name}": deref(schemas.get(name, {}), depth + 1, seen | {name})}
    out = {}
    if "anyOf" in node or "oneOf" in node:
        key = "anyOf" if "anyOf" in node else "oneOf"
        return {key: [deref(x, depth + 1, seen) for x in node[key]]}
    t = node.get("type")
    if t == "object":
        props = node.get("properties", {})
        for k, v in props.items():
            out[k] = deref(v, depth + 1, seen)
        return out
    if t == "array":
        return [deref(node.get("items", {}), depth + 1, seen)]
    if "const" in node:
        return f"const:{node['const']}"
    if "enum" in node:
        return f"enum:{node['enum']}"
    return t or "any"


def show(title, node):
    print(f"\n=== {title} ===")
    print(json.dumps(deref(node), indent=2, ensure_ascii=False)[:6000])


paths = spec["paths"]
for p in ("/api/session/{id}/message", "/api/session/{id}/message/{messageID}"):
    if p in paths:
        op = paths[p].get("get", {})
        body = op.get("responses", {}).get("200", {}).get("content", {}).get(
            "application/json", {}
        ).get("schema", {})
        show(f"GET {p} -> 200", body)

print("\n=== schema names containing 'Message' or 'Part' ===")
print([n for n in schemas if "Message" in n or "Part" in n][:80])

for name in ("SessionMessage", "SessionMessageV2", "MessageV2", "Message"):
    if name in schemas:
        show(f"schema {name}", schemas[name])
        break
