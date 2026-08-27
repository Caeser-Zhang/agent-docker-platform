#!/bin/sh
# Extract opencode's own frontend CSS variables (design tokens) from the served SPA.
AUTH="opencode:$OPENCODE_SERVER_PASSWORD"
BASE="http://127.0.0.1:4096"
# Find the CSS asset name from index.html
HTML=$(curl -sf -u "$AUTH" "$BASE/")
CSS=$(echo "$HTML" | grep -o '/assets/index-[^"]*\.css' | head -1)
echo "css asset = $CSS"
curl -sf -u "$AUTH" "$BASE$CSS" > /tmp/oc.css
echo "css bytes: $(wc -c < /tmp/oc.css)"
echo "=== :root / theme CSS variables (light) ==="
grep -o '\-\-[a-z0-9-]*:[^;]*;' /tmp/oc.css | grep -iE 'background|bg|foreground|fg|text|border|accent|primary|surface|muted|panel|sidebar|color' | sort -u | head -80
