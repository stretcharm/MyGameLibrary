import pytest

from game_insights import build_analytics_and_recommendations as build


def rec_named(payload, name):
    return next(r for r in payload["recommendations"] if r["name"] == name)


@pytest.fixture
def library(make_game):
    return [
        make_game("Alpha", "Steam", 10, score=80.0, installed=True, size_gb=2.0, genres=["RPG"]),
        make_game("Bravo", "GOG", 0, genres=["RPG"]),  # unscored
        make_game("Charlie", "Steam", 0.5, score=60.0, genres=["RPG"]),
        make_game("Secret", "Steam", 100, score=100.0, hidden=True),
    ]


class TestKpis:
    def test_counts_and_totals(self, library):
        k = build(library)["kpis"]
        assert k["total_games"] == 3
        assert k["played_games"] == 2  # anything above 0h, including 0.5h
        assert k["unplayed_games"] == 1
        assert k["played_pct"] == 66.7
        assert k["total_hours"] == 10.5
        assert k["installed_games"] == 1
        assert k["total_install_gb"] == 2.0
        assert k["hidden_games"] == 1
        assert k["include_hidden"] is False

    def test_include_hidden(self, library):
        k = build(library, include_hidden=True)["kpis"]
        assert k["total_games"] == 4
        assert k["total_hours"] == 110.5
        assert k["include_hidden"] is True

    def test_empty_library(self):
        k = build([])["kpis"]
        assert k["total_games"] == 0
        assert k["played_pct"] == 0.0
        assert k["avg_score"] == 0.0


class TestUnscoredGames:
    """Regression for #2: no score means neutral 50, not 0, and no effect on averages."""

    def test_avg_score_ignores_unscored(self, library):
        assert build(library)["kpis"]["avg_score"] == 70.0  # (80 + 60) / 2, Bravo excluded

    def test_scatter_excludes_unscored(self, library):
        names = {p["name"] for p in build(library)["scatter"]}
        assert names == {"Alpha", "Charlie"}

    def test_neutral_fallback_for_quality_points(self, make_game):
        rec = rec_named(build([make_game("Mystery", "Steam", 0)]), "Mystery")
        assert rec["score"] is None
        assert rec["quality_pts"] == 15.0
        assert rec["rec_score"] == 15.0

    def test_scored_game_uses_its_score(self, make_game):
        rec = rec_named(build([make_game("Good", "Steam", 0, score=90.0)]), "Good")
        assert rec["quality_pts"] == 27.0


class TestRecommendations:
    def test_played_games_are_not_recommended(self, library):
        names = {r["name"] for r in build(library)["recommendations"]}
        assert names == {"Bravo", "Charlie"}  # Charlie is < 1h so still a candidate

    def test_pools(self, library):
        payload = build(library)
        assert rec_named(payload, "Bravo")["pool"] == "unplayed"
        assert rec_named(payload, "Charlie")["pool"] == "barely_touched"

    def test_title_played_elsewhere_is_excluded(self, make_game):
        games = [make_game("Hades", "Steam", 20), make_game("Hades", "GOG", 0)]
        assert build(games)["recommendations"] == []

    def test_cross_store_copies_merge_into_one_recommendation(self, make_game):
        games = [
            make_game("Hades", "Steam", 0, score=70.0),
            make_game("Hades", "GOG", 0, score=90.0, installed=True, size_gb=5.0),
        ]
        recs = build(games)["recommendations"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec["stores"] == ["Steam", "GOG"]
        assert rec["store"] == "Steam, GOG"
        assert rec["score"] == 90.0  # the better-scoring copy wins
        assert rec["installed"] is True
        assert rec["size_gb"] == 5.0

    def test_same_store_copies_do_not_repeat_the_store(self, make_game):
        games = [make_game("Hades", "Steam", 0), make_game("Hades", "Steam", 0)]
        rec = build(games)["recommendations"][0]
        assert rec["stores"] == ["Steam"]

    def test_series_bonus_from_played_anchor(self, make_game):
        games = [
            make_game("Mass Effect", "Steam", 30, series=["Mass Effect"]),
            make_game("Mass Effect 2", "Steam", 0, series=["Mass Effect"]),
        ]
        rec = build(games)["recommendations"][0]
        assert rec["bonus_pts"] > 0
        assert any("Same series as Mass Effect" in r for r in rec["reasons"])


class TestAnchors:
    @staticmethod
    def _library(make_game, played):
        games = [make_game(name, "Steam", hrs, genres=["RPG"], **extra) for name, hrs, extra in played]
        games.append(make_game("Unplayed RPG", "Steam", 0, genres=["RPG"], score=80.0))
        return games

    def test_anchor_lists_similar_unplayed_games(self, make_game):
        payload = build(self._library(make_game, [("Skyrim", 50, {})]))
        assert [a["name"] for a in payload["anchors"]] == ["Skyrim"]
        match = payload["anchors"][0]["matches"][0]
        assert match["name"] == "Unplayed RPG"
        assert match["shared_genres"] == ["RPG"]

    def test_empty_and_colon_prefixed_names_do_not_crash(self, make_game):
        """Regression for #4: ': Prologue' and an empty name have no words before the colon."""
        payload = build(self._library(make_game, [(": Prologue", 20, {}), ("", 10, {})]))
        assert [a["name"] for a in payload["anchors"]] == [": Prologue", ""]

    def test_games_sharing_a_name_prefix_yield_one_anchor(self, make_game):
        payload = build(self._library(make_game, [("Borderlands 2", 50, {}), ("Borderlands: Pre-Sequel", 30, {})]))
        assert [a["name"] for a in payload["anchors"]] == ["Borderlands 2"]

    def test_games_sharing_a_series_yield_one_anchor(self, make_game):
        played = [("Fable", 50, {"series": ["Fable"]}), ("Albion", 30, {"series": ["Fable"]})]
        payload = build(self._library(make_game, played))
        assert [a["name"] for a in payload["anchors"]] == ["Fable"]

    def test_game_without_genres_is_never_an_anchor(self, make_game):
        games = [make_game("Plain", "Steam", 50), make_game("Other", "Steam", 0, genres=["RPG"])]
        assert build(games)["anchors"] == []

    def test_at_most_eight_anchors(self, make_game):
        played = [(f"Title{chr(97 + i)} {i}", 50 - i, {}) for i in range(10)]
        assert len(build(self._library(make_game, played))["anchors"]) == 8
