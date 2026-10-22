"""Clear cached LLM analyses so they regenerate fresh on next dashboard view.

By default this clears only the executive summaries (cheap to rebuild — they're
derived from stats + themes, not raw review text), which is what you want after
changing summary prompts or the timeline context. Theme extraction (the
token-heavy part) is left intact unless you pass --themes or --all.

Usage:
    python clear_cache.py                 # summaries only (default)
    python clear_cache.py --themes        # themes only
    python clear_cache.py --all           # everything
    python clear_cache.py --appid 1295660 # restrict to one title
"""

from __future__ import annotations

import argparse

import config
import db

SUMMARY_KINDS = ("summary_product", "summary_qa")
THEME_KINDS = ("themes_good", "themes_bad", "themes_recent")


def main() -> int:
    ap = argparse.ArgumentParser(description="Clear cached LLM analyses.")
    ap.add_argument("--themes", action="store_true", help="clear theme caches instead of summaries")
    ap.add_argument("--all", action="store_true", help="clear both themes and summaries")
    ap.add_argument("--appid", type=int, default=None, help="restrict to a single title")
    args = ap.parse_args()

    if args.all:
        kinds = SUMMARY_KINDS + THEME_KINDS
    elif args.themes:
        kinds = THEME_KINDS
    else:
        kinds = SUMMARY_KINDS

    conn = db.connect(config.db_target())
    placeholders = ",".join(["?"] * len(kinds))
    sql = f"DELETE FROM analyses WHERE kind IN ({placeholders})"
    params: list = list(kinds)
    if args.appid is not None:
        sql += " AND appid = ?"
        params.append(args.appid)
    cur = conn.execute(sql, params)
    conn.commit()
    conn.close()

    scope = f"appid {args.appid}" if args.appid is not None else "all titles"
    print(f"Cleared {cur.rowcount} cached row(s) [{', '.join(kinds)}] for {scope}.")
    print("They will regenerate the next time each view is opened in the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
