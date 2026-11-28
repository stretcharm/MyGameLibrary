"""Serve the gamelib DuckDB database over DuckDB's Quack protocol (localhost only).

While this runs it holds the database file, and gamelib-export / gamelib-insights
(via gamelib_db.connect) route their queries through it instead of opening the
file directly. Connection info (uri, url, token) is written to
``<db>.quack.json`` and removed on a clean exit.

Usage:
    uv run gamelib-server
    uv run duckdb_server.py --db playnite_export/playnite.duckdb --port 9494

Stop with Ctrl+C.
"""
import argparse
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path

import duckdb

from gamelib_db import DEFAULT_DB, info_path, sql_str

TOKEN_ENV = "GAMELIB_QUACK_TOKEN"


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same folder, so readers never see a partial file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _stop(con: "duckdb.DuckDBPyConnection", uri: str) -> None:
    """Stop the Quack server (an un-stopped server hangs interpreter exit) and close the database."""
    try:
        con.execute(f"CALL quack_stop({sql_str(uri)})")
    except duckdb.Error as e:
        print(f"!! quack_stop failed: {e}", file=sys.stderr)
    con.close()


def main():
    ap = argparse.ArgumentParser(description="Serve playnite.duckdb over DuckDB Quack on localhost")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB file to serve")
    ap.add_argument("--port", type=int, help="Port to listen on (default: Quack's default, 9494)")
    args = ap.parse_args()

    db_path: Path = args.db
    if not db_path.is_file():
        print(f"{db_path} doesn't exist yet; creating an empty database (run gamelib-export to fill it)")
        db_path.parent.mkdir(parents=True, exist_ok=True)

    token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(24)
    listen_uri = f"quack:localhost:{args.port}" if args.port else "quack:localhost"

    # Let a polite kill (SIGTERM, or Ctrl+Break on Windows) clean up like Ctrl+C does.
    # Registered before the server starts and the info file is written, so neither is left behind.
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise_interrupt)

    try:
        con = duckdb.connect(str(db_path))
    except duckdb.Error as e:
        sys.exit(f"Couldn't open {db_path} (is another process using it?): {e}")

    info_file = info_path(db_path)
    try:
        con.execute("INSTALL quack")
        con.execute("LOAD quack")
        uri, url, _ = con.execute(f"CALL quack_serve({sql_str(listen_uri)}, token = {sql_str(token)})").fetchone()
    except duckdb.Error as e:
        con.close()
        sys.exit(f"Couldn't start the Quack server on {listen_uri}: {e}")

    try:
        info = {"uri": uri, "url": url, "token": token, "db": str(db_path.resolve()), "pid": os.getpid()}
        write_atomic(info_file, json.dumps(info, indent=2))
    except BaseException:
        info_file.unlink(missing_ok=True)
        _stop(con, uri)
        raise

    print(f"Serving {db_path.resolve()} at {url} ({uri})")
    print(f"Connection info: {info_file}")
    print("Other gamelib commands now go through this server. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        info_file.unlink(missing_ok=True)
        _stop(con, uri)
    print("Server stopped.")


if __name__ == "__main__":
    main()
