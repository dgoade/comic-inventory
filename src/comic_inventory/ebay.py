"""eBay Sell Inventory API: draft offer, then publish. Secrets stay in the environment."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from comic_inventory.cgc_client import CommandError
from comic_inventory.http_util import request

POLICY_ENV = (
    "EBAY_FULFILLMENT_POLICY_ID",
    "EBAY_PAYMENT_POLICY_ID",
    "EBAY_RETURN_POLICY_ID",
)
TOKEN_ENV = (
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
    "EBAY_REFRESH_TOKEN",
)
SELL_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory"


@dataclass(frozen=True)
class PublishedListing:
    offer_id: str
    listing_id: str
    url: str


def missing_names(env: dict[str, str], names: tuple[str, ...]) -> list[str]:
    return [name for name in names if not (env.get(name) or "").strip()]


def api_root(env: dict[str, str]) -> str:
    if (env.get("EBAY_ENV") or "production").strip().lower() == "sandbox":
        return "https://api.sandbox.ebay.com"
    return "https://api.ebay.com"


def listing_url(env: dict[str, str], listing_id: str) -> str:
    if (env.get("EBAY_ENV") or "production").strip().lower() == "sandbox":
        return f"https://www.sandbox.ebay.com/itm/{listing_id}"
    return f"https://www.ebay.com/itm/{listing_id}"


def package_weight_and_size(env: dict[str, str]) -> dict:
    def number(name: str, default: str) -> float:
        raw = (env.get(name) or default).strip() or default
        try:
            return float(raw)
        except ValueError as exc:
            raise CommandError(f"{name} is not a number") from exc

    return {
        "weight": {"value": number("EBAY_PACKAGE_WEIGHT_OZ", "24"), "unit": "OUNCE"},
        "dimensions": {
            "length": number("EBAY_PACKAGE_LENGTH_IN", "13"),
            "width": number("EBAY_PACKAGE_WIDTH_IN", "10"),
            "height": number("EBAY_PACKAGE_HEIGHT_IN", "3"),
            "unit": "INCH",
        },
    }


def inventory_item_body(
    *,
    title: str,
    description: str,
    aspects: dict[str, list[str]],
    image_urls: list[str],
    cert_number: str,
    grade_text: str,
    package: dict,
) -> dict:
    return {
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
        "condition": "USED_VERY_GOOD",
        "conditionDescription": f"CGC {grade_text} slab. Certification {cert_number}.",
        "product": {
            "title": title,
            "description": description,
            "aspects": aspects,
            "imageUrls": image_urls,
        },
        "packageWeightAndSize": package,
    }


def offer_body(
    *,
    sku: str,
    description: str,
    price: str,
    category_id: str,
    env: dict[str, str],
) -> dict:
    body = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": category_id,
        "listingDescription": description,
        "listingPolicies": {
            "fulfillmentPolicyId": env["EBAY_FULFILLMENT_POLICY_ID"].strip(),
            "paymentPolicyId": env["EBAY_PAYMENT_POLICY_ID"].strip(),
            "returnPolicyId": env["EBAY_RETURN_POLICY_ID"].strip(),
        },
        "pricingSummary": {"price": {"value": price, "currency": "USD"}},
        "merchantLocationKey": (env.get("EBAY_MERCHANT_LOCATION_KEY") or "").strip(),
    }
    if not body["merchantLocationKey"]:
        del body["merchantLocationKey"]
    return body


def ebay_error_text(payload) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors") or []
        parts = []
        for err in errors:
            if isinstance(err, dict):
                parts.append(f"{err.get('errorId')} {err.get('message')}".strip())
        if parts:
            return "; ".join(parts)
        if payload.get("error_description"):
            return str(payload["error_description"])
    return "eBay request failed"


def _sell_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }


def _json_body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def refresh_access_token(env: dict[str, str]) -> str:
    missing = missing_names(env, TOKEN_ENV)
    if missing:
        raise CommandError("missing " + ", ".join(missing))
    raw = f"{env['EBAY_CLIENT_ID'].strip()}:{env['EBAY_CLIENT_SECRET'].strip()}".encode()
    basic = base64.b64encode(raw).decode()
    form = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": env["EBAY_REFRESH_TOKEN"].strip(),
            "scope": SELL_SCOPE,
        }
    ).encode()
    result = request(
        "POST",
        f"{api_root(env)}/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=form,
    )
    payload = None
    if result.body:
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if result.status != 200 or not token:
        detail = ebay_error_text(payload) if payload is not None else "eBay token refresh failed"
        raise CommandError(f"eBay token refresh failed ({result.status}): {detail}")
    return token


def _expect(result, action: str) -> dict | None:
    payload = None
    if result.body:
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = {"errors": [{"message": result.body.decode(errors="replace")[:300]}]}
    if result.status not in (200, 201, 204):
        raise CommandError(f"eBay {action} failed ({result.status}): {ebay_error_text(payload)}")
    return payload if isinstance(payload, dict) else None


def publish_listing(
    env: dict[str, str],
    *,
    sku: str,
    title: str,
    description: str,
    aspects: dict[str, list[str]],
    image_urls: list[str],
    price: str,
    category_id: str,
    grade_text: str,
    offer_id: str | None,
) -> PublishedListing:
    missing = missing_names(env, POLICY_ENV)
    if missing:
        raise CommandError("missing " + ", ".join(missing))
    token = refresh_access_token(env)
    root = api_root(env)
    headers = _sell_headers(token)
    item = inventory_item_body(
        title=title,
        description=description,
        aspects=aspects,
        image_urls=image_urls,
        cert_number=sku,
        grade_text=grade_text,
        package=package_weight_and_size(env),
    )
    put = request(
        "PUT",
        f"{root}/sell/inventory/v1/inventory_item/{quote(sku)}",
        headers=headers,
        data=_json_body(item),
    )
    _expect(put, "inventory item")

    existing = _offer_for_sku(root, headers, sku)
    if existing and existing.get("listing_id"):
        return PublishedListing(
            offer_id=existing["offer_id"],
            listing_id=existing["listing_id"],
            url=listing_url(env, existing["listing_id"]),
        )

    offer = offer_body(
        sku=sku,
        description=description,
        price=price,
        category_id=category_id,
        env=env,
    )
    chosen = offer_id or (existing["offer_id"] if existing else None)
    if chosen:
        updated = request(
            "PUT",
            f"{root}/sell/inventory/v1/offer/{quote(chosen)}",
            headers=headers,
            data=_json_body(offer),
        )
        _expect(updated, "offer update")
    else:
        created = request(
            "POST",
            f"{root}/sell/inventory/v1/offer",
            headers=headers,
            data=_json_body(offer),
        )
        created_body = _expect(created, "offer create") or {}
        chosen = str(created_body.get("offerId") or "")
        if not chosen:
            raise CommandError("eBay created an offer without an offerId")

    published = request(
        "POST",
        f"{root}/sell/inventory/v1/offer/{quote(chosen)}/publish",
        headers=headers,
    )
    body = _expect(published, "publish") or {}
    listing_id = str(body.get("listingId") or "")
    if not listing_id:
        raise CommandError("eBay publish did not return a listing id")
    return PublishedListing(offer_id=chosen, listing_id=listing_id, url=listing_url(env, listing_id))


def _offer_for_sku(root: str, headers: dict[str, str], sku: str) -> dict | None:
    result = request(
        "GET",
        f"{root}/sell/inventory/v1/offer?sku={quote(sku)}",
        headers=headers,
    )
    if result.status == 404:
        return None
    payload = _expect(result, "offer lookup") or {}
    offers = payload.get("offers") or []
    if not offers:
        return None
    offer = offers[0]
    listing = offer.get("listing") or {}
    listing_id = listing.get("listingId") or ""
    status = str(offer.get("status") or "")
    if status.upper() != "PUBLISHED":
        listing_id = ""
    return {"offer_id": str(offer.get("offerId") or ""), "listing_id": str(listing_id)}
