import json, subprocess, sys

pw = subprocess.run(
    ["bash", "-lc", 'echo -n "$OPENCODE_SERVER_PASSWORD"'],
    capture_output=True, text=True, check=True,
).stdout
raw = subprocess.run(
    ["curl", "-fsS", "-u", f"opencode:{pw}", "http://127.0.0.1:4096/skill"],
    capture_output=True, text=True, check=True,
).stdout
for s in json.loads(raw):
    print(f"{s['name']:24s} -> {s['location']}")
