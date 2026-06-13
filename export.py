"""Optional: dump reviews to JSON for downstream programs that can't read SQLite.

  python export.py                      # all titles -> reviews_export.json
  python export.py --today              # only reviews fetched today
  python export.py --appid 1145360      # one title by appid
  python export.py --title Hades        # one title by name (substring match)
  python export.py --out path.json      # custom output path

Each row includes the title name (joined from the products table).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import db

CONFIG_PATH = Path(__file__).with_name("config.json")
DB_PATH = Path(__file__).with_name("reviews.db")


def _db_path() -> str:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("db_path", str(DB_PATH))
    return str(DB_PATH)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", action="store_true", help="only reviews fetched today")
    ap.add_argument("--appid", type=int, help="filter to one title by appid")
    ap.add_argument("--title", help="filter to one title by name (substring)")
    ap.add_argument("--out", default=str(Path(__file__).with_name("reviews_export.json")))
    args = ap.parse_args()

    conn = db.connect(_db_path())

    # LEFT JOIN so reviews are included even if their product row was deleted
    # from the products table (title column will be NULL in that case).
    sql = """
        SELECT r.*, p.name AS title
        FROM reviews r
        LEFT JOIN products p ON p.appid = r.appid
    """
    where: list[str] = []
    params: list = []
    if args.today:
        midnight = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
        where.append("r.fetched_at >= ?")
        params.append(midnight)
    if args.appid:
        where.append("r.appid = ?")
        params.append(args.appid)
    if args.title:
        where.append("p.name LIKE ?")
        params.append(f"%{args.title}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.timestamp_created DESC"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    Path(args.out).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    conn.close()
    print(f"Wrote {len(rows)} reviews -> {args.out}")


if __name__ == "__main__":
    main()
