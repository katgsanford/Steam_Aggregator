"""Build ids as the only release signal: the mirror, the source chain, record_public_build,
refresh_releases and collect_all. The network is mocked throughout; nothing here touches Steam."""

import enum
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collect  # noqa: E402
import db  # noqa: E402
import steam_api  # noqa: E402
from steam_api import BuildInfo  # noqa: E402

# api.steamcmd.net/v1/info/<appid>, trimmed to the part we read (shape taken from a live response).
MIRROR_PAYLOAD = {
    "status": "success",
    "data": {"730": {"appid": "730", "depots": {"branches": {
        "public": {"buildid": "26087496", "timeupdated": "1792535451"},
        "beta": {"buildid": "999", "timeupdated": "1"},
    }}}},
}


class FakeResponse:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc = payload, exc

    def json(self):
        if self.exc:
            raise self.exc
        return self.payload


@pytest.fixture
def mirror_get(monkeypatch):
    """Patch steam_api._get; returns the list of (url, params) calls. Set .response to change the reply."""
    calls = []
    box = types.SimpleNamespace(calls=calls, response=FakeResponse(MIRROR_PAYLOAD), raises=None)

    def fake_get(url, params, **kwargs):
        calls.append((url, params))
        if box.raises:
            raise box.raises
        return box.response

    monkeypatch.setattr(steam_api, "_get", fake_get)
    return box


# --- the mirror -----------------------------------------------------------------------------

def test_mirror_reads_the_public_branch_only(mirror_get):
    assert steam_api.get_public_build_id_mirror(730) == ("26087496", 1792535451)
    assert mirror_get.calls[0][0] == "https://api.steamcmd.net/v1/info/730"


def test_mirror_without_public_branch_is_none(mirror_get):
    mirror_get.response = FakeResponse({"status": "success", "data": {"730": {"depots": {"branches": {}}}}})
    assert steam_api.get_public_build_id_mirror(730) is None


def test_mirror_missing_timeupdated_is_zero(mirror_get):
    mirror_get.response = FakeResponse(
        {"status": "success", "data": {"730": {"depots": {"branches": {"public": {"buildid": "5"}}}}}})
    assert steam_api.get_public_build_id_mirror(730) == ("5", 0)


@pytest.mark.parametrize("payload", [
    {"status": "failed", "data": {}},
    {"status": "success", "data": {}},
    {"status": "success", "data": {"730": {"depots": {"branches": {"public": {"buildid": ""}}}}}},
    {"status": "success", "data": {"730": {"depots": ["not", "a", "dict"]}}},
    ["not a dict"],
])
def test_mirror_bad_payload_is_none(mirror_get, payload):
    mirror_get.response = FakeResponse(payload)
    assert steam_api.get_public_build_id_mirror(730) is None


def test_mirror_http_or_json_failure_is_none(mirror_get):
    mirror_get.raises = steam_api.SteamError("down")
    assert steam_api.get_public_build_id_mirror(730) is None
    mirror_get.raises = None
    mirror_get.response = FakeResponse(exc=ValueError("not json"))
    assert steam_api.get_public_build_id_mirror(730) is None


# --- the chain: PICS WebSocket, then the mirror ---------------------------------------------------

@pytest.fixture
def chain(monkeypatch):
    """Control both sources. box.pics is {appid: (id, ts)}; box.mirror maps appid -> (id, ts) or None."""
    box = types.SimpleNamespace(pics={}, mirror={}, mirror_calls=[], pics_calls=[])

    def fake_pics(appids, **kwargs):
        box.pics_calls.append(list(appids))
        return {a: box.pics[a] for a in appids if a in box.pics}

    def fake_mirror(appid, **kwargs):
        box.mirror_calls.append(appid)
        return box.mirror.get(appid)

    monkeypatch.setattr(steam_api, "get_public_build_ids", fake_pics)
    monkeypatch.setattr(steam_api, "get_public_build_id_mirror", fake_mirror)
    return box


def test_pics_answers_first_and_the_mirror_is_not_asked(chain):
    chain.pics = {730: ("111", 1000)}
    chain.mirror = {730: ("222", 2000)}
    assert steam_api.build_source(730) == BuildInfo("111", 1000, "pics_ws")
    assert chain.mirror_calls == []


def test_mirror_is_the_fallback_when_pics_fails(chain):
    chain.mirror = {730: ("222", 2000)}
    assert steam_api.build_source(730) == BuildInfo("222", 2000, "steamcmd_net")


def test_both_sources_failing_is_none(chain):
    assert steam_api.build_source(730) is None
    assert chain.mirror_calls == [730]


def test_batch_makes_one_pics_call_and_asks_the_mirror_only_for_the_gaps(chain):
    chain.pics = {1: ("a", 10), 3: ("c", 30)}
    chain.mirror = {2: ("b", 20)}
    out = steam_api.build_sources([1, 2, 3, 4])
    assert chain.pics_calls == [[1, 2, 3, 4]]
    assert chain.mirror_calls == [2, 4]
    assert out == {1: BuildInfo("a", 10, "pics_ws"), 2: BuildInfo("b", 20, "steamcmd_net"),
                   3: BuildInfo("c", 30, "pics_ws"), 4: None}


def test_stale_mirror_answer_is_dropped(chain):
    chain.mirror = {730: ("old", 1000)}
    assert steam_api.build_source(730, min_timeupdated=2000) is None


def test_mirror_answer_as_new_as_the_held_build_is_kept(chain):
    chain.mirror = {730: ("same", 2000)}
    assert steam_api.build_source(730, min_timeupdated=2000) == BuildInfo("same", 2000, "steamcmd_net")


def test_mirror_answer_without_a_timestamp_cannot_be_judged_stale(chain):
    chain.mirror = {730: ("x", 0)}
    assert steam_api.build_source(730, min_timeupdated=2000) == BuildInfo("x", 0, "steamcmd_net")


def test_pics_answer_is_trusted_even_if_older_than_held(chain):
    # PICS is first-party; only the third-party mirror gets the staleness check.
    chain.pics = {730: ("rollback", 500)}
    assert steam_api.build_source(730, min_timeupdated=2000).source == "pics_ws"


# --- PICS over WebSocket, with the CM stack faked -------------------------------------------------

class FakeEResult(enum.Enum):
    OK = 1
    Fail = 2


class FakeTimeout(BaseException):
    """gevent.Timeout derives from BaseException, not Exception."""

    def __init__(self, seconds=None):
        self.seconds = seconds

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_stack(info=None, login=FakeEResult.OK, product_error=None):
    log = types.SimpleNamespace(disconnected=False, apps=None, hosts=None)

    class Client:
        def __init__(self, hosts):
            log.hosts = hosts

        def anonymous_login(self):
            return login

        def get_product_info(self, apps, timeout):
            log.apps = apps
            if product_error:
                raise product_error
            return info

        def disconnect(self):
            log.disconnected = True

    stack = types.SimpleNamespace(gevent=types.SimpleNamespace(Timeout=FakeTimeout),
                                  client_cls=Client, EResult=FakeEResult)
    return stack, log


@pytest.fixture
def cm_hosts(monkeypatch):
    monkeypatch.setattr(steam_api, "cm_ws_hosts", lambda: [("cm1.example", 443)])


def public(build, ts):
    return {"depots": {"branches": {"public": {"buildid": build, "timeupdated": ts}}}}


def test_pics_ws_reads_every_app_with_one_login(monkeypatch, cm_hosts):
    info = {"apps": {730: public(111, "1000"), 570: public(222, 2000), 440: {"depots": {"branches": {}}}}}
    stack, log = fake_stack(info)
    monkeypatch.setattr(steam_api, "_ws_stack", lambda: stack)
    out = steam_api.get_public_build_ids([730, 570, 440, 999])
    assert out == {730: ("111", 1000), 570: ("222", 2000)}  # no public branch / not returned: absent
    assert log.apps == [730, 570, 440, 999] and log.hosts == [("cm1.example", 443)]
    assert log.disconnected


def test_pics_ws_single_app_form(monkeypatch, cm_hosts):
    stack, _ = fake_stack({"apps": {730: public(111, 1000)}})
    monkeypatch.setattr(steam_api, "_ws_stack", lambda: stack)
    assert steam_api.get_public_build_id(730) == ("111", 1000)


def test_pics_ws_failures_return_empty_and_still_disconnect(monkeypatch, cm_hosts):
    stack, log = fake_stack(login=FakeEResult.Fail)
    monkeypatch.setattr(steam_api, "_ws_stack", lambda: stack)
    assert steam_api.get_public_build_ids([730]) == {}
    assert log.disconnected

    stack, log = fake_stack(product_error=RuntimeError("cm hiccup"))
    monkeypatch.setattr(steam_api, "_ws_stack", lambda: stack)
    assert steam_api.get_public_build_ids([730]) == {}

    stack, log = fake_stack(product_error=FakeTimeout())  # a gevent timeout is not an Exception
    monkeypatch.setattr(steam_api, "_ws_stack", lambda: stack)
    assert steam_api.get_public_build_ids([730]) == {}
    assert log.disconnected


def test_pics_ws_without_the_steam_package_is_empty(monkeypatch):
    def no_steam():
        raise ImportError("No module named 'steam'")

    monkeypatch.setattr(steam_api, "_ws_stack", no_steam)
    assert steam_api.get_public_build_ids([730]) == {}
    assert steam_api.get_public_build_ids([]) == {}


def test_cm_directory_keeps_only_port_443_websocket_servers(mirror_get):
    mirror_get.response = FakeResponse({"response": {"serverlist": [
        {"endpoint": "cmp1.steamserver.net:443", "type": "websockets"},
        {"endpoint": "cmp2.steamserver.net:27017", "type": "netfilter"},
        {"endpoint": "cmp3.steamserver.net:8443", "type": "websockets"},
        {"endpoint": "cmp4.steamserver.net:443", "type": "websockets"},
    ]}})
    assert steam_api.cm_ws_hosts(limit=8) == [("cmp1.steamserver.net", 443), ("cmp4.steamserver.net", 443)]
    assert steam_api.cm_ws_hosts(limit=1) == [("cmp1.steamserver.net", 443)]


def test_ws_shim_builds_on_the_steam_stack():
    pytest.importorskip("steam")
    pytest.importorskip("gevent")
    stack = steam_api._build_ws_stack()
    client = stack.client_cls([("cm1.example", 443)])
    assert type(client.connection).__name__ == "WSConnection"
    assert client._handle_cm_list(None) is None  # the pushed TCP list is ignored


# --- database-backed behaviour -----------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def quiet_log(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "LOG_PATH", tmp_path / "collect.log")


def release_ids(conn, appid=730):
    return [r["gid"] for r in conn.execute("SELECT gid FROM releases WHERE appid=? ORDER BY date", (appid,))]


def test_first_build_is_a_baseline(conn):
    info = collect.record_public_build(conn, 730, build=BuildInfo("100", 5000, "pics_ws"))
    assert info == {"appid": 730, "build_id": "100", "build_ts": 5000, "source": "pics_ws",
                    "previous_build_id": None, "inserted": True, "status": "baseline"}
    row = conn.execute("SELECT title, date, is_major FROM releases WHERE gid='build_100'").fetchone()
    assert (row["title"], row["date"], row["is_major"]) == ("Build 100", 5000, 1)


def test_same_build_again_is_unchanged_and_a_new_one_is_new(conn):
    collect.record_public_build(conn, 730, build=BuildInfo("100", 5000, "pics_ws"))
    again = collect.record_public_build(conn, 730, build=BuildInfo("100", 5000, "steamcmd_net"))
    assert (again["status"], again["inserted"]) == ("unchanged", False)
    new = collect.record_public_build(conn, 730, build=BuildInfo("101", 9000, "steamcmd_net"))
    assert (new["status"], new["inserted"], new["previous_build_id"]) == ("new", True, "100")
    assert release_ids(conn) == ["build_100", "build_101"]


def test_unavailable_makes_no_claim_and_writes_nothing(conn):
    info = collect.record_public_build(conn, 730, build=None)
    assert info["status"] == "unavailable" and info["build_id"] is None and not info["inserted"]
    assert release_ids(conn) == []


def test_a_nearby_news_release_no_longer_suppresses_a_build(conn):
    # The old 36-hour de-dup skipped a build whenever a news release was near it.
    db.upsert_release(conn, 730, "1234567", "Patch notes", 5000 - 3600, "http://x", is_major=True)
    info = collect.record_public_build(conn, 730, build=BuildInfo("100", 5000, "pics_ws"))
    assert info["inserted"] is True
    assert release_ids(conn) == ["1234567", "build_100"]


def test_build_without_a_timestamp_is_dated_now(conn):
    before = int(time.time())
    collect.record_public_build(conn, 730, build=BuildInfo("100", 0, "steamcmd_net"))
    assert conn.execute("SELECT date FROM releases WHERE gid='build_100'").fetchone()["date"] >= before


def test_lookup_passes_the_newest_held_build_time_for_the_stale_check(conn, monkeypatch):
    collect.record_public_build(conn, 730, build=BuildInfo("100", 5000, "pics_ws"))
    seen = {}

    def fake_source(appid, min_timeupdated=0):
        seen.update(appid=appid, min=min_timeupdated)
        return BuildInfo("100", 5000, "pics_ws")

    monkeypatch.setattr(steam_api, "build_source", fake_source)
    assert collect.record_public_build(conn, 730)["status"] == "unchanged"
    assert seen == {"appid": 730, "min": 5000}


def test_refresh_releases_keeps_launch_ignores_news_and_deletes_nothing(conn, monkeypatch):
    db.upsert_release(conn, 730, "111", "Old patch", 100, "u", is_major=True)    # from the news feed, earlier
    db.upsert_release(conn, 730, "222", "Store post", 200, "u", is_major=False)  # a non-patch news post
    monkeypatch.setattr(steam_api, "get_release_date", lambda appid: 50)

    def no_news(*a, **k):
        raise AssertionError("refresh_releases must not read the news feed")

    monkeypatch.setattr(steam_api, "get_news", no_news)
    assert collect.refresh_releases(conn, 730) == 3
    assert release_ids(conn) == ["launch", "111", "222"]


# --- collect_all ---------------------------------------------------------------------------------

def review(rid, ts, up=1):
    return {"recommendationid": rid, "author": {"steamid": "s"}, "voted_up": up, "review": "text",
            "timestamp_created": ts, "timestamp_updated": ts, "votes_up": 0, "votes_funny": 0,
            "weighted_vote_score": "0.5", "steam_purchase": True}


@pytest.fixture
def world(conn, monkeypatch):
    """Two titles; english works, german fails for 730. Builds come from a fake chain."""
    db.upsert_product(conn, 730, "Counter-Strike 2", ["english", "german"])
    db.upsert_product(conn, 570, "Dota 2", ["english"])
    conn.commit()
    w = types.SimpleNamespace(builds={730: BuildInfo("100", 5000, "pics_ws"), 570: BuildInfo("7", 6000, "pics_ws")},
                              build_calls=[])

    def fake_build_sources(appids, min_timeupdated=None):
        w.build_calls.append((list(appids), min_timeupdated))
        return {a: w.builds.get(a) for a in appids}

    def fake_iter(appid, language, stop_before_ts=0, **kw):
        if (appid, language) == (730, "german"):
            yield review(f"{appid}-{language}-1", 1000)
            raise steam_api.SteamError("429 storm")
        for i in range(2):
            yield review(f"{appid}-{language}-{i}", 2000 + i)

    monkeypatch.setattr(steam_api, "build_sources", fake_build_sources)
    monkeypatch.setattr(steam_api, "get_release_date", lambda appid: 50)
    monkeypatch.setattr(steam_api, "iter_reviews", fake_iter)
    return w


def titles_by_appid(result):
    return {t["appid"]: t for t in result["titles"]}


def test_collect_all_returns_per_title_and_per_language_results(conn, world):
    result = collect.collect_all(conn)
    t = titles_by_appid(result)
    assert result["total_new"] == t[730]["reviews"]["new"] + t[570]["reviews"]["new"]
    assert t[570]["reviews"] == {"new": 2, "languages": [
        {"language": "english", "new": 2, "status": "complete", "error": None}]}
    langs = {l["language"]: l for l in t[730]["reviews"]["languages"]}
    assert langs["english"]["status"] == "complete" and langs["english"]["new"] == 2
    assert langs["german"]["status"] == "error" and "429 storm" in langs["german"]["error"]
    assert langs["german"]["new"] == 1  # stored before the failure
    assert t[730]["releases"] == {"count": 1, "error": None}  # the launch row, counted before the build
    assert release_ids(conn, 730) == ["launch", "build_100"]
    assert t[730]["build"]["status"] == "baseline"
    assert world.build_calls == [([730, 570], {730: 0, 570: 0})]  # one lookup for every title
    assert (result["build_source"], result["build_status"]) == ("pics_ws", "ok")


def test_collect_all_build_summary_when_only_the_mirror_answers(conn, world):
    world.builds = {730: BuildInfo("100", 5000, "steamcmd_net"), 570: BuildInfo("7", 6000, "steamcmd_net")}
    result = collect.collect_all(conn)
    assert (result["build_source"], result["build_status"]) == ("steamcmd_net", "ok")


def test_collect_all_partial_and_unavailable_builds(conn, world):
    world.builds = {730: BuildInfo("100", 5000, "pics_ws")}
    result = collect.collect_all(conn)
    assert (result["build_source"], result["build_status"]) == ("pics_ws", "partial")
    assert titles_by_appid(result)[570]["build"]["status"] == "unavailable"

    world.builds = {}
    result = collect.collect_all(conn)
    assert (result["build_source"], result["build_status"]) == ("none", "unavailable")
    assert result["total_new"] > 0  # reviews are still collected


def test_a_failing_build_lookup_does_not_stop_review_collection(conn, world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cm exploded")

    monkeypatch.setattr(steam_api, "build_sources", boom)
    result = collect.collect_all(conn)
    assert result["total_new"] > 0
    assert result["build_status"] == "unavailable"


def test_a_failing_release_refresh_is_recorded_and_skipped(conn, world, monkeypatch):
    def boom(appid):
        raise steam_api.SteamError("store down")

    monkeypatch.setattr(steam_api, "get_release_date", boom)
    t = titles_by_appid(collect.collect_all(conn))
    assert "store down" in t[730]["releases"]["error"]
    assert t[730]["reviews"]["new"] > 0


def test_collect_all_with_no_titles(conn):
    assert collect.collect_all(conn)["titles"] == []


def test_logging_goes_to_stdout_and_the_log_file(conn, world, capsys, tmp_path):
    collect.collect_all(conn)
    out = capsys.readouterr().out
    assert "Run start: 2 title(s)" in out
    assert "public build 100 via pics_ws: baseline" in out
    assert "Run done:" in out
    assert "Run start: 2 title(s)" in (tmp_path / "collect.log").read_text(encoding="utf-8")


def test_logging_survives_an_unwritable_log_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(collect, "LOG_PATH", tmp_path)  # a directory: open() raises
    collect._log("still visible")
    assert "still visible" in capsys.readouterr().out


def test_steam_api_warnings_reach_the_run_log(capsys):
    steam_api._LOG.warning("PICS WebSocket failed: test")
    assert "PICS WebSocket failed: test" in capsys.readouterr().out


def test_main_is_a_thin_wrapper(tmp_path, world, monkeypatch, capsys):
    path = tmp_path / "main.db"
    c = db.connect(path)
    db.upsert_product(c, 570, "Dota 2", ["english"])
    c.commit()
    c.close()
    monkeypatch.setattr(collect, "_db_path", lambda: str(path))
    assert collect.main() == 0
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
    c.close()


def test_main_without_titles_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "_db_path", lambda: str(tmp_path / "empty.db"))
    assert collect.main() == 1
