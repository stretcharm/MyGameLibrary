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
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

import gamelib_db
from gamelib_db import DEFAULT_DB

DEFAULT_OUT = Path(__file__).parent / "playnite_export" / "game_insights.html"
TEMPLATE_PATH = Path(__file__).parent / "templates" / "insights.html"


def normalize_title(name: str) -> str:
    """Normalize a game title for cross-store deduplication."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


# Edition words only count as a trailing suffix, optionally after ":", "-" or "–"
# (e.g. "Skyrim Special Edition", "Halo - Anniversary"), never mid-title ("Gold Rush: The Game").
_EDITION_RE = re.compile(
    r"(?:[\s:\-–—]+(?:game of the year|goty|definitive|enhanced|complete|remastered|director'?s cut|"
    r"legendary|ultimate|special|deluxe|standard|anniversary|gold|hd|edition)\b)+\s*$",
    re.IGNORECASE,
)


def normalize_base_title(name: str) -> str:
    """Strip trailing edition suffixes before normalizing."""
    stripped = _EDITION_RE.sub("", name.lower())
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

    con = gamelib_db.connect(db_path, read_only=True)
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
            CAST(playnite_id AS VARCHAR) AS id,
            game_name,
            COALESCE(store_source, 'Unknown') AS store_source,
            genres,
            series,
            COALESCE(hours_played, 0.0) AS hours_played,
            COALESCE(is_installed, FALSE) AS is_installed,
            COALESCE(install_size, 0) AS install_size,
            community_score,
            critic_score,
            score,
            CAST(release_date AS VARCHAR) AS release_date,
            CAST(last_played AS VARCHAR) AS last_played,
            COALESCE(completion_status, 'Not Played') AS completion_status,
            developer_ids,
            COALESCE(is_hidden, FALSE) AS is_hidden
        FROM playnite_games
        """
        try:
            rows = con.execute(query).fetchall()
        except (duckdb.BinderException, duckdb.CatalogException) as e:
            sys.exit(
                "The playnite_games view in this database is missing or out of date. "
                f"Re-run playnite_to_duckdb.py to rebuild it.\nOriginal error: {e}"
            )
    finally:
        con.close()

    games: list[dict[str, Any]] = []
    for r in rows:
        (
            gid,
            name,
            store,
            genres,
            series_list,
            hours,
            is_installed,
            install_size,
            comm_score,
            crit_score,
            score,
            release_date,
            last_played,
            completion_status,
            dev_ids,
            is_hidden,
        ) = r

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
            "stores": [g["store"]],
        }
        if norm in dedup_recs:
            existing = dedup_recs[norm]
            if g["store"] not in existing["stores"]:
                existing["stores"].append(g["store"])
                existing["store"] = ", ".join(existing["stores"])
            if g["installed"] and not existing["installed"]:
                existing["installed"] = True
                existing["size_gb"] = g["size_gb"]
            if entry["rec_score"] > existing["rec_score"]:
                entry["store"] = existing["store"]
                entry["stores"] = existing["stores"]
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
        prefix_words = g["name"].split(":")[0].split()
        prefix = prefix_words[0].lower() if prefix_words else normalize_title(g["name"])
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


def generate_html(db_path: Path, out_path: Path, include_hidden: bool = False) -> dict[str, Any]:
    games = load_games(db_path)
    payload = build_analytics_and_recommendations(games, include_hidden=include_hidden)
    # Prevent </script> or <!-- in game titles/data from breaking inline JSON script tag
    json_str = (
        json.dumps(payload, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__PAYLOAD_JSON__", json_str)
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
