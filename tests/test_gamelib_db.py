"""gamelib_db.connect: direct fallback, and routing through a live gamelib-server."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

import gamelib_db
from game_insights import load_games
from playnite_to_duckdb import create_playnite_games_view

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fresh_notes():
    gamelib_db._announced.clear()
    yield
    gamelib_db._announced.clear()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_info(db, port, token="abcd1234", for_db=None):
    gamelib_db.info_path(db).write_text(
        json.dumps(
            {
                "uri": f"quack:localhost:{port}",
                "url": f"http://localhost:{port}",
                "token": token,
                "db": str(for_db if for_db is not None else db.resolve()),
            }
        ),
        encoding="utf-8",
    )


def test_no_info_file_connects_directly(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    con = gamelib_db.connect(db)
    con.execute("CREATE TABLE t AS SELECT 1 AS i")
    con.close()
    assert db.is_file()
    assert "directly" in capsys.readouterr().err
    con = gamelib_db.connect(db, read_only=True)
    assert con.execute("FROM t").fetchall() == [(1,)]
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE u (i INT)")  # read_only honoured
    con.close()


def test_stale_info_file_falls_back_to_direct(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    write_info(db, free_port())
    con = gamelib_db.connect(db)
    con.execute("CREATE TABLE t AS SELECT 1 AS i")
    assert con.execute("FROM t").fetchall() == [(1,)]
    con.close()
    err = capsys.readouterr().err
    assert "ignoring stale" in err
    assert "nothing listening" in err
    assert err.count("[gamelib]") == 1


def test_malformed_info_file_falls_back_to_direct(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    gamelib_db.info_path(db).write_text("not json", encoding="utf-8")
    con = gamelib_db.connect(db)
    assert con.execute("SELECT 1").fetchone() == (1,)
    con.close()
    assert "ignoring unreadable" in capsys.readouterr().err


def test_info_file_for_another_db_is_ignored(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    write_info(db, free_port(), for_db=tmp_path / "other.duckdb")
    assert gamelib_db.read_server_info(db) is None
    gamelib_db.connect(db).close()
    err = capsys.readouterr().err
    assert "it is for" in err and "other.duckdb" in err


def test_info_db_path_match_is_case_insensitive_on_windows(tmp_path):
    db = tmp_path / "x.duckdb"
    write_info(db, free_port(), for_db=str(db.resolve()).upper())
    assert (gamelib_db.read_server_info(db) is not None) == (os.name == "nt")


def test_sql_str_escapes_quotes():
    assert gamelib_db.sql_str("it's") == "'it''s'"


def test_mode_note_printed_once(tmp_path, capsys):
    db = tmp_path / "x.duckdb"
    for _ in range(3):
        gamelib_db.connect(db).close()
    assert capsys.readouterr().err.count("directly") == 1


def _quack_available() -> bool:
    try:
        with duckdb.connect() as con:
            con.execute("INSTALL quack")
            con.execute("LOAD quack")
        return True
    except duckdb.Error:
        return False


def stop_server(proc):
    proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture
def server(tmp_path, build_db, uid):
    """Run duckdb_server.py on a small Playnite-shaped DB; yield (db path, process, log path)."""
    if not _quack_available():
        pytest.skip("DuckDB quack extension not installable here")
    db = tmp_path / "playnite.duckdb"
    con = build_db(
        [{"Name": "Hades", "SourceId": "steam", "Playtime": 7200, "DeveloperIds": ["sg"]}],
        sources={"steam": "Steam"},
        path=db,
    )
    con.execute('CREATE TABLE companies_Company (_id UUID, "Name" VARCHAR)')
    con.execute("INSERT INTO companies_Company VALUES (?, 'Supergiant')", [uid("sg")])
    create_playnite_games_view(con)
    con.close()

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    log_path = tmp_path / "server.log"
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "duckdb_server.py"), "--db", str(db), "--port", str(free_port())],
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    info = gamelib_db.info_path(db)
    deadline = time.monotonic() + 60
    while not info.is_file():
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            pytest.fail(f"server didn't start:\n{log_path.read_text(encoding='utf-8')}")
        time.sleep(0.2)
    try:
        yield db, proc, log_path
    finally:
        if proc.poll() is None:
            stop_server(proc)


def test_routes_through_running_server(server, capsys):
    db, proc, log_path = server
    # The server holds the file, so a direct connection is refused...
    with pytest.raises(duckdb.IOException):
        duckdb.connect(str(db), read_only=True)

    # ...but the helper goes through the server (it could not have opened the locked
    # file directly), and unqualified names resolve on the remote.
    con = gamelib_db.connect(db)
    con.execute("CREATE OR REPLACE TABLE scratch AS SELECT 41 + 1 AS answer")
    assert con.execute("FROM scratch").fetchall() == [(42,)]
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"games_Game", "playnite_games", "scratch"} <= tables
    create_playnite_games_view(con)  # DDL + DESCRIBE over the remote connection
    con.execute('DROP TABLE IF EXISTS "scratch"')
    con.close()
    assert "using DuckDB server" in capsys.readouterr().err

    games = load_games(db)
    assert [(g["name"], g["store"], g["hours"], g["developers"]) for g in games] == [
        ("Hades", "Steam", 2.0, ["Supergiant"])
    ]

    # Clean stop removes the info file and releases the lock.
    stop_server(proc)
    out = log_path.read_text(encoding="utf-8")
    assert "Server stopped." in out, out
    assert not gamelib_db.info_path(db).exists()
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM games_Game").fetchone() == (1,)
    assert "scratch" not in {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    con.close()


def test_bad_token_with_live_server_raises(server):
    db, _, _ = server
    info = json.loads(gamelib_db.info_path(db).read_text(encoding="utf-8"))
    info["token"] = "wrong-token"
    gamelib_db.info_path(db).write_text(json.dumps(info), encoding="utf-8")
    with pytest.raises(duckdb.Error, match="Authentication failed"):
        gamelib_db.connect(db)


def test_locked_file_without_server_info_exits_with_hint(server):
    db, _, _ = server
    gamelib_db.info_path(db).unlink()  # another process holds the file, with no gamelib-server info
    with pytest.raises(SystemExit) as exc:
        gamelib_db.connect(db, read_only=True)
    assert "locked by another process" in str(exc.value)
    assert "gamelib-server" in str(exc.value)
