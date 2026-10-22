"""scopes.py: window resolution, scope labels and the per-language analysis gate."""

import importlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scopes  # noqa: E402

DAY = 86400
LAUNCH = int(datetime(2020, 9, 17, tzinfo=timezone.utc).timestamp())
RELEASES = [
    {"gid": "launch", "title": "Initial release", "date": LAUNCH},
    {"gid": "build_100", "title": "Build 100", "date": LAUNCH + 400 * DAY},
    {"gid": "build_200", "title": "Build 200", "date": LAUNCH + 800 * DAY},
]


# --- no Flask, same defaults as .env.example ------------------------------------------

def test_scopes_does_not_import_flask():
    code = "import sys, scopes; sys.exit(1 if 'flask' in sys.modules else 0)"
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_default_thresholds_match_env_example(monkeypatch):
    monkeypatch.delenv("STEAM_LANG_MIN_VOLUME", raising=False)
    monkeypatch.delenv("STEAM_LANG_MIN_DIVERGENCE", raising=False)
    fresh = importlib.reload(scopes)
    try:
        assert fresh.LANG_MIN_VOLUME == 500
        assert fresh.LANG_MIN_DIVERGENCE == 8.0
    finally:
        monkeypatch.undo()
        importlib.reload(scopes)


def test_thresholds_come_from_env(monkeypatch):
    monkeypatch.setenv("STEAM_LANG_MIN_VOLUME", "123")
    monkeypatch.setenv("STEAM_LANG_MIN_DIVERGENCE", "2.5")
    fresh = importlib.reload(scopes)
    try:
        assert (fresh.LANG_MIN_VOLUME, fresh.LANG_MIN_DIVERGENCE) == (123, 2.5)
    finally:
        monkeypatch.undo()
        importlib.reload(scopes)


# --- _resolve_window --------------------------------------------------------------------

def test_resolve_window_all_is_unbounded():
    assert scopes._resolve_window(RELEASES, "all") == (None, None)


def test_resolve_window_runs_to_the_next_release():
    assert scopes._resolve_window(RELEASES, "build_100") == (LAUNCH + 400 * DAY, LAUNCH + 800 * DAY)


def test_resolve_window_latest_release_is_open_ended():
    assert scopes._resolve_window(RELEASES, "build_200") == (LAUNCH + 800 * DAY, None)


def test_resolve_window_orders_releases_by_date_not_input_order():
    shuffled = [RELEASES[2], RELEASES[0], RELEASES[1]]
    assert scopes._resolve_window(shuffled, "launch") == (LAUNCH, LAUNCH + 400 * DAY)


def test_resolve_window_unknown_scope_falls_back_to_all():
    assert scopes._resolve_window(RELEASES, "nope") == (None, None)


# --- _scope_label -----------------------------------------------------------------------

def test_scope_label_all_time_warns_against_launch_framing():
    label = scopes._scope_label(RELEASES, "all", "build_200", None)
    assert "2020-09-17" in label and "NOT a launch snapshot" in label


def test_scope_label_all_time_without_releases():
    assert scopes._scope_label([], "all", None, None).startswith("All reviews to date")


def test_scope_label_launch_window():
    label = scopes._scope_label(RELEASES, "launch", "build_200", LAUNCH + 400 * DAY)
    assert "initial launch window" in label
    assert "(the next release)" in label


def test_scope_label_post_launch_latest_release():
    label = scopes._scope_label(RELEASES, "build_200", "build_200", None)
    assert "post-launch update, NOT the launch" in label
    assert "~2.2 years" in label
    assert "current/latest release" in label
    assert "runs to the present" in label


def test_scope_label_unknown_scope():
    assert "Release window 'zzz'" in scopes._scope_label(RELEASES, "zzz", None, None)


# --- _language_eligibility ---------------------------------------------------------------

@pytest.fixture
def thresholds(monkeypatch):
    monkeypatch.setattr(scopes, "LANG_MIN_VOLUME", 500)
    monkeypatch.setattr(scopes, "LANG_MIN_DIVERGENCE", 8.0)


def test_language_needs_volume_and_divergence(thresholds):
    breakdown = [
        ("english", 10000, 8000),   # 80% -> sets the blend
        ("german", 600, 300),       # 50%: big enough, diverges -> eligible
        ("french", 600, 480),       # 80%: big enough, no divergence
        ("polish", 100, 10),        # diverges, too small
    ]
    out = scopes._language_eligibility(breakdown)
    stats = out["lang_stats"]
    assert stats["german"]["eligible"] is True
    assert stats["french"]["eligible"] is False
    assert stats["polish"]["eligible"] is False
    assert out["analysis_languages"] == ["all", "german"]
    assert out["languages"] == ["english", "german", "french", "polish"]  # ordered by volume
    assert out["thresholds"] == {"min_volume": 500, "min_divergence": 8.0}


def test_volume_threshold_is_inclusive_and_default_is_500(thresholds):
    # 500 reviews qualifies at the corrected default of 500 (it did not at the old 1000).
    out = scopes._language_eligibility([("english", 5000, 4000), ("german", 500, 200)])
    assert out["lang_stats"]["german"]["eligible"] is True


def test_chinese_bucket_replaces_variants_for_analysis(thresholds):
    breakdown = [
        ("english", 10000, 8000),
        ("schinese", 400, 100),     # each below 500, together 800 -> bucket qualifies
        ("tchinese", 400, 100),
    ]
    out = scopes._language_eligibility(breakdown)
    assert out["lang_stats"]["chinese"]["count"] == 800
    assert out["lang_stats"]["chinese"]["eligible"] is True
    assert out["analysis_languages"] == ["all", "chinese"]
    assert out["languages"] == ["english", "schinese", "tchinese", "chinese"]


def test_empty_breakdown(thresholds):
    out = scopes._language_eligibility([])
    assert out["analysis_languages"] == ["all"]
    assert out["lang_stats"] == {"_blend": 0.0}


def test_app_uses_the_scopes_functions():
    pytest.importorskip("flask")
    pytest.importorskip("anthropic")
    import app

    # (compare by origin: other tests reload scopes, which rebinds its function objects)
    for fn in (app._resolve_window, app._scope_label, app._language_eligibility):
        assert fn.__module__ == "scopes"
