#!/usr/bin/env python3
"""Load public.comics into inventory (re-runnable, does not write public).

  poetry run inventory-etl status
  poetry run inventory-etl load
  poetry run inventory-etl run
  poetry run inventory-etl run --skip-load
"""

from __future__ import annotations

import argparse
import sys

from psycopg2.extras import execute_values

from comic_inventory.db import connect
from comic_inventory.legacy_map import extract_cover_names

# Keep in sync with comic_inventory.legacy_map.
SQL_ISSUE_NUMBER = """
CASE
    WHEN s.number IS NULL OR btrim(s.number) = '' THEN '-'
    WHEN regexp_replace(btrim(s.number), '^#+', '') = '' THEN '-'
    WHEN regexp_replace(btrim(s.number), '^#+', '') ~ '^0+[0-9]+$'
        THEN COALESCE(NULLIF(ltrim(regexp_replace(btrim(s.number), '^#+', ''), '0'), ''), '0')
    ELSE regexp_replace(btrim(s.number), '^#+', '')
END
"""

SQL_VOLUME = """
CASE
    WHEN s.series IS NULL OR btrim(s.series) IN ('', '-') THEN ''
    ELSE btrim(s.series)
END
"""

SQL_MONTH = """
CASE
    WHEN s.publish_month ~ '^(0?[1-9]|1[0-2])$' THEN s.publish_month::smallint
    ELSE NULL
END
"""

SQL_COVER_DATE = """
CASE
    WHEN s.publish_date ~ '^(0?[1-9]|1[0-2])/[0-9]{4}$'
        THEN make_date(
            split_part(s.publish_date, '/', 2)::int,
            split_part(s.publish_date, '/', 1)::int,
            1
        )
    ELSE NULL
END
"""

SQL_GRADE = """
CASE upper(btrim(s.grade))
    WHEN 'M'  THEN 'mint'
    WHEN 'NM' THEN 'near_mint'
    WHEN 'VF' THEN 'very_fine'
    WHEN 'FN' THEN 'fine'
    WHEN 'F'  THEN 'fair'
    WHEN 'VG' THEN 'very_good'
    WHEN 'G'  THEN 'good'
    WHEN 'P'  THEN 'poor'
    ELSE NULL
END
"""

LOAD_STAGING_SQL = """
TRUNCATE inventory.staging_legacy_comics;

INSERT INTO inventory.staging_legacy_comics (
    legacy_id, title, series, publishing_company, volume, number, copy,
    series_notes, newbox, publish_date, publish_year, publish_month,
    writer, art, inks, colors, grade, grading_comments, cover_price,
    purchase_price, near_mint_value, comments, box,
    very_fine_value, fine_value, very_good_value, good_value, fair_value,
    entry_date, as_of_date, order_number
)
SELECT
    id, title, series, publishing_company, volume, number, copy,
    series_notes, newbox, publish_date, publish_year, publish_month,
    writer, art, inks, colors, grade, grading_comments, cover_price,
    purchase_price, near_mint_value, comments, box,
    very_fine_value, fine_value, very_good_value, good_value, fair_value,
    entry_date AT TIME ZONE 'UTC',
    as_of_date AT TIME ZONE 'UTC',
    order_number
FROM public.comics;
"""

UPSERT_PUBLISHERS_SQL = """
INSERT INTO inventory.publishers (name)
SELECT DISTINCT btrim(publishing_company)
FROM inventory.staging_legacy_comics
WHERE publishing_company IS NOT NULL AND btrim(publishing_company) <> ''
ON CONFLICT (name) DO NOTHING;
"""

STATUS_SQL = """
SELECT
    (SELECT count(*) FROM public.comics) AS comics,
    (SELECT count(*) FROM inventory.staging_legacy_comics) AS staging,
    (SELECT count(*) FROM inventory.publishers WHERE deleted_at IS NULL) AS publishers,
    (SELECT count(*) FROM inventory.series WHERE deleted_at IS NULL) AS series,
    (SELECT count(*) FROM inventory.issues WHERE deleted_at IS NULL) AS issues,
    (SELECT count(*) FROM inventory.variants WHERE deleted_at IS NULL) AS variants,
    (SELECT count(*) FROM inventory.creators) AS creators,
    (SELECT count(*) FROM inventory.issue_creators) AS issue_creators,
    (SELECT count(*) FROM inventory.inventory_items WHERE deleted_at IS NULL) AS items,
    (SELECT count(*) FROM inventory.inventory_fmv) AS fmv,
    (SELECT count(*) FROM public.comics_gocollect_fmv) AS source_fmv
"""

ETL_ROLES = ("writer", "penciller", "inker", "colorist", "cover")


def _set_search_path(cur) -> None:
    cur.execute("SET LOCAL search_path TO inventory, public")


def load_staging(cur) -> int:
    cur.execute(LOAD_STAGING_SQL)
    cur.execute("SELECT count(*) FROM inventory.staging_legacy_comics")
    return cur.fetchone()[0]


def _issue_from_staging() -> str:
    return f"""
        FROM inventory.staging_legacy_comics s
        JOIN inventory.publishers p ON p.name = btrim(s.publishing_company)
        JOIN inventory.series ser
          ON ser.publisher_id = p.id
         AND ser.name = btrim(s.title)
         AND ser.volume = {SQL_VOLUME}
        JOIN inventory.issues iss
          ON iss.series_id = ser.id
         AND iss.number = {SQL_ISSUE_NUMBER}
    """


def _split_column(column: str) -> str:
    return f"""
        regexp_split_to_table(
            replace(replace(
                regexp_replace(
                    coalesce(s.{column}, ''),
                    ',\\s*(Jr\\.?|Sr\\.?|III|II|IV)\\y',
                    ' \\1',
                    'gi'
                ),
                '&', ','),
                '/', ','),
            '\\s*,\\s*'
        )
    """


def _credit_insert_sql(column: str, role: str, extra_where: str = "") -> str:
    where = extra_where.strip()
    if where and not where.upper().startswith("AND"):
        where = "AND " + where
    return f"""
        INSERT INTO etl_credits (issue_id, role, name)
        SELECT iss.id, '{role}', regexp_replace(btrim(part), '\\s+', ' ', 'g')
        {_issue_from_staging()}
        CROSS JOIN LATERAL {_split_column(column)} AS part
        WHERE btrim(part) <> ''
          AND btrim(part) <> '-'
          {where}
    """


def transform_creators(cur) -> None:
    cur.execute(
        """
        CREATE TEMP TABLE etl_credits (
            issue_id uuid NOT NULL,
            role     text NOT NULL,
            name     text NOT NULL
        ) ON COMMIT DROP
        """
    )
    cur.execute(_credit_insert_sql("writer", "writer"))
    cur.execute(_credit_insert_sql("art", "penciller"))
    cur.execute(
        _credit_insert_sql(
            "inks",
            "inker",
            "(s.inks IS NOT NULL AND btrim(s.inks) NOT IN ('', '-'))",
        )
    )
    cur.execute(
        _credit_insert_sql(
            "art",
            "inker",
            "(s.inks IS NULL OR btrim(s.inks) IN ('', '-'))",
        )
    )
    cur.execute(_credit_insert_sql("colors", "colorist"))

    cur.execute(
        f"""
        SELECT DISTINCT iss.id, s.comments
        {_issue_from_staging()}
        WHERE s.comments IS NOT NULL
          AND s.comments ~* 'cover'
        """
    )
    cover_rows = []
    for issue_id, comments in cur.fetchall():
        for name in extract_cover_names(comments):
            cover_rows.append((issue_id, "cover", name))
    if cover_rows:
        execute_values(
            cur,
            "INSERT INTO etl_credits (issue_id, role, name) VALUES %s",
            cover_rows,
        )

    cur.execute(
        """
        INSERT INTO inventory.creators (name)
        SELECT name
        FROM (
            SELECT name,
                   row_number() OVER (
                       PARTITION BY lower(name)
                       ORDER BY cnt DESC, name
                   ) AS rn
            FROM (
                SELECT name, count(*) AS cnt
                FROM etl_credits
                GROUP BY name
            ) t
        ) x
        WHERE rn = 1
        ON CONFLICT ((lower(name))) DO NOTHING
        """
    )

    cur.execute(
        f"""
        DELETE FROM inventory.issue_creators ic
        WHERE ic.role IN %s
          AND ic.issue_id IN (
              SELECT iss.id
              {_issue_from_staging()}
          )
        """,
        (ETL_ROLES,),
    )

    cur.execute(
        """
        INSERT INTO inventory.issue_creators (issue_id, creator_id, role)
        SELECT DISTINCT c.issue_id, cr.id, c.role
        FROM etl_credits c
        JOIN inventory.creators cr ON lower(cr.name) = lower(c.name)
        ON CONFLICT DO NOTHING
        """
    )


def transform(cur) -> None:
    cur.execute(UPSERT_PUBLISHERS_SQL)

    cur.execute(
        f"""
        INSERT INTO inventory.series (publisher_id, name, volume, start_year)
        SELECT
            p.id,
            btrim(s.title),
            {SQL_VOLUME},
            MIN(s.publish_year)
        FROM inventory.staging_legacy_comics s
        JOIN inventory.publishers p ON p.name = btrim(s.publishing_company)
        WHERE s.title IS NOT NULL AND btrim(s.title) <> ''
        GROUP BY p.id, btrim(s.title), {SQL_VOLUME}
        ON CONFLICT (publisher_id, name, volume) DO UPDATE
        SET start_year = CASE
            WHEN EXCLUDED.start_year IS NULL THEN series.start_year
            WHEN series.start_year IS NULL THEN EXCLUDED.start_year
            ELSE LEAST(series.start_year, EXCLUDED.start_year)
        END
        """
    )

    cur.execute(
        f"""
        INSERT INTO inventory.issues (
            series_id, number, publish_year, publish_month, cover_date, cover_price
        )
        SELECT
            ser.id,
            {SQL_ISSUE_NUMBER},
            MIN(s.publish_year),
            MIN({SQL_MONTH}),
            MIN({SQL_COVER_DATE}),
            MAX(NULLIF(s.cover_price, 0))
        FROM inventory.staging_legacy_comics s
        JOIN inventory.publishers p ON p.name = btrim(s.publishing_company)
        JOIN inventory.series ser
          ON ser.publisher_id = p.id
         AND ser.name = btrim(s.title)
         AND ser.volume = {SQL_VOLUME}
        GROUP BY ser.id, {SQL_ISSUE_NUMBER}
        ON CONFLICT (series_id, number) DO UPDATE
        SET publish_year = EXCLUDED.publish_year,
            publish_month = EXCLUDED.publish_month,
            cover_date = EXCLUDED.cover_date,
            cover_price = EXCLUDED.cover_price
        """
    )

    cur.execute(
        f"""
        INSERT INTO inventory.variants (issue_id, name, is_default)
        SELECT DISTINCT iss.id, 'Standard', true
        FROM inventory.staging_legacy_comics s
        JOIN inventory.publishers p ON p.name = btrim(s.publishing_company)
        JOIN inventory.series ser
          ON ser.publisher_id = p.id
         AND ser.name = btrim(s.title)
         AND ser.volume = {SQL_VOLUME}
        JOIN inventory.issues iss
          ON iss.series_id = ser.id
         AND iss.number = {SQL_ISSUE_NUMBER}
        ON CONFLICT (issue_id, name) DO NOTHING
        """
    )

    cur.execute(
        f"""
        INSERT INTO inventory.variant_guide_values (
            variant_id, near_mint, very_fine, fine, very_good, good, fair, source
        )
        SELECT
            v.id,
            MAX(NULLIF(s.near_mint_value, 0)),
            MAX(NULLIF(s.very_fine_value, 0)),
            MAX(NULLIF(s.fine_value, 0)),
            MAX(NULLIF(s.very_good_value, 0)),
            MAX(NULLIF(s.good_value, 0)),
            MAX(NULLIF(s.fair_value, 0)),
            'legacy_import'
        FROM inventory.staging_legacy_comics s
        JOIN inventory.publishers p ON p.name = btrim(s.publishing_company)
        JOIN inventory.series ser
          ON ser.publisher_id = p.id
         AND ser.name = btrim(s.title)
         AND ser.volume = {SQL_VOLUME}
        JOIN inventory.issues iss
          ON iss.series_id = ser.id
         AND iss.number = {SQL_ISSUE_NUMBER}
        JOIN inventory.variants v
          ON v.issue_id = iss.id
         AND v.name = 'Standard'
        GROUP BY v.id
        ON CONFLICT (variant_id) DO UPDATE
        SET near_mint = EXCLUDED.near_mint,
            very_fine = EXCLUDED.very_fine,
            fine = EXCLUDED.fine,
            very_good = EXCLUDED.very_good,
            good = EXCLUDED.good,
            fair = EXCLUDED.fair,
            source = EXCLUDED.source,
            updated_at = now()
        """
    )

    transform_creators(cur)

    cur.execute(
        f"""
        INSERT INTO inventory.inventory_items (
            variant_id, legacy_id, copy_label,
            grade_scale, grade, grading_comments, notes,
            location_code, purchase_price, cover_price_paid,
            status, quantity, entry_date, as_of_date
        )
        SELECT
            v.id,
            s.legacy_id,
            s.copy,
            'raw',
            {SQL_GRADE}::inventory.condition_grade,
            s.grading_comments,
            s.comments,
            NULLIF(btrim(s.box), ''),
            s.purchase_price,
            s.cover_price,
            'in_stock',
            1,
            COALESCE(s.entry_date, now()),
            s.as_of_date
        FROM inventory.staging_legacy_comics s
        JOIN inventory.publishers p ON p.name = btrim(s.publishing_company)
        JOIN inventory.series ser
          ON ser.publisher_id = p.id
         AND ser.name = btrim(s.title)
         AND ser.volume = {SQL_VOLUME}
        JOIN inventory.issues iss
          ON iss.series_id = ser.id
         AND iss.number = {SQL_ISSUE_NUMBER}
        JOIN inventory.variants v
          ON v.issue_id = iss.id
         AND v.name = 'Standard'
        ON CONFLICT (legacy_id) DO UPDATE
        SET variant_id = EXCLUDED.variant_id,
            copy_label = EXCLUDED.copy_label,
            grade_scale = EXCLUDED.grade_scale,
            grade = EXCLUDED.grade,
            grading_comments = EXCLUDED.grading_comments,
            notes = EXCLUDED.notes,
            location_code = EXCLUDED.location_code,
            purchase_price = EXCLUDED.purchase_price,
            cover_price_paid = EXCLUDED.cover_price_paid,
            entry_date = EXCLUDED.entry_date,
            as_of_date = EXCLUDED.as_of_date
        """
    )

    cur.execute(
        """
        INSERT INTO inventory.inventory_fmv (
            inventory_item_id, source, closest_grade, fmv
        )
        SELECT i.id, 'gocollect', f.cgc_closest_grade, f.gocollect_fmv
        FROM public.comics_gocollect_fmv f
        JOIN inventory.inventory_items i ON i.legacy_id = f.comics_id
        ON CONFLICT (inventory_item_id) DO UPDATE
        SET source = EXCLUDED.source,
            closest_grade = EXCLUDED.closest_grade,
            fmv = EXCLUDED.fmv,
            updated_at = now()
        """
    )

    cur.execute(
        """
        DELETE FROM inventory.inventory_fmv fmv
        USING inventory.inventory_items i
        WHERE fmv.inventory_item_id = i.id
          AND i.legacy_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM public.comics_gocollect_fmv f
              WHERE f.comics_id = i.legacy_id
          )
        """
    )


def gather_report(cur) -> dict:
    cur.execute(STATUS_SQL)
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    counts = dict(zip(cols, row))

    cur.execute(
        """
        SELECT DISTINCT grade
        FROM inventory.staging_legacy_comics
        WHERE grade IS NOT NULL
          AND btrim(grade) <> ''
          AND upper(btrim(grade)) NOT IN ('M', 'NM', 'VF', 'FN', 'F', 'VG', 'G', 'P')
        ORDER BY 1
        """
    )
    unmapped_grades = [r[0] for r in cur.fetchall()]

    cur.execute(
        """
        SELECT count(*)
        FROM inventory.staging_legacy_comics s
        WHERE NOT EXISTS (
            SELECT 1
            FROM inventory.inventory_items i
            WHERE i.legacy_id = s.legacy_id
              AND i.deleted_at IS NULL
        )
        """
    )
    staging_without_item = cur.fetchone()[0]

    cur.execute(
        """
        SELECT count(*)
        FROM inventory.inventory_items i
        WHERE i.deleted_at IS NULL
          AND i.variant_id IS NULL
        """
    )
    items_missing_variant = cur.fetchone()[0]

    cur.execute(
        """
        SELECT count(*)
        FROM public.comics_gocollect_fmv f
        WHERE NOT EXISTS (
            SELECT 1
            FROM inventory.inventory_items i
            JOIN inventory.inventory_fmv fmv ON fmv.inventory_item_id = i.id
            WHERE i.legacy_id = f.comics_id
        )
        """
    )
    fmv_not_landed = cur.fetchone()[0]

    cur.execute(
        """
        SELECT count(*)
        FROM inventory.issues iss
        WHERE iss.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM inventory.issue_creators ic
              WHERE ic.issue_id = iss.id
                AND ic.role = 'writer'
          )
        """
    )
    issues_without_writer = cur.fetchone()[0]

    cur.execute(
        """
        SELECT count(*)
        FROM inventory.staging_legacy_comics
        WHERE writer IS NOT NULL
          AND btrim(writer) NOT IN ('', '-')
        """
    )
    staging_with_writer = cur.fetchone()[0]

    errors: list[str] = []
    if counts["items"] != counts["comics"]:
        errors.append(
            f"inventory_items ({counts['items']}) != public.comics ({counts['comics']})"
        )
    if staging_without_item:
        errors.append(f"{staging_without_item} staging rows have no inventory_items")
    if items_missing_variant:
        errors.append(f"{items_missing_variant} items missing variant_id")
    if fmv_not_landed:
        errors.append(f"{fmv_not_landed} comics_gocollect_fmv rows did not land")
    if unmapped_grades:
        errors.append(f"unmapped grades: {unmapped_grades}")
    if staging_with_writer and counts["issue_creators"] == 0:
        errors.append("staging has writers but issue_creators is empty")

    return {
        "counts": counts,
        "unmapped_grades": unmapped_grades,
        "staging_without_item": staging_without_item,
        "items_missing_variant": items_missing_variant,
        "fmv_not_landed": fmv_not_landed,
        "issues_without_writer": issues_without_writer,
        "errors": errors,
    }


def print_report(report: dict) -> None:
    c = report["counts"]
    print(f"{'source':<22} count")
    print("-" * 36)
    print(f"{'public.comics':<22} {c['comics']}")
    print(f"{'staging':<22} {c['staging']}")
    print(f"{'publishers':<22} {c['publishers']}")
    print(f"{'series':<22} {c['series']}")
    print(f"{'issues':<22} {c['issues']}")
    print(f"{'variants':<22} {c['variants']}")
    print(f"{'creators':<22} {c['creators']}")
    print(f"{'issue_creators':<22} {c['issue_creators']}")
    print(f"{'inventory_items':<22} {c['items']}")
    print(f"{'inventory_fmv':<22} {c['fmv']}")
    print(f"{'source fmv':<22} {c['source_fmv']}")
    print("-" * 36)
    print(f"unmapped grades: {report['unmapped_grades'] or '(none)'}")
    print(f"staging without item: {report['staging_without_item']}")
    print(f"items missing variant: {report['items_missing_variant']}")
    print(f"fmv not landed: {report['fmv_not_landed']}")
    print(f"issues without writer: {report['issues_without_writer']}")
    if report["errors"]:
        print("ERRORS:")
        for err in report["errors"]:
            print(f"  - {err}")


def cmd_status(conn) -> int:
    with conn.cursor() as cur:
        _set_search_path(cur)
        report = gather_report(cur)
    print_report(report)
    return 1 if report["errors"] else 0


def cmd_load(conn) -> int:
    with conn.cursor() as cur:
        _set_search_path(cur)
        n = load_staging(cur)
    conn.commit()
    print(f"Loaded {n} rows into inventory.staging_legacy_comics")
    return 0


def cmd_run(conn, skip_load: bool) -> int:
    with conn.cursor() as cur:
        _set_search_path(cur)
        if skip_load:
            cur.execute("SELECT count(*) FROM inventory.staging_legacy_comics")
            if cur.fetchone()[0] == 0:
                print("staging is empty; run without --skip-load", file=sys.stderr)
                conn.rollback()
                return 1
            print("Using existing staging (--skip-load)")
        else:
            n = load_staging(cur)
            print(f"Loaded {n} rows into staging")
        transform(cur)
        report = gather_report(cur)
        print_report(report)
        if report["errors"]:
            conn.rollback()
            print("Rolled back.", file=sys.stderr)
            return 1
    conn.commit()
    print("Committed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Count comics vs staging vs catalog vs items vs FMV")
    sub.add_parser("load", help="Truncate staging and copy public.comics")
    run = sub.add_parser("run", help="Load staging (unless skipped) and upsert inventory")
    run.add_argument(
        "--skip-load",
        action="store_true",
        help="Transform existing staging only",
    )
    args = parser.parse_args(argv)

    conn = connect()
    try:
        if args.command == "status":
            return cmd_status(conn)
        if args.command == "load":
            return cmd_load(conn)
        if args.command == "run":
            return cmd_run(conn, args.skip_load)
        parser.error(f"unknown command {args.command}")
        return 2
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
