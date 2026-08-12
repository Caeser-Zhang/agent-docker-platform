#!/usr/bin/env python3
"""Dump the payload shape of every `session.next.*` SSE event opencode emits.

The frontend reducer in frontend/src/components/Chat.tsx is written directly
against this output, so re-run it whenever the opencode version changes:

    python3 scripts/probe-events.py /tmp/octest/openapi.json
"""
import json
import sys

spec = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/octest/openapi.json", encoding="utf-8"))
schemas = spec["components"]["schemas"]


def event_type_of(schema: dict) -> str | None:
    props = schema.get("properties", {})
    enum = props.get("type", {}).get("enum")
    if enum and len(enum) == 1:
        return enum[0]
    return None


def summarize_data(schema: dict, depth: int = 0) -> str:
    """One-line summary of the event's `data` object keys."""
    data = schema.get("properties", {}).get("data", {})
    ref = data.get("$ref")
    if ref:
        data = schemas.get(ref.rsplit("/", 1)[-1], {})
    props = data.get("properties", {})
    if not props:
        return "{}"
    parts = []
    for key, val in props.items():
        kind = val.get("type")
        if not kind and val.get("$ref"):
            kind = val["$ref"].rsplit("/", 1)[-1]
        if not kind and val.get("anyOf"):
            kind = "anyOf"
        parts.append(f"{key}:{kind}")
    return "{ " + ", ".join(parts) + " }"


rows = []
for name, schema in schemas.items():
    etype = event_type_of(schema)
    if etype and etype.startswith("session."):
        rows.append((etype, name, summarize_data(schema)))

for etype, name, data in sorted(rows):
    print(f"{etype}")
    print(f"    component : {name}")
    print(f"    data      : {data}")
