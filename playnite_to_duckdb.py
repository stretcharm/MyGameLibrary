"""
Export Playnite's LiteDB library files (*.db) to NDJSON + a DuckDB database.

Playnite's .db files are LiteDB (a .NET document store), not SQLite, so DuckDB
can't read them directly. This script loads the LiteDB.dll that ships with
Playnite (via pythonnet), dumps every collection to newline-delimited JSON,
then loads each into a DuckDB table.

Close Playnite before running (files are copied first, but a running Playnite
may have un-flushed changes).

Usage:
    uv run playnite_to_duckdb.py
    uv run playnite_to_duckdb.py --dll "K:\\Games\\Playnite\\Playnite\\LiteDB.dll" --out export

Exits non-zero (after exporting everything it can) if any file or table failed.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb

DEFAULT_LIBRARY = Path(os.environ.get("APPDATA", "")) / "Playnite" / "library"

# Flattened, analysis-friendly view over the raw Playnite tables. game_insights.py
# reads every per-game field from this view (developer names come from a separate
# companies_Company lookup), so every game field it needs must be exposed here.
PLAYNITE_GAMES_VIEW_SQL = """
CREATE OR REPLACE VIEW playnite_games AS
SELECT
    g._Id AS playnite_id,
    g."Name" AS game_name,
    s."Name" AS store_source,
    COALESCE((SELECT list(ge."Name" ORDER BY ge."Name")
              FROM genres_Genre AS ge WHERE list_contains(g.GenreIds, ge._Id)),
             []::VARCHAR[]) AS genres,
    COALESCE((SELECT list(se."Name" ORDER BY se."Name")
              FROM series AS se WHERE list_contains(g.SeriesIds, se._Id)),
             []::VARCHAR[]) AS series,
    round(g.Playtime / 3600.0, 2) AS hours_played,
    g.IsInstalled AS is_installed,
    g.InstallSize AS install_size,
    CAST(g.Added AS TIMESTAMP) AS date_added,
    CAST(g.LastActivity AS TIMESTAMP) AS last_played,
    g.CommunityScore AS community_score,
    g.CriticScore AS critic_score,
    CASE
        WHEN g.CommunityScore IS NULL AND g.CriticScore IS NULL THEN NULL
        ELSE (COALESCE(g.CommunityScore, 0) + COALESCE(g.CriticScore, 0))
             / CASE WHEN g.CriticScore IS NOT NULL AND g.CommunityScore IS NOT NULL THEN 2 ELSE 1 END
    END AS score,
    g.ReleaseDate AS release_date,
    g.PlayCount AS play_count,
    g.CompletionStatus."Name" AS completion_status,
    g.DeveloperIds AS developer_ids,
    g.Hidden AS is_hidden
FROM games_Game AS g
LEFT JOIN sources_GameSource AS s ON g.SourceId = s._Id
"""

# Lookup tables the view joins to. Playnite omits a collection/field from the
# export when nothing uses it (e.g. a library with no series), so any that are
# missing are created empty so the view still binds and its columns come out NULL.
VIEW_LOOKUP_TABLES = {
    "genres_Genre": '(_Id UUID, "Name" VARCHAR)',
    "series": '(_Id UUID, "Name" VARCHAR)',
    "sources_GameSource": '(_Id UUID, "Name" VARCHAR)',
}
VIEW_GAME_COLUMNS = {
    "Name": "VARCHAR",
    "GenreIds": "UUID[]",
    "SeriesIds": "UUID[]",
    "DeveloperIds": "UUID[]",
    "SourceId": "UUID",
    "Playtime": "BIGINT",
    "IsInstalled": "BOOLEAN",
    "InstallSize": "BIGINT",
    "Added": "TIMESTAMP WITH TIME ZONE",
    "LastActivity": "TIMESTAMP WITH TIME ZONE",
    "CommunityScore": "BIGINT",
    "CriticScore": "BIGINT",
    "ReleaseDate": "DATE",
    "PlayCount": "BIGINT",
    "CompletionStatus": 'STRUCT(_id UUID, "Name" VARCHAR)',
    "Hidden": "BOOLEAN",
}


def create_playnite_games_view(con: "duckdb.DuckDBPyConnection") -> None:
    """Create the playnite_games view, filling in any inputs the export left out.

    Raises duckdb.Error if the view can't be built.
    """
    existing = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "games_Game" not in existing:
        raise duckdb.Error("no games_Game table was exported")
    for table, columns in VIEW_LOOKUP_TABLES.items():
        if table not in existing:
            print(f"!! {table} missing from the export; its playnite_games column will be NULL")
            con.execute(f'CREATE TABLE "{table}" {columns}')
    have = {r[0] for r in con.execute("DESCRIBE games_Game").fetchall()}
    for column, col_type in VIEW_GAME_COLUMNS.items():
        if column not in have:
            print(f"!! games_Game.{column} missing from the export; its playnite_games column will be NULL")
            con.execute(f'ALTER TABLE games_Game ADD COLUMN "{column}" {col_type}')
    con.execute(PLAYNITE_GAMES_VIEW_SQL)


def find_litedb_dll(explicit: str | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates += [
        local / "Playnite" / "LiteDB.dll",
        Path(os.environ.get("PROGRAMFILES", "")) / "Playnite" / "LiteDB.dll",
        Path(os.environ.get("APPDATA", "")) / "Playnite" / "LiteDB.dll",
    ]
    for c in candidates:
        if c.is_file():
            return c
    sys.exit(
        "Couldn't find LiteDB.dll. Pass --dll pointing at the copy in your "
        "Playnite install folder (the one containing Playnite.DesktopApp.exe)."
    )


def load_litedb(dll: Path):
    """Load the .NET runtime and LiteDB.dll; return (JsonSerializer, LiteDatabase).

    Done here rather than at import time so the module can be imported (e.g. for
    tests) without pythonnet's runtime or Windows.
    """
    from pythonnet import load

    load("netfx")  # Playnite's LiteDB.dll loads fine on the Windows .NET Framework runtime
    import clr

    clr.AddReference(str(dll))
    from LiteDB import JsonSerializer, LiteDatabase  # type: ignore

    return JsonSerializer, LiteDatabase


def unwrap(o):
    """Turn LiteDB extended-JSON wrappers ($guid, $date, ...) into plain values."""
    if isinstance(o, dict):
        if len(o) == 1:
            k, v = next(iter(o.items()))
            if k in ("$guid", "$date", "$oid", "$numberDecimal", "$binary"):
                return v
            if k == "$numberLong":
                return int(v)
            if k in ("$minValue", "$maxValue"):
                return None
        return {k: unwrap(v) for k, v in o.items()}
    if isinstance(o, list):
        return [unwrap(v) for v in o]
    return o


def print_failures(failures: list[str]) -> None:
    if failures:
        print(f"\n!! {len(failures)} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"   - {f}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", default=str(DEFAULT_LIBRARY), help="Playnite library folder")
    ap.add_argument("--dll", help="Path to Playnite's LiteDB.dll")
    ap.add_argument("--out", default="playnite_export", help="Output folder")
    ap.add_argument("--duckdb", default=None, help="DuckDB file (default: <out>/playnite.duckdb)")
    args = ap.parse_args()

    library = Path(args.library)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.duckdb) if args.duckdb else out / "playnite.duckdb"

    dll = find_litedb_dll(args.dll)
    print(f"Using {dll}")
    JsonSerializer, LiteDatabase = load_litedb(dll)

    exported: list[tuple[str, Path, int]] = []
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        for src in sorted(library.glob("*.db")):
            copy = Path(tmp) / src.name
            try:
                shutil.copy2(src, copy)  # never touch the live file
                with LiteDatabase(f"Filename={copy};ReadOnly=true") as db:
                    for coll_name in db.GetCollectionNames():
                        table = src.stem if coll_name.lower() in (src.stem.lower(), "") else f"{src.stem}_{coll_name}"
                        target = out / f"{table}.ndjson"
                        n = 0
                        with open(target, "w", encoding="utf-8") as f:
                            for doc in db.GetCollection(coll_name).FindAll():
                                row = unwrap(json.loads(JsonSerializer.Serialize(doc)))
                                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                                n += 1
                        print(f"{src.name:28} -> {target.name:28} {n:>6} rows")
                        exported.append((table, target, n))
            except Exception as e:  # keep going if one file is odd
                print(f"!! {src.name}: {e}")
                failures.append(f"{src.name}: {e}")

    con = duckdb.connect(str(db_path))
    for table, target, n in exported:
        if n == 0:
            # An empty collection must not leave a previous run's data behind. If the
            # view needs it, create_playnite_games_view recreates an empty typed stub.
            try:
                con.execute(f'DROP TABLE IF EXISTS "{table}"')
            except Exception as e:
                print(f"!! DuckDB drop failed for {table}: {e}")
                failures.append(f"DuckDB drop {table}: {e}")
            continue
        path = target.resolve().as_posix().replace("'", "''")
        try:
            con.execute(
                f'CREATE OR REPLACE TABLE "{table}" AS '
                f"SELECT * FROM read_json('{path}', format='newline_delimited', "
                f"sample_size=-1, union_by_name=true)"
            )
        except Exception as e:
            print(f"!! DuckDB load failed for {table}: {e}")
            failures.append(f"DuckDB load {table}: {e}")
    try:
        create_playnite_games_view(con)
    except duckdb.Error as e:
        con.close()
        print_failures(failures)
        sys.exit(f"!! Couldn't create playnite_games view: {e}")
    con.close()
    print(f"\nDone. DuckDB file: {db_path}")
    if failures:
        print_failures(failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
