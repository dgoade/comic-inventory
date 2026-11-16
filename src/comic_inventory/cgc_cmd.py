#!/usr/bin/env python3
"""List a CGC slab on eBay using local photos.

  poetry run inventory-cgc show 4719983018 --legacy-id 123 --grade 9.8 \\
      --front front.jpg --back back.jpg
  poetry run inventory-cgc prepare 4719983018 --legacy-id 123 --price 25.00 --grade 9.8 \\
      --front front.jpg --back back.jpg
  poetry run inventory-cgc publish 4719983018

show prints the copy and the local photos. It writes nothing and does not
call CGC. prepare saves the slab, uploads those two files, and saves a draft
listing. publish is the only step that calls eBay, then reserves the copy.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from comic_inventory.cgc_client import (
    CgcCert,
    CommandError,
    build_aspects,
    build_description,
    build_title,
    cgc_comment_block,
    clean_cert,
    merge_grading_comments,
    parse_grade,
    parse_price,
)
from comic_inventory.db import connect, load_project_env
from comic_inventory.ebay import POLICY_ENV, missing_names, publish_listing
from comic_inventory.storage import listing_channel_data, read_local_image, save_image_rows, store_pair

EBAY_CHANNEL = {"marketplace_id": "EBAY_US", "category_id": "259104"}


def _image_paths(front: str, back: str) -> tuple[Path, Path]:
    front_path = Path(front).expanduser()
    back_path = Path(back).expanduser()
    if front_path.resolve() == back_path.resolve():
        raise CommandError("front and back must be two different files")
    return front_path, back_path


def slab_from_copy(cert_number: str, grade_raw: str, copy: dict) -> CgcCert:
    """Listing facts from the inventory copy and the grade typed by the operator."""
    grade = parse_grade(grade_raw)
    display = f"{grade:.1f}" if grade is not None else " ".join(grade_raw.split())
    year = copy["publish_year"]
    year_int = int(year) if isinstance(year, int) and year > 0 else None
    return CgcCert(
        cert_number=cert_number,
        display_grade=display,
        grade_numeric=grade,
        grade_category="",
        title=copy["series_name"] or "",
        issue=str(copy["issue_number"] or ""),
        publisher=copy["publisher"] or "",
        year=year_int,
        variant="",
        page_quality="",
        grader_notes=(),
        signatures=(),
        label_text="",
        front_url="",
        rear_url="",
        signed=False,
        restored=False,
    )


def _money(value) -> str:
    if value is None:
        return "-"
    return f"{Decimal(value):.2f}"


def load_copy(cur, legacy_id: int) -> dict:
    cur.execute(
        """
        SELECT
            i.id::text AS item_id,
            i.legacy_id,
            i.copy_label,
            i.grade_scale::text AS grade_scale,
            i.grade::text AS grade,
            i.grade_numeric,
            i.certification_number,
            i.status::text AS status,
            i.location_code,
            i.grading_comments,
            i.quantity,
            i.reserved_quantity,
            f.fmv,
            p.name AS publisher,
            s.name AS series_name,
            s.volume AS series_volume,
            iss.number AS issue_number,
            iss.publish_year
        FROM inventory.inventory_items i
        JOIN inventory.variants v ON v.id = i.variant_id AND v.deleted_at IS NULL
        JOIN inventory.issues iss ON iss.id = v.issue_id AND iss.deleted_at IS NULL
        JOIN inventory.series s ON s.id = iss.series_id AND s.deleted_at IS NULL
        LEFT JOIN inventory.publishers p ON p.id = s.publisher_id AND p.deleted_at IS NULL
        LEFT JOIN inventory.inventory_fmv f ON f.inventory_item_id = i.id
        WHERE i.deleted_at IS NULL
          AND i.legacy_id = %s
        ORDER BY i.id
        """,
        (legacy_id,),
    )
    rows = list(cur.fetchall())
    if not rows:
        raise CommandError(f"no inventory copy with legacy_id {legacy_id}")
    if len(rows) > 1:
        raise CommandError(f"legacy_id {legacy_id} matches {len(rows)} copies")
    return rows[0]


def _refuse_other_cert(cur, item_id: str, cert_number: str, current: str | None) -> None:
    if current and current.strip() and current.strip() != cert_number:
        raise CommandError(f"this copy is already certification {current}")
    cur.execute(
        """
        SELECT legacy_id
        FROM inventory.inventory_items
        WHERE certification_number = %s
          AND deleted_at IS NULL
          AND id <> %s
        """,
        (cert_number, item_id),
    )
    other = cur.fetchone()
    if other:
        raise CommandError(
            f"cert {cert_number} is already on legacy_id {other['legacy_id']}"
        )


def _category_id(config) -> str:
    if isinstance(config, dict) and config.get("category_id"):
        return str(config["category_id"])
    return EBAY_CHANNEL["category_id"]


def ensure_ebay_channel(cur) -> dict:
    cur.execute(
        """
        INSERT INTO inventory.sales_channels (type, name, config)
        VALUES ('ebay', 'eBay', %s)
        ON CONFLICT (type, name) DO NOTHING
        """,
        (Json(EBAY_CHANNEL),),
    )
    cur.execute(
        """
        SELECT id::text AS id, config
        FROM inventory.sales_channels
        WHERE type = 'ebay' AND name = 'eBay'
        """
    )
    row = cur.fetchone()
    if row is None:
        raise CommandError("eBay sales channel was not created")
    return row


def upsert_draft(
    cur,
    *,
    item_id: str,
    channel_id: str,
    title: str,
    description: str,
    price: Decimal,
    cert_number: str,
    channel_data: dict,
) -> None:
    cur.execute(
        """
        SELECT id::text AS id, status::text AS status, channel_data
        FROM inventory.channel_listings
        WHERE inventory_item_id = %s
          AND channel_id = %s
          AND deleted_at IS NULL
        ORDER BY created_at
        """,
        (item_id, channel_id),
    )
    rows = list(cur.fetchall())
    if rows:
        drafts = [row for row in rows if row["status"] == "draft"]
        target = (drafts or rows)[-1]
        previous = target["channel_data"] if isinstance(target["channel_data"], dict) else {}
        previous_offer = ((previous.get("ebay") or {}).get("offer_id"))
        if previous_offer and not channel_data["ebay"].get("offer_id"):
            channel_data["ebay"]["offer_id"] = previous_offer
        if len(rows) > 1:
            print(
                f"{len(rows)} eBay listing rows exist for this copy. "
                "Updated one and did not add another."
            )
        if target["status"] == "active":
            cur.execute(
                """
                UPDATE inventory.channel_listings
                SET title = %s,
                    description = %s,
                    price = %s,
                    external_sku = %s,
                    channel_data = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (title, description, price, cert_number, Json(channel_data), target["id"]),
            )
            print("Listing is already active. Updated the text and left the status active.")
            return "active"
        else:
            cur.execute(
                """
                UPDATE inventory.channel_listings
                SET title = %s,
                    description = %s,
                    price = %s,
                    external_sku = %s,
                    channel_data = %s,
                    status = 'draft',
                    quantity_listed = 1,
                    updated_at = now()
                WHERE id = %s
                """,
                (title, description, price, cert_number, Json(channel_data), target["id"]),
            )
        return "draft"

    cur.execute(
        """
        INSERT INTO inventory.channel_listings (
            inventory_item_id, channel_id, external_sku, title, description,
            price, currency, quantity_listed, status, channel_data
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'USD', 1, 'draft', %s)
        """,
        (item_id, channel_id, cert_number, title, description, price, Json(channel_data)),
    )
    return "draft"


def _read_photos(front: str, back: str) -> tuple[Path, Path, tuple[bytes, str], tuple[bytes, str]]:
    front_path, back_path = _image_paths(front, back)
    front_bytes, front_type, _front_ext = read_local_image(front_path)
    back_bytes, back_type, _back_ext = read_local_image(back_path)
    return front_path, back_path, (front_bytes, front_type), (back_bytes, back_type)


def _print_slab(cert: CgcCert, chosen: dict, front_path: Path, back_path: Path) -> None:
    volume = f" v{chosen['series_volume']}" if chosen["series_volume"] else ""
    year = f" {cert.year}" if cert.year else ""
    print(f"CGC {cert.cert_number}")
    print(
        f"  legacy_id={chosen['legacy_id']} copy={chosen['copy_label'] or '-'} "
        f"loc={chosen['location_code'] or '-'} fmv={_money(chosen['fmv'])}"
    )
    print(f"  {chosen['publisher'] or ''} {chosen['series_name']}{volume} #{chosen['issue_number']}{year}")
    print(f"  grade {cert.grade_text}")
    print(f"  front: {front_path}")
    print(f"  back: {back_path}")


def cmd_show(conn, cert_raw: str, legacy_id: int, grade_raw: str, front: str, back: str) -> int:
    cert_number = clean_cert(cert_raw)
    front_path, back_path, _front, _back = _read_photos(front, back)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SET LOCAL search_path TO inventory, public")
        chosen = load_copy(cur, legacy_id)
    cert = slab_from_copy(cert_number, grade_raw, chosen)
    _print_slab(cert, chosen, front_path, back_path)
    if cert.grade_numeric is None:
        print("This grade is not an eBay Grade value. publish will refuse it.")
    print("No database writes. Photos stay on this machine until prepare.")
    return 0


def _save_photos(cur, item_id: str, series: str, issue: str, grade: str, front, back) -> list:
    photos = store_pair(
        os.environ,
        item_id=item_id,
        front=front,
        rear=back,
        alt_front=f"{series} #{issue} CGC {grade} front",
        alt_rear=f"{series} #{issue} CGC {grade} back",
    )
    save_image_rows(cur, item_id, photos)
    return photos


def cmd_prepare(
    conn,
    cert_raw: str,
    legacy_id: int,
    price_raw: str,
    grade_raw: str,
    front: str,
    back: str,
) -> int:
    cert_number = clean_cert(cert_raw)
    price = parse_price(price_raw)
    front_path, back_path, front_photo, back_photo = _read_photos(front, back)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL search_path TO inventory, public")
            chosen = load_copy(cur, legacy_id)
            cert = slab_from_copy(cert_number, grade_raw, chosen)
            _print_slab(cert, chosen, front_path, back_path)
            _refuse_other_cert(cur, chosen["item_id"], cert.cert_number, chosen["certification_number"])
            comments = merge_grading_comments(chosen["grading_comments"], cgc_comment_block(cert))
            cur.execute(
                """
                UPDATE inventory.inventory_items
                SET grade_scale = 'cgc',
                    grade_numeric = %s,
                    certification_number = %s,
                    grader = 'CGC',
                    is_signed = %s,
                    is_restored = %s,
                    grading_comments = %s,
                    updated_at = now()
                WHERE id = %s
                  AND deleted_at IS NULL
                """,
                (
                    cert.grade_numeric,
                    cert.cert_number,
                    cert.signed,
                    cert.restored,
                    comments,
                    chosen["item_id"],
                ),
            )
            if cur.rowcount != 1:
                raise CommandError(f"legacy_id {legacy_id} could not be updated")
            if cert.grade_numeric is None:
                print(
                    "This grade is not on the 0.5-10.0 scale. "
                    "The copy was saved. publish will refuse until the grade maps."
                )

            series = chosen["series_name"]
            issue = chosen["issue_number"]
            publisher = chosen["publisher"] or ""
            year = cert.year
            photos = _save_photos(
                cur, chosen["item_id"], series, issue, cert.grade_text, front_photo, back_photo
            )
            channel = ensure_ebay_channel(cur)
            title = build_title(
                series=series,
                issue=issue,
                grade=cert.grade_text,
                restored=cert.restored,
                variant=cert.variant,
                publisher=publisher,
                year=year,
            )
            description = build_description(cert, series=series, issue=issue, publisher=publisher or "")
            aspects = build_aspects(
                cert_number=cert.cert_number,
                series=series,
                issue=issue,
                publisher=publisher,
                year=year,
                grade=cert.grade_numeric,
                signed=cert.signed,
            )
            channel_data = listing_channel_data(
                aspects=aspects,
                variant=cert.variant,
                cert_number=cert.cert_number,
                display_grade=cert.display_grade,
                grade_category=cert.grade_category,
                page_quality=cert.page_quality,
                front_url=str(front_path),
                rear_url=str(back_path),
                offer_id=None,
                category_id=_category_id(channel["config"]),
            )
            listing_state = upsert_draft(
                cur,
                item_id=chosen["item_id"],
                channel_id=channel["id"],
                title=title,
                description=description,
                price=price,
                cert_number=cert.cert_number,
                channel_data=channel_data,
            )
        conn.commit()
    except psycopg2.Error as exc:
        conn.rollback()
        if getattr(exc, "pgcode", None) == "23505":
            raise CommandError("certification number is already on another copy") from exc
        raise
    except Exception:
        conn.rollback()
        raise

    print(f"Using legacy_id {legacy_id}")
    fmv = _money(chosen["fmv"])
    print(f"Listing price {price:.2f}. inventory_fmv {fmv} is a hint, not the price.")
    if photos:
        print("Images:")
        for photo in photos:
            print(f"  {photo.url}")
    if listing_state == "active":
        print("Copy and photos saved. The eBay listing was already active.")
    else:
        print(
            "Draft listing saved. Not on eBay until: "
            f"poetry run inventory-cgc publish {cert.cert_number}"
        )
    return 0


def _image_urls(cur, item_id: str) -> list[str]:
    cur.execute(
        """
        SELECT url
        FROM inventory.inventory_images
        WHERE inventory_item_id = %s
          AND deleted_at IS NULL
          AND url IS NOT NULL
        ORDER BY is_primary DESC, sort_order, created_at
        """,
        (item_id,),
    )
    urls = []
    for row in cur.fetchall():
        url = row["url"]
        if isinstance(url, str) and url.startswith("https://") and url not in urls:
            urls.append(url)
    return urls


def _reserve(cur, item_id: str, listing_id: str) -> None:
    cur.execute(
        """
        UPDATE inventory.inventory_items
        SET reserved_quantity = 1, updated_at = now()
        WHERE id = %s
          AND deleted_at IS NULL
          AND status = 'in_stock'
          AND reserved_quantity = 0
          AND quantity >= 1
        RETURNING quantity
        """,
        (item_id,),
    )
    updated = cur.fetchone()
    if updated is None:
        raise CommandError("eBay listing is live, but the copy could not be reserved")
    quantity = updated["quantity"]
    cur.execute(
        """
        INSERT INTO inventory.stock_movements (
            inventory_item_id, movement_type, quantity_change,
            quantity_before, quantity_after, listing_id, reason
        )
        VALUES (%s, 'reserve', 0, %s, %s, %s, 'ebay_reserve')
        """,
        (item_id, quantity, quantity, listing_id),
    )


def cmd_publish(conn, cert_raw: str) -> int:
    cert_number = clean_cert(cert_raw)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SET LOCAL search_path TO inventory, public")
        cur.execute(
            """
            SELECT
                cl.id::text AS listing_id,
                cl.status::text AS listing_status,
                cl.price,
                cl.title,
                cl.description,
                cl.channel_data,
                cl.external_listing_id,
                cl.url,
                i.id::text AS item_id,
                i.quantity,
                i.reserved_quantity,
                i.status::text AS item_status,
                i.grade_numeric,
                sc.config AS channel_config
            FROM inventory.channel_listings cl
            JOIN inventory.inventory_items i ON i.id = cl.inventory_item_id
            JOIN inventory.sales_channels sc ON sc.id = cl.channel_id
            WHERE i.certification_number = %s
              AND sc.type = 'ebay'
              AND cl.deleted_at IS NULL
              AND i.deleted_at IS NULL
            ORDER BY cl.created_at DESC
            LIMIT 1
            """,
            (cert_number,),
        )
        row = cur.fetchone()
        if row is None:
            raise CommandError(f"no eBay draft for cert {cert_number}. Run prepare first.")
        if row["listing_status"] == "active" and row["external_listing_id"]:
            print(f"already active: {row['url'] or row['external_listing_id']}")
            return 0
        missing = missing_names(os.environ, POLICY_ENV)
        if missing:
            raise CommandError(
                "publish left the draft in place. Missing " + ", ".join(missing)
            )
        if row["item_status"] != "in_stock":
            raise CommandError(f"copy status is {row['item_status']}")
        if row["reserved_quantity"] != 0:
            raise CommandError("copy is already reserved")
        if row["quantity"] < 1:
            raise CommandError("copy quantity is 0")
        channel_data = row["channel_data"] if isinstance(row["channel_data"], dict) else {}
        aspects = channel_data.get("aspects") or {}
        if "Grade" not in aspects:
            raise CommandError("publish refused: CGC grade is not an eBay Grade value")
        urls = _image_urls(cur, row["item_id"])
        if len(urls) < 2:
            raise CommandError("publish needs the front and back https image URLs")
        offer_id = ((channel_data.get("ebay") or {}).get("offer_id")) or None
        category_id = ((channel_data.get("ebay") or {}).get("category_id")) or _category_id(
            row["channel_config"]
        )
        grade_text = f"{Decimal(row['grade_numeric']):.1f}" if row["grade_numeric"] is not None else "slab"
        published = publish_listing(
            os.environ,
            sku=cert_number,
            title=row["title"],
            description=row["description"] or "",
            aspects=aspects,
            image_urls=urls[:12],
            price=f"{Decimal(row['price']):.2f}",
            category_id=str(category_id),
            grade_text=grade_text,
            offer_id=offer_id,
        )
        ebay = dict(channel_data.get("ebay") or {})
        ebay["offer_id"] = published.offer_id
        ebay["listing_id"] = published.listing_id
        channel_data["ebay"] = ebay
        cur.execute(
            """
            UPDATE inventory.channel_listings
            SET status = 'active',
                external_listing_id = %s,
                url = %s,
                listed_at = now(),
                channel_data = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (published.listing_id, published.url, Json(channel_data), row["listing_id"]),
        )
        try:
            _reserve(cur, row["item_id"], row["listing_id"])
        except CommandError as exc:
            raise CommandError(f"{exc}. Listing URL: {published.url}") from exc
    conn.commit()
    print(f"active: {published.url}")
    print("Reserved quantity is 1. Physical status is unchanged.")
    return 0


def _add_photo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("cert")
    parser.add_argument("--legacy-id", type=int, required=True)
    parser.add_argument("--grade", required=True, help="CGC grade from the slab, such as 9.8.")
    parser.add_argument("--front", required=True, help="Local front photo. Jpeg, png, or webp.")
    parser.add_argument("--back", required=True, help="Local back photo. Jpeg, png, or webp.")


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="Print the copy and local photos. No database writes.")
    _add_photo_args(show)
    prepare = sub.add_parser("prepare", help="Bind the copy, upload local photos, save an eBay draft")
    _add_photo_args(prepare)
    prepare.add_argument("--price", required=True, help="Listing price in USD. Not taken from FMV.")
    publish = sub.add_parser("publish", help="Send the draft to eBay and reserve the copy")
    publish.add_argument("cert")
    args = parser.parse_args(argv)

    conn = None
    try:
        if args.command == "show":
            conn = connect()
            return cmd_show(conn, args.cert, args.legacy_id, args.grade, args.front, args.back)
        conn = connect()
        if args.command == "prepare":
            return cmd_prepare(
                conn, args.cert, args.legacy_id, args.price, args.grade, args.front, args.back
            )
        if args.command == "publish":
            return cmd_publish(conn, args.cert)
        parser.error(f"unknown command {args.command}")
        return 2
    except CommandError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
