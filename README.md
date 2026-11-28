# gamelib

Export your [Playnite](https://playnite.link/) game library to DuckDB, then
generate a single self-contained HTML dashboard from it.

## Requirements

- Windows (Playnite is Windows-only)
- Playnite installed. The export loads Playnite's own `LiteDB.dll`, because the
  library `*.db` files are LiteDB (a .NET document store), not SQLite.
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12+ and dependencies)

```
uv sync
```

## Workflow

1. **Close Playnite.** A running Playnite locks its library files and the copy
   fails with a sharing violation.
2. **Export** the library to NDJSON and a DuckDB database:

   ```
   uv run gamelib-export
   ```

   Files are copied first (the live library is never touched), dumped to
   NDJSON, and loaded into DuckDB tables plus a flattened `playnite_games`
   view. The default output is `playnite_export/playnite.duckdb`. The command
   exits non-zero if any file or table failed.
3. **Generate the dashboard:**

   ```
   uv run gamelib-insights --open
   ```

   This writes one self-contained HTML file (default
   `playnite_export/game_insights.html`) with KPIs, charts, a recommender,
   "Because you loved" suggestions, duplicate detection and a game explorer.

## CLI options

`gamelib-export` (or `uv run playnite_to_duckdb.py`)

| Option | Default | Description |
| --- | --- | --- |
| `--library` | `%APPDATA%\Playnite\library` | Playnite library folder |
| `--dll` | auto-detected | Path to Playnite's `LiteDB.dll` (see below) |
| `--out` | `playnite_export` | Output folder |
| `--duckdb` | `<out>/playnite.duckdb` | DuckDB file to write |

`LiteDB.dll` is found automatically. The first match wins, and the export prints
which source it used:

1. `--dll`
2. the `GAMELIB_LITEDB` environment variable
3. the folder of a running `Playnite.DesktopApp.exe` / `Playnite.FullscreenApp.exe`
   (this finds portable installs)
4. `InstallLocation` in the
   `Software\Microsoft\Windows\CurrentVersion\Uninstall\Playnite` registry key
   (`HKCU`, then `HKLM`)
5. `%LOCALAPPDATA%\Playnite`, `%PROGRAMFILES%\Playnite`, `%APPDATA%\Playnite`
6. `K:\Games\Playnite\Playnite\LiteDB.dll`

An explicit `--dll` or `GAMELIB_LITEDB` that doesn't exist is an error; it is not
silently replaced by auto-detection. If none of these work, pass `--dll` or set `GAMELIB_LITEDB` to the copy next to
`Playnite.DesktopApp.exe`.

`gamelib-insights` (or `uv run game_insights.py`)

| Option | Default | Description |
| --- | --- | --- |
| `--db` | `playnite_export/playnite.duckdb` | DuckDB file produced by the export |
| `--out` | `playnite_export/game_insights.html` | Output HTML file |
| `--open` | off | Open the dashboard in the default browser |
| `--include-hidden` | off | Include games marked Hidden in Playnite |

Both tools can also be run directly as scripts, e.g.
`uv run playnite_to_duckdb.py --help`.

The default paths are relative to the project folder, so run the tools from a
checkout (the `uv sync` install is editable).

Run tests: `uv run pytest`
