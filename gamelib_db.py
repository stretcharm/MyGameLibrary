"""Open the gamelib DuckDB database, through a running Quack server if there is one.

While ``gamelib-server`` (duckdb_server.py) is serving a database file it holds the
file's lock, so a direct ``duckdb.connect`` from another process fails. ``connect``
looks for the server's connection-info file next to the database
(``<db>.quack.json``). If it names this database and its port is open, it returns
an in-memory connection that has ATTACHed the server and run ``CONNECT`` on it, so
unqualified SQL (e.g. ``FROM playnite_games``) runs on the served database
unchanged. With no usable info file, or nothing listening on its port, it returns
a plain ``duckdb.connect``.

Limitations of Quack/CONNECT in DuckDB 2.0:
- Parameterised statements (``execute(sql, params)``) are refused while
  CONNECT-ed, so callers should inline values (see ``sql_str``).
- Quack attachments can't be READ_ONLY ("not currently supported for Quack
  databases"), so ``read_only`` only applies to direct connections.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

DEFAULT_DB = Path(__file__).parent / "playnite_export" / "playnite.duckdb"
INFO_SUFFIX = ".quack.json"
REMOTE_ALIAS = "gamelib_remote"

_announced: set[str] = set()


def sql_str(s: str) -> str:
    """Quote ``s`` as a SQL string literal."""
    return "'" + s.replace("'", "''") + "'"


def info_path(db_path: str | Path) -> Path:
    """Where gamelib-server writes connection info for ``db_path``."""
    return Path(str(db_path) + INFO_SUFFIX)


def same_db(a: str | Path, b: str | Path) -> bool:
    """True if two paths name the same database file (case-insensitive on Windows)."""
    return os.path.normcase(Path(a).resolve()) == os.path.normcase(Path(b).resolve())


def _note(msg: str) -> None:
    """Print a connection-mode note once per process."""
    if msg not in _announced:
        _announced.add(msg)
        print(msg, file=sys.stderr)


def read_server_info(db_path: str | Path) -> dict | None:
    """Return the server info for ``db_path`` (uri, url, token, db, ...), or None if absent or unusable."""
    path = info_path(db_path)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        _note(f"[gamelib] ignoring unreadable {path.name}: {e}")
        return None
    if not isinstance(info, dict) or not all(isinstance(info.get(k), str) for k in ("uri", "url", "token", "db")):
        _note(f"[gamelib] ignoring malformed {path.name}")
        return None
    if not same_db(info["db"], db_path):
        _note(f"[gamelib] ignoring {path.name}: it is for {info['db']}, not {Path(db_path).resolve()}")
        return None
    return info


def port_open(url: str, timeout: float = 0.5) -> bool:
    """Cheap TCP probe: is anything listening at ``url``'s host and port?"""
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def connect_remote(info: dict) -> duckdb.DuckDBPyConnection:
    """Return an in-memory connection CONNECT-ed to the Quack server described by ``info``.

    Loads the quack extension explicitly (INSTALL is a no-op once installed)
    rather than relying on ATTACH's autoload, so a missing extension fails here
    with its own error. Raises duckdb.Error on failure (e.g. a wrong token).
    """
    con = duckdb.connect()
    try:
        con.execute("INSTALL quack")
        con.execute("LOAD quack")
        con.execute(f"ATTACH {sql_str(info['uri'])} AS {REMOTE_ALIAS} (TOKEN {sql_str(info['token'])})")
        con.execute(f"CONNECT {REMOTE_ALIAS}")
    except BaseException:
        con.close()
        raise
    return con


def _is_lock_error(e: duckdb.Error) -> bool:
    msg = str(e).lower()
    return "being used by another process" in msg or "conflicting lock" in msg or "could not set lock" in msg


def connect(db_path: str | Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Connect to ``db_path``, routed through gamelib-server when it is serving that file.

    - Info file for this db and its port is open: connect through the server.
      Any ATTACH/CONNECT failure (bad token, extension can't load, ...) is raised.
    - No usable info file, or nothing listening: open the file directly. If another
      process holds the file's lock, exit with a message saying what to do.

    ``read_only`` only applies to direct connections (Quack can't attach read-only).
    """
    direct_note = f"[gamelib] using DuckDB file directly: {db_path}"
    info = read_server_info(db_path)
    if info is not None:
        if port_open(info["url"]):
            con = connect_remote(info)
            _note(f"[gamelib] using DuckDB server at {info['url']}")
            return con
        direct_note = (
            f"[gamelib] ignoring stale {info_path(db_path).name}: nothing listening at "
            f"{info['url']}; using DuckDB file directly"
        )
    _note(direct_note)
    try:
        return duckdb.connect(str(db_path), read_only=read_only)
    except duckdb.IOException as e:
        if _is_lock_error(e):
            sys.exit(
                f"!! {db_path} is locked by another process.\n"
                "   Close the other DuckDB client using it, or serve it with `uv run gamelib-server` "
                "so gamelib commands can share it.\n"
                f"   ({e})"
            )
        raise
