#!/usr/bin/env python3
"""Dump the full prompt endpoint spec from opencode's OpenAPI doc."""
import json
import sys

d = json.load(sys.stdin)
spec = d.get("paths", {}).get("/api/session/{sessionID}/prompt", {}).get("post", {})
print(json.dumps(spec, indent=2)[:3000])
