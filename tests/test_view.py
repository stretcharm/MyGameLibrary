"""playnite_games view SQL, run against a tiny DuckDB shaped like Playnite's export."""
import datetime as dt

import duckdb
import pytest

from game_insights import load_games
from playnite_to_duckdb import create_playnite_games_view


def view_rows(con):
    """Rows of the view keyed by game name."""
    cur = con.execute("SELECT * FROM playnite_games")
    cols = [d[0] for d in cur.description]
    return {r[1]: dict(zip(cols, r)) for r in cur.fetchall()}


@pytest.fixture
def lookups():
    return dict(
        genres={"rpg": "RPG", "act": "Action", "zen": "Zen, Chill"},
        series={"s-b": "B Series", "s-a": "A Series"},
        sources={"steam": "Steam"},
    )


class TestScore:
    """Regression for #2: unscored games are NULL, not 0."""

    @pytest.fixture
    def rows(self, build_db):
        con = build_db(
            [
                {"Name": "none"},
                {"Name": "community", "CommunityScore": 80},
                {"Name": "critic", "CriticScore": 70},
                {"Name": "both", "CommunityScore": 80, "CriticScore": 70},
            ]
        )
        create_playnite_games_view(con)
        return view_rows(con)

    def test_neither_score_is_null(self, rows):
        assert rows["none"]["score"] is None

    def test_one_score_is_used_as_is(self, rows):
        assert rows["community"]["score"] == 80
        assert rows["critic"]["score"] == 70

    def test_two_scores_are_averaged(self, rows):
        assert rows["both"]["score"] == 75


class TestListColumns:
    """Regression for #3: genres/series are sorted lists; names are never split on commas."""

    def test_sorted_lists_and_comma_in_name(self, build_db, lookups):
        con = build_db(
            [{"Name": "g", "GenreIds": ["rpg", "zen", "act"], "SeriesIds": ["s-b", "s-a"]}], **lookups
        )
        create_playnite_games_view(con)
        row = view_rows(con)["g"]
        assert row["genres"] == ["Action", "RPG", "Zen, Chill"]
        assert row["series"] == ["A Series", "B Series"]

    def test_empty_when_none(self, build_db, lookups):
        con = build_db([{"Name": "plain"}, {"Name": "empty", "GenreIds": [], "SeriesIds": []}], **lookups)
        create_playnite_games_view(con)
        rows = view_rows(con)
        for name in ("plain", "empty"):
            assert rows[name]["genres"] == []
            assert rows[name]["series"] == []

    def test_load_games_reads_the_view(self, build_db, lookups, tmp_path):
        db_file = tmp_path / "fixture.duckdb"
        con = build_db([{"Name": "g", "GenreIds": ["zen", "act"]}], path=db_file, **lookups)
        create_playnite_games_view(con)
        con.close()

        (game,) = load_games(db_file)
        assert game["name"] == "g"
        assert game["genres"] == ["Action", "Zen, Chill"]
        assert game["series"] == []
        assert game["score"] is None
        assert game["store"] == "Unknown"


class TestColumns:
    def test_flattened_values(self, build_db, lookups):
        con = build_db(
            [
                {
                    "Name": "Hades",
                    "SourceId": "steam",
                    "Playtime": 5400,
                    "IsInstalled": True,
                    "InstallSize": 1024,
                    "CommunityScore": 90,
                    "ReleaseDate": dt.date(2020, 9, 17),
                    "PlayCount": 3,
                    "CompletionStatus": "Playing",
                    "Hidden": False,
                }
            ],
            **lookups,
        )
        create_playnite_games_view(con)
        row = view_rows(con)["Hades"]
        assert row["store_source"] == "Steam"
        assert row["hours_played"] == 1.5
        assert row["is_installed"] is True
        assert row["install_size"] == 1024
        assert row["release_date"] == dt.date(2020, 9, 17)
        assert row["play_count"] == 3
        assert row["completion_status"] == "Playing"
        assert row["is_hidden"] is False

    def test_game_without_a_source_still_appears(self, build_db, lookups):
        con = build_db([{"Name": "orphan"}], **lookups)
        create_playnite_games_view(con)
        assert view_rows(con)["orphan"]["store_source"] is None


class TestDegradation:
    """Regression for #1/#5: missing inputs warn and yield NULL/empty columns; no games table is fatal."""

    def test_missing_lookup_table(self, build_db, lookups, capsys):
        con = build_db([{"Name": "g", "GenreIds": ["rpg"], "SeriesIds": ["s-a"]}], skip_tables=("series",), **lookups)
        create_playnite_games_view(con)
        row = view_rows(con)["g"]
        assert row["genres"] == ["RPG"]
        assert row["series"] == []
        assert "series missing from the export" in capsys.readouterr().out

    def test_missing_every_lookup_table(self, build_db, capsys):
        con = build_db([{"Name": "g"}], skip_tables=("genres_Genre", "series", "sources_GameSource"))
        create_playnite_games_view(con)
        row = view_rows(con)["g"]
        assert row["genres"] == [] and row["series"] == [] and row["store_source"] is None
        assert capsys.readouterr().out.count("missing from the export") == 3

    def test_missing_games_column(self, build_db, lookups, capsys):
        con = build_db([{"Name": "g", "CommunityScore": 80}], skip_columns=("CriticScore", "Hidden"), **lookups)
        create_playnite_games_view(con)
        row = view_rows(con)["g"]
        assert row["critic_score"] is None
        assert row["is_hidden"] is None
        assert row["score"] == 80  # the column that is present still works
        out = capsys.readouterr().out
        assert "games_Game.CriticScore missing" in out
        assert "games_Game.Hidden missing" in out

    def test_missing_games_table_raises(self, build_db):
        con = build_db([], skip_tables=("games_Game",))
        with pytest.raises(duckdb.Error, match="games_Game"):
            create_playnite_games_view(con)

    def test_complete_export_prints_no_warnings(self, build_db, lookups, capsys):
        con = build_db([{"Name": "g"}], **lookups)
        create_playnite_games_view(con)
        assert capsys.readouterr().out == ""
