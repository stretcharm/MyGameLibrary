from game_insights import detect_duplicates


def kinds(groups):
    return [g["kind"] for g in groups]


def test_no_duplicates(make_game):
    assert detect_duplicates([make_game("Portal"), make_game("Portal 2")]) == []


def test_same_store_exact(make_game):
    a = make_game("Hades", "Steam", 5)
    b = make_game("HADES", "Steam", 1)
    groups = detect_duplicates([a, b, make_game("Celeste", "Steam")])
    assert kinds(groups) == ["same_store_exact"]
    g = groups[0]
    assert g["stores"] == ["Steam"]
    assert g["max_hours"] == 5
    assert [it["id"] for it in g["items"]] == [a["id"], b["id"]]  # most played first
    assert "2×" in g["reason"]


def test_cross_store(make_game):
    groups = detect_duplicates([make_game("Hades", "Steam", 3), make_game("Hades", "GOG", 0)])
    assert kinds(groups) == ["cross_store"]
    assert groups[0]["stores"] == ["GOG", "Steam"]


def test_exact_and_cross_store_together(make_game):
    games = [
        make_game("Hades", "Steam"),
        make_game("Hades", "Steam"),
        make_game("Hades", "GOG"),
    ]
    assert kinds(detect_duplicates(games)) == ["same_store_exact", "cross_store"]


def test_alias_identical_playtime_with_shared_tokens(make_game):
    a = make_game("FIFA 11", "Steam", 12.5)
    b = make_game("FIFA Soccer 11", "Steam", 12.5)
    groups = detect_duplicates([a, b])
    assert kinds(groups) == ["same_store_alias"]
    assert "Identical playtime" in groups[0]["reason"]
    assert groups[0]["title"] == "FIFA 11"  # shortest name


def test_alias_playtime_path_needs_shared_tokens(make_game):
    games = [make_game("Portal", "Steam", 4), make_game("Stardew Valley", "Steam", 4)]
    assert detect_duplicates(games) == []


def test_alias_playtime_path_ignores_under_one_hour(make_game):
    games = [make_game("FIFA 11", "Steam", 0.5), make_game("FIFA Soccer 11", "Steam", 0.5)]
    assert detect_duplicates(games) == []


def test_alias_playtime_path_is_per_store(make_game):
    games = [make_game("FIFA 11", "Steam", 10), make_game("FIFA Soccer 11", "GOG", 10)]
    assert detect_duplicates(games) == []


def test_alias_trailing_edition(make_game):
    groups = detect_duplicates([make_game("Skyrim", "Steam", 3), make_game("Skyrim Special Edition", "Steam", 9)])
    assert kinds(groups) == ["same_store_alias"]
    assert "editions" in groups[0]["reason"]
    assert groups[0]["title"] == "Skyrim"


def test_mid_title_word_is_not_an_edition(make_game):
    games = [make_game("Gold Rush: The Game", "Steam"), make_game("Rush: The Game", "Steam")]
    assert detect_duplicates(games) == []


def test_edition_across_stores_is_not_a_duplicate(make_game):
    games = [make_game("Skyrim", "Steam"), make_game("Skyrim Special Edition", "GOG")]
    assert detect_duplicates(games) == []


def test_groups_sorted_by_tier_then_hours(make_game):
    games = [
        make_game("Hades", "Steam", 1),
        make_game("Hades", "GOG", 1),  # cross_store
        make_game("Celeste", "Steam", 2),
        make_game("Celeste", "Steam", 3),  # same_store_exact
        make_game("Skyrim", "Epic", 0),
        make_game("Skyrim Special Edition", "Epic", 0),  # same_store_alias
    ]
    assert kinds(detect_duplicates(games)) == ["same_store_exact", "same_store_alias", "cross_store"]
