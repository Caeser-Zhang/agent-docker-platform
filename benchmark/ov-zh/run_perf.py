#!/usr/bin/env python3
"""Phase 4 performance harness for the OpenViking memory service.

Companion to ``run_eval.py``: that one measures *retrieval quality*, this one
measures *cost*. Same query set, same HTTP client shape, but the numbers it
reports are latency percentiles, throughput and concurrency degradation rather
than recall.

It is written to run **inside the backend container** (``docker exec -i
agent-docker-demo-backend-1 python /tmp/p4/run_perf.py ...``) because that is
the only place both measurement targets are reachable at once:

  * ``http://openviking:1933`` — the service, for the unproxied baseline
  * ``http://127.0.0.1:8000/ov`` — the platform proxy the agent actually uses

Measuring the proxy hop from anywhere else would fold network differences into
a number that is supposed to be the proxy's own overhead.

Credentials are never embedded: pass ``OV_API_KEY`` (a real OpenViking User
Key) and ``OV_PROXY_TOKEN`` (an ``ovproxy:<uid>`` token for the same user).
The wrapper script derives both inside the container so no secret touches disk.

Suites
------
latency      P4-01/02/03  search percentiles, cold first call reported separately
find         P4-04        /search/find percentiles
proxy        P4-07        direct vs proxied, interleaved so drift cancels
concurrency  P4-08        N workers vs serial, P95 degradation ratio
write        P4-05        content/write throughput, async and wait=True
commit       P4-06        MCP remember -> recallable, end to end
scale        P4-09        grow the vector index, sample P95 at each checkpoint

Every suite prints a JSON blob on stdout and, with ``--out``, writes the same
blob to a file. Exit status is 0 unless the service never answered.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))

DIRECT = os.environ.get("OV_DIRECT_URL", "http://openviking:1933")
PROXY = os.environ.get("OV_PROXY_URL", "http://127.0.0.1:8000/ov")

# The user whose 40-document Chinese corpus run_eval.py seeded. Treated as
# read-only here: nothing in this harness writes into it, so the quality
# baseline stays reproducible after the performance run.
EVAL_USER = os.environ.get("OV_USER_ID", "ovzh-eval")

# Latency thresholds from the test plan (docs/openviking-integration-test-plan.md
# "Phase 4 — 性能基准"). Kept here so the harness can print PASS/FAIL itself
# instead of leaving the comparison to whoever reads the log.
LIMITS = {
    "search_p50_ms": 300.0,
    "search_p95_ms": 800.0,
    "search_p99_ms": 1500.0,
    "find_p95_ms": 500.0,
    "write_per_sec": 20.0,
    "commit_seconds": 30.0,
    "proxy_overhead_ms": 50.0,
    "concurrency_p95_ratio": 2.0,
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def pct(xs, q):
    """Nearest-rank percentile, the same convention run_eval.py uses."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(math.ceil(q * len(s))) - 1))
    return round(s[k], 1)


def stats(xs):
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": round(min(xs), 1),
        "p50": pct(xs, 0.50),
        "p95": pct(xs, 0.95),
        "p99": pct(xs, 0.99),
        "max": round(max(xs), 1),
        "mean": round(sum(xs) / len(xs), 1),
    }


def verdict(metric, value, better="lower"):
    limit = LIMITS.get(metric)
    if limit is None or value is None:
        return "n/a"
    ok = value <= limit if better == "lower" else value >= limit
    return "PASS" if ok else "FAIL"


def load_queries(path, limit=0):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


def headers_for(token, bearer=False):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        h["Authorization"] = "Bearer " + token
    else:
        h["X-API-Key"] = token
    return h


def emit(payload, out):
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# P4-01/02/03 — search latency
# --------------------------------------------------------------------------- #
def suite_latency(client, key, queries, n, warmup, mode):
    """Serial search latency. The cold call is measured, then thrown away."""
    hdr = headers_for(key)
    body_of = lambda q: (
        {"query": q, "mode": "context", "purpose": "coding"}
        if mode == "context"
        else {"query": q, "limit": 5}
    )

    # Cold: the first request after the process (and often the embedding model)
    # has been idle. Reported separately — averaging it into P50 would hide a
    # real first-hit cost, and hiding it in P50 would make P50 meaningless.
    t0 = time.perf_counter()
    r = client.post("/api/v1/search/search", json=body_of(queries[0]["query"]), headers=hdr)
    cold_ms = (time.perf_counter() - t0) * 1000.0
    cold_http = r.status_code

    errors = 0
    for i in range(warmup):
        q = queries[i % len(queries)]["query"]
        try:
            client.post("/api/v1/search/search", json=body_of(q), headers=hdr)
        except httpx.HTTPError:
            errors += 1

    samples, used_tokens, http_codes = [], [], {}
    for i in range(n):
        q = queries[i % len(queries)]["query"]
        t0 = time.perf_counter()
        try:
            r = client.post("/api/v1/search/search", json=body_of(q), headers=hdr)
        except httpx.HTTPError as exc:
            errors += 1
            print("  !! %s" % exc, file=sys.stderr)
            continue
        samples.append((time.perf_counter() - t0) * 1000.0)
        http_codes[r.status_code] = http_codes.get(r.status_code, 0) + 1
        if r.status_code == 200 and mode == "context":
            st = (r.json().get("result") or {}).get("stats") or {}
            if st.get("used_tokens") is not None:
                used_tokens.append(st["used_tokens"])

    s = stats(samples)
    return {
        "suite": "latency",
        "mode": mode,
        "endpoint": "/api/v1/search/search",
        "target": "direct",
        "n_requested": n,
        "warmup_discarded": warmup,
        "cold_first_ms": round(cold_ms, 1),
        "cold_first_http": cold_http,
        "http_codes": http_codes,
        "errors": errors,
        "latency_ms": s,
        "used_tokens": stats([float(x) for x in used_tokens]) if used_tokens else None,
        "checks": {
            "P4-01 search_p50<=300ms": [s.get("p50"), verdict("search_p50_ms", s.get("p50"))],
            "P4-02 search_p95<=800ms": [s.get("p95"), verdict("search_p95_ms", s.get("p95"))],
            "P4-03 search_p99<=1500ms": [s.get("p99"), verdict("search_p99_ms", s.get("p99"))],
        },
    }


# --------------------------------------------------------------------------- #
# P4-04 — /search/find (L0 abstract face)
# --------------------------------------------------------------------------- #
def result_items(res):
    """Hits out of a search/find response.

    Both endpoints group their hits under ``memories`` / ``resources`` /
    ``skills`` rather than a flat ``items`` list (same shape run_eval.py
    scores). Reading ``items`` silently yields nothing, which is how the smoke
    run ended up with an empty level histogram.
    """
    res = res or {}
    if isinstance(res.get("items"), list):
        return res["items"]
    out = []
    for grp in ("memories", "resources", "skills"):
        out.extend(res.get(grp) or [])
    return out


def suite_find(client, key, queries, n, warmup):
    hdr = headers_for(key)
    for i in range(warmup):
        client.post("/api/v1/search/find", json={"query": queries[i % len(queries)]["query"], "limit": 5}, headers=hdr)
    samples, http_codes, levels = [], {}, {}
    hits = 0
    for i in range(n):
        q = queries[i % len(queries)]["query"]
        t0 = time.perf_counter()
        r = client.post("/api/v1/search/find", json={"query": q, "limit": 5}, headers=hdr)
        samples.append((time.perf_counter() - t0) * 1000.0)
        http_codes[r.status_code] = http_codes.get(r.status_code, 0) + 1
        if r.status_code == 200:
            items = result_items((r.json().get("result") or {}))
            hits += len(items)
            for it in items:
                lv = it.get("level")
                levels[str(lv)] = levels.get(str(lv), 0) + 1
    s = stats(samples)
    return {
        "suite": "find",
        "endpoint": "/api/v1/search/find",
        "n": n,
        "http_codes": http_codes,
        "total_hits": hits,
        "avg_hits": round(hits / n, 2) if n else None,
        "levels_returned": levels,
        "latency_ms": s,
        "checks": {"P4-04 find_p95<=500ms": [s.get("p95"), verdict("find_p95_ms", s.get("p95"))]},
    }


# --------------------------------------------------------------------------- #
# P4-07 — proxy overhead, interleaved
# --------------------------------------------------------------------------- #
def suite_proxy(direct, proxy, key, token, queries, n):
    """Direct vs proxied on the *same* body, alternating.

    Interleaving matters: the two runs are minutes apart otherwise, and any
    warm-up or GC drift in the service lands entirely on whichever went second.
    The proxied call also carries the scaffold-exclusion rewrite, so it does
    slightly less upstream work — that is part of what is being measured, since
    it is what production actually pays.
    """
    dh = headers_for(key)
    ph = headers_for(token)
    body = None
    d_samples, p_samples = [], []
    d_codes, p_codes = {}, {}
    order = ["direct", "proxy"] * n
    random.Random(4).shuffle(order)
    for i, which in enumerate(order):
        q = queries[i % len(queries)]["query"]
        b = {"query": q, "mode": "context", "purpose": "coding"}
        body = b
        t0 = time.perf_counter()
        if which == "direct":
            r = direct.post("/api/v1/search/search", json=b, headers=dh)
            d_samples.append((time.perf_counter() - t0) * 1000.0)
            d_codes[r.status_code] = d_codes.get(r.status_code, 0) + 1
        else:
            r = proxy.post("/api/v1/search/search", json=b, headers=ph)
            p_samples.append((time.perf_counter() - t0) * 1000.0)
            p_codes[r.status_code] = p_codes.get(r.status_code, 0) + 1
    ds, ps = stats(d_samples), stats(p_samples)
    delta_p50 = None if not (ds.get("p50") and ps.get("p50")) else round(ps["p50"] - ds["p50"], 1)
    delta_mean = None if not (ds.get("mean") and ps.get("mean")) else round(ps["mean"] - ds["mean"], 1)
    return {
        "suite": "proxy",
        "endpoint": "/api/v1/search/search",
        "mode": "context",
        "interleaved": True,
        "n_each": n,
        "sample_body": body,
        "direct_http": d_codes,
        "proxy_http": p_codes,
        "direct_ms": ds,
        "proxy_ms": ps,
        "overhead_p50_ms": delta_p50,
        "overhead_mean_ms": delta_mean,
        "checks": {
            "P4-07 proxy_overhead_p50<=50ms": [delta_p50, verdict("proxy_overhead_ms", delta_p50)]
        },
    }


# --------------------------------------------------------------------------- #
# P4-08 — concurrency
# --------------------------------------------------------------------------- #
async def suite_concurrency(key, queries, serial_p95, workers, total):
    hdr = headers_for(key)
    samples = []
    codes = {}
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(workers)

    async def one(client, i):
        q = queries[i % len(queries)]["query"]
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.post("/api/v1/search/search", json={"query": q, "limit": 5}, headers=hdr)
                ms = (time.perf_counter() - t0) * 1000.0
                code = r.status_code
            except httpx.HTTPError:
                ms = (time.perf_counter() - t0) * 1000.0
                code = -1
        async with lock:
            samples.append(ms)
            codes[code] = codes.get(code, 0) + 1

    limits = httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers * 2)
    wall0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=DIRECT, timeout=120.0, limits=limits) as client:
        await asyncio.gather(*(one(client, i) for i in range(total)))
    wall = time.perf_counter() - wall0

    s = stats(samples)
    ratio = None
    if serial_p95 and s.get("p95"):
        ratio = round(s["p95"] / serial_p95, 2)
    return {
        "suite": "concurrency",
        "workers": workers,
        "total_requests": total,
        "wall_seconds": round(wall, 1),
        "throughput_rps": round(total / wall, 1) if wall else None,
        "http_codes": codes,
        "serial_p95_ms": serial_p95,
        "concurrent_ms": s,
        "p95_degradation_ratio": ratio,
        "checks": {
            "P4-08 p95_ratio<=2.0": [ratio, "n/a" if ratio is None else verdict("concurrency_p95_ratio", ratio)]
        },
    }


# --------------------------------------------------------------------------- #
# P4-05 — write throughput
# --------------------------------------------------------------------------- #
DOC_TEMPLATE = (
    "# {title}\n\n"
    "在 {svc} 服务上，我们约定{metric}的阈值为 {val}，超过则触发告警并自动扩容一个副本。"
    "该规则由平台组于 2026 年 {mon} 月评审通过，适用于生产与预发环境，测试环境可放宽两倍。"
    "变更需要走灰度发布流程，先在单个可用区观察十五分钟，再全量推进。"
)


def make_doc(i):
    return {
        "title": "性能探针记录 %04d" % i,
        "content": DOC_TEMPLATE.format(
            title="性能探针记录 %04d" % i,
            svc="probe-svc-%02d" % (i % 7),
            metric=["响应时间", "错误率", "队列积压", "连接数"][i % 4],
            val=(i % 40) * 5 + 10,
            mon=(i % 12) + 1,
        ),
    }


def suite_write(client, key, user, docs):
    """Two throughput numbers, because one number cannot describe this path.

    ``wait=False`` measures what the API accepts — the honest reading of "写入
    吞吐". ``wait=True`` additionally blocks until embedding finished, which is
    what a caller who needs read-after-write actually experiences. Neither is
    the LLM-backed ``remember`` path; that one is quoted from P3-01.
    """
    hdr = headers_for(key)
    out = {"suite": "write", "user": user, "docs": docs}

    for label, wait in (("async_wait_false", False), ("sync_wait_true", True)):
        samples, codes = [], {}
        t_wall = time.perf_counter()
        for i in range(docs):
            d = make_doc(i)
            uri = "viking://user/%s/memories/cases/%s.md" % (user, "p4w-%s-%04d" % (label[:5], i))
            body = {"uri": uri, "content": d["content"], "mode": "replace", "wait": wait}
            t0 = time.perf_counter()
            try:
                r = client.post("/api/v1/content/write", json=body, headers=hdr, timeout=180.0)
                code = r.status_code
            except httpx.HTTPError:
                code = -1
            samples.append((time.perf_counter() - t0) * 1000.0)
            codes[code] = codes.get(code, 0) + 1
        wall = time.perf_counter() - t_wall
        out[label] = {
            "wall_seconds": round(wall, 2),
            "writes_per_second": round(docs / wall, 2) if wall else None,
            "http_codes": codes,
            "per_call_ms": stats(samples),
        }

    rps = out["async_wait_false"].get("writes_per_second")
    out["checks"] = {
        "P4-05 write_per_sec>=20 (API accept, wait=false)": [rps, verdict("write_per_sec", rps, "higher")],
        "P4-05 write_per_sec>=20 (indexed, wait=true)": [
            out["sync_wait_true"].get("writes_per_second"),
            verdict("write_per_sec", out["sync_wait_true"].get("writes_per_second"), "higher"),
        ],
    }
    return out


# --------------------------------------------------------------------------- #
# P4-06 — remember -> recallable
# --------------------------------------------------------------------------- #
def mcp_call(client, key, name, arguments, timeout=180.0):
    """One MCP tools/call over streamable HTTP.

    Both Accept types are mandatory: with only ``application/json`` the server
    answers 406 (R20).
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    hdr = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": key,
    }
    r = client.post("/mcp", json=body, headers=hdr, timeout=timeout)
    text = r.text
    if text.lstrip().startswith("event:") or "\ndata:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    try:
        return r.status_code, json.loads(text)
    except ValueError:
        return r.status_code, {"raw": text[:400]}


def mcp_error(resp):
    """The tool-level error text, or None.

    A failed MCP tool call still comes back as HTTP 200 with a well-formed
    JSON-RPC result, so the status code proves nothing. Caught by the smoke run:
    passing ``text`` instead of ``messages`` produced a 200 plus a pydantic
    validation error, and the suite then polled a nonce that could never appear
    for the full timeout.
    """
    res = (resp or {}).get("result") or {}
    if res.get("isError"):
        parts = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict)]
        return " | ".join(p for p in parts if p)[:300] or "isError"
    for c in res.get("content") or []:
        t = c.get("text", "") if isinstance(c, dict) else ""
        if t.startswith("Error executing tool"):
            return t[:300]
    if (resp or {}).get("error"):
        return json.dumps(resp["error"], ensure_ascii=False)[:300]
    return None


def suite_commit(client, key, user, poll_interval, timeout_s):
    nonce = "p4commit%d" % int(time.time())
    # remember() takes a conversation, not a string: its input schema is
    # {"messages": [{"role": "user"|"assistant", "content": str}]} and
    # `messages` is required.
    text = (
        "请记住：性能探针 %s 的约定是，所有面向用户的中文错误提示必须先给出结论，"
        "再给出可执行的下一步，禁止只贴堆栈。" % nonce
    )
    arguments = {
        "messages": [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "已记录：%s 的错误提示约定为结论先行、附下一步、禁止裸堆栈。" % nonce},
        ]
    }
    t0 = time.perf_counter()
    code, resp = mcp_call(client, key, "remember", arguments)
    accept_ms = (time.perf_counter() - t0) * 1000.0
    tool_error = mcp_error(resp)

    recall_ms = None
    polls = 0
    while not tool_error and (time.perf_counter() - t0) < timeout_s:
        time.sleep(poll_interval)
        polls += 1
        r = client.post(
            "/api/v1/search/search",
            json={"query": "性能探针 %s 错误提示约定" % nonce, "limit": 10},
            headers=headers_for(key),
        )
        if r.status_code != 200:
            continue
        blob = json.dumps(r.json(), ensure_ascii=False)
        if nonce in blob:
            recall_ms = (time.perf_counter() - t0) * 1000.0
            break

    total_s = round((time.perf_counter() - t0), 1)
    return {
        "suite": "commit",
        "user": user,
        "nonce": nonce,
        "remember_http": code,
        "remember_accepted_ms": round(accept_ms, 1),
        "remember_tool_error": tool_error,
        "remember_response": json.dumps(resp, ensure_ascii=False)[:300],
        "poll_interval_s": poll_interval,
        "polls": polls,
        "recallable_after_s": None if recall_ms is None else round(recall_ms / 1000.0, 1),
        "elapsed_s": total_s,
        "checks": {
            "P4-06 commit<=30s": [
                None if recall_ms is None else round(recall_ms / 1000.0, 1),
                verdict("commit_seconds", None if recall_ms is None else recall_ms / 1000.0),
            ]
        },
    }


# --------------------------------------------------------------------------- #
# P4-09 — scale curve
# --------------------------------------------------------------------------- #
def vector_count(client, key):
    try:
        r = client.get("/api/v1/debug/vector/count", headers=headers_for(key), timeout=30.0)
        if r.status_code == 200:
            return (r.json().get("result") or {}).get("count")
    except (httpx.HTTPError, ValueError):
        pass
    return None


def measure_p95(client, key, queries, n):
    hdr = headers_for(key)
    xs = []
    for i in range(n):
        t0 = time.perf_counter()
        try:
            client.post("/api/v1/search/search", json={"query": queries[i % len(queries)]["query"], "limit": 5}, headers=hdr)
        except httpx.HTTPError:
            continue
        xs.append((time.perf_counter() - t0) * 1000.0)
    return stats(xs)


def suite_scale(client, key, user, queries, checkpoints, sample_n):
    """Grow one user's space and sample P95 at each checkpoint.

    The index is shared across users, so this also grows the total vector count
    the service has to search — which is the thing the test plan actually wants
    a curve for. Retrieval stays scoped to this user's root, so the *quality*
    baseline on ``ovzh-eval`` is untouched.

    Writes go through a thread pool over one sync client rather than asyncio:
    the sampling helpers below are synchronous, and mixing an AsyncClient into
    them silently returns coroutines instead of responses.
    """
    hdr = headers_for(key)
    rows = []
    written = 0

    rows.append(
        {
            "checkpoint": 0,
            "docs_written": 0,
            "vector_count": vector_count(client, key),
            "latency_ms": measure_p95(client, key, queries, sample_n),
        }
    )
    for cp in checkpoints:
        need = cp - written

        def put(i):
            d = make_doc(i)
            uri = "viking://user/%s/memories/cases/p4scale-%05d.md" % (user, i)
            try:
                client.post(
                    "/api/v1/content/write",
                    json={"uri": uri, "content": d["content"], "mode": "replace", "wait": False},
                    headers=hdr,
                    timeout=180.0,
                ).status_code
            except httpx.HTTPError:
                return -1
            return 0

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(put, range(written, cp)))
        write_wall = time.perf_counter() - t0
        written = cp

        # wait for vector/count to stop moving before sampling, otherwise the
        # latency numbers describe a service that is also busy indexing
        vc, stable = vector_count(client, key), 0
        last = None
        for _ in range(60):
            if vc == last:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            last = vc
            time.sleep(3)
            vc = vector_count(client, key)

        rows.append(
            {
                "checkpoint": cp,
                "docs_written": written,
                "batch_write_wall_s": round(write_wall, 1),
                "batch_writes_per_sec": round(need / write_wall, 1) if write_wall else None,
                "vector_count": vc,
                "latency_ms": measure_p95(client, key, queries, sample_n),
            }
        )
    return {"suite": "scale", "user": user, "checkpoints": checkpoints,
            "samples_per_checkpoint": sample_n, "rows": rows}


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="latency",
                    choices=["latency", "find", "proxy", "concurrency", "write", "commit", "scale"])
    ap.add_argument("--queries", default=os.path.join(HERE, "queries.jsonl"))
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--mode", default="list", choices=["list", "context"])
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--serial-p95", type=float, default=0.0)
    ap.add_argument("--docs", type=int, default=40)
    ap.add_argument("--write-user", default="p4-perf-write")
    # remember() is LLM-backed and files its output wherever the extractor
    # decides, so it must not run against the corpus that the quality baseline
    # (run_eval.py) reproduces from.
    ap.add_argument("--commit-user", default="p4-commit")
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--timeout-s", type=float, default=180.0)
    ap.add_argument("--scale-user", default="p4-scale")
    ap.add_argument("--checkpoints", default="100,200,300")
    ap.add_argument("--batch-samples", type=int, default=30)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    key = os.environ.get("OV_API_KEY", "")
    token = os.environ.get("OV_PROXY_TOKEN", "")
    if not key:
        print("FATAL: OV_API_KEY not set", file=sys.stderr)
        return 2

    queries = load_queries(args.queries)
    base = {"label": args.label or args.suite, "suite": args.suite,
            "direct_url": DIRECT, "proxy_url": PROXY, "eval_user": EVAL_USER,
            "queries_n": len(queries), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}

    with httpx.Client(base_url=DIRECT, timeout=120.0,
                      limits=httpx.Limits(max_connections=32, max_keepalive_connections=32)) as client:
        r = client.get("/health", headers=headers_for(key))
        base["health"] = {"http": r.status_code, "body": r.text[:120]}
        base["vector_count_before"] = vector_count(client, key)

        if args.suite == "latency":
            payload = suite_latency(client, key, queries, args.n, args.warmup, args.mode)
        elif args.suite == "find":
            payload = suite_find(client, key, queries, args.n, args.warmup)
        elif args.suite == "proxy":
            if not token:
                print("FATAL: OV_PROXY_TOKEN not set", file=sys.stderr)
                return 2
            with httpx.Client(base_url=PROXY, timeout=120.0) as pclient:
                payload = suite_proxy(client, pclient, key, token, queries, args.n)
        elif args.suite == "concurrency":
            payload = asyncio.run(suite_concurrency(key, queries, args.serial_p95, args.workers, args.n))
        elif args.suite == "write":
            payload = suite_write(client, key, args.write_user, args.docs)
        elif args.suite == "commit":
            payload = suite_commit(client, key, args.commit_user, args.poll_interval, args.timeout_s)
        elif args.suite == "scale":
            cps = [int(x) for x in args.checkpoints.split(",") if x.strip()]
            payload = suite_scale(client, key, args.scale_user, queries, cps, args.batch_samples)
        else:
            print("FATAL: unknown suite", file=sys.stderr)
            return 2

        base["vector_count_after"] = vector_count(client, key)

    emit({**base, **payload}, args.out)
    print("PERF-DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
