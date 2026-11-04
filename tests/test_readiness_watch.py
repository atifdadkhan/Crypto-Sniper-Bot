"""No-network tests for a read-only external deployment checker."""
from monitoring.readiness_check import verify_health, verify_readiness, check_once, main


def healthy_payload():
    return {"status": "ok", "mode": "signals_only",
            "service": "A&T TrendHive Crypto Sniper"}


def ready_payload():
    return {"status": "ready", "scanner_fresh": True, "last_error": None,
            "last_scan_age_seconds": 312,
            "scan": {"symbols": 80, "universe": 312, "scan_batch": 80,
                     "four_hour_watch": 20, "signals": 0,
                     "core15m_queued": 0, "rs4h_queued": 0,
                     "slow4h_queued": 0}}


def test_healthy_scanner_with_zero_signals_is_success():
    assert verify_health(healthy_payload()) == []
    assert verify_readiness(ready_payload()) == []


def test_health_must_be_expected_signals_only_bot():
    bad = healthy_payload()
    bad["mode"] = "auto_trading"
    assert "signals-only" in " ".join(verify_health(bad))
    bad = healthy_payload()
    bad["service"] = "other project"
    assert "identity" in " ".join(verify_health(bad))


def test_readiness_rejects_missing_watch_or_stale_scan():
    bad = ready_payload()
    bad["last_scan_age_seconds"] = 1800
    bad["scanner_fresh"] = False
    bad["scan"]["four_hour_watch"] = 0
    issues = verify_readiness(bad)
    assert len(issues) == 3
    bad = ready_payload()
    bad["last_error"] = "All sampled markets failed data checks"
    assert "last_error" in " ".join(verify_readiness(bad))


def test_readiness_rejects_empty_scan_counts():
    bad = ready_payload()
    bad["scan"]["symbols"] = 0
    bad["scan"]["universe"] = 0
    bad["scan"]["scan_batch"] = -1
    issues = verify_readiness(bad)
    assert len(issues) == 3


def test_external_status_is_verified_not_invented(monkeypatch):
    def fake_fetch(url, timeout_seconds=20):
        if url.endswith("/health"):
            return 200, healthy_payload()
        return 200, ready_payload()
    monkeypatch.setattr("monitoring.readiness_check.fetch_json", fake_fetch)
    data = check_once()
    assert data["issues"] == []
    assert data["alerts_delivered_last_scan"] == 0
    assert main(["--attempts", "1"]) == 0


def test_http_503_degraded_is_failure_even_without_exception(monkeypatch):
    def fake_fetch(url, timeout_seconds=20):
        if url.endswith("/health"):
            return 200, healthy_payload()
        data = ready_payload()
        data["status"] = "degraded"
        return 503, data
    monkeypatch.setattr("monitoring.readiness_check.fetch_json", fake_fetch)
    assert "ready HTTP status 503" in check_once()["issues"]
    assert main(["--attempts", "1"]) == 1
