import argparse
import csv
import json
import statistics
import time
import urllib.request
import urllib.error


def _request(url, method="GET", payload=None, timeout=10):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            elapsed = (time.perf_counter() - start) * 1000
            return True, resp.status, elapsed, ""
    except urllib.error.HTTPError as e:
        elapsed = (time.perf_counter() - start) * 1000
        return False, e.code, elapsed, str(e)
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return False, "ERR", elapsed, str(e)


def _percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    k = int(round((p / 100.0) * (len(values) - 1)))
    k = max(0, min(k, len(values) - 1))
    return values[k]


def main():
    parser = argparse.ArgumentParser(description="Simple local perf test for Flask app")
    parser.add_argument("--base", default="http://127.0.0.1:5000", help="Base URL")
    parser.add_argument("--rounds", type=int, default=20, help="Requests per endpoint")
    parser.add_argument("--timeout", type=int, default=10, help="Timeout seconds")
    parser.add_argument("--out", default="analysis/perf_results.csv", help="Per-request CSV")
    parser.add_argument("--summary", default="analysis/perf_summary.csv", help="Summary CSV")
    args = parser.parse_args()

    endpoints = [
        ("page_data", "GET", "/data", None),
        ("api_data_summary", "GET", "/api/data/summary", None),
        ("api_data_list", "GET", "/api/data/list", None),
        ("api_eis_files", "GET", "/api/eis/files", None),
        ("api_stats_files", "GET", "/api/stats/files", None),
        ("api_ml_datasets", "GET", "/api/ml/datasets", None),
    ]

    rows = []
    for name, method, path, payload in endpoints:
        url = args.base.rstrip("/") + path
        for i in range(args.rounds):
            ok, status, ms, err = _request(url, method=method, payload=payload, timeout=args.timeout)
            rows.append({
                "endpoint": name,
                "method": method,
                "path": path,
                "ok": int(ok),
                "status": status,
                "ms": round(ms, 2),
                "error": err,
            })

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["endpoint", "method", "path", "ok", "status", "ms", "error"])
        w.writeheader()
        w.writerows(rows)

    summary = []
    by_ep = {}
    for r in rows:
        by_ep.setdefault(r["endpoint"], []).append(r)

    for ep, items in by_ep.items():
        times = [r["ms"] for r in items]
        ok_count = sum(r["ok"] for r in items)
        fail_count = len(items) - ok_count
        summary.append({
            "endpoint": ep,
            "count": len(items),
            "ok": ok_count,
            "fail": fail_count,
            "avg_ms": round(statistics.mean(times), 2) if times else 0.0,
            "p95_ms": round(_percentile(times, 95), 2) if times else 0.0,
            "min_ms": round(min(times), 2) if times else 0.0,
            "max_ms": round(max(times), 2) if times else 0.0,
        })

    with open(args.summary, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["endpoint", "count", "ok", "fail", "avg_ms", "p95_ms", "min_ms", "max_ms"])
        w.writeheader()
        w.writerows(summary)

    print("Wrote:")
    print("  " + args.out)
    print("  " + args.summary)


if __name__ == "__main__":
    main()
