"""Read-only external health monitor of the deployed Crypto Sniper service.

Run from GitHub Actions or a terminal; never loads Telegram token or Bitget
credentials, never sends a message or places orders. Alert counts can be zero
without being unhealthy. Check identity, liveness, and scanner freshness.
"""
import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://crypto-sniper-bot-1fej.onrender.com"


def verify_health(payload):
    problems = []
    if not isinstance(payload, dict):
        return ["health did not return a JSON object"]
    if payload.get("status") != "ok":
        problems.append("health status is not ok")
    if payload.get("service") != "A&T TrendHive Crypto Sniper":
        problems.append("unexpected service identity")
    if payload.get("mode") != "signals_only":
        problems.append("service is not in signals-only mode")
    return problems


def verify_readiness(payload):
    problems = []
    if not isinstance(payload, dict):
        return ["ready did not return a JSON object"]
    if payload.get("status") != "ready" or payload.get("scanner_fresh") is not True:
        problems.append("scanner is not ready/fresh")
    if payload.get("last_error"):
        problems.append("scanner last_error is set")
    age = payload.get("last_scan_age_seconds")
    if not isinstance(age, (int, float)) or isinstance(age, bool) or not 0 <= age < 1800:
        problems.append("latest completed scan is missing/stale (>= 30m)")
    scan = payload.get("scan")
    if not isinstance(scan, dict):
        return problems + ["scan statistics missing"]
    for key in ("symbols", "universe", "scan_batch"):
        n = scan.get(key)
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            problems.append(f"{key} missing or nonpositive")
    watch = scan.get("four_hour_watch")
    if not isinstance(watch, int) or isinstance(watch, bool) or watch < 1 or watch > 20:
        problems.append("4H top-20 watch is not reporting a valid count")
    # No alert is a normal possible outcome: zero alerts is NOT an error.
    return problems


def fetch_json(url, timeout_seconds=20):
    request = Request(url, headers={"User-Agent": "TrendHive-Readiness-Watch/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.getcode(), json.load(response)
    except HTTPError as exc:
        # /ready legitimately returns HTTP 503 when scanner is degraded.
        try:
            return exc.code, json.load(exc)
        except (ValueError, OSError):
            return exc.code, {}


def check_once(base_url=DEFAULT_BASE_URL):
    base_url = base_url.rstrip("/")
    health_code, health = fetch_json(base_url + "/health")
    ready_code, ready = fetch_json(base_url + "/ready")
    issues = verify_health(health) + verify_readiness(ready)
    if health_code != 200:
        issues.append(f"health HTTP status {health_code}")
    if ready_code != 200:
        issues.append(f"ready HTTP status {ready_code}")
    summary = {
        "health_http": health_code, "ready_http": ready_code,
        "scan_age_s": ready.get("last_scan_age_seconds") if isinstance(ready, dict) else None,
        "markets_scanned": (ready.get("scan") or {}).get("symbols") if isinstance(ready, dict) else None,
        "four_hour_watch": (ready.get("scan") or {}).get("four_hour_watch") if isinstance(ready, dict) else None,
        "alerts_delivered_last_scan": (ready.get("scan") or {}).get("signals") if isinstance(ready, dict) else None,
        "issues": issues,
    }
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--delay", type=int, default=20)
    args = parser.parse_args(argv)
    if not args.base_url.startswith("https://"):
        parser.error("Public production URL must use https")
    if not 1 <= args.attempts <= 5 or not 0 <= args.delay <= 120:
        parser.error("attempts must be 1..5, delay 0..120")
    summary = {}
    for attempt in range(args.attempts):
        try:
            summary = check_once(args.base_url)
        except (URLError, OSError, ValueError, TimeoutError) as exc:
            summary = {"issues": [f"endpoint unavailable: {type(exc).__name__}"]}
        if not summary["issues"]:
            print("PASS: external Crypto Sniper scanner is ready")
            print(json.dumps(summary, indent=2))
            return 0
        print(f"Attempt {attempt + 1}/{args.attempts}: " + ", ".join(summary["issues"]))
        if attempt < args.attempts - 1:
            time.sleep(args.delay)
    print("FAIL: Crypto Sniper service health or scanner readiness", file=sys.stderr)
    print(json.dumps(summary, indent=2), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
