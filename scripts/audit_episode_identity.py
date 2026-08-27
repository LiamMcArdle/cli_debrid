#!/usr/bin/env python3
"""Read-only audit of collected anime files against the shared identity rules."""

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_IDENTITY_PATH = os.path.join(ROOT, 'scraper', 'functions', 'season_resolution.py')
_identity_spec = importlib.util.spec_from_file_location('season_resolution', _IDENTITY_PATH)
_identity_module = importlib.util.module_from_spec(_identity_spec)
_identity_spec.loader.exec_module(_identity_module)
episode_identity_verdict = _identity_module.episode_identity_verdict


def ro_connection(path):
    absolute = os.path.abspath(path)
    return sqlite3.connect(
        f"file:{absolute}?mode=ro&immutable=1", uri=True)


def parse_filename(filename):
    try:
        from PTT import parse_title
        parsed = parse_title(filename) or {}
        return parsed.get('seasons') or [], parsed.get('episodes') or [], parsed.get('date')
    except Exception:
        match = re.search(r'(?i)(?<![A-Za-z0-9])S(\d{1,2})[ ._-]*E(\d{1,4})(?!\d)', filename or '')
        if match:
            return [int(match.group(1))], [int(match.group(2))], None
        match = re.search(
            r'(?i)(?<![A-Za-z0-9])S(\d{1,2})\s*[-–—]\s*(\d{1,4})(?!\d)',
            filename or '')
        if match:
            return [int(match.group(1))], [int(match.group(2))], None
        # Conservative anime fallback: a bare episode is conventionally
        # delimited as " - 81 ".  Do not guess from years, resolution, CRCs, or
        # pack ranges when PTT is unavailable.
        numbers = re.findall(
            r'(?:\s|_)-(?:\s|_)(\d{1,4})(?=\s|_|\[|\.|$)',
            os.path.basename(filename or ''),
        )
        return [], [int(numbers[-1])] if numbers else [], None


def episode_metadata(conn, imdb_id, season, episode):
    row = conn.execute(
        "SELECT e.absolute_episode, e.first_aired, e.title, i.id "
        "FROM items i JOIN seasons s ON s.item_id=i.id "
        "JOIN episodes e ON e.season_id=s.id "
        "WHERE i.imdb_id=? AND s.season_number=? AND e.episode_number=? LIMIT 1",
        (imdb_id, season, episode),
    ).fetchone()
    if not row:
        return None, None, None, []
    absolute, first_aired, title, item_id = row
    others = [value[0] for value in conn.execute(
        "SELECT e.title FROM episodes e JOIN seasons s ON s.id=e.season_id "
        "WHERE s.item_id=? AND NOT (s.season_number=? AND e.episode_number=?) "
        "AND e.title IS NOT NULL",
        (item_id, season, episode),
    )]
    airdate = str(first_aired)[:10] if first_aired else None
    return absolute, airdate, title, others


def audit(media_db, battery_db, limit=None):
    media = ro_connection(media_db)
    battery = ro_connection(battery_db)
    media.row_factory = sqlite3.Row
    query = (
        "SELECT id, imdb_id, title, season_number, episode_number, episode_title, "
        "release_date, filled_by_title, filled_by_file, state FROM media_items "
        "WHERE type='episode' AND state IN ('Collected','Upgrading') "
        "AND lower(COALESCE(genres,'')) LIKE '%anime%' "
        "AND filled_by_file IS NOT NULL ORDER BY id"
    )
    if limit:
        query += " LIMIT ?"
        rows = media.execute(query, (limit,)).fetchall()
    else:
        rows = media.execute(query).fetchall()

    findings = []
    for row in rows:
        item = dict(row)
        absolute, airdate, battery_title, other_titles = episode_metadata(
            battery, item['imdb_id'], item['season_number'], item['episode_number'])
        filename = item['filled_by_file'] or ''
        seasons, episodes, parsed_date = parse_filename(filename)
        ok, reason = episode_identity_verdict(
            target_coordinates=[(item['season_number'], item['episode_number'])],
            file_seasons=seasons,
            file_numbers=episodes,
            filename=filename,
            absolute_episode=absolute,
            is_anime=True,
            target_air_date=airdate or item.get('release_date'),
            file_air_date=parsed_date,
            episode_title=item.get('episode_title') or battery_title,
            other_episode_titles=other_titles,
            series_title=item.get('title'),
        )
        if not ok:
            parent = item.get('filled_by_title') or ''
            severity = 'deterministic' if reason == _identity_module.IDENTITY_EXPLICIT_CONFLICT else 'suspicious'
            range_match = re.search(r'(?<!\d)(\d{1,4})\s*[-–]\s*(\d{1,4})(?!\d)', parent)
            if range_match and absolute is not None:
                start, end = map(int, range_match.groups())
                if not start <= int(absolute) <= end:
                    severity = 'deterministic'
                    reason = f"parent pack range {start}-{end} excludes absolute episode {absolute}"
            findings.append({
                'id': item['id'],
                'imdb_id': item['imdb_id'],
                'target': f"S{item['season_number']:02d}E{item['episode_number']:02d}",
                'absolute_episode': absolute,
                'filled_by_title': item.get('filled_by_title'),
                'filled_by_file': filename,
                'parsed_seasons': seasons,
                'parsed_episodes': episodes,
                'reason': reason,
                'severity': severity,
            })
    media.close()
    battery.close()
    deterministic = sum(finding['severity'] == 'deterministic' for finding in findings)
    return {
        'audited': len(rows),
        'mismatches': len(findings),
        'deterministic': deterministic,
        'suspicious': len(findings) - deterministic,
        'findings': findings,
    }


def main():
    default_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    parser = argparse.ArgumentParser()
    parser.add_argument('--media-db', default=os.path.join(default_dir, 'media_items.db'))
    parser.add_argument('--battery-db', default=os.path.join(default_dir, 'cli_battery.db'))
    parser.add_argument('--limit', type=int)
    parser.add_argument('--output')
    parser.add_argument('--fail-on-mismatch', action='store_true')
    args = parser.parse_args()
    report = audit(args.media_db, args.battery_db, args.limit)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as handle:
            handle.write(rendered + '\n')
    print(rendered)
    return 2 if args.fail_on_mismatch and report['mismatches'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
