#!/usr/bin/env python3
"""Parse the prompt API schema from opencode's OpenAPI doc."""
import json
import sys

d = json.load(sys.stdin)
spec = d.get("paths", {}).get("/api/session/{sessionID}/prompt", {}).get("post", {})
body_ref = spec.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("$ref", "")
print("body schema ref:", body_ref)
if body_ref:
    ref_name = body_ref.split("/")[-1]
    schema = d.get("components", {}).get("schemas", {}).get(ref_name, {})
    print("schema:", json.dumps(schema, indent=2)[:2000])
