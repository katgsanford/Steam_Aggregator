"""models.py: the narrative role, structured-output params, and usage accounting;
plus the analyze.py call sites that use them. No network: the Anthropic client is faked."""

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("anthropic")
pytest.importorskip("pydantic")

import analyze  # noqa: E402
import models  # noqa: E402


@pytest.fixture(autouse=True)
def clean_models(monkeypatch):
    """Isolate module state: no env overrides, no discovery, empty usage log and hooks."""
    for role in models.ROLES.values():
        monkeypatch.delenv(role["env"], raising=False)
        monkeypatch.delenv(role["effort_env"], raising=False)
    monkeypatch.delenv("STEAM_REVIEW_MODEL", raising=False)
    monkeypatch.setattr(models, "AUTO", False)
    monkeypatch.setattr(models, "_resolved", {})
    monkeypatch.setattr(models, "_warned", set())
    monkeypatch.setattr(models, "_session_usage", [])
    monkeypatch.setattr(models, "_usage_hooks", [])


# --- the narrative role ---------------------------------------------------------------------

def test_narrative_role_is_the_opus_family_at_medium_effort():
    spec = models.ROLES["narrative"]
    assert spec["family"] == "claude-opus"
    assert spec["pinned"] == "claude-opus-5-5"
    assert (spec["env"], spec["effort_env"]) == ("STEAM_NARRATIVE_MODEL", "STEAM_NARRATIVE_EFFORT")
    assert models.effort("narrative") == "medium"
    assert models.resolve("narrative") == "claude-opus-5-5"


def test_narrative_env_overrides(monkeypatch):
    monkeypatch.setenv("STEAM_NARRATIVE_MODEL", "claude-opus-9-9")
    monkeypatch.setenv("STEAM_NARRATIVE_EFFORT", "HIGH")
    assert models.resolve("narrative") == "claude-opus-9-9"
    assert models.effort("narrative") == "high"
    assert models.resolve("summary") == "claude-opus-5-5"  # the other roles are unaffected


def test_bad_narrative_effort_falls_back_with_a_warning(monkeypatch, capsys):
    monkeypatch.setenv("STEAM_NARRATIVE_EFFORT", "turbo")
    assert models.effort("narrative") == "medium"
    assert "STEAM_NARRATIVE_EFFORT" in capsys.readouterr().out


def test_narrative_discovery_picks_the_newest_undated_opus(monkeypatch):
    monkeypatch.setattr(models, "AUTO", True)
    monkeypatch.setattr(models, "_listed", [
        ("claude-opus-5-6-20260101", "2026-01-01"), ("claude-haiku-5-6", "2026-01-01"),
        ("claude-opus-5-6", "2026-01-01"), ("claude-opus-5-5", "2025-12-01")])
    assert models.resolve("narrative", client=object()) == "claude-opus-5-6"


def test_existing_roles_are_unchanged():
    assert models.ROLES["extract"]["pinned"] == "claude-haiku-5-5" and models.effort("extract") == "low"
    assert models.ROLES["summary"]["pinned"] == "claude-opus-5-5" and models.effort("summary") == "high"


# --- request_params / structured output ----------------------------------------------------------

def test_request_params_without_schema_is_thinking_and_effort():
    assert models.request_params("narrative", "claude-opus-5-5") == {
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}


def test_request_params_skips_thinking_and_effort_on_old_models():
    assert models.request_params("extract", "claude-haiku-4-5") == {}
    assert models.request_params("extract", "mystery-model") == {}


def test_schema_joins_the_effort_in_one_output_config():
    params = models.request_params("extract", "claude-haiku-5-5", schema=analyze.ThemeList)
    cfg = params["output_config"]
    assert cfg["effort"] == "low"
    assert cfg["format"]["type"] == "json_schema"
    schema = cfg["format"]["schema"]
    assert schema["additionalProperties"] is False and "themes" in schema["properties"]
    assert params["thinking"] == {"type": "adaptive"}
    assert "output_format" not in params


def test_schema_on_an_old_model_sends_only_the_format():
    params = models.request_params("extract", "claude-haiku-4-5", schema=analyze.ThemeList)
    assert list(params) == ["output_config"] and list(params["output_config"]) == ["format"]


def test_format_matches_what_the_sdk_builds_for_output_format():
    # parse(output_format=T) used to build the format; the explicit config must be identical.
    from anthropic import transform_schema
    from pydantic import TypeAdapter

    expected = {"type": "json_schema", "schema": transform_schema(TypeAdapter(analyze.ThemeList).json_schema())}
    assert models.json_format(analyze.ThemeList) == expected


def fake_message(text, usage=None):
    block = types.SimpleNamespace(type="text", text=text)
    usage = usage or types.SimpleNamespace(input_tokens=1000, output_tokens=500,
                                           cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="thinking", thinking="..."), block],
                                 usage=usage)


THEMES_JSON = json.dumps({"themes": [
    {"label": "L", "description": "d", "mention_estimate": 3, "representative_quote": "q"}]})


def test_parse_json_validates_the_text_blocks():
    parsed = models.parse_json(fake_message(THEMES_JSON), analyze.ThemeList)
    assert parsed.themes[0].label == "L"


def test_parse_json_with_no_text_is_none_and_bad_json_raises():
    assert models.parse_json(types.SimpleNamespace(content=[]), analyze.ThemeList) is None
    with pytest.raises(Exception):
        models.parse_json(fake_message('{"themes": [{"label"'), analyze.ThemeList)  # truncated


# --- cost and usage accounting -------------------------------------------------------------------

def test_cost_usd_uses_the_family_price_table():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert models.cost_usd("claude-opus-5-5", usage) == pytest.approx(4.00 + 20.00 + 0.20)
    assert models.cost_usd("claude-haiku-5-5", usage) == pytest.approx(0.10 + 0.50 + 0.01)


def test_cost_usd_prices_cache_writes_at_1_25x_input():
    assert models.cost_usd("claude-opus-5-5", {"cache_creation_input_tokens": 1_000_000}) == pytest.approx(5.00)


def test_unknown_model_is_priced_at_the_highest_rate(capsys):
    assert models.cost_usd("claude-newthing-9", {"output_tokens": 1_000_000}) == pytest.approx(20.00)
    assert "no price for model" in capsys.readouterr().out


def test_cost_usd_accepts_sdk_objects_and_missing_fields():
    usage = types.SimpleNamespace(input_tokens=2000, output_tokens=None)
    assert models.cost_usd("claude-opus-5-5", usage) == pytest.approx(0.008)


def usage_db(with_table=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if with_table:
        # Section 4.6 of docs/AGENT_PLAN.md
        conn.execute("""create table llm_usage (
            id integer primary key autoincrement, ts integer, report_date text, purpose text,
            model text, input_tokens integer, output_tokens integer, cache_read_tokens integer,
            cost_usd real)""")
    return conn


def test_record_usage_writes_the_llm_usage_row():
    conn = usage_db()
    usage = types.SimpleNamespace(input_tokens=1000, output_tokens=500, cache_read_input_tokens=200,
                                  cache_creation_input_tokens=0)
    row = models.record_usage("narrative", "claude-opus-5-5", usage, conn=conn, report_date="2026-10-23")
    stored = dict(conn.execute("select * from llm_usage").fetchone())
    assert stored == {"id": 1, "ts": row["ts"], "report_date": "2026-10-23", "purpose": "narrative",
                      "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 500,
                      "cache_read_tokens": 200, "cost_usd": pytest.approx(0.01404)}


def test_record_usage_tolerates_a_missing_table(capsys):
    conn = usage_db(with_table=False)
    row = models.record_usage("dashboard", "claude-haiku-5-5", {"input_tokens": 10, "output_tokens": 10}, conn=conn)
    assert row["purpose"] == "dashboard"
    assert models.session_cost("dashboard") == pytest.approx(row["cost_usd"])  # still counted in memory
    assert "llm_usage table does not exist" in capsys.readouterr().out
    conn.execute("create table x (a)")  # the connection is still usable


def test_record_usage_never_raises_on_other_write_errors():
    class BrokenConn:
        def execute(self, *a):
            raise RuntimeError("disk full")

    models.record_usage("dashboard", "claude-haiku-5-5", {"input_tokens": 1}, conn=BrokenConn())


def test_record_usage_without_a_connection_only_tracks_in_memory():
    models.record_usage("backfill", "claude-opus-5-5", {"output_tokens": 1_000_000})
    models.record_usage("dashboard", "claude-opus-5-5", {"output_tokens": 500_000})
    assert models.session_cost() == pytest.approx(30.0)
    assert models.session_cost("backfill") == pytest.approx(20.0)


def test_hooks_see_every_row_and_a_failing_hook_is_isolated():
    seen = []
    models.add_usage_hook(lambda row: 1 / 0)
    models.add_usage_hook(seen.append)
    models.record_usage("narrative", "claude-opus-5-5", {"input_tokens": 1})
    assert [r["purpose"] for r in seen] == ["narrative"]
    models.clear_usage_hooks()
    models.record_usage("narrative", "claude-opus-5-5", {"input_tokens": 1})
    assert len(seen) == 1


def test_unknown_purpose_warns_but_records(capsys):
    models.record_usage("typo", "claude-opus-5-5", {"input_tokens": 1})
    assert "unknown usage purpose" in capsys.readouterr().out
    assert len(models._session_usage) == 1


# --- the analyze.py call sites ---------------------------------------------------------------

class FakeClient:
    """Records every messages.create call and replies with canned text."""

    def __init__(self, text):
        self.calls = []
        self.text = text
        self.messages = types.SimpleNamespace(create=self._create, parse=self._no_parse)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return fake_message(self.text)

    def _no_parse(self, **kwargs):
        raise AssertionError("analyze must not use messages.parse / output_format any more")


def test_extract_themes_sends_effort_and_format_and_logs_usage():
    conn = usage_db()
    client = FakeClient(THEMES_JSON)
    themes = analyze._extract_themes(client, "Game", ["great", "fun"], "positive", purpose="backfill", conn=conn)
    assert themes[0]["label"] == "L"
    call = client.calls[0]
    assert "output_format" not in call
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["thinking"] == {"type": "adaptive"}
    row = conn.execute("select purpose, model, input_tokens from llm_usage").fetchone()
    assert (row["purpose"], row["model"], row["input_tokens"]) == ("backfill", "claude-haiku-5-5", 1000)


def test_update_themes_sends_effort_and_format_and_logs_usage():
    conn = usage_db()
    client = FakeClient(THEMES_JSON)
    prev = [{"label": "old", "description": "", "mention_estimate": 1, "representative_quote": ""}]
    themes = analyze._update_themes(client, "Game", prev, ["new review"], "negative", purpose="dashboard", conn=conn)
    assert themes[0]["label"] == "L"
    call = client.calls[0]
    assert call["output_config"]["effort"] == "low" and "format" in call["output_config"]
    assert conn.execute("select purpose from llm_usage").fetchone()["purpose"] == "dashboard"


def test_update_themes_keeps_the_previous_themes_when_there_is_nothing_new():
    client = FakeClient(THEMES_JSON)
    prev = [{"label": "old"}]
    assert analyze._update_themes(client, "Game", prev, [], "positive", purpose="dashboard") == prev
    assert client.calls == []


def test_write_summary_logs_usage_with_the_purpose():
    conn = usage_db()
    client = FakeClient("A short summary.")
    text = analyze._write_summary(client, "product", "Game", "All time", {"total_reviews": 10}, [], [],
                                  purpose="backfill_lazy", conn=conn)
    assert text == "A short summary."
    assert client.calls[0]["output_config"] == {"effort": "high"}  # a text call: effort only, no format
    row = conn.execute("select purpose, model from llm_usage").fetchone()
    assert (row["purpose"], row["model"]) == ("backfill_lazy", "claude-opus-5-5")


def test_get_analysis_labels_every_call_with_its_purpose(monkeypatch, tmp_path):
    import db

    conn = db.connect(tmp_path / "t.db")
    db.upsert_product(conn, 1, "Game", ["english"])
    for i in range(30):
        db.upsert_review(conn, 1, "english", {
            "recommendationid": f"r{i}", "author": {}, "voted_up": i % 2, "review": f"review {i}",
            "timestamp_created": 1_700_000_000 + i, "timestamp_updated": 0, "votes_up": i}, 1)
    conn.commit()

    texts = iter([THEMES_JSON, THEMES_JSON, "product summary", "qa summary"])
    client = types.SimpleNamespace(messages=types.SimpleNamespace(
        create=lambda **kw: fake_message(next(texts))))
    monkeypatch.setattr(analyze, "_client", lambda: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    analyze.get_analysis(conn, 1, scope="all", language_filter="all", ts_from=None, ts_to=None,
                         product_name="Game", include_recent=False, purpose="backfill")
    purposes = [r["purpose"] for r in conn.execute("select purpose from llm_usage")]
    assert purposes == ["backfill"] * 4

    # and the dashboard default
    texts = iter([THEMES_JSON, THEMES_JSON, "product summary", "qa summary"])
    analyze.get_analysis(conn, 1, scope="other", language_filter="all", ts_from=None, ts_to=None,
                         product_name="Game", include_recent=False)
    assert [r["purpose"] for r in conn.execute("select purpose from llm_usage")][4:] == ["dashboard"] * 4
    conn.close()
