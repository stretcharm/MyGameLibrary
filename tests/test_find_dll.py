"""LiteDB.dll discovery order (#20). Probes are monkeypatched; nothing touches real Playnite."""
import pytest

import playnite_to_duckdb as m


@pytest.fixture
def probes(monkeypatch, tmp_path):
    """Four install folders, each holding a LiteDB.dll, wired in as the four probes."""
    monkeypatch.delenv(m.LITEDB_ENV, raising=False)
    folders = {}
    for name in ("running", "registry", "known", "default"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "LiteDB.dll").write_bytes(b"")
        folders[name] = folder
    monkeypatch.setattr(m, "_running_playnite_dirs", lambda: [folders["running"]])
    monkeypatch.setattr(m, "_registry_install_dirs", lambda: [folders["registry"]])
    monkeypatch.setattr(m, "_well_known_dirs", lambda: [folders["known"]])
    monkeypatch.setattr(m, "DEFAULT_LITEDB", folders["default"] / "LiteDB.dll")
    return folders


def test_order_of_sources(probes, monkeypatch, tmp_path, capsys):
    explicit = tmp_path / "explicit.dll"
    explicit.write_bytes(b"")
    env = tmp_path / "env.dll"
    env.write_bytes(b"")
    monkeypatch.setenv(m.LITEDB_ENV, str(env))

    assert m.find_litedb_dll(str(explicit)) == explicit
    assert "--dll" in capsys.readouterr().out
    assert m.find_litedb_dll(None) == env
    assert m.LITEDB_ENV in capsys.readouterr().out
    monkeypatch.delenv(m.LITEDB_ENV)

    for name, source in [
        ("running", "running Playnite"),
        ("registry", "registry"),
        ("known", "well-known"),
        ("default", "default"),
    ]:
        assert m.find_litedb_dll(None) == probes[name] / "LiteDB.dll"
        assert source in capsys.readouterr().out
        (probes[name] / "LiteDB.dll").unlink()


def test_missing_explicit_dll_is_an_error(probes, tmp_path):
    with pytest.raises(SystemExit) as exc:
        m.find_litedb_dll(str(tmp_path / "nope.dll"))
    assert "--dll" in str(exc.value) and "not a file" in str(exc.value)


def test_missing_env_dll_is_an_error(probes, monkeypatch, tmp_path):
    monkeypatch.setenv(m.LITEDB_ENV, str(tmp_path / "nope.dll"))
    with pytest.raises(SystemExit) as exc:
        m.find_litedb_dll(None)
    assert m.LITEDB_ENV in str(exc.value)


def test_failing_probe_is_skipped(probes, monkeypatch):
    def boom():
        raise OSError("access denied")

    monkeypatch.setattr(m, "_running_playnite_dirs", boom)
    assert m.find_litedb_dll(None) == probes["registry"] / "LiteDB.dll"


def test_nothing_found_message(probes, monkeypatch):
    for folder in probes.values():
        (folder / "LiteDB.dll").unlink()
    with pytest.raises(SystemExit) as exc:
        m.find_litedb_dll(None)
    assert "GAMELIB_LITEDB" in str(exc.value)
    assert "--dll" in str(exc.value)


def test_find_playnite_exe_uses_same_folders(probes):
    assert m.find_playnite_exe() is None
    (probes["registry"] / m.DESKTOP_EXE).write_bytes(b"")
    assert m.find_playnite_exe() == probes["registry"] / m.DESKTOP_EXE


def test_real_probes_fail_soft():
    """Real probes return lists (possibly empty) and never raise."""
    assert isinstance(m._running_playnite_dirs(), list)
    assert isinstance(m._registry_install_dirs(), list)
