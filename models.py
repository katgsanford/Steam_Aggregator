"""Single source of truth for which Claude model each task in this project calls.

No other module names a model. Each role resolves the same way:

  1. the role's env override — STEAM_EXTRACT_MODEL / STEAM_SUMMARY_MODEL /
     STEAM_NARRATIVE_MODEL, or the legacy STEAM_REVIEW_MODEL, which pins every role;
  2. the newest model the API currently serves in that role's family, read at
     runtime from the Models API, so a new Haiku or Opus is picked up without an
     edit here;
  3. the pinned ID below, when discovery is switched off (STEAM_MODEL_AUTO=0) or
     the lookup fails.

Each role also carries an effort level, overridable the same way with
STEAM_EXTRACT_EFFORT / STEAM_SUMMARY_EFFORT / STEAM_NARRATIVE_EFFORT (one of
EFFORT_LEVELS below).

This module also owns the two things every Claude call site shares: structured-output
request parameters (request_params(..., schema=...)) and usage accounting
(record_usage, which feeds the llm_usage table and the spend caps).

Step 2 is what keeps this current. The Messages API has no floating "latest"
alias — every request names one exact model ID — so "always use the current
model" has to be resolved by asking the API which models exist, not by writing a
magic string into the request. Discovery costs one unbilled Models API call per
day (the result is cached), and never blocks a run: if it fails for any reason,
including a bad key, the pinned ID is used and the real error surfaces on the
Messages call that follows.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import anthropic

# The family each role draws from, and the ID to fall back on. Theme extraction
# reads whole batches of reviews and dominates token spend, so it takes the
# cheapest current model; the executive summary is one short, quality-sensitive
# call, so it takes the strongest Opus. Fable is deliberately not a summary
# candidate — it prices above Opus for no gain on a 300-word brief.
#
# The effort defaults are not arbitrary: the current Haiku thinks by default, so the
# bulk passes run at "low" to keep output spend near what the pre-thinking Haiku cost,
# while the one short summary call runs at "high" because the current Opus defaults to
# "medium" and that is the call where quality is worth paying for. The narrative is the
# daily report's "what changed and why it matters" call: the same Opus family, but it
# runs every day over a bigger facts bundle, so it takes the model's default "medium".
ROLES = {
    "extract": {
        "family": "claude-haiku",
        "pinned": "claude-haiku-5-5",
        "env": "STEAM_EXTRACT_MODEL",
        "effort": "low",
        "effort_env": "STEAM_EXTRACT_EFFORT",
    },
    "summary": {
        "family": "claude-opus",
        "pinned": "claude-opus-5-5",
        "env": "STEAM_SUMMARY_MODEL",
        "effort": "high",
        "effort_env": "STEAM_SUMMARY_EFFORT",
    },
    "narrative": {
        "family": "claude-opus",
        "pinned": "claude-opus-5-5",
        "env": "STEAM_NARRATIVE_MODEL",
        "effort": "medium",
        "effort_env": "STEAM_NARRATIVE_EFFORT",
    },
}

# Valid effort levels, cheapest first. An unrecognised value would come back from the
# API as a 400, so it is rejected here in favour of the role's default instead.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

_LEGACY_ENV = "STEAM_REVIEW_MODEL"  # pins both roles (legacy single-model config)

AUTO = os.environ.get("STEAM_MODEL_AUTO", "1").lower() not in ("0", "false", "no")

# Discovery is cached on disk because collect.py runs as a short-lived scheduled
# process — without it every run would re-list the models.
_CACHE_PATH = Path(__file__).with_name(".model-cache.json")
_CACHE_SECONDS = int(os.environ.get("STEAM_MODEL_CACHE_HOURS", "24")) * 3600

# Dated snapshots (…-20250805) and Vertex-style IDs (…@20250805) pin one build of
# a model; the undated ID is the one to request, so snapshots are never chosen.
_SNAPSHOT_RE = re.compile(r"(-\d{8}$|@)")

# Adaptive thinking and output_config.effort arrived with the 4.6 generation;
# sending either to anything older is a 400.
_MODERN_PARAMS_MIN = (4, 6)

_resolved: dict[str, str] = {}
_listed: list[tuple[str, str]] | None = None  # (id, created_at ISO), newest first
_warned: set[str] = set()


def _warn(message: str) -> None:
    """Print once per distinct message — these sit in per-review-batch call paths."""
    if message not in _warned:
        print(f"[models] {message}")
        _warned.add(message)


def _read_cache() -> list[tuple[str, str]] | None:
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - raw["fetched_at"] > _CACHE_SECONDS:
            return None
        return [(entry[0], entry[1]) for entry in raw["models"]]
    except Exception:
        return None


def _write_cache(models: list[tuple[str, str]]) -> None:
    try:
        _CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "models": models}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a read-only checkout just re-lists next run


def _candidates(client: anthropic.Anthropic) -> list[tuple[str, str]]:
    """Every model the key can see, newest first. Empty if the lookup failed."""
    global _listed
    if _listed is not None:
        return _listed
    cached = _read_cache()
    if cached is not None:
        _listed = cached
        return _listed

    found: list[tuple[str, str]] = []
    try:
        for i, model in enumerate(client.models.list()):
            if i >= 200:
                break
            created = getattr(model, "created_at", None)
            found.append(
                (model.id, created.isoformat() if hasattr(created, "isoformat") else str(created or ""))
            )
    except Exception as exc:
        _warn(f"could not list models ({exc.__class__.__name__}); using pinned model IDs")
        _listed = []
        return _listed

    found.sort(key=lambda pair: pair[1], reverse=True)
    _write_cache(found)
    _listed = found
    return _listed


def resolve(role: str, client: anthropic.Anthropic | None = None) -> str:
    """The model ID to send for `role` ("extract", "summary" or "narrative")."""
    spec = ROLES[role]
    override = os.environ.get(spec["env"]) or os.environ.get(_LEGACY_ENV)
    if override:
        return override
    if role in _resolved:
        return _resolved[role]

    chosen = spec["pinned"]
    if AUTO and client is not None:
        prefix = spec["family"] + "-"
        for model_id, _created in _candidates(client):
            if model_id.startswith(prefix) and not _SNAPSHOT_RE.search(model_id):
                chosen = model_id
                break
    _resolved[role] = chosen
    return chosen


def _version(model_id: str) -> tuple[int, int] | None:
    """(major, minor) parsed off an undated model ID, or None if there isn't one."""
    base = _SNAPSHOT_RE.sub("", model_id)
    match = re.search(r"-(\d+)(?:-(\d+))?$", base)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def effort(role: str) -> str:
    """The effort level for `role`: its env override, else the default in ROLES."""
    spec = ROLES[role]
    override = os.environ.get(spec["effort_env"], "").strip().lower()
    if override:
        if override in EFFORT_LEVELS:
            return override
        _warn(
            f"{spec['effort_env']}={override!r} is not one of "
            f"{', '.join(EFFORT_LEVELS)}; using {spec['effort']}"
        )
    return spec["effort"]


def json_format(schema: type) -> dict:
    """The `output_config.format` block for a Pydantic model (structured output).

    Built the way the SDK's own `output_format=` helper builds it (pydantic JSON schema
    run through `transform_schema`), so the wire request is identical.
    """
    from pydantic import TypeAdapter

    json_schema = TypeAdapter(schema).json_schema()
    try:
        from anthropic import transform_schema
    except ImportError:  # older SDK without the helper: strict object schemas by hand
        json_schema.setdefault("additionalProperties", False)
    else:
        json_schema = transform_schema(json_schema)
    return {"type": "json_schema", "schema": json_schema}


def request_params(role: str, model: str, schema: type | None = None) -> dict:
    """Request kwargs for `model` in `role`: adaptive thinking + the role's effort.

    Gated on the model's version rather than a list of IDs, so a newer model needs
    no edit here. A model whose version can't be read is treated as pre-4.6:
    omitting these is always accepted, sending them to an older model is a 400.

    With `schema` (a Pydantic model), the structured-output format joins the SAME
    `output_config` as the effort, so the two are sent together. Pass the result to
    `client.messages.create(**...)` and read the answer back with parse_json().
    """
    version = _version(model)
    if version is None or version < _MODERN_PARAMS_MIN:
        params: dict = {}
    else:
        params = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort(role)},
        }
    if schema is not None:
        params["output_config"] = {**params.get("output_config", {}), "format": json_format(schema)}
    return params


def parse_json(response, schema: type):
    """The structured answer in `response` as a `schema` instance, or None if it has no text.

    Replaces `messages.parse(output_format=...)`, which fills `parsed_output` only when the
    schema is passed as `output_format`; with `output_config.format` we validate the text
    ourselves. Malformed JSON (e.g. a truncated answer) raises, as the SDK helper does.
    """
    from pydantic import TypeAdapter

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return None
    return TypeAdapter(schema).validate_json(text)


# --- usage accounting -------------------------------------------------------------------
#
# Every Claude call site reports its `usage` here with a `purpose`. The row matches the
# llm_usage table (docs/AGENT_PLAN.md Section 4.6): ts, report_date, purpose, model,
# input_tokens, output_tokens, cache_read_tokens, cost_usd. The daily and monthly caps
# sum cost_usd by purpose, so the purpose decides which cap a call counts against:
#   narrative | backfill | backfill_lazy | dashboard | theme_tagging
PURPOSES = ("narrative", "backfill", "backfill_lazy", "dashboard", "theme_tagging")

# $ per million tokens: (input, output, cache read), by model family (plan Section 7.4).
# Cache writes are billed at 1.25x input (5-minute TTL). An unknown family is priced at
# the dearest rate here, so a new model can only over-count spend, never hide it.
PRICES = {
    "claude-haiku": (0.10, 0.50, 0.01),
    "claude-opus": (4.00, 20.00, 0.20),
}
_CACHE_WRITE_MULTIPLIER = 1.25

_session_usage: list[dict] = []
_usage_hooks: list = []


def _price(model: str) -> tuple[float, float, float]:
    for family, rates in PRICES.items():
        if model.startswith(family):
            return rates
    _warn(f"no price for model {model!r}; counting it at the highest known rate")
    return tuple(max(r[i] for r in PRICES.values()) for i in range(3))  # type: ignore[return-value]


def _tokens(usage, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value or 0)


def cost_usd(model: str, usage) -> float:
    """Dollar cost of one response's `usage` (an SDK Usage object or a dict)."""
    p_in, p_out, p_cache = _price(model)
    return (
        _tokens(usage, "input_tokens") * p_in
        + _tokens(usage, "output_tokens") * p_out
        + _tokens(usage, "cache_read_input_tokens") * p_cache
        + _tokens(usage, "cache_creation_input_tokens") * p_in * _CACHE_WRITE_MULTIPLIER
    ) / 1e6


def write_usage_row(conn, row: dict) -> bool:
    """INSERT one usage row into llm_usage; False if it could not be written.

    Tolerates the table not existing yet (a DB created before llm_usage was added to
    db.SCHEMA) and any other write error: accounting must never fail a paid call, and
    the response has already been billed. Does not commit; the caller's transaction does.
    """
    try:
        conn.execute(
            "INSERT INTO llm_usage (ts, report_date, purpose, model, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?)",
            (row["ts"], row["report_date"], row["purpose"], row["model"], row["input_tokens"],
             row["output_tokens"], row["cache_read_tokens"], row["cost_usd"]),
        )
        return True
    except Exception as exc:
        if "no such table" in str(exc).lower():
            _warn("llm_usage table does not exist yet; usage is tracked in memory only")
        else:
            _warn(f"could not write llm_usage row ({exc.__class__.__name__}: {exc})")
        return False


def record_usage(purpose: str, model: str, usage, *, conn=None, report_date: str | None = None) -> dict:
    """Record one Claude call's token usage and cost; returns the row.

    Always appends to the in-memory session log (see session_cost) and runs the hooks
    from add_usage_hook. With `conn`, also INSERTs into llm_usage (write_usage_row).
    Never raises.
    """
    if purpose not in PURPOSES:
        _warn(f"unknown usage purpose {purpose!r}; expected one of {', '.join(PURPOSES)}")
    row = {
        "ts": int(time.time()),
        "report_date": report_date,
        "purpose": purpose,
        "model": model,
        "input_tokens": _tokens(usage, "input_tokens"),
        "output_tokens": _tokens(usage, "output_tokens"),
        "cache_read_tokens": _tokens(usage, "cache_read_input_tokens"),
        "cost_usd": cost_usd(model, usage),
    }
    _session_usage.append(row)
    for hook in list(_usage_hooks):
        try:
            hook(row)
        except Exception as exc:
            _warn(f"usage hook failed ({exc.__class__.__name__}: {exc})")
    if conn is not None:
        write_usage_row(conn, row)
    return row


def add_usage_hook(hook) -> None:
    """Call `hook(row)` after every record_usage (e.g. a backfill budget guard)."""
    _usage_hooks.append(hook)


def clear_usage_hooks() -> None:
    _usage_hooks.clear()


def session_cost(purpose: str | None = None) -> float:
    """Dollars recorded by this process so far, optionally for one purpose."""
    return sum(r["cost_usd"] for r in _session_usage if purpose is None or r["purpose"] == purpose)
