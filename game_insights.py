"""
Generate an interactive HTML dashboard and deterministic game recommender
from Playnite's exported DuckDB database (`playnite.duckdb`).

Usage:
    uv run game_insights.py
    uv run game_insights.py --db playnite_export/playnite.duckdb --out playnite_export/game_insights.html --open
"""
from __future__ import annotations

import argparse
import json
import math
import re
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

DEFAULT_DB = Path(__file__).parent / "playnite_export" / "playnite.duckdb"
DEFAULT_OUT = Path(__file__).parent / "playnite_export" / "game_insights.html"


def split_csv(val: str | None) -> list[str]:
    if not val:
        return []
    return [part.strip() for part in val.split(",") if part.strip()]


def normalize_title(name: str) -> str:
    """Normalize a game title for cross-store deduplication."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


_EDITION_RE = re.compile(
    r"\b(game of the year|goty|definitive|enhanced|complete|remastered|director'?s cut|"
    r"legendary|ultimate|special|deluxe|standard|anniversary|gold|hd|edition)\b",
    re.IGNORECASE,
)


def normalize_base_title(name: str) -> str:
    """Strip common edition suffixes before normalizing."""
    stripped = _EDITION_RE.sub(" ", name.lower())
    return "".join(ch for ch in stripped if ch.isalnum())


def title_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 2 or t.isdigit()}


def detect_duplicates(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Detect duplicate game entries across three tiers:
      1. same_store_exact: identical normalized title on the same storefront.
      2. same_store_alias: same storefront + identical non-zero playtime (>=1h) with shared tokens
         (e.g. FIFA 11 vs FIFA Soccer 11), or identical base title after stripping edition suffixes.
      3. cross_store: same normalized title owned across multiple storefronts.
    """
    groups: list[dict[str, Any]] = []
    seen_id_pairs: set[frozenset[str]] = set()

    def add_group(kind: str, label: str, reason: str, items: list[dict[str, Any]]) -> None:
        ids = frozenset(it["id"] for it in items)
        if len(ids) < 2 or ids in seen_id_pairs:
            return
        seen_id_pairs.add(ids)
        groups.append(
            {
                "kind": kind,
                "label": label,
                "reason": reason,
                "title": min((it["name"] for it in items), key=len),
                "stores": sorted({it["store"] for it in items}),
                "max_hours": max(it["hours"] for it in items),
                "items": sorted(items, key=lambda x: (-x["hours"], x["store"], x["name"])),
            }
        )

    # 1. Exact normalized title duplicates (same store vs cross-store)
    by_norm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in games:
        by_norm[normalize_title(g["name"])].append(g)

    for norm, items in by_norm.items():
        if len(items) < 2:
            continue
        by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for it in items:
            by_store[it["store"]].append(it)
        for store, sitems in by_store.items():
            if len(sitems) > 1:
                add_group(
                    "same_store_exact",
                    "Same-Store Exact",
                    f"Identical title appears {len(sitems)}× on {store}",
                    sitems,
                )
        if len(by_store) > 1:
            stores_str = ", ".join(sorted(by_store))
            add_group(
                "cross_store",
                "Cross-Store",
                f"Owned on {len(by_store)} stores ({stores_str})",
                items,
            )

    # 2. Same-store alias / edition duplicates:
    #    (a) identical non-zero playtime (>= 1h) on the same store + shared title tokens
    by_store_list: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in games:
        by_store_list[g["store"]].append(g)

    for store, sitems in by_store_list.items():
        # Group by rounded hours (>= 1.0h)
        by_hrs: dict[float, list[dict[str, Any]]] = defaultdict(list)
        for it in sitems:
            if it["hours"] >= 1.0:
                by_hrs[round(it["hours"], 2)].append(it)
        for hrs, hitems in by_hrs.items():
            if len(hitems) < 2:
                continue
            for i in range(len(hitems)):
                for j in range(i + 1, len(hitems)):
                    a, b = hitems[i], hitems[j]
                    if normalize_title(a["name"]) == normalize_title(b["name"]):
                        continue
                    ta, tb = title_tokens(a["name"]), title_tokens(b["name"])
                    if not ta or not tb:
                        continue
                    jacc = len(ta & tb) / len(ta | tb)
                    if jacc >= 0.4:
                        add_group(
                            "same_store_alias",
                            "Same-Store Alias",
                            f"Same store ({store}) · Identical playtime ({hrs}h) · Similar title",
                            [a, b],
                        )

        # (b) identical base title after stripping edition suffixes on the same store
        by_base: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for it in sitems:
            base = normalize_base_title(it["name"])
            if base:
                by_base[base].append(it)
        for base, bitems in by_base.items():
            if len(bitems) > 1 and len({normalize_title(x["name"]) for x in bitems}) > 1:
                add_group(
                    "same_store_alias",
                    "Same-Store Edition",
                    f"Same store ({store}) · Multiple editions of same base title",
                    bitems,
                )

    kind_order = {"same_store_exact": 0, "same_store_alias": 1, "cross_store": 2}
    groups.sort(key=lambda g: (kind_order.get(g["kind"], 9), -g["max_hours"], g["title"]))
    return groups


def load_games(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        raise FileNotFoundError(f"DuckDB file not found: {db_path}")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        # Map company UUID -> Name for developer lookup
        dev_map: dict[str, str] = {}
        try:
            for cid, cname in con.execute(
                'SELECT CAST(_id AS VARCHAR), "Name" FROM companies_Company'
            ).fetchall():
                if cid and cname:
                    dev_map[cid] = cname
        except duckdb.Error:
            pass

        query = """
        SELECT
            CAST(pg.playnite_id AS VARCHAR) AS id,
            pg.game_name,
            COALESCE(pg.store_source, 'Unknown') AS store_source,
            pg.genres,
            pg.series,
            COALESCE(pg.hours_played, 0.0) AS hours_played,
            COALESCE(pg.is_installed, FALSE) AS is_installed,
            COALESCE(pg.instal_size, gg."InstallSize", 0) AS install_size,
            pg.community_score,
            pg.critic_score,
            pg.score,
            CAST(gg."ReleaseDate" AS VARCHAR) AS release_date,
            COALESCE(gg."PlayCount", 0) AS play_count,
            CAST(pg.last_played AS VARCHAR) AS last_played,
            COALESCE(gg."CompletionStatus"."Name", 'Not Played') AS completion_status,
            gg."DeveloperIds" AS developer_ids,
            COALESCE(gg."Hidden", FALSE) AS is_hidden
        FROM playnite_games pg
        LEFT JOIN games_Game gg ON pg.playnite_id = gg._id
        """
        rows = con.execute(query).fetchall()
    finally:
        con.close()

    games: list[dict[str, Any]] = []
    for r in rows:
        (
            gid,
            name,
            store,
            genres_str,
            series_str,
            hours,
            is_installed,
            install_size,
            comm_score,
            crit_score,
            score,
            release_date,
            play_count,
            last_played,
            completion_status,
            dev_ids,
            is_hidden,
        ) = r

        genres = split_csv(genres_str)
        series_list = split_csv(series_str)
        developers: list[str] = []
        if dev_ids:
            for did in dev_ids:
                dname = dev_map.get(str(did))
                if dname:
                    developers.append(dname)

        # Clean bogus 1970 epoch dates
        if last_played and last_played.startswith("1970-"):
            last_played_clean = None
        elif last_played:
            last_played_clean = last_played[:10]
        else:
            last_played_clean = None

        release_year = int(release_date[:4]) if release_date and len(release_date) >= 4 else None

        games.append(
            {
                "id": gid,
                "name": name or "Unknown",
                "store": store,
                "genres": genres,
                "series": series_list,
                "developers": developers,
                "hours": round(float(hours or 0.0), 2),
                "installed": bool(is_installed),
                "size_gb": round(float(install_size or 0) / (1024**3), 2),
                "community_score": int(comm_score) if comm_score is not None else None,
                "critic_score": int(crit_score) if crit_score is not None else None,
                "score": round(float(score), 1) if score is not None else None,
                "release_year": release_year,
                "play_count": int(play_count or 0),
                "last_played": last_played_clean,
                "status": completion_status,
                "hidden": bool(is_hidden),
            }
        )
    return games


def build_analytics_and_recommendations(
    all_games: list[dict[str, Any]], include_hidden: bool = False
) -> dict[str, Any]:
    hidden_count = sum(1 for g in all_games if g["hidden"])
    games = all_games if include_hidden else [g for g in all_games if not g["hidden"]]
    total_games = len(games)
    played_games = [g for g in games if g["hours"] > 0]
    unplayed_games = [g for g in games if g["hours"] == 0]
    total_hours = round(sum(g["hours"] for g in games), 1)
    installed_games = [g for g in games if g["installed"]]
    total_install_gb = round(sum(g["size_gb"] for g in installed_games), 1)

    scored_games = [g["score"] for g in games if g["score"] is not None]
    avg_score = round(sum(scored_games) / len(scored_games), 1) if scored_games else 0.0

    # Genre stats + Genre Affinity (using log1p(hours) so one 777h outlier doesn't drown out all other genres)
    genre_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "total": 0, "played": 0, "hours": 0.0, "affinity_raw": 0.0}
    )
    for g in games:
        w = math.log1p(g["hours"]) if g["hours"] > 0 else 0.0
        for gen in g["genres"]:
            st = genre_stats[gen]
            st["name"] = gen
            st["total"] += 1
            if g["hours"] > 0:
                st["played"] += 1
                st["hours"] += g["hours"]
                st["affinity_raw"] += w

    max_aff = max((v["affinity_raw"] for v in genre_stats.values()), default=1.0) or 1.0
    genre_affinity: dict[str, float] = {}
    genre_list: list[dict[str, Any]] = []
    for gen, st in genre_stats.items():
        aff = st["affinity_raw"] / max_aff
        genre_affinity[gen] = aff
        genre_list.append(
            {
                "name": gen,
                "total": st["total"],
                "played": st["played"],
                "unplayed": st["total"] - st["played"],
                "hours": round(st["hours"], 1),
                "affinity": round(aff * 100, 1),
            }
        )
    genre_list.sort(key=lambda x: x["hours"], reverse=True)

    # Store stats
    store_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "total": 0, "played": 0, "hours": 0.0, "installed": 0}
    )
    for g in games:
        st = store_stats[g["store"]]
        st["name"] = g["store"]
        st["total"] += 1
        if g["hours"] > 0:
            st["played"] += 1
            st["hours"] += g["hours"]
        if g["installed"]:
            st["installed"] += 1
    store_list = sorted(
        (
            {
                **v,
                "hours": round(v["hours"], 1),
                "pct_games": round(v["total"] / total_games * 100, 1) if total_games else 0.0,
                "pct_hours": round(v["hours"] / total_hours * 100, 1) if total_hours else 0.0,
            }
            for v in store_stats.values()
        ),
        key=lambda x: x["hours"],
        reverse=True,
    )

    # Playtime buckets
    buckets = [
        ("Unplayed (0h)", lambda h: h == 0),
        ("< 1h", lambda h: 0 < h < 1),
        ("1–5h", lambda h: 1 <= h < 5),
        ("5–20h", lambda h: 5 <= h < 20),
        ("20–50h", lambda h: 20 <= h < 50),
        ("50–100h", lambda h: 50 <= h < 100),
        ("100h+", lambda h: h >= 100),
    ]
    bucket_counts = [
        {"label": label, "count": sum(1 for g in games if pred(g["hours"]))}
        for label, pred in buckets
    ]

    # Top played games
    top_played = sorted(played_games, key=lambda g: g["hours"], reverse=True)[:15]

    # Series & Developer affinity from played games (>= 1h), plus played title set
    played_titles: set[str] = {
        normalize_title(g["name"]) for g in played_games if g["hours"] >= 1.0
    }
    series_best: dict[str, dict[str, Any]] = {}
    dev_best: dict[str, dict[str, Any]] = {}
    for g in sorted(played_games, key=lambda x: x["hours"], reverse=True):
        if g["hours"] < 1.0:
            continue
        for s in g["series"]:
            if s not in series_best:
                series_best[s] = {"name": g["name"], "hours": g["hours"]}
        for d in g["developers"]:
            if d not in dev_best:
                dev_best[d] = {"name": g["name"], "hours": g["hours"]}

    # Score candidate games (hours == 0 or barely touched < 1h), deduplicating across stores
    dedup_recs: dict[str, dict[str, Any]] = {}
    for g in games:
        if g["hours"] >= 1.0:
            continue
        norm = normalize_title(g["name"])
        if norm in played_titles:
            continue

        # 1. Genre match (0 - 55 pts)
        if g["genres"]:
            affs = sorted((genre_affinity.get(gen, 0.0) for gen in g["genres"]), reverse=True)
            top_aff = affs[0]
            avg_aff = sum(affs) / len(affs)
            genre_pts = (0.60 * top_aff + 0.40 * avg_aff) * 55.0
            matched_genres = [
                gen
                for gen in sorted(g["genres"], key=lambda x: genre_affinity.get(x, 0.0), reverse=True)
                if genre_affinity.get(gen, 0.0) >= 0.25
            ][:3]
        else:
            genre_pts = 0.0
            matched_genres = []

        # 2. Quality score (0 - 30 pts)
        eff_score = g["score"] if g["score"] is not None else 50.0
        quality_pts = (eff_score / 100.0) * 30.0

        # 3. Series & Developer bonus (0 - 15 pts, scaled by anchor playtime)
        bonus_pts = 0.0
        reasons: list[str] = []
        if matched_genres:
            reasons.append("Matches your top genres: " + ", ".join(matched_genres))

        matched_series_anchor = None
        for s in g["series"]:
            if s in series_best:
                matched_series_anchor = series_best[s]
                bonus_pts += min(13.0, 5.0 + 2.0 * math.log1p(matched_series_anchor["hours"]))
                reasons.append(
                    f"Same series as {matched_series_anchor['name']} ({matched_series_anchor['hours']}h)"
                )
                break

        if not matched_series_anchor:
            for d in g["developers"]:
                if d in dev_best:
                    anchor = dev_best[d]
                    bonus_pts += min(8.0, 3.0 + 1.2 * math.log1p(anchor["hours"]))
                    reasons.append(f"Same developer ({d}) as {anchor['name']} ({anchor['hours']}h)")
                    break

        bonus_pts = min(bonus_pts, 15.0)
        total_rec_score = round(min(100.0, genre_pts + quality_pts + bonus_pts), 1)

        if g["critic_score"] is not None and g["community_score"] is not None:
            reasons.append(f"Critic {g['critic_score']} · Community {g['community_score']}")
        elif g["score"] is not None:
            reasons.append(f"Score {g['score']}/100")

        entry = {
            **g,
            "rec_score": total_rec_score,
            "genre_pts": round(genre_pts, 1),
            "quality_pts": round(quality_pts, 1),
            "bonus_pts": round(bonus_pts, 1),
            "pool": "unplayed" if g["hours"] == 0 else "barely_touched",
            "reasons": reasons,
        }
        if norm in dedup_recs:
            existing = dedup_recs[norm]
            if g["store"] not in existing["store"].split(", "):
                existing["store"] = f"{existing['store']}, {g['store']}"
            if g["installed"] and not existing["installed"]:
                existing["installed"] = True
                existing["size_gb"] = g["size_gb"]
            if entry["rec_score"] > existing["rec_score"]:
                entry["store"] = existing["store"]
                entry["installed"] = existing["installed"]
                entry["size_gb"] = existing["size_gb"]
                dedup_recs[norm] = entry
        else:
            dedup_recs[norm] = entry

    recommendations = sorted(
        dedup_recs.values(),
        key=lambda x: (x["rec_score"], x["score"] or 0),
        reverse=True,
    )

    # "Because You Loved..." anchors: pick up to 8 diverse top-played games
    anchors: list[dict[str, Any]] = []
    seen_series: set[str] = set()
    seen_prefixes: set[str] = set()
    unplayed_candidates = [r for r in recommendations if r["pool"] == "unplayed" and r["genres"]]

    for g in sorted(played_games, key=lambda x: x["hours"], reverse=True):
        if len(anchors) >= 8:
            break
        if not g["genres"]:
            continue
        # Deduplicate near-identical entries (e.g. FIFA Soccer 11 vs FIFA 11, Borderlands 2 vs Pre-Sequel)
        prefix = g["name"].split(":")[0].split()[0].lower()
        if any(s in seen_series for s in g["series"]):
            continue
        if prefix in seen_prefixes and g["hours"] < 200:
            continue

        anchor_genres = set(g["genres"])
        anchor_series = set(g["series"])

        # Score unplayed games by similarity to this specific anchor game
        similar: list[tuple[float, dict[str, Any]]] = []
        for cand in unplayed_candidates:
            cand_genres = set(cand["genres"])
            union = anchor_genres | cand_genres
            inter = anchor_genres & cand_genres
            if not inter:
                continue
            jaccard = len(inter) / len(union)
            series_boost = 0.45 if (anchor_series & set(cand["series"])) else 0.0
            quality_norm = ((cand["score"] or 50.0) / 100.0) * 0.25
            sim = jaccard * 0.65 + series_boost + quality_norm
            similar.append((sim, cand))

        similar.sort(key=lambda pair: pair[0], reverse=True)
        top_matches = [
            {
                "name": m["name"],
                "store": m["store"],
                "genres": m["genres"],
                "score": m["score"],
                "rec_score": m["rec_score"],
                "shared_genres": sorted(anchor_genres & set(m["genres"])),
                "same_series": bool(anchor_series & set(m["series"])),
            }
            for _, m in similar[:4]
        ]
        if top_matches:
            anchors.append(
                {
                    "name": g["name"],
                    "hours": g["hours"],
                    "store": g["store"],
                    "genres": g["genres"],
                    "score": g["score"],
                    "matches": top_matches,
                }
            )
            seen_series.update(g["series"])
            seen_prefixes.add(prefix)

    # Scatter data: all games with a score (played + top unplayed gems)
    scatter_points = [
        {
            "name": g["name"],
            "hours": g["hours"],
            "score": g["score"],
            "store": g["store"],
            "genres": g["genres"][:3],
            "played": g["hours"] > 0,
        }
        for g in games
        if g["score"] is not None
    ]

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kpis": {
            "total_games": total_games,
            "played_games": len(played_games),
            "unplayed_games": len(unplayed_games),
            "played_pct": round(len(played_games) / total_games * 100, 1) if total_games else 0.0,
            "total_hours": total_hours,
            "installed_games": len(installed_games),
            "total_install_gb": total_install_gb,
            "avg_score": avg_score,
            "hidden_games": hidden_count,
            "include_hidden": include_hidden,
        },
        "genres": genre_list,
        "stores": store_list,
        "buckets": bucket_counts,
        "top_played": [
            {
                "name": g["name"],
                "hours": g["hours"],
                "store": g["store"],
                "genres": g["genres"][:3],
                "score": g["score"],
            }
            for g in top_played
        ],
        "scatter": scatter_points,
        "recommendations": recommendations,
        "anchors": anchors,
        "duplicates": detect_duplicates(games),
        "all_games": all_games,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Playnite Game Library Insights &amp; Recommender</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b0f19;
    --surface: #13192b;
    --surface-2: #1a2238;
    --border: #25304d;
    --text: #e6ecf7;
    --muted: #93a3c0;
    --accent: #6366f1;
    --accent-2: #22d3ee;
    --emerald: #10b981;
    --amber: #f59e0b;
    --rose: #f43f5e;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 24px 20px 64px;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  h1 {
    margin: 0;
    font-size: 1.75rem;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
  .subtitle {
    color: var(--muted);
    font-size: 0.9rem;
    margin-top: 4px;
  }
  .nav-pills {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .nav-pills a {
    color: var(--text);
    text-decoration: none;
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 0.85rem;
    transition: border-color 0.15s, background 0.15s;
  }
  .nav-pills a:hover {
    border-color: var(--accent);
    background: var(--surface-2);
  }
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 14px;
    margin-bottom: 24px;
  }
  .kpi-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 18px;
  }
  .kpi-label {
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  .kpi-value {
    font-size: 1.75rem;
    font-weight: 700;
    margin: 4px 0 2px;
    font-variant-numeric: tabular-nums;
  }
  .kpi-sub {
    font-size: 0.82rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .charts-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(460px, 1fr));
    gap: 18px;
    margin-bottom: 32px;
  }
  @media (max-width: 540px) {
    .charts-grid { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
  }
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 14px;
  }
  .card-title {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 600;
  }
  .card-hint {
    font-size: 0.78rem;
    color: var(--muted);
  }
  .section-title {
    font-size: 1.35rem;
    font-weight: 700;
    margin: 36px 0 14px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px;
    margin-bottom: 16px;
  }
  .controls label {
    font-size: 0.82rem;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  select, input[type="search"], input[type="text"] {
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 0.88rem;
  }
  select:focus, input:focus {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .btn-reset {
    margin-left: auto;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .btn-reset:hover { border-color: var(--accent); }
  .rec-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
    gap: 14px;
  }
  .rec-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    transition: border-color 0.15s, transform 0.15s;
  }
  .rec-card:hover {
    border-color: var(--accent);
    transform: translateY(-1px);
  }
  .rec-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 10px;
  }
  .rec-title {
    font-weight: 650;
    font-size: 1rem;
    line-height: 1.3;
  }
  .rec-rank {
    color: var(--muted);
    font-size: 0.8rem;
    margin-right: 4px;
  }
  .match-badge {
    flex-shrink: 0;
    background: rgba(99, 102, 241, 0.18);
    color: #a5b4fc;
    border: 1px solid rgba(99, 102, 241, 0.45);
    border-radius: 8px;
    padding: 3px 8px;
    font-size: 0.82rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .pill-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .pill {
    font-size: 0.74rem;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .pill.store { color: #67e8f9; border-color: rgba(34, 211, 238, 0.35); }
  .pill.installed { color: #6ee7b7; border-color: rgba(16, 185, 129, 0.4); }
  .pill.touched { color: #fcd34d; border-color: rgba(245, 158, 11, 0.4); }
  .pill.dup-exact { color: #fda4af; border-color: rgba(244, 63, 94, 0.45); background: rgba(244, 63, 94, 0.14); }
  .pill.dup-alias { color: #fcd34d; border-color: rgba(245, 158, 11, 0.45); background: rgba(245, 158, 11, 0.14); }
  .pill.dup-cross { color: #67e8f9; border-color: rgba(34, 211, 238, 0.4); background: rgba(34, 211, 238, 0.12); }
  .dup-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
    gap: 14px;
  }
  @media (max-width: 480px) {
    .dup-grid { grid-template-columns: 1fr; }
  }
  .dup-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 15px 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .dup-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    padding: 6px 8px;
    border-radius: 8px;
    background: var(--surface-2);
    font-size: 0.84rem;
  }
  .dup-row-meta {
    font-size: 0.76rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .reasons {
    font-size: 0.82rem;
    color: #cbd5e1;
    margin: 0;
    padding-left: 16px;
  }
  .reasons li { margin-bottom: 2px; }
  .anchors-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 14px;
  }
  .anchor-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
  }
  .anchor-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    padding-bottom: 10px;
    margin-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .anchor-name { font-weight: 700; font-size: 0.98rem; }
  .anchor-hours { color: var(--accent-2); font-size: 0.84rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .anchor-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    padding: 6px 0;
    font-size: 0.86rem;
    border-bottom: 1px dashed rgba(37, 48, 77, 0.6);
  }
  .anchor-item:last-child { border-bottom: none; }
  .anchor-sub { font-size: 0.75rem; color: var(--muted); }
  .table-wrap {
    overflow-x: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    max-height: 600px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.86rem;
    font-variant-numeric: tabular-nums;
  }
  th, td {
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }
  th {
    position: sticky;
    top: 0;
    background: var(--surface-2);
    font-weight: 600;
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }
  th:hover { color: var(--accent-2); }
  tr:hover td { background: rgba(99, 102, 241, 0.06); }
  .num { text-align: right; }
  svg { width: 100%; height: auto; display: block; }
  .bar-row { cursor: pointer; }
  .bar-row:hover rect.bar-fill { opacity: 0.85; }
  .tooltip {
    position: fixed;
    pointer-events: none;
    background: #0f172a;
    border: 1px solid var(--accent);
    color: var(--text);
    padding: 8px 11px;
    border-radius: 8px;
    font-size: 0.8rem;
    box-shadow: 0 10px 25px rgba(0,0,0,0.45);
    z-index: 9999;
    display: none;
    max-width: 280px;
  }
  .empty-state {
    padding: 32px;
    text-align: center;
    color: var(--muted);
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: var(--radius);
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Playnite Game Library Insights</h1>
      <div class="subtitle" id="generated-meta"></div>
    </div>
    <nav class="nav-pills">
      <a href="#charts">Visualizations</a>
      <a href="#recommender">Next Games to Play</a>
      <a href="#because-you-loved">Because You Loved</a>
      <a href="#duplicates">Duplicates</a>
      <a href="#explorer">Library Explorer</a>
    </nav>
  </header>

  <section class="kpi-grid" id="kpi-grid"></section>

  <section id="charts" class="charts-grid">
    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Top Genres by Hours Played</h2>
        <span class="card-hint">Click a genre bar to filter recommendations</span>
      </div>
      <div id="chart-genres"></div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Library Size vs. Playtime by Store</h2>
        <span class="card-hint">Owned % vs Hours %</span>
      </div>
      <div id="chart-stores"></div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Top 15 Most Played Games</h2>
        <span class="card-hint">Hours logged</span>
      </div>
      <div id="chart-top-games"></div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2 class="card-title">Rating vs. Playtime</h2>
        <span class="card-hint">Blue = Played · Amber = Unplayed Gems</span>
      </div>
      <div id="chart-scatter"></div>
    </div>

    <div class="card" style="grid-column: 1 / -1;">
      <div class="card-header">
        <h2 class="card-title">Playtime Distribution</h2>
        <span class="card-hint">Engagement funnel across your entire library</span>
      </div>
      <div id="chart-buckets"></div>
    </div>
  </section>

  <h2 class="section-title" id="recommender">What Should I Play Next?</h2>
  <div class="controls">
    <label>Pool
      <select id="rec-pool">
        <option value="unplayed">Unplayed (0h)</option>
        <option value="barely_touched">Barely Touched (&lt; 1h)</option>
        <option value="all">Both (0h &amp; &lt; 1h)</option>
      </select>
    </label>
    <label>Genre
      <select id="rec-genre"><option value="">All Genres</option></select>
    </label>
    <label>Store
      <select id="rec-store"><option value="">All Stores</option></select>
    </label>
    <label>Sort
      <select id="rec-sort">
        <option value="rec_score">Best Match</option>
        <option value="score">Highest Rating</option>
        <option value="genre_pts">Top Genre Affinity</option>
      </select>
    </label>
    <label><input type="checkbox" id="rec-installed" /> Installed only</label>
    <label>Search <input type="search" id="rec-search" placeholder="Title or series..." /></label>
    <button class="btn-reset" id="rec-reset" type="button">Reset</button>
  </div>
  <div id="rec-count" class="subtitle" style="margin-bottom: 10px;"></div>
  <div id="rec-grid" class="rec-grid"></div>

  <h2 class="section-title" id="because-you-loved">Because You Loved...</h2>
  <div id="anchors-grid" class="anchors-grid"></div>

  <h2 class="section-title" id="duplicates">Duplicate Games</h2>
  <div class="controls">
    <label>Type
      <select id="dup-kind">
        <option value="">All Types</option>
        <option value="same_store_exact">Same-Store Exact</option>
        <option value="same_store_alias">Same-Store Alias / Edition</option>
        <option value="cross_store">Cross-Store</option>
      </select>
    </label>
    <label>Store
      <select id="dup-store"><option value="">All Stores</option></select>
    </label>
    <label>Search <input type="search" id="dup-search" placeholder="Title..." /></label>
  </div>
  <div id="dup-count" class="subtitle" style="margin-bottom: 10px;"></div>
  <div id="dup-grid" class="dup-grid"></div>

  <h2 class="section-title" id="explorer">Full Library Explorer</h2>
  <div class="controls">
    <label>Search <input type="search" id="tbl-search" placeholder="Game, genre, series..." /></label>
    <label>Status
      <select id="tbl-status">
        <option value="">Visible</option>
        <option value="played">Played (&gt; 0h)</option>
        <option value="unplayed">Unplayed (0h)</option>
        <option value="installed">Installed</option>
        <option value="hidden">Hidden in Playnite</option>
      </select>
    </label>
    <label>Store
      <select id="tbl-store"><option value="">All Stores</option></select>
    </label>
    <label>Show
      <select id="tbl-limit">
        <option value="50">50 rows</option>
        <option value="150">150 rows</option>
        <option value="500">500 rows</option>
        <option value="9999">All rows</option>
      </select>
    </label>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th data-sort="name">Game</th>
          <th data-sort="store">Store</th>
          <th data-sort="genres">Genres</th>
          <th data-sort="hours" class="num">Hours</th>
          <th data-sort="score" class="num">Score</th>
          <th data-sort="release_year" class="num">Year</th>
          <th data-sort="installed">Installed</th>
        </tr>
      </thead>
      <tbody id="tbl-body"></tbody>
    </table>
  </div>
</div>

<div id="tooltip" class="tooltip"></div>

<script id="library-data" type="application/json">__PAYLOAD_JSON__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById("library-data").textContent);
  const tooltip = document.getElementById("tooltip");

  function esc(str) {
    return String(str ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function showTip(evt, html) {
    tooltip.innerHTML = html;
    tooltip.style.display = "block";
    const x = Math.min(evt.clientX + 14, window.innerWidth - 290);
    const y = Math.min(evt.clientY + 14, window.innerHeight - 120);
    tooltip.style.left = x + "px";
    tooltip.style.top = y + "px";
  }
  function hideTip() { tooltip.style.display = "none"; }

  // Header & KPIs
  const k = data.kpis;
  const hiddenNote = k.hidden_games && !k.include_hidden ? ` · ${k.hidden_games} hidden excluded` : "";
  document.getElementById("generated-meta").textContent =
    `Generated ${data.generated_at} · ${k.total_games.toLocaleString()} visible games${hiddenNote} · Deterministic DuckDB analysis`;

  const kpis = [
    { label: "Total Library", value: k.total_games.toLocaleString(), sub: `${k.installed_games} installed (${k.total_install_gb} GB)` },
    { label: "Total Hours Played", value: k.total_hours.toLocaleString() + "h", sub: `Across ${k.played_games} played games` },
    { label: "Library Played", value: k.played_pct + "%", sub: `${k.unplayed_games.toLocaleString()} unplayed in backlog` },
    { label: "Average Rating", value: k.avg_score + " / 100", sub: "Blended critic & community score" }
  ];
  document.getElementById("kpi-grid").innerHTML = kpis.map(item => `
    <div class="kpi-card">
      <div class="kpi-label">${esc(item.label)}</div>
      <div class="kpi-value">${esc(item.value)}</div>
      <div class="kpi-sub">${esc(item.sub)}</div>
    </div>
  `).join("");

  // SVG Helper for Horizontal Bars
  function renderHBarChart(containerId, items, { labelKey, valueKey, maxVal, color, formatVal, tipFn, onClick }) {
    const el = document.getElementById(containerId);
    const rowH = 26, padL = 175, padR = 64, padT = 8, padB = 10, w = 540;
    const h = padT + padB + items.length * rowH;
    const barW = w - padL - padR;
    const m = maxVal || Math.max(...items.map(d => d[valueKey]), 1);

    let svg = `<svg viewBox="0 0 ${w} ${h}" role="img">`;
    items.forEach((d, i) => {
      const y = padT + i * rowH;
      const bw = Math.max(2, (d[valueKey] / m) * barW);
      const label = String(d[labelKey]);
      const shortLabel = label.length > 24 ? label.slice(0, 22) + "…" : label;
      svg += `
        <g class="bar-row" data-idx="${i}">
          <text x="${padL - 8}" y="${y + 16}" text-anchor="end" fill="#cbd5e1" font-size="11.5">${esc(shortLabel)}</text>
          <rect x="${padL}" y="${y + 4}" width="${barW}" height="16" rx="4" fill="#1e293b" />
          <rect class="bar-fill" x="${padL}" y="${y + 4}" width="${bw.toFixed(1)}" height="16" rx="4" fill="${color}" />
          <text x="${padL + bw + 6}" y="${y + 16}" fill="#94a3b8" font-size="11">${esc(formatVal(d))}</text>
        </g>`;
    });
    svg += "</svg>";
    el.innerHTML = svg;
    el.querySelectorAll(".bar-row").forEach(g => {
      const item = items[Number(g.dataset.idx)];
      g.addEventListener("mousemove", e => showTip(e, tipFn(item)));
      g.addEventListener("mouseleave", hideTip);
      if (onClick) g.addEventListener("click", () => onClick(item));
    });
  }

  // 1. Top Genres
  const topGenres = data.genres.slice(0, 14);
  renderHBarChart("chart-genres", topGenres, {
    labelKey: "name",
    valueKey: "hours",
    color: "#6366f1",
    formatVal: d => `${d.hours.toLocaleString()}h`,
    tipFn: d => `<strong>${esc(d.name)}</strong><br/>${d.hours.toLocaleString()}h played<br/>${d.played} played / ${d.unplayed} unplayed (${d.total} total)<br/>Affinity: ${d.affinity}%`,
    onClick: d => {
      document.getElementById("rec-genre").value = d.name;
      renderRecs();
      document.getElementById("recommender").scrollIntoView({ behavior: "smooth" });
    }
  });

  // 2. Store Comparison (Owned % vs Hours %)
  (() => {
    const el = document.getElementById("chart-stores");
    const stores = data.stores;
    const rowH = 38, padL = 125, padR = 52, padT = 22, padB = 10, w = 540;
    const h = padT + padB + stores.length * rowH;
    const barW = w - padL - padR;
    let svg = `<svg viewBox="0 0 ${w} ${h}">
      <rect x="${padL}" y="4" width="10" height="10" rx="2" fill="#22d3ee" />
      <text x="${padL + 14}" y="13" fill="#94a3b8" font-size="11">% of Games</text>
      <rect x="${padL + 95}" y="4" width="10" height="10" rx="2" fill="#10b981" />
      <text x="${padL + 109}" y="13" fill="#94a3b8" font-size="11">% of Hours</text>`;
    stores.forEach((s, i) => {
      const y = padT + i * rowH;
      const wG = Math.max(2, (s.pct_games / 100) * barW);
      const wH = Math.max(2, (s.pct_hours / 100) * barW);
      svg += `
        <g class="bar-row" data-idx="${i}">
          <text x="${padL - 8}" y="${y + 18}" text-anchor="end" fill="#cbd5e1" font-size="11.5">${esc(s.name)}</text>
          <rect x="${padL}" y="${y + 4}" width="${wG.toFixed(1)}" height="10" rx="3" fill="#22d3ee" />
          <text x="${padL + wG + 5}" y="${y + 12}" fill="#67e8f9" font-size="10">${s.pct_games}%</text>
          <rect x="${padL}" y="${y + 17}" width="${wH.toFixed(1)}" height="10" rx="3" fill="#10b981" />
          <text x="${padL + wH + 5}" y="${y + 25}" fill="#6ee7b7" font-size="10">${s.pct_hours}%</text>
        </g>`;
    });
    svg += "</svg>";
    el.innerHTML = svg;
    el.querySelectorAll(".bar-row").forEach(g => {
      const s = stores[Number(g.dataset.idx)];
      g.addEventListener("mousemove", e => showTip(e,
        `<strong>${esc(s.name)}</strong><br/>${s.total} games (${s.pct_games}%)<br/>${s.hours.toLocaleString()}h played (${s.pct_hours}%)<br/>${s.installed} installed`
      ));
      g.addEventListener("mouseleave", hideTip);
    });
  })();

  // 3. Top 15 Played Games
  renderHBarChart("chart-top-games", data.top_played, {
    labelKey: "name",
    valueKey: "hours",
    color: "#22d3ee",
    formatVal: d => `${d.hours}h`,
    tipFn: d => `<strong>${esc(d.name)}</strong><br/>${d.hours}h · ${esc(d.store)}<br/>${esc(d.genres.join(", "))}${d.score ? `<br/>Score: ${d.score}` : ""}`
  });

  // 4. Score vs Playtime Scatter
  (() => {
    const el = document.getElementById("chart-scatter");
    const w = 540, h = 360, padL = 44, padR = 16, padT = 16, padB = 34;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const maxLog = Math.log10(Math.max(...data.scatter.map(d => d.hours), 10) + 1);
    let svg = `<svg viewBox="0 0 ${w} ${h}">
      <line x1="${padL}" y1="${padT + plotH}" x2="${w - padR}" y2="${padT + plotH}" stroke="#334155" />
      <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + plotH}" stroke="#334155" />`;
    [0, 25, 50, 75, 100].forEach(sc => {
      const y = padT + plotH - (sc / 100) * plotH;
      svg += `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="#1e293b" />
              <text x="${padL - 6}" y="${y + 4}" text-anchor="end" fill="#64748b" font-size="10">${sc}</text>`;
    });
    [0, 1, 10, 50, 200, 700].forEach(hr => {
      const x = padL + (Math.log10(hr + 1) / maxLog) * plotW;
      svg += `<text x="${x}" y="${padT + plotH + 16}" text-anchor="middle" fill="#64748b" font-size="10">${hr}h</text>`;
    });
    svg += `<text x="${padL + plotW / 2}" y="${h - 4}" text-anchor="middle" fill="#94a3b8" font-size="11">Hours Played (log scale)</text>`;
    data.scatter.forEach((p, idx) => {
      const x = padL + (Math.log10(p.hours + 1) / maxLog) * plotW;
      const y = padT + plotH - (p.score / 100) * plotH;
      const fill = p.played ? "#38bdf8" : (p.score >= 80 ? "#f59e0b" : "#475569");
      const op = p.played ? 0.78 : (p.score >= 80 ? 0.65 : 0.28);
      const r = p.played ? 3.8 : (p.score >= 80 ? 3.2 : 2.3);
      svg += `<circle class="pt" data-idx="${idx}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" fill="${fill}" fill-opacity="${op}" />`;
    });
    svg += "</svg>";
    el.innerHTML = svg;
    el.querySelectorAll(".pt").forEach(c => {
      const p = data.scatter[Number(c.dataset.idx)];
      c.addEventListener("mousemove", e => showTip(e,
        `<strong>${esc(p.name)}</strong><br/>Score: ${p.score} · ${p.hours}h<br/>${esc(p.store)} · ${esc(p.genres.join(", "))}`
      ));
      c.addEventListener("mouseleave", hideTip);
    });
  })();

  // 5. Playtime Buckets
  (() => {
    const el = document.getElementById("chart-buckets");
    const items = data.buckets;
    const w = 960, h = 175, padL = 20, padR = 20, padT = 22, padB = 34;
    const colW = (w - padL - padR) / items.length;
    const maxC = Math.max(...items.map(d => d.count), 1);
    const plotH = h - padT - padB;
    let svg = `<svg viewBox="0 0 ${w} ${h}">`;
    items.forEach((b, i) => {
      const bh = Math.max(4, (b.count / maxC) * plotH);
      const x = padL + i * colW + 10;
      const bw = colW - 20;
      const y = padT + plotH - bh;
      const color = i === 0 ? "#475569" : "#6366f1";
      svg += `
        <g>
          <rect x="${x}" y="${y}" width="${bw}" height="${bh}" rx="6" fill="${color}" />
          <text x="${x + bw / 2}" y="${y - 6}" text-anchor="middle" fill="#e2e8f0" font-size="12" font-weight="600">${b.count}</text>
          <text x="${x + bw / 2}" y="${h - 10}" text-anchor="middle" fill="#94a3b8" font-size="11.5">${esc(b.label)}</text>
        </g>`;
    });
    svg += "</svg>";
    el.innerHTML = svg;
  })();

  // Populate Filter Dropdowns
  const genreSel = document.getElementById("rec-genre");
  data.genres.forEach(g => genreSel.add(new Option(`${g.name} (${g.unplayed} unplayed)`, g.name)));
  const storeSel = document.getElementById("rec-store");
  const dupStoreSel = document.getElementById("dup-store");
  const tblStoreSel = document.getElementById("tbl-store");
  data.stores.forEach(s => {
    storeSel.add(new Option(s.name, s.name));
    dupStoreSel.add(new Option(s.name, s.name));
    tblStoreSel.add(new Option(s.name, s.name));
  });

  // Recommender Rendering
  function renderRecs() {
    const pool = document.getElementById("rec-pool").value;
    const genre = document.getElementById("rec-genre").value;
    const store = document.getElementById("rec-store").value;
    const sortBy = document.getElementById("rec-sort").value;
    const instOnly = document.getElementById("rec-installed").checked;
    const q = document.getElementById("rec-search").value.trim().toLowerCase();

    let list = data.recommendations.filter(r => {
      if (pool !== "all" && r.pool !== pool) return false;
      if (genre && !r.genres.includes(genre)) return false;
      if (store && !r.store.split(", ").includes(store)) return false;
      if (instOnly && !r.installed) return false;
      if (q && !(r.name.toLowerCase().includes(q) || r.series.join(" ").toLowerCase().includes(q))) return false;
      return true;
    });

    list.sort((a, b) => (b[sortBy] ?? 0) - (a[sortBy] ?? 0) || (b.score ?? 0) - (a.score ?? 0));
    const shown = list.slice(0, 36);
    document.getElementById("rec-count").textContent =
      `Showing top ${shown.length} of ${list.length} matching candidates`;

    const grid = document.getElementById("rec-grid");
    if (!shown.length) {
      grid.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;">No games match these filters. Try clearing one!</div>`;
      return;
    }
    grid.innerHTML = shown.map((r, idx) => `
      <article class="rec-card">
        <div class="rec-top">
          <div class="rec-title"><span class="rec-rank">#${idx + 1}</span> ${esc(r.name)}</div>
          <div class="match-badge">${r.rec_score}</div>
        </div>
        <div class="pill-row">
          <span class="pill store">${esc(r.store)}</span>
          ${r.installed ? `<span class="pill installed">Installed (${r.size_gb} GB)</span>` : ""}
          ${r.hours > 0 ? `<span class="pill touched">${r.hours}h played</span>` : ""}
          ${r.genres.slice(0, 3).map(g => `<span class="pill">${esc(g)}</span>`).join("")}
        </div>
        <ul class="reasons">
          ${r.reasons.map(rs => `<li>${esc(rs)}</li>`).join("")}
        </ul>
      </article>
    `).join("");
  }

  ["rec-pool", "rec-genre", "rec-store", "rec-sort", "rec-installed"].forEach(id =>
    document.getElementById(id).addEventListener("change", renderRecs)
  );
  document.getElementById("rec-search").addEventListener("input", renderRecs);
  document.getElementById("rec-reset").addEventListener("click", () => {
    document.getElementById("rec-pool").value = "unplayed";
    document.getElementById("rec-genre").value = "";
    document.getElementById("rec-store").value = "";
    document.getElementById("rec-sort").value = "rec_score";
    document.getElementById("rec-installed").checked = false;
    document.getElementById("rec-search").value = "";
    renderRecs();
  });
  renderRecs();

  // Because You Loved...
  document.getElementById("anchors-grid").innerHTML = data.anchors.map(a => `
    <div class="anchor-card">
      <div class="anchor-head">
        <div>
          <div class="anchor-name">${esc(a.name)}</div>
          <div class="anchor-sub">${esc(a.store)} · ${esc(a.genres.slice(0, 3).join(", "))}</div>
        </div>
        <div class="anchor-hours">${a.hours}h</div>
      </div>
      ${a.matches.map(m => `
        <div class="anchor-item">
          <div>
            <div>${esc(m.name)}</div>
            <div class="anchor-sub">${esc(m.store)} · ${m.same_series ? "Same series · " : ""}${esc(m.shared_genres.join(", "))}</div>
          </div>
          <div class="match-badge">${m.rec_score}</div>
        </div>
      `).join("")}
    </div>
  `).join("");

  // Duplicate Games
  function renderDuplicates() {
    const kind = document.getElementById("dup-kind").value;
    const store = document.getElementById("dup-store").value;
    const q = document.getElementById("dup-search").value.trim().toLowerCase();

    const list = data.duplicates.filter(g => {
      if (kind && g.kind !== kind) return false;
      if (store && !g.stores.includes(store)) return false;
      if (q && !g.items.some(it => it.name.toLowerCase().includes(q))) return false;
      return true;
    });

    document.getElementById("dup-count").textContent =
      `${list.length} duplicate groups found (${data.duplicates.length} total)`;

    const grid = document.getElementById("dup-grid");
    if (!list.length) {
      grid.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;">No duplicate groups match these filters.</div>`;
      return;
    }
    const badgeClass = k => k === "same_store_exact" ? "dup-exact" : (k === "same_store_alias" ? "dup-alias" : "dup-cross");
    grid.innerHTML = list.map(g => `
      <div class="dup-card">
        <div class="rec-top">
          <div class="rec-title">${esc(g.title)}</div>
          <span class="pill ${badgeClass(g.kind)}">${esc(g.label)}</span>
        </div>
        <div class="anchor-sub">${esc(g.reason)}</div>
        ${g.items.map(it => `
          <div class="dup-row">
            <div>
              <div>${esc(it.name)}</div>
              <div class="dup-row-meta">${esc(it.store)}${it.release_year ? ` · ${it.release_year}` : ""}${it.installed ? ` · Installed (${it.size_gb} GB)` : ""}</div>
            </div>
            <div class="dup-row-meta" style="text-align:right; color:#e2e8f0; font-weight:600;">
              ${it.hours.toFixed(2)}h
            </div>
          </div>
        `).join("")}
      </div>
    `).join("");
  }
  ["dup-kind", "dup-store"].forEach(id =>
    document.getElementById(id).addEventListener("change", renderDuplicates)
  );
  document.getElementById("dup-search").addEventListener("input", renderDuplicates);
  renderDuplicates();

  // Library Explorer Table
  let sortCol = "hours", sortDir = -1;
  function renderTable() {
    const q = document.getElementById("tbl-search").value.trim().toLowerCase();
    const status = document.getElementById("tbl-status").value;
    const store = document.getElementById("tbl-store").value;
    const limit = Number(document.getElementById("tbl-limit").value);

    let rows = data.all_games.filter(g => {
      if (status === "hidden") { if (!g.hidden) return false; }
      else if (g.hidden && !k.include_hidden) return false;
      if (status === "played" && g.hours <= 0) return false;
      if (status === "unplayed" && g.hours > 0) return false;
      if (status === "installed" && !g.installed) return false;
      if (store && g.store !== store) return false;
      if (q) {
        const hay = `${g.name} ${g.genres.join(" ")} ${g.series.join(" ")}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    rows.sort((a, b) => {
      let va = a[sortCol], vb = b[sortCol];
      if (Array.isArray(va)) va = va.join(", ");
      if (Array.isArray(vb)) vb = vb.join(", ");
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * sortDir;
      return String(va).localeCompare(String(vb)) * sortDir;
    });

    document.getElementById("tbl-body").innerHTML = rows.slice(0, limit).map(g => `
      <tr>
        <td>${esc(g.name)}</td>
        <td>${esc(g.store)}</td>
        <td style="color:#94a3b8">${esc(g.genres.join(", "))}</td>
        <td class="num">${g.hours.toFixed(1)}</td>
        <td class="num">${g.score ?? "—"}</td>
        <td class="num">${g.release_year ?? "—"}</td>
        <td>${g.installed ? `Yes (${g.size_gb} GB)` : "No"}</td>
      </tr>
    `).join("");
  }

  document.querySelectorAll("th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (sortCol === col) sortDir *= -1;
      else { sortCol = col; sortDir = (col === "hours" || col === "score" || col === "release_year") ? -1 : 1; }
      renderTable();
    });
  });
  ["tbl-status", "tbl-store", "tbl-limit"].forEach(id =>
    document.getElementById(id).addEventListener("change", renderTable)
  );
  document.getElementById("tbl-search").addEventListener("input", renderTable);
  renderTable();
})();
</script>
</body>
</html>
"""


def generate_html(db_path: Path, out_path: Path, include_hidden: bool = False) -> dict[str, Any]:
    games = load_games(db_path)
    payload = build_analytics_and_recommendations(games, include_hidden=include_hidden)
    # Prevent </script> or <!-- in game titles/data from breaking inline JSON script tag
    json_str = (
        json.dumps(payload, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )
    html = HTML_TEMPLATE.replace("__PAYLOAD_JSON__", json_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate HTML visualizations and game recommendations from playnite.duckdb"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to playnite.duckdb")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output HTML file path")
    ap.add_argument("--open", action="store_true", help="Open generated HTML in default browser")
    ap.add_argument("--include-hidden", action="store_true", help="Include games marked Hidden in Playnite")
    args = ap.parse_args()

    payload = generate_html(args.db, args.out, include_hidden=args.include_hidden)
    k = payload["kpis"]
    print(f"Wrote {args.out}")
    print(
        f"Library: {k['total_games']} visible games ({k['hidden_games']} hidden) | "
        f"{k['played_games']} played ({k['total_hours']}h) | {k['unplayed_games']} unplayed | "
        f"{len(payload['duplicates'])} remaining duplicate groups"
    )
    print("Top 5 recommendations:")
    for i, rec in enumerate(payload["recommendations"][:5], 1):
        print(f"  {i}. {rec['name']} ({rec['store']}) — match {rec['rec_score']} | {'; '.join(rec['reasons'])}")

    if args.open:
        webbrowser.open(args.out.resolve().as_uri())


if __name__ == "__main__":
    main()
