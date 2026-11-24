"""
Export Playnite's LiteDB library files (*.db) to NDJSON + a DuckDB database.

Playnite's .db files are LiteDB (a .NET document store), not SQLite, so DuckDB
can't read them directly. This script loads the LiteDB.dll that ships with
Playnite (via pythonnet), dumps every collection to newline-delimited JSON,
then loads each into a DuckDB table.

Close Playnite before running (files are copied first, but a running Playnite
may have un-flushed changes).

Usage:
    uv add pythonnet
    uv run playnite_to_duckdb.py
    uv run playnite_to_duckdb.py --dll ""K:\\Games\\Playnite\\Playnite\\LiteDB.dll"" --out export
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from pythonnet import load

load("netfx")  # Playnite's LiteDB.dll loads fine on the Windows .NET Framework runtime
import clr  # noqa: E402

import duckdb  # noqa: E402

DEFAULT_LIBRARY = Path(os.environ.get("APPDATA", "")) / "Playnite" / "library"


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
    clr.AddReference(str(dll))
    from LiteDB import JsonSerializer, LiteDatabase  # type: ignore  # noqa: E402

    exported: list[tuple[str, Path, int]] = []

    with tempfile.TemporaryDirectory() as tmp:
        for src in sorted(library.glob("*.db")):
            copy = Path(tmp) / src.name
            shutil.copy2(src, copy)  # never touch the live file
            try:
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

    con = duckdb.connect(str(db_path))
    for table, target, n in exported:
        if n == 0:
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
    con.close()
    print(f"\nDone. DuckDB file: {db_path}")


if __name__ == "__main__":
    main()
