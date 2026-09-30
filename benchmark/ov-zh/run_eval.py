#!/usr/bin/env python3
"""OpenViking Chinese retrieval benchmark runner (sample set).

Pure HTTP client -- no docker / no repo imports. The API key must be supplied via
the OV_API_KEY environment variable (a derived User Key for --user-id).

Modes:
  seed   write every corpus doc to viking://user/<uid>/memories/<category>/<slug>.md,
         read it back and assert character-exact equality (P3-07 / P3-08),
         then wait for the vector index to stabilise.
  eval   run every query, resolve returned URIs to corpus ids, score them.
  both   seed then eval.

Metrics (computed per group G1..G4 and overall):
  hit@5        1 if any expected id appears in the top 5
  recall@5     |expected ∩ top5| / |expected|
  mrr          1 / rank of the first expected id in the top 5 (0 if absent)
  precision@5  |expected ∩ top5| / 5        (meaningful only for G4)
  scaffold@5   fraction of top-5 slots taken by .abstract.md / .overview.md (R15)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LEVEL_SUFFIXES = ("/.abstract.md", "/.overview.md")


def log(*a):
    print(*a, flush=True)


def http(base, path, key, body=None, timeout=90):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST" if data else "GET")
    req.add_header("X-API-Key", key)
    if data:
        req.add_header("Content-Type", "application/json")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    return code, raw, (time.perf_counter() - t0) * 1000.0


def norm_uri(uri):
    u = str(uri or "")
    for s in LEVEL_SUFFIXES:
        if u.endswith(s):
            u = u[: -len(s)]
            break
    return u.rstrip("/")


def is_scaffold(uri):
    u = str(uri or "")
    return any(u.endswith(s) for s in LEVEL_SUFFIXES)


def load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                log("FATAL: %s line %d: %s" % (path, ln, e))
                sys.exit(2)
    return out


def corpus_uri(uid, doc):
    return "viking://user/%s/memories/%s/%s.md" % (uid, doc["category"], doc["slug"])


# --------------------------------------------------------------------------- seed


def seed(base, key, uid, corpus):
    log("=" * 72)
    log("SEED  %d docs -> viking://user/%s/memories/" % (len(corpus), uid))
    log("=" * 72)
    wrote, mismatch, errors = 0, [], []
    lat = []
    for doc in corpus:
        uri = corpus_uri(uid, doc)
        code, raw, ms = http(base, "/api/v1/content/write", key, {
            "uri": uri, "content": doc["content"], "mode": "replace", "wait": True,
        })
        lat.append(ms)
        if code != 200:
            errors.append((doc["id"], code, raw[:160].decode("utf-8", "replace")))
            continue
        wrote += 1
        # P3-07 / P3-08: character-exact round trip
        rc, rraw, _ = http(base, "/api/v1/content/read?uri=" + urllib.parse.quote(uri, safe=":/?&="), key)
        if rc != 200:
            errors.append((doc["id"], "read=%s" % rc, rraw[:160].decode("utf-8", "replace")))
            continue
        try:
            payload = json.loads(rraw.decode("utf-8"))
            got = payload.get("result") if isinstance(payload, dict) else payload
        except Exception as e:  # noqa: BLE001
            errors.append((doc["id"], "read-parse", str(e)))
            continue
        text = got.get("content") if isinstance(got, dict) else got
        if not isinstance(text, str):
            text = json.dumps(got, ensure_ascii=False)
        if not mismatch and not wrote % 20:
            log("  read-shape probe: %s" % json.dumps(
                {"top_keys": sorted(payload) if isinstance(payload, dict) else type(payload).__name__,
                 "result_type": type(got).__name__,
                 "result_keys": sorted(got) if isinstance(got, dict) else None,
                 "text_len": len(text)}, ensure_ascii=False))
        if text.strip() != doc["content"].strip():
            mismatch.append({
                "id": doc["id"], "uri": uri,
                "expected_len": len(doc["content"]), "got_len": len(text),
                "expected_cp": [hex(ord(c)) for c in doc["content"][:8]],
                "got_cp": [hex(ord(c)) for c in text[:8]],
                "got_head": text[:80],
            })
    lat.sort()
    log("  written       : %d/%d" % (wrote, len(corpus)))
    log("  write latency : p50=%.0fms  max=%.0fms" % (
        lat[len(lat) // 2] if lat else 0, lat[-1] if lat else 0))
    log("  P3-07/08 exact: %s  (%d mismatch)" % ("PASS" if not mismatch else "FAIL", len(mismatch)))
    for m in mismatch[:6]:
        log("    - %s exp_len=%d got_len=%d got_head=%r" % (m["id"], m["expected_len"], m["got_len"], m["got_head"]))
        log("      exp_cp=%s" % m["expected_cp"])
        log("      got_cp=%s" % m["got_cp"])
    if errors:
        log("  errors        : %d" % len(errors))
        for e in errors[:6]:
            log("    - %s" % (e,))
    for bad in ("\ufffd", "?"):
        hits = [m["id"] for m in mismatch if bad in m["got_head"]]
        if hits:
            log("  mojibake %r in: %s" % (bad, hits))
    return {"written": wrote, "total": len(corpus), "mismatch": mismatch, "errors": errors}


def wait_index(base, key, stable_rounds=3, max_wait=180):
    """vector/count is asynchronous; wait until it stops changing."""
    log("-" * 72)
    last, same, t_end = -1, 0, time.time() + max_wait
    while time.time() < t_end:
        code, raw, _ = http(base, "/api/v1/debug/vector/count", key)
        try:
            n = json.loads(raw.decode("utf-8"))["result"]["count"]
        except Exception:  # noqa: BLE001
            n = -2
        if n == last:
            same += 1
            if same >= stable_rounds:
                log("  vector/count stable at %s" % n)
                return n
        else:
            same = 0
            log("  vector/count = %s" % n)
        last = n
        time.sleep(3)
    log("  !! vector/count never stabilised (last=%s)" % last)
    return last


# --------------------------------------------------------------------------- eval


def evaluate(base, key, uid, corpus, queries, limit=5, verbose=True, filter_scaffold=False,
             overfetch=0):
    by_uri = {}
    for doc in corpus:
        by_uri[norm_uri(corpus_uri(uid, doc))] = doc["id"]

    rows = []
    for q in queries:
        fetch = limit + overfetch
        code, raw, ms = http(base, "/api/v1/search/search", key, {"query": q["query"], "limit": fetch})
        exp = list(q.get("expected") or [])
        row = {"id": q["id"], "group": q["group"], "query": q["query"], "expected": exp,
               "latency_ms": round(ms), "http": code, "top": [], "ids": [], "scaffold_slots": 0}
        if code != 200:
            row["error"] = raw[:200].decode("utf-8", "replace")
            rows.append(row)
            continue
        res = json.loads(raw.decode("utf-8")).get("result") or {}
        items = []
        for grp in ("memories", "resources", "skills"):
            for it in (res.get(grp) or []):
                it["_grp"] = grp
                items.append(it)
        window = items[:fetch]
        # scaffold_slots is always measured on the first `limit` slots of the
        # unfiltered window, so it stays comparable across rerank states and
        # across --filter-scaffold / --overfetch runs.
        row["scaffold_slots"] = sum(1 for it in window[:limit] if is_scaffold(it.get("uri")))
        row["fetched"] = len(window)
        if filter_scaffold:
            items = [it for it in window if not is_scaffold(it.get("uri"))][:limit]
        else:
            items = window[:limit]
        rank_of = {}
        for i, it in enumerate(items, 1):
            uri = it.get("uri")
            cid = by_uri.get(norm_uri(uri))
            scaf = is_scaffold(uri)
            row["top"].append({"rank": i, "score": round(float(it.get("score") or 0), 4),
                               "level": it.get("level"), "grp": it["_grp"],
                               "id": cid, "scaffold": scaf,
                               "uri": str(uri).replace("viking://user/%s/" % uid, "")})
            if cid and cid not in rank_of:
                rank_of[cid] = i
        row["ids"] = [t["id"] for t in row["top"]]
        row["returned"] = len(items)
        found = [c for c in exp if c in rank_of]
        row["found"] = found
        row["hit@5"] = 1.0 if found else 0.0
        row["recall@5"] = (len(found) / len(exp)) if exp else 0.0
        first = min((rank_of[c] for c in found), default=0)
        row["mrr"] = (1.0 / first) if first else 0.0
        row["precision@5"] = len(found) / limit
        row["precision@returned"] = (len(found) / len(items)) if items else 0.0
        rows.append(row)

    if verbose:
        for row in rows:
            log("  %-4s %-3s hit=%d r@5=%.2f mrr=%.2f p@5=%.2f p@ret=%.2f ret=%d scaf=%d %4dms  %s" % (
                row["id"], row["group"], row.get("hit@5", 0), row.get("recall@5", 0),
                row.get("mrr", 0), row.get("precision@5", 0), row.get("precision@returned", 0),
                row.get("returned", 0), row["scaffold_slots"],
                row["latency_ms"], row["query"][:34]))
            for t in row["top"]:
                mark = "OK " if t["id"] in row["expected"] else ("SCF" if t["scaffold"] else "   ")
                log("        %s#%d %-7s L%s %-9s %s" % (
                    mark, t["rank"], t["score"], t["level"], t["id"] or "-", t["uri"][:64]))
            if row.get("error"):
                log("        ERROR http=%s %s" % (row["http"], row["error"]))
    return rows


def summarize(rows):
    out = {}
    groups = sorted({r["group"] for r in rows})
    for g in groups + ["ALL"]:
        sel = rows if g == "ALL" else [r for r in rows if r["group"] == g]
        if not sel:
            continue
        n = len(sel)
        out[g] = {
            "n": n,
            "hit@5": round(sum(r.get("hit@5", 0) for r in sel) / n, 4),
            "recall@5": round(sum(r.get("recall@5", 0) for r in sel) / n, 4),
            "mrr": round(sum(r.get("mrr", 0) for r in sel) / n, 4),
            "precision@5": round(sum(r.get("precision@5", 0) for r in sel) / n, 4),
            "precision@returned": round(sum(r.get("precision@returned", 0) for r in sel) / n, 4),
            "avg_returned": round(sum(r.get("returned", 0) for r in sel) / n, 2),
            "avg_fetched": round(sum(r.get("fetched", r.get("returned", 0)) for r in sel) / n, 2),
            "scaffold_rate": round(sum(r["scaffold_slots"] for r in sel) / (n * 5), 4),
            "lat_p50_ms": sorted(r["latency_ms"] for r in sel)[n // 2],
            "lat_p95_ms": sorted(r["latency_ms"] for r in sel)[max(0, int(n * 0.95) - 1)],
            "errors": sum(1 for r in sel if r.get("error")),
        }
    return out


def print_summary(summary, label):
    log("")
    log("=" * 78)
    log("SUMMARY [%s]" % label)
    log("=" * 78)
    log("  %-5s %3s %8s %9s %8s %7s %7s %5s %9s %7s %7s" % (
        "grp", "n", "hit@5", "recall@5", "mrr", "p@5", "p@ret", "ret", "scaf_rate", "p50_ms", "p95_ms"))
    for g, m in summary.items():
        log("  %-5s %3d %8.3f %9.3f %8.3f %7.3f %7.3f %5.2f %9.3f %7d %7d" % (
            g, m["n"], m["hit@5"], m["recall@5"], m["mrr"], m["precision@5"],
            m["precision@returned"], m["avg_returned"],
            m["scaffold_rate"], m["lat_p50_ms"], m["lat_p95_ms"]))
    log("")
    log("  thresholds (test plan): G1 recall@5>=0.95 mrr>=0.90 | G2 recall@5>=0.80 mrr>=0.70")
    log("                          G3 recall@5>=0.70          | G4 precision@5>=0.75")
    log("  informational:         G4 precision@returned>=0.75 (fair when fewer than 5 come back)")
    verdict = []
    def chk(g, metric, thr):
        m = summary.get(g)
        if not m:
            return
        ok = m[metric] >= thr
        verdict.append("    %s %s=%.3f vs %.2f -> %s" % (g, metric, m[metric], thr, "PASS" if ok else "FAIL"))
    chk("G1", "recall@5", 0.95); chk("G1", "mrr", 0.90)
    chk("G2", "recall@5", 0.80); chk("G2", "mrr", 0.70)
    chk("G3", "recall@5", 0.70)
    chk("G4", "precision@5", 0.75)
    chk("G4", "precision@returned", 0.75)
    log("  verdict:")
    for v in verdict:
        log(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:1933")
    ap.add_argument("--user-id", default="ovzh-eval")
    ap.add_argument("--mode", choices=["seed", "eval", "both"], default="both")
    ap.add_argument("--corpus", default=os.path.join(os.path.dirname(__file__), "corpus.jsonl"))
    ap.add_argument("--queries", default=os.path.join(os.path.dirname(__file__), "queries.jsonl"))
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--filter-scaffold", action="store_true",
                    help="drop .abstract.md/.overview.md hits from the top-k window before scoring")
    ap.add_argument("--overfetch", type=int, default=0,
                    help="request limit+N from the server so filtering can still fill limit slots")
    ap.add_argument("--label", default="unlabelled")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    key = os.environ.get("OV_API_KEY", "")
    if not key:
        log("FATAL: OV_API_KEY not set"); sys.exit(2)
    log("base=%s user=%s key_len=%d label=%s" % (a.base_url, a.user_id, len(key), a.label))

    corpus = load_jsonl(a.corpus)
    queries = load_jsonl(a.queries)
    log("corpus=%d docs  queries=%d  groups=%s" % (
        len(corpus), len(queries),
        {g: sum(1 for q in queries if q["group"] == g) for g in sorted({q["group"] for q in queries})}))
    cats = {}
    for d in corpus:
        cats[d["category"]] = cats.get(d["category"], 0) + 1
    log("categories: %s" % json.dumps(cats, ensure_ascii=False, sort_keys=True))

    # rerank state, straight from the only reliable observability surface
    try:
        with urllib.request.urlopen(a.base_url.rstrip("/") + "/metrics", timeout=10) as r:
            met = r.read().decode("utf-8", "replace")
        rr = [l for l in met.splitlines() if l.startswith('openviking_model_usage_available{model_type="rerank"')]
        log("rerank available gauge: %s" % (rr[0].split()[-1] if rr else "ABSENT"))
    except Exception as e:  # noqa: BLE001
        log("rerank gauge unreadable: %s" % e)

    result = {"label": a.label, "corpus_n": len(corpus), "queries_n": len(queries),
              "filter_scaffold": a.filter_scaffold, "overfetch": a.overfetch}
    if a.mode in ("seed", "both"):
        result["seed"] = seed(a.base_url, key, a.user_id, corpus)
        result["vector_count"] = wait_index(a.base_url, key)
    if a.mode in ("eval", "both"):
        log("")
        log("=" * 72)
        log("EVAL  label=%s  limit=%d  filter_scaffold=%s  overfetch=%d" % (
            a.label, a.limit, a.filter_scaffold, a.overfetch))
        log("=" * 72)
        rows = evaluate(a.base_url, key, a.user_id, corpus, queries, a.limit,
                        filter_scaffold=a.filter_scaffold, overfetch=a.overfetch)
        summary = summarize(rows)
        print_summary(summary, a.label)
        result["rows"] = rows
        result["summary"] = summary
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        log("\nwrote %s" % a.out)
    log("RUN-DONE")


if __name__ == "__main__":
    main()
