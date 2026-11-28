import pytest

from game_insights import normalize_base_title, normalize_title, title_tokens


def test_normalize_title_lowercases_and_drops_punctuation():
    assert normalize_title("The Witcher 3: Wild Hunt!") == "thewitcher3wildhunt"
    assert normalize_title("  Half-Life  2 ") == "halflife2"


def test_normalize_title_empty():
    assert normalize_title("") == ""
    assert normalize_title("::") == ""


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Skyrim Special Edition", "skyrim"),
        ("Witcher 3: Game of the Year Edition", "witcher3"),
        ("Halo - Anniversary", "halo"),
        ("Fallout 4 GOTY", "fallout4"),
        ("Doom (2016)", "doom2016"),
    ],
)
def test_base_title_strips_trailing_editions(name, expected):
    assert normalize_base_title(name) == expected


def test_base_title_does_not_strip_mid_title_words():
    # "Gold" is an edition word, but only as a trailing suffix.
    assert normalize_base_title("Gold Rush: The Game") == "goldrushthegame"
    assert normalize_base_title("Ultimate Chicken Horse") == "ultimatechickenhorse"


def test_base_title_matches_between_editions():
    assert normalize_base_title("Skyrim") == normalize_base_title("Skyrim Special Edition")


def test_title_tokens_lowercases_and_splits():
    assert title_tokens("FIFA Soccer 11") == {"fifa", "soccer", "11"}


def test_title_tokens_drops_single_letters_but_keeps_single_digits():
    assert title_tokens("A Plague Tale 2 X") == {"plague", "tale", "2"}


def test_title_tokens_empty():
    assert title_tokens("") == set()
