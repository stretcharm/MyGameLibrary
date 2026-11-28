"""Shared fixtures: game-dict factory and an in-memory DuckDB builder for the view."""
import uuid

import duckdb
import pytest

from playnite_to_duckdb import VIEW_GAME_COLUMNS


@pytest.fixture
def make_game():
    """Factory for the game dicts that load_games() returns (what analytics/dedup consume)."""
    counter = iter(range(1, 10_000))

    def _make(name, store="Steam", hours=0.0, **overrides):
        game = {
            "id": f"id-{next(counter)}",
            "name": name,
            "store": store,
            "genres": [],
            "series": [],
            "developers": [],
            "hours": hours,
            "installed": False,
            "size_gb": 0.0,
            "community_score": None,
            "critic_score": None,
            "score": None,
            "release_year": None,
            "last_played": None,
            "status": "Not Played",
            "hidden": False,
        }
        game.update(overrides)
        return game

    return _make


def _uid(label: str) -> uuid.UUID:
    """Stable UUID per label so fixtures stay readable."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, label)


@pytest.fixture
def uid():
    return _uid


@pytest.fixture
def build_db():
    """Return a builder that makes an in-memory DuckDB shaped like Playnite's export.

    Pass lookup tables as {label: name} dicts and games as dicts using Playnite
    column names. Pass ``path`` to write a DuckDB file instead of using memory, and
    ``skip_tables`` / ``skip_columns`` to simulate an export
    that left them out.
    """
    connections = []

    def _build(games, genres=None, series=None, sources=None, skip_tables=(), skip_columns=(), path=":memory:"):
        con = duckdb.connect(str(path))
        connections.append(con)
        lookups = {
            "genres_Genre": genres or {},
            "series": series or {},
            "sources_GameSource": sources or {},
        }
        for table, rows in lookups.items():
            if table in skip_tables:
                continue
            con.execute(f'CREATE TABLE "{table}" (_id UUID, "Name" VARCHAR)')
            for label, name in rows.items():
                con.execute(f'INSERT INTO "{table}" VALUES (?, ?)', [_uid(label), name])
        if "games_Game" in skip_tables:
            return con

        columns = {k: v for k, v in VIEW_GAME_COLUMNS.items() if k not in skip_columns}
        col_sql = ", ".join(f'"{c}" {t}' for c, t in columns.items())
        con.execute(f"CREATE TABLE games_Game (_id UUID, {col_sql})")
        for i, g in enumerate(games):
            row = {"_id": _uid(f"game-{i}"), "Name": g.get("Name", f"Game {i}")}
            row.update(g)
            # Translate labels to UUIDs for the id-typed columns.
            for key in ("GenreIds", "SeriesIds", "DeveloperIds"):
                if key in row:
                    row[key] = [_uid(x) for x in row[key]]
            if row.get("SourceId") is not None:
                row["SourceId"] = _uid(row["SourceId"])
            if "CompletionStatus" in row and isinstance(row["CompletionStatus"], str):
                row["CompletionStatus"] = {"_id": _uid(row["CompletionStatus"]), "Name": row["CompletionStatus"]}
            present = {"_id", *columns}
            names = [c for c in row if c in present]
            con.execute(
                f"INSERT INTO games_Game ({', '.join(chr(34) + c + chr(34) for c in names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                [row[c] for c in names],
            )
        return con

    yield _build
    for con in connections:
        con.close()
