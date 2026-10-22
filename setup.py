"""Interactive setup: add a title to track.

Run once per product (re-run any time to add more):  python setup.py
It resolves the product name to a Steam appid, shows the languages Steam lists
for it, lets you pick one or more, and adds the title to the products table in
reviews.db. Existing titles are left untouched; re-adding one updates it.
"""

from __future__ import annotations

import sys

import config
import db
import steam_api


def _prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)


def load_db_path() -> str:
    """config.json now only stores the shared DB path (created on first run).

    Returns where the title gets added: that local path, or the libSQL server
    when DATABASE_URL points at one (see config.py).
    """
    config.ensure_config_file()
    return config.db_target()


def choose_app() -> dict:
    term = _prompt("Product name to search on Steam: ")
    if not term:
        print("No product name given.")
        sys.exit(1)
    items = steam_api.search_apps(term)
    if not items:
        print(f"No Steam apps matched '{term}'. Try a different spelling.")
        sys.exit(1)
    print("\nMatches:")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item['name']}  (appid {item['id']})")
    pick = _prompt(f"\nChoose 1-{len(items)} [1]: ") or "1"
    if not pick.isdigit() or not (1 <= int(pick) <= len(items)):
        print("Invalid choice.")
        sys.exit(1)
    chosen = items[int(pick) - 1]
    return {"appid": int(chosen["id"]), "name": chosen["name"]}


def choose_languages(appid: int) -> list[dict]:
    langs = steam_api.get_supported_languages(appid)
    if not langs:
        print("Steam listed no mappable review languages for this title.")
        sys.exit(1)
    print("\nSupported languages:")
    for i, (name, code) in enumerate(langs, 1):
        print(f"  {i}. {name}  ({code})")
    raw = _prompt("\nSelect languages by number (comma-separated, or 'all'): ").lower()
    if raw == "all":
        chosen = langs
    else:
        idxs = [p.strip() for p in raw.split(",") if p.strip()]
        if not idxs or not all(p.isdigit() and 1 <= int(p) <= len(langs) for p in idxs):
            print("Invalid selection.")
            sys.exit(1)
        chosen = [langs[int(p) - 1] for p in idxs]
    return [{"name": n, "code": c} for n, c in chosen]


def main() -> None:
    db_path = load_db_path()
    conn = db.connect(db_path)

    existing = db.list_products(conn)
    if existing:
        print("Already tracking:")
        for p in existing:
            print(f"  - {p['name']} (appid {p['appid']}): {', '.join(p['languages'])}")
        print()

    app = choose_app()
    languages = choose_languages(app["appid"])
    codes = [l["code"] for l in languages]
    db.upsert_product(conn, app["appid"], app["name"], codes)
    conn.commit()
    conn.close()

    print(f"\nAdded/updated '{app['name']}' (appid {app['appid']})")
    print(f"  Languages: {', '.join(codes)}")
    print(f"  DB:        {db_path}")
    print("\nRe-run setup.py to add more titles, or 'python collect.py' to pull now.")


if __name__ == "__main__":
    main()
