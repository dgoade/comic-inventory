"""Read local slab photos and store them in the public comic_images bucket."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from comic_inventory.cgc_client import CommandError
from comic_inventory.http_util import request

BUCKET = "comic_images"
MAX_IMAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class StoredPhoto:
    sort_order: int
    is_primary: bool
    storage_path: str
    url: str
    content_type: str
    file_size_bytes: int
    content_hash: str
    alt_text: str


def sniff_image(data: bytes, content_type: str) -> tuple[str, str]:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG"):
        return "image/png", "png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    lowered = content_type.casefold()
    if "png" in lowered:
        return "image/png", "png"
    if "webp" in lowered:
        return "image/webp", "webp"
    return "image/jpeg", "jpg"


def read_local_image(path: Path) -> tuple[bytes, str, str]:
    """Read a jpeg, png, or webp file. Does not contact CGC."""
    file = Path(path)
    if not file.is_file():
        raise CommandError(f"image file not found: {file}")
    data = file.read_bytes()
    if len(data) < 32:
        raise CommandError(f"image file is empty: {file}")
    if len(data) > MAX_IMAGE_BYTES:
        raise CommandError(f"image is larger than 20 MB: {file}")
    if data.startswith(b"\xff\xd8"):
        return data, "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG"):
        return data, "image/png", "png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data, "image/webp", "webp"
    raise CommandError(f"image must be jpeg, png, or webp: {file}")


def download_image(url: str, bearer: str | None = None) -> tuple[bytes, str, str]:
    headers = {"Accept": "image/*,*/*"}
    result = request("GET", url, headers=headers, timeout=90)
    if result.status in (401, 403) and bearer:
        headers = {**headers, "Authorization": f"Bearer {bearer}"}
        result = request("GET", url, headers=headers, timeout=90)
    if result.status != 200:
        raise CommandError(f"image download failed ({result.status})")
    if "text/html" in result.content_type.casefold():
        raise CommandError("image URL returned HTML instead of a photo")
    if not result.body or len(result.body) < 32:
        raise CommandError("image download was empty")
    if len(result.body) > MAX_IMAGE_BYTES:
        raise CommandError("image is larger than 20 MB")
    content_type, extension = sniff_image(result.body, result.content_type)
    return result.body, content_type, extension


def public_object_url(supabase_url: str, object_key: str) -> str:
    base = supabase_url.rstrip("/")
    return f"{base}/storage/v1/object/public/{BUCKET}/{object_key}"


def _storage_headers(service_key: str, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def ensure_bucket(supabase_url: str, service_key: str) -> None:
    base = supabase_url.rstrip("/")
    body = json.dumps({"id": BUCKET, "name": BUCKET, "public": True}).encode()
    result = request(
        "POST",
        f"{base}/storage/v1/bucket",
        headers=_storage_headers(service_key, "application/json"),
        data=body,
    )
    if result.status in (200, 201, 409):
        return
    # A duplicate-bucket response is sometimes 400 with a "already exists" message.
    text = result.body.decode(errors="replace").casefold()
    if result.status == 400 and "exist" in text:
        return
    raise CommandError(f"could not create storage bucket {BUCKET} ({result.status})")


def upload_object(
    supabase_url: str,
    service_key: str,
    object_key: str,
    data: bytes,
    content_type: str,
) -> str:
    base = supabase_url.rstrip("/")
    result = request(
        "POST",
        f"{base}/storage/v1/object/{BUCKET}/{object_key}",
        headers={
            **_storage_headers(service_key, content_type),
            "x-upsert": "true",
            "cache-control": "public, max-age=31536000",
        },
        data=data,
        timeout=90,
    )
    if result.status == 400 and b"bucket" in result.body.casefold() and b"not found" in result.body.casefold():
        ensure_bucket(supabase_url, service_key)
        result = request(
            "POST",
            f"{base}/storage/v1/object/{BUCKET}/{object_key}",
            headers={
                **_storage_headers(service_key, content_type),
                "x-upsert": "true",
            },
            data=data,
            timeout=90,
        )
    if result.status not in (200, 201):
        raise CommandError(f"storage upload failed ({result.status}) for {object_key}")
    return public_object_url(supabase_url, object_key)


def store_pair(
    env: dict[str, str],
    *,
    item_id: str,
    front: tuple[bytes, str],
    rear: tuple[bytes, str],
    alt_front: str,
    alt_rear: str,
) -> list[StoredPhoto]:
    supabase_url = (env.get("SUPABASE_URL") or "").strip()
    service_key = (env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not supabase_url or not service_key:
        raise CommandError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    ensure_bucket(supabase_url, service_key)
    photos: list[StoredPhoto] = []
    for sort_order, is_primary, blob, content_type, alt_text, leaf in (
        (0, True, front[0], front[1], alt_front, "01"),
        (1, False, rear[0], rear[1], alt_rear, "02"),
    ):
        extension = "jpg"
        if content_type == "image/png":
            extension = "png"
        elif content_type == "image/webp":
            extension = "webp"
        object_key = f"{item_id}/{leaf}.{extension}"
        url = upload_object(supabase_url, service_key, object_key, blob, content_type)
        photos.append(
            StoredPhoto(
                sort_order=sort_order,
                is_primary=is_primary,
                storage_path=f"{BUCKET}/{object_key}",
                url=url,
                content_type=content_type,
                file_size_bytes=len(blob),
                content_hash=hashlib.sha256(blob).hexdigest(),
                alt_text=alt_text,
            )
        )
    return photos


def save_image_rows(cur, item_id: str, photos: list[StoredPhoto]) -> None:
    """Insert or update the two CGC rows. Extra live CGC rows for this copy are soft-deleted."""
    # Clear primaries first. The one-primary index is checked before the
    # AFTER trigger that would otherwise unset the previous primary.
    cur.execute(
        """
        UPDATE inventory.inventory_images
        SET is_primary = false, updated_at = now()
        WHERE inventory_item_id = %s
          AND is_primary
          AND deleted_at IS NULL
        """,
        (item_id,),
    )
    cur.execute(
        """
        SELECT id::text AS id, sort_order
        FROM inventory.inventory_images
        WHERE inventory_item_id = %s
          AND source = 'cgc'
          AND deleted_at IS NULL
        ORDER BY sort_order, created_at
        """,
        (item_id,),
    )
    existing = list(cur.fetchall())
    used: set[str] = set()
    for photo in photos:
        match = next(
            (row for row in existing if row["sort_order"] == photo.sort_order and row["id"] not in used),
            None,
        )
        if match:
            used.add(match["id"])
            cur.execute(
                """
                UPDATE inventory.inventory_images
                SET storage_path = %s,
                    url = %s,
                    is_primary = %s,
                    sort_order = %s,
                    content_type = %s,
                    file_size_bytes = %s,
                    content_hash = %s,
                    alt_text = %s,
                    source = 'cgc',
                    deleted_at = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    photo.storage_path,
                    photo.url,
                    photo.is_primary,
                    photo.sort_order,
                    photo.content_type,
                    photo.file_size_bytes,
                    photo.content_hash,
                    photo.alt_text,
                    match["id"],
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO inventory.inventory_images (
                    inventory_item_id, storage_path, url, sort_order, is_primary,
                    alt_text, content_type, file_size_bytes, content_hash,
                    uploaded_by, source
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'inventory-cgc', 'cgc')
                """,
                (
                    item_id,
                    photo.storage_path,
                    photo.url,
                    photo.sort_order,
                    photo.is_primary,
                    photo.alt_text,
                    photo.content_type,
                    photo.file_size_bytes,
                    photo.content_hash,
                ),
            )
    for row in existing:
        if row["id"] not in used:
            cur.execute(
                """
                UPDATE inventory.inventory_images
                SET deleted_at = now(), is_primary = false, updated_at = now()
                WHERE id = %s
                """,
                (row["id"],),
            )


def listing_channel_data(
    *,
    aspects: dict[str, list[str]],
    variant: str,
    cert_number: str,
    display_grade: str,
    grade_category: str,
    page_quality: str,
    front_url: str,
    rear_url: str,
    offer_id: str | None,
    category_id: str,
) -> dict:
    return {
        "aspects": aspects,
        "variant": variant,
        "cgc": {
            "cert": cert_number,
            "display_grade": display_grade,
            "grade_category": grade_category,
            "page_quality": page_quality,
            "image_urls": {"front": front_url, "rear": rear_url},
        },
        "ebay": {
            "sku": cert_number,
            "offer_id": offer_id,
            "marketplace_id": "EBAY_US",
            "category_id": category_id,
        },
    }


