"""Backup fallback, --from-backup and --fresh-backup (part of #10). No real Playnite is involved."""
import json
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import playnite_to_duckdb as m


def write_backup(path: Path, stamp: str, files=("games.db", "genres.db"), extra=True) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name in files:
            z.writestr(f"library/{name}", f"data of {name} in {stamp}")
        if extra:
            z.writestr("config.json", "{}")
            z.writestr("library/database.json", "{}")
            z.writestr("library/nested/other.db", "nope")
    return path


def make_backup(folder: Path, stamp: str, **kw) -> Path:
    return write_backup(folder / f"PlayniteBackup-{stamp}.zip", stamp, **kw)


def args(**kw):
    base = dict(from_backup=None, fresh_backup=False, backup_timeout=5)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "data" / "library"
    lib.mkdir(parents=True)
    (lib / "games.db").write_text("live games")
    (lib / "series.db").write_text("live series")
    (lib.parent / "config.json").write_text("{}")
    return lib


@pytest.fixture
def lock_files(monkeypatch):
    """Make every copy of a library file fail like Playnite's exclusive lock does."""

    def locked(src, dst, *a, **k):
        e = PermissionError(13, "The process cannot access the file because it is being used by another process")
        e.winerror = 32
        raise e

    monkeypatch.setattr(m.shutil, "copy2", locked)


class TestNewestBackup:
    def test_picks_newest_by_name_timestamp(self, tmp_path):
        make_backup(tmp_path, "2026-01-02-03-04-05")
        newest = make_backup(tmp_path, "2026-11-24-20-56-27")
        make_backup(tmp_path, "2026-11-24-08-00-00")
        (tmp_path / "PlayniteBackup-bogus.zip").write_bytes(b"")
        (tmp_path / "notes.zip").write_bytes(b"")
        assert m.find_newest_backup(tmp_path) == newest

    def test_none_when_empty_or_missing(self, tmp_path):
        assert m.find_newest_backup(tmp_path) is None
        assert m.find_newest_backup(tmp_path / "absent") is None

    def test_timestamp_and_age(self, tmp_path):
        z = make_backup(tmp_path, "2026-11-24-20-56-27")
        assert m.backup_timestamp(z) == datetime(2026, 11, 24, 20, 56, 27)
        assert m.format_age(timedelta(days=3, hours=16, minutes=5)) == "3d 16h"
        assert m.format_age(timedelta(hours=2, minutes=30)) == "2h 30m"
        assert m.format_age(timedelta(seconds=90)) == "1m"

    def test_backup_dir_from_config(self, tmp_path):
        assert m.backup_dir_for(tmp_path) == tmp_path / "Backup"
        (tmp_path / "config.json").write_text(json.dumps({"AutoBackupDir": str(tmp_path / "elsewhere")}))
        assert m.backup_dir_for(tmp_path) == tmp_path / "elsewhere"
        (tmp_path / "config.json").write_text("not json")
        assert m.backup_dir_for(tmp_path) == tmp_path / "Backup"


class TestLockDetection:
    def test_only_sharing_and_lock_violations_count(self):
        def err(code):
            e = PermissionError(13, "x")
            e.winerror = code
            return e

        assert m.is_lock_error(err(32)) and m.is_lock_error(err(33))
        assert not m.is_lock_error(err(5))  # access denied is a real failure
        assert not m.is_lock_error(PermissionError(13, "no winerror"))
        assert not m.is_lock_error(FileNotFoundError(2, "gone"))

    def test_plain_permission_error_is_a_failure_not_a_fallback(self, library, tmp_path, monkeypatch):
        def denied(src, dst, *a, **k):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(m.shutil, "copy2", denied)
        failures = m.stage_library(args(), library, tmp_path / "staged", tmp_path)
        assert len(failures) == 2


class TestAutoBackupDir:
    def test_expands_env_and_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_BACKUPS", str(tmp_path / "from-env"))
        (tmp_path / "config.json").write_text(json.dumps({"AutoBackupDir": "%MY_BACKUPS%" if m.os.name == "nt" else "$MY_BACKUPS"}))
        assert m.backup_dir_for(tmp_path) == tmp_path / "from-env"
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        (tmp_path / "config.json").write_text(json.dumps({"AutoBackupDir": "~/bk"}))
        assert m.backup_dir_for(tmp_path) == tmp_path / "home" / "bk"

    def test_relative_path_is_relative_to_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path.parent)
        (tmp_path / "config.json").write_text(json.dumps({"AutoBackupDir": "Backups2"}))
        assert m.backup_dir_for(tmp_path) == tmp_path / "Backups2"


class TestExtract:
    def test_extracts_only_library_db_files(self, tmp_path):
        z = make_backup(tmp_path, "2026-11-24-20-56-27")
        dest = tmp_path / "out"
        assert m.extract_library(z, dest) == 2
        assert sorted(p.name for p in dest.iterdir()) == ["games.db", "genres.db"]
        assert (dest / "games.db").read_text() == "data of games.db in 2026-11-24-20-56-27"

    def test_zip_without_library_is_an_error(self, tmp_path):
        z = make_backup(tmp_path, "2026-11-24-20-56-27", files=(), extra=False)
        with pytest.raises(ValueError):
            m.extract_library(z, tmp_path / "out")

    def test_backslash_entry_names_count_as_library_and_complete(self, tmp_path):
        z = tmp_path / "PlayniteBackup-2026-11-24-20-56-27.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("library\\games.db", "data")
        assert m.zip_is_complete(z)
        assert m.extract_library(z, tmp_path / "out") == 1
        assert (tmp_path / "out" / "games.db").read_text() == "data"

    def test_zip_without_library_is_not_complete(self, tmp_path):
        assert not m.zip_is_complete(make_backup(tmp_path, "2026-11-24-20-56-27", files=(), extra=False))

    def test_does_not_modify_the_backup(self, tmp_path):
        z = make_backup(tmp_path, "2026-11-24-20-56-27")
        before = z.read_bytes()
        m.extract_library(z, tmp_path / "out")
        assert z.read_bytes() == before


class TestLockFallback:
    def test_unlocked_library_is_copied_directly(self, library, tmp_path, capsys):
        staged = tmp_path / "staged"
        assert m.stage_library(args(), library, staged, tmp_path) == []
        assert (staged / "games.db").read_text() == "live games"
        assert "backup" not in capsys.readouterr().out.lower()

    def test_lock_falls_back_to_newest_backup_with_warning(self, library, tmp_path, lock_files, capsys):
        backups = library.parent / "Backup"
        backups.mkdir()
        make_backup(backups, "2026-01-01-00-00-00")
        make_backup(backups, "2026-11-24-20-56-27")
        staged = tmp_path / "staged"
        assert m.stage_library(args(), library, staged, tmp_path) == []
        assert (staged / "games.db").read_text() == "data of games.db in 2026-11-24-20-56-27"
        assert not (staged / "series.db").exists()  # nothing left over from the failed live copy
        out = capsys.readouterr().out
        assert "locked" in out and "PlayniteBackup-2026-11-24-20-56-27.zip" in out
        assert "2026-11-24 20:56:27" in out and "ago" in out

    def test_unusable_newest_backup_falls_back_to_next_newest(self, library, tmp_path, lock_files, capsys):
        backups = library.parent / "Backup"
        backups.mkdir()
        make_backup(backups, "2026-01-01-00-00-00")
        make_backup(backups, "2026-06-01-00-00-00", files=(), extra=False)  # no library/*.db
        (backups / "PlayniteBackup-2026-11-24-20-56-27.zip").write_bytes(b"corrupt")
        staged = tmp_path / "staged"
        m.stage_library(args(), library, staged, tmp_path)
        assert (staged / "games.db").read_text() == "data of games.db in 2026-01-01-00-00-00"
        out = capsys.readouterr().out
        assert out.count("trying the next-newest backup") == 2

    def test_all_backups_unusable_exits(self, library, tmp_path, lock_files):
        backups = library.parent / "Backup"
        backups.mkdir()
        (backups / "PlayniteBackup-2026-11-24-20-56-27.zip").write_bytes(b"corrupt")
        with pytest.raises(SystemExit) as exc:
            m.stage_library(args(), library, tmp_path / "staged", tmp_path)
        assert "could be used" in str(exc.value) and "--fresh-backup" in str(exc.value)

    def test_uses_autobackupdir_from_config(self, library, tmp_path, lock_files):
        custom = tmp_path / "custom-backups"
        custom.mkdir()
        make_backup(custom, "2026-05-05-05-05-05")
        (library.parent / "config.json").write_text(json.dumps({"AutoBackupDir": str(custom)}))
        staged = tmp_path / "staged"
        m.stage_library(args(), library, staged, tmp_path)
        assert (staged / "games.db").read_text() == "data of games.db in 2026-05-05-05-05-05"

    def test_lock_without_backup_tells_user_what_to_do(self, library, tmp_path, lock_files):
        with pytest.raises(SystemExit) as exc:
            m.stage_library(args(), library, tmp_path / "staged", tmp_path)
        msg = str(exc.value)
        assert "Close Playnite" in msg and "--fresh-backup" in msg

    def test_other_copy_errors_are_failures_not_fallback(self, library, tmp_path, monkeypatch):
        def broken(src, dst, *a, **k):
            raise OSError("disk on fire")

        monkeypatch.setattr(m.shutil, "copy2", broken)
        failures = m.stage_library(args(), library, tmp_path / "staged", tmp_path)
        assert len(failures) == 2 and "disk on fire" in failures[0]

    def test_from_backup_skips_live_copy(self, library, tmp_path, lock_files):
        z = make_backup(tmp_path, "2026-03-03-03-03-03")
        staged = tmp_path / "staged"
        m.stage_library(args(from_backup=str(z)), library, staged, tmp_path)
        assert (staged / "games.db").read_text() == "data of games.db in 2026-03-03-03-03-03"

    def test_from_backup_bad_path(self, library, tmp_path):
        with pytest.raises(SystemExit) as exc:
            m.stage_library(args(from_backup=str(tmp_path / "missing.zip")), library, tmp_path / "s", tmp_path)
        assert "Couldn't read backup" in str(exc.value)


class TestFreshBackup:
    @pytest.fixture
    def fake_exe(self, monkeypatch, tmp_path):
        exe = tmp_path / "Playnite.DesktopApp.exe"
        exe.write_bytes(b"")
        monkeypatch.setattr(m, "find_playnite_exe", lambda: exe)
        monkeypatch.setattr(m, "BACKUP_POLL_SECONDS", 0.05)
        return exe

    def test_waits_for_complete_zip_and_exports_from_it(self, library, tmp_path, fake_exe, monkeypatch, capsys):
        """Fake Playnite reads the --backup config and writes the zip slowly, truncated first."""
        configs = []

        def launch(exe, config):
            cfg = json.loads(Path(config).read_text())
            configs.append(cfg)
            final = Path(cfg["OutputDir"]) / "PlayniteBackup-2026-11-28-12-00-00.zip"

            def work():
                final.write_bytes(b"PK\x03\x04 truncated")
                time.sleep(0.3)
                write_backup(final, "2026-11-28-12-00-00")

            threading.Thread(target=work, daemon=True).start()

        monkeypatch.setattr(m, "launch_playnite_backup", launch)
        staged = tmp_path / "staged"
        assert m.stage_library(args(fresh_backup=True, backup_timeout=10), library, staged, tmp_path, fake_exe) == []
        assert (staged / "games.db").read_text() == "data of games.db in 2026-11-28-12-00-00"
        (cfg,) = configs
        assert cfg["BackupItems"] == [1]
        assert cfg["LibraryDir"] == str(library)
        assert Path(cfg["OutputDir"]).parent == tmp_path
        assert "RESTART" in capsys.readouterr().out

    def test_times_out_when_no_zip_appears(self, library, tmp_path, fake_exe, monkeypatch):
        monkeypatch.setattr(m, "launch_playnite_backup", lambda exe, config: None)
        with pytest.raises(SystemExit) as exc:
            m.fresh_backup(fake_exe, library, tmp_path, timeout=0.3)
        assert "Timed out" in str(exc.value)

    def test_corrupt_zip_is_never_accepted(self, library, tmp_path, fake_exe, monkeypatch):
        def launch(exe, config):
            out = Path(json.loads(Path(config).read_text())["OutputDir"])
            (out / "PlayniteBackup-2026-11-28-12-00-00.zip").write_bytes(b"not a zip")

        monkeypatch.setattr(m, "launch_playnite_backup", launch)
        with pytest.raises(SystemExit):
            m.fresh_backup(fake_exe, library, tmp_path, timeout=0.4)

    def test_file_vanishing_mid_poll_is_not_a_crash(self, library, tmp_path, fake_exe, monkeypatch):
        real_stat = Path.stat
        calls = {"n": 0}

        def flaky_stat(self, *a, **k):
            if self.suffix == ".zip" and calls["n"] < 2:
                calls["n"] += 1
                raise FileNotFoundError(2, "renamed away")
            return real_stat(self, *a, **k)

        def launch(exe, config):
            out = Path(json.loads(Path(config).read_text())["OutputDir"])
            make_backup(out, "2026-11-28-12-00-00")

        monkeypatch.setattr(m, "launch_playnite_backup", launch)
        monkeypatch.setattr(Path, "stat", flaky_stat)
        zip_path = m.fresh_backup(fake_exe, library, tmp_path, timeout=5)
        assert zip_path.name == "PlayniteBackup-2026-11-28-12-00-00.zip" and calls["n"] == 2

    def test_no_playnite_exe_fails_early_with_advice(self, monkeypatch):
        monkeypatch.setattr(m, "find_playnite_exe", lambda *a, **k: None)
        with pytest.raises(SystemExit) as exc:
            m.require_playnite_exe()
        msg = str(exc.value)
        assert "Start Playnite" in msg and "GAMELIB_LITEDB" in msg and "--dll" in msg

    def test_main_checks_exe_before_loading_dotnet(self, monkeypatch):
        monkeypatch.setattr(m, "find_playnite_exe", lambda *a, **k: None)
        monkeypatch.setattr(m, "load_litedb", lambda dll: pytest.fail("loaded .NET before checking for Playnite"))
        monkeypatch.setattr(m.sys, "argv", ["gamelib-export", "--fresh-backup", "--out", "ignored-by-test"])
        monkeypatch.setattr(m.Path, "mkdir", lambda *a, **k: None)
        with pytest.raises(SystemExit) as exc:
            m.main()
        assert "--fresh-backup" in str(exc.value)

    def test_exe_found_next_to_explicit_dll(self, tmp_path, monkeypatch):
        monkeypatch.delenv(m.LITEDB_ENV, raising=False)
        monkeypatch.setattr(m, "_install_dir_probes", lambda: [])
        (tmp_path / "LiteDB.dll").write_bytes(b"")
        (tmp_path / m.DESKTOP_EXE).write_bytes(b"")
        assert m.find_playnite_exe(str(tmp_path / "LiteDB.dll")) == tmp_path / m.DESKTOP_EXE
