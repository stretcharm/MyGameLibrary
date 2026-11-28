"""
Export Playnite's LiteDB library files (*.db) to NDJSON + a DuckDB database.

Playnite's .db files are LiteDB (a .NET document store), not SQLite, so DuckDB
can't read them directly. This script loads the LiteDB.dll that ships with
Playnite (via pythonnet), dumps every collection to newline-delimited JSON,
then loads each into a DuckDB table.

Playnite doesn't need closing. It holds its library files exclusively while
running, so if they can't be copied the newest Playnite backup is exported
instead (with a warning about its age). Use --from-backup PATH for a specific
backup, or --fresh-backup to have Playnite write one first (this restarts it).

Usage:
    uv run playnite_to_duckdb.py
    uv run playnite_to_duckdb.py --dll "K:\\Games\\Playnite\\Playnite\\LiteDB.dll" --out export

Exits non-zero (after exporting everything it can) if any file or table failed.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

import gamelib_db

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


LITEDB_ENV = "GAMELIB_LITEDB"
DEFAULT_LITEDB = Path(r"K:\Games\Playnite\Playnite\LiteDB.dll")
PLAYNITE_EXE_NAMES = ("Playnite.DesktopApp.exe", "Playnite.FullscreenApp.exe")
DESKTOP_EXE = "Playnite.DesktopApp.exe"


def _running_playnite_dirs() -> list[Path]:
    """Folders of any running Playnite.DesktopApp.exe / Playnite.FullscreenApp.exe.

    Walks a ToolHelp process snapshot with ctypes. Fails soft: returns [] off
    Windows or if anything goes wrong (e.g. access denied for a process).
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snap is None or snap == ctypes.c_void_p(-1).value:
            return []
        wanted = {n.lower() for n in PLAYNITE_EXE_NAMES}
        dirs: list[Path] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() in wanted:
                    proc = k32.OpenProcess(0x1000, False, entry.th32ProcessID)  # QUERY_LIMITED_INFORMATION
                    if proc:
                        try:
                            buf = ctypes.create_unicode_buffer(32768)
                            size = wintypes.DWORD(len(buf))
                            if k32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
                                folder = Path(buf.value).parent
                                if folder not in dirs:
                                    dirs.append(folder)
                        finally:
                            k32.CloseHandle(proc)
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        return dirs
    except Exception:
        return []


def _registry_install_dirs() -> list[Path]:
    """InstallLocation from the Playnite uninstall key (HKCU, then HKLM). Fails soft."""
    try:
        import winreg
    except ImportError:
        return []
    dirs: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Playnite") as key:
                value, _ = winreg.QueryValueEx(key, "InstallLocation")
            if value:
                dirs.append(Path(str(value)))
        except Exception:
            continue
    return dirs


def _well_known_dirs() -> list[Path]:
    return [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Playnite",
        Path(os.environ.get("PROGRAMFILES", "")) / "Playnite",
        Path(os.environ.get("APPDATA", "")) / "Playnite",
    ]


def _install_dir_probes():
    """(source description, probe -> folders) in search order, after --dll/env var.

    Looked up at call time so tests can monkeypatch each probe.
    """
    return [
        ("a running Playnite process", _running_playnite_dirs),
        ("the Playnite uninstall registry key", _registry_install_dirs),
        ("a well-known install folder", _well_known_dirs),
        ("the built-in default path", lambda: [DEFAULT_LITEDB.parent]),
    ]


def _is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _search_install_dirs(filename: str):
    """Yield (source, path) for each existing `filename` in the install-folder probes."""
    for source, probe in _install_dir_probes():
        try:
            folders = probe()
        except Exception:
            continue
        for folder in folders:
            path = Path(folder) / filename
            if _is_file(path):
                yield source, path


def find_litedb_dll(explicit: str | None) -> Path:
    """Locate LiteDB.dll and print which source supplied it.

    Order: --dll, $GAMELIB_LITEDB, a running Playnite, the uninstall registry
    key, well-known folders, the built-in default path. Every probe fails soft.
    An explicit --dll or $GAMELIB_LITEDB that isn't a file is an error, not a
    reason to fall through to auto-detection.
    """
    for label, value in (("--dll", explicit), (f"${LITEDB_ENV}", os.environ.get(LITEDB_ENV))):
        if value:  # explicit choices are never second-guessed by auto-detection
            path = Path(value)
            if not _is_file(path):
                sys.exit(f"{label} points at {path}, which is not a file. Fix or remove it to auto-detect LiteDB.dll.")
            print(f"Using {path} (from {label})")
            return path
    for source, path in _search_install_dirs("LiteDB.dll"):
        print(f"Using {path} (found via {source})")
        return path
    sys.exit(
        "Couldn't find LiteDB.dll. Pass --dll (or set GAMELIB_LITEDB) pointing at the "
        "copy in your Playnite install folder (the one containing Playnite.DesktopApp.exe)."
    )


def find_playnite_exe(dll_hint: str | None = None) -> Path | None:
    """Playnite.DesktopApp.exe: next to an explicit LiteDB.dll (--dll / $GAMELIB_LITEDB), else
    through the same folders as LiteDB.dll auto-detection."""
    hint = dll_hint or os.environ.get(LITEDB_ENV)
    if hint and _is_file(Path(hint).parent / DESKTOP_EXE):
        return Path(hint).parent / DESKTOP_EXE
    return next((path for _source, path in _search_install_dirs(DESKTOP_EXE)), None)


BACKUP_NAME_RE = re.compile(r"^PlayniteBackup-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.zip$", re.IGNORECASE)
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
BACKUP_POLL_SECONDS = 1.0


class LibraryLocked(Exception):
    """The Playnite library files are held open (exclusively) by a running Playnite."""


def is_lock_error(e: OSError) -> bool:
    """True only for Windows sharing/lock violations (32/33); other permission errors are real failures."""
    return getattr(e, "winerror", None) in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION)


def copy_library(library: Path, dest: Path) -> list[str]:
    """Copy library/*.db into dest. Returns per-file failures (other than locks).

    Raises LibraryLocked if any file is locked; the caller should discard dest.
    """
    dest.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for src in sorted(library.glob("*.db")):
        try:
            shutil.copy2(src, dest / src.name)  # never touch the live file
        except OSError as e:
            if is_lock_error(e):
                raise LibraryLocked(f"{src.name}: {e}") from e
            print(f"!! {src.name}: {e}")
            failures.append(f"{src.name}: {e}")
    return failures


def backup_timestamp(zip_path: Path) -> datetime:
    """Backup time from the PlayniteBackup-YYYY-MM-DD-HH-MM-SS.zip name (local time), else mtime."""
    m = BACKUP_NAME_RE.match(zip_path.name)
    if m:
        return datetime(*map(int, m.groups()))
    return datetime.fromtimestamp(zip_path.stat().st_mtime)


def find_backups(backup_dir: Path) -> list[Path]:
    """PlayniteBackup-*.zip files in backup_dir, newest first (by the timestamp in the name)."""
    try:
        zips = [p for p in backup_dir.iterdir() if BACKUP_NAME_RE.match(p.name) and p.is_file()]
    except OSError:
        return []
    return sorted(zips, key=lambda p: BACKUP_NAME_RE.match(p.name).groups(), reverse=True)


def find_newest_backup(backup_dir: Path) -> Path | None:
    return next(iter(find_backups(backup_dir)), None)


def playnite_data_dir(library: Path) -> Path:
    """Playnite's user data folder (holds config.json): the library's parent, else %APPDATA%\\Playnite."""
    if (library.parent / "config.json").is_file():
        return library.parent
    return Path(os.environ.get("APPDATA", "")) / "Playnite"


def backup_dir_for(data_dir: Path) -> Path:
    """AutoBackupDir from Playnite's config.json, falling back to <data dir>\\Backup.

    Environment variables and ~ are expanded; a relative path is taken relative to
    Playnite's data dir, not the current working directory.
    """
    try:
        config = json.loads((data_dir / "config.json").read_text(encoding="utf-8-sig"))
        configured = config.get("AutoBackupDir")
        if configured:
            path = Path(os.path.expanduser(os.path.expandvars(str(configured))))
            return path if path.is_absolute() else data_dir / path
    except (OSError, ValueError, AttributeError):
        pass
    return data_dir / "Backup"


def format_age(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def library_db_members(z: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """(entry, file name) for each library/*.db in the zip; accepts / or \\ separators."""
    members = []
    for info in z.infolist():
        parts = info.filename.replace("\\", "/").split("/")
        if len(parts) == 2 and parts[0].lower() == "library" and parts[1].lower().endswith(".db"):
            members.append((info, parts[1]))
    return members


def extract_library(zip_path: Path, dest: Path) -> int:
    """Extract only library/*.db from a Playnite backup into dest (flat). Returns the file count."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as z:  # opened read-only; the backup itself is never modified
        for info, name in library_db_members(z):
            with z.open(info) as src, open(dest / name, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    if not count:
        raise ValueError(f"{zip_path.name} has no library/*.db files (was the Library item included?)")
    return count


class BackupUnusable(Exception):
    """A backup zip is missing, corrupt, or has no library/*.db files."""


def stage_from_backup(zip_path: Path, dest: Path, reason: str) -> None:
    """Extract the backup's library files into dest and warn how old they are.

    Raises BackupUnusable if the zip can't be read or has no library files.
    """
    try:
        taken = backup_timestamp(zip_path)
        age = format_age(datetime.now() - taken)
        print(
            f"!! {reason} Exporting from backup {zip_path.name} (taken {taken:%Y-%m-%d %H:%M:%S}, {age} ago). "
            "Changes made in Playnite since then are not included."
        )
        n = extract_library(zip_path, dest)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise BackupUnusable(f"Couldn't read backup {zip_path}: {e}") from e
    print(f"Extracted {n} library file(s) from {zip_path}")


def zip_is_complete(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None and bool(library_db_members(z))
    except (OSError, zipfile.BadZipFile):
        return False


def launch_playnite_backup(exe: Path, config: Path) -> None:
    """Start `Playnite.DesktopApp.exe --backup <config>` without waiting for it."""
    subprocess.Popen([str(exe), "--backup", str(config)], cwd=str(exe.parent))


def require_playnite_exe(dll_hint: str | None = None) -> Path:
    """Playnite.DesktopApp.exe for --fresh-backup, or exit with advice. Cheap, so run it early."""
    exe = find_playnite_exe(dll_hint)
    if exe is None:
        sys.exit(
            "--fresh-backup needs Playnite.DesktopApp.exe but couldn't find it. Start Playnite (so the "
            "running process can be found), or set GAMELIB_LITEDB / pass --dll pointing at the LiteDB.dll "
            "next to Playnite.DesktopApp.exe."
        )
    return exe


def fresh_backup(exe: Path, library: Path, work: Path, timeout: float, poll: float | None = None) -> Path:
    """Have Playnite write a new backup (restarting Playnite) and return the finished zip.

    Writes a temp backup config whose OutputDir is a temp folder and which includes
    only the core library item, launches Playnite's own backup, then waits for a
    complete zip: stable size and opens as a zip.
    """
    out_dir = work / "fresh-backup"
    out_dir.mkdir(parents=True, exist_ok=True)
    config = work / "backup-config.json"
    config.write_text(
        json.dumps(
            {
                "DataDir": str(playnite_data_dir(library)),
                "LibraryDir": str(library),
                "OutputDir": str(out_dir),
                # BackupDataItem: Settings=0, Library=1, LibraryFiles=2 (media), Extensions=3, Themes=4,
                # ExtensionsData=5. Playnite's Backup.cs always writes the settings and core library
                # files whatever is listed; 1 just names the Library item and adds nothing else.
                "BackupItems": [1],
                "ClosedWhenDone": False,
                "CancelIfGameRunning": True,  # don't restart Playnite under a running game
                "RotatingBackups": 0,
            }
        ),
        encoding="utf-8",
    )
    print(
        f"!! --fresh-backup: Playnite ({exe}) will now RESTART to write a backup. "
        f"Waiting up to {timeout:g}s for it."
    )
    launch_playnite_backup(exe, config)
    poll = BACKUP_POLL_SECONDS if poll is None else poll
    deadline = time.monotonic() + timeout
    last: tuple[Path, int] | None = None
    while time.monotonic() < deadline:
        time.sleep(poll)
        try:
            newest = find_newest_backup(out_dir)
            if newest is None:  # OutputDir naming is Playnite's own, but tolerate any zip
                newest = max(out_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, default=None)
            if newest is None:
                continue
            size = newest.stat().st_size
        except OSError:  # renamed/rotated mid-poll: not ready yet
            last = None
            continue
        if size > 0 and last == (newest, size) and zip_is_complete(newest):
            return newest
        last = (newest, size)
    sys.exit(
        f"Timed out after {timeout:g}s waiting for Playnite to write a backup. Playnite cancels the "
        "backup if a game is running. Close the game, raise --backup-timeout, or use --from-backup."
    )


def stage_library(args, library: Path, staged: Path, work: Path, playnite_exe: Path | None = None) -> list[str]:
    """Put a private copy of the library *.db files in `staged`; return non-fatal copy failures.

    Order: --from-backup, --fresh-backup (needs playnite_exe), a direct copy of the
    live files, then (if Playnite has them locked) the newest usable Playnite backup.
    """
    if args.from_backup:
        try:
            stage_from_backup(Path(args.from_backup), staged, "--from-backup given.")
        except BackupUnusable as e:
            sys.exit(str(e))
        return []
    if args.fresh_backup:
        zip_path = fresh_backup(playnite_exe or require_playnite_exe(), library, work, args.backup_timeout)
        try:
            stage_from_backup(zip_path, staged, "Fresh backup written.")
        except BackupUnusable as e:
            sys.exit(str(e))
        return []
    try:
        return copy_library(library, staged)
    except LibraryLocked as e:
        shutil.rmtree(staged, ignore_errors=True)
        backup_dir = backup_dir_for(playnite_data_dir(library))
        backups = find_backups(backup_dir)
        if not backups:
            sys.exit(
                f"The Playnite library is locked ({e}) and no backup was found in {backup_dir}. "
                "Close Playnite, or use --fresh-backup to have Playnite write one (this restarts Playnite)."
            )
        for zip_path in backups:
            try:
                stage_from_backup(zip_path, staged, "Playnite has the library locked.")
                return []
            except BackupUnusable as err:
                print(f"!! {err}; trying the next-newest backup")
        sys.exit(
            f"The Playnite library is locked ({e}) and none of the {len(backups)} backup(s) in {backup_dir} "
            "could be used. Close Playnite, or use --fresh-backup to have Playnite write one (this restarts Playnite)."
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
    ap.add_argument("--dll", help="Path to Playnite's LiteDB.dll (default: $GAMELIB_LITEDB, else auto-detected)")
    ap.add_argument("--out", default="playnite_export", help="Output folder")
    ap.add_argument("--duckdb", default=None, help="DuckDB file (default: <out>/playnite.duckdb)")
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--from-backup", metavar="PATH", help="Export from this Playnite backup zip instead of the live library")
    source.add_argument(
        "--fresh-backup",
        action="store_true",
        help="Have Playnite write a new backup first, then export from it (RESTARTS Playnite)",
    )
    ap.add_argument("--backup-timeout", type=float, default=180, help="Seconds to wait for --fresh-backup (default: 180)")
    args = ap.parse_args()

    library = Path(args.library)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.duckdb) if args.duckdb else out / "playnite.duckdb"
    playnite_exe = require_playnite_exe(args.dll) if args.fresh_backup else None  # before loading .NET
    gamelib_db.connect(db_path).close()  # fail fast (before the slow dump) if the DuckDB file is locked

    dll = find_litedb_dll(args.dll)
    JsonSerializer, LiteDatabase = load_litedb(dll)

    exported: list[tuple[str, Path, int]] = []
    failures: list[str] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        staged = Path(tmp) / "library"
        failures += stage_library(args, library, staged, Path(tmp), playnite_exe)
        for src in sorted(staged.glob("*.db")):
            try:
                with LiteDatabase(f"Filename={src};ReadOnly=true") as db:
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

    con = gamelib_db.connect(db_path)
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
