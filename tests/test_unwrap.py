from playnite_to_duckdb import unwrap


def test_unwraps_simple_wrappers():
    assert unwrap({"$guid": "abc"}) == "abc"
    assert unwrap({"$date": "2024-01-01T00:00:00Z"}) == "2024-01-01T00:00:00Z"
    assert unwrap({"$oid": "507f"}) == "507f"


def test_number_long_becomes_int():
    assert unwrap({"$numberLong": "9007199254740993"}) == 9007199254740993


def test_min_and_max_value_become_none():
    assert unwrap({"$minValue": "1"}) is None
    assert unwrap({"$maxValue": "1"}) is None


def test_recurses_into_dicts_and_lists():
    doc = {"_id": {"$guid": "g1"}, "Ids": [{"$guid": "a"}, {"$guid": "b"}], "Playtime": {"$numberLong": "7"}}
    assert unwrap(doc) == {"_id": "g1", "Ids": ["a", "b"], "Playtime": 7}


def test_leaves_plain_values_and_multi_key_dicts_alone():
    assert unwrap("x") == "x"
    assert unwrap(3) == 3
    assert unwrap(None) is None
    multi = {"$guid": "a", "other": 1}
    assert unwrap(multi) == multi
    assert unwrap({"Name": "x"}) == {"Name": "x"}
