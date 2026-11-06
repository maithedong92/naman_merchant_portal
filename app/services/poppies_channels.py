"""
Poppies ERP 14 Canonical Sales Channels & Backward-Compatible Alias Resolution.
Synchronized 100% with live Poppies ERP Production (`GET /api/v1/pos/pricing/channels`).
"""

from typing import Any, Dict, List, Optional
import inspect
import re


POPPIES_STANDARD_CHANNELS: List[Dict[str, Any]] = [
    {
        "channel_code": "STORE",
        "legacy_channel_code": "POS_OFFLINE",
        "pos_channel_code": "STORE",
        "arito_channel_code": "K21",
        "ma_kenh": "K21",
        "channel_group": "store",
        "earn_enabled": True,
        "channel_name": "Bán trực tiếp tại cửa hàng (POS)",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["POS_OFFLINE", "OFFLINE", "STORE_POS", "TAI_QUAY"],
    },
    {
        "channel_code": "SHOPEE",
        "legacy_channel_code": "SHOPEE",
        "pos_channel_code": "SHOPEE",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "marketplace",
        "earn_enabled": False,
        "channel_name": "Shopee Mall Nam An",
        "default_markup_percent": 20.0,
        "packaging_fee_vnd": 3000.0,
        "has_partner_pricing": True,
        "inherited_from": None,
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["SHOPEE_MALL"],
    },
    {
        "channel_code": "SHOPEEMART",
        "legacy_channel_code": "SHOPEEMART",
        "pos_channel_code": "SHOPEEMART",
        "arito_channel_code": "K12",
        "ma_kenh": "K12",
        "channel_group": "marketplace",
        "earn_enabled": False,
        "channel_name": "ShopeeMart",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": True,
        "inherited_from": None,
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["SHOPEE_MART", "SHOPEE_FRESH"],
    },
    {
        "channel_code": "TIKTOK",
        "legacy_channel_code": "TIKTOK_SHOP",
        "pos_channel_code": "TIKTOK",
        "arito_channel_code": "K17",
        "ma_kenh": "K17",
        "channel_group": "marketplace",
        "earn_enabled": False,
        "channel_name": "TikTok Shop",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": True,
        "inherited_from": None,
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["TIKTOK_SHOP", "TIKTOKSHOP", "TTS"],
    },
    {
        "channel_code": "LAZADA",
        "legacy_channel_code": "LAZADA",
        "pos_channel_code": "LAZADA",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "marketplace",
        "earn_enabled": False,
        "channel_name": "Lazada Mall Nam An",
        "default_markup_percent": 16.5,
        "packaging_fee_vnd": 3000.0,
        "has_partner_pricing": True,
        "inherited_from": None,
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["LAZADA_MALL"],
    },
    {
        "channel_code": "GRAB",
        "legacy_channel_code": "GRAB_MART",
        "pos_channel_code": "GRAB",
        "arito_channel_code": "K15",
        "ma_kenh": "K15",
        "channel_group": "delivery_app",
        "earn_enabled": False,
        "channel_name": "GrabMart Express",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": True,
        "inherited_from": None,
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["GRAB_MART", "GRABMART"],
    },
    {
        "channel_code": "SHOPEEFOOD",
        "legacy_channel_code": "SHOPEE_FOOD",
        "pos_channel_code": "SHOPEEFOOD",
        "arito_channel_code": "K22",
        "ma_kenh": "K22",
        "channel_group": "delivery_app",
        "earn_enabled": False,
        "channel_name": "ShopeeFood / Now",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": True,
        "inherited_from": None,
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["SHOPEE_FOOD", "NOW_VN"],
    },
    {
        "channel_code": "WEB",
        "legacy_channel_code": "HARAVAN_WEB",
        "pos_channel_code": "WEB",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Website Nam An Market",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": True,
        "inherited_from": None,
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["HARAVAN_WEB", "HARAVAN", "WEBSITE", "NAMANMARKET_VN"],
    },
    {
        "channel_code": "ZALO",
        "legacy_channel_code": "ZALO_OA",
        "pos_channel_code": "ZALO",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Zalo OA & Mini App",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["ZALO_OA", "ZALO_MINI_APP", "ZALO_APP"],
    },
    {
        "channel_code": "FACEBOOK",
        "legacy_channel_code": "FACEBOOK",
        "pos_channel_code": "FACEBOOK",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Facebook Fanpage / Messenger",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["FB", "FB_SHOP"],
    },
    {
        "channel_code": "INSTAGRAM",
        "legacy_channel_code": "INSTAGRAM",
        "pos_channel_code": "INSTAGRAM",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Instagram Direct",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["IG", "IG_SHOP"],
    },
    {
        "channel_code": "GOOGLESHOP",
        "legacy_channel_code": "GOOGLESHOP",
        "pos_channel_code": "GOOGLESHOP",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Google Shopping",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["GOOGLE_SHOP", "GG_SHOP", "GOOGLE"],
    },
    {
        "channel_code": "HARASOCIAL",
        "legacy_channel_code": "HARASOCIAL",
        "pos_channel_code": "HARASOCIAL",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "HaraSocial Omnichannel",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["HARA_SOCIAL", "HARASOCIAL_CHAT"],
    },
    {
        "channel_code": "PHONE",
        "legacy_channel_code": "PHONE",
        "pos_channel_code": "PHONE",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Hotline / Telesale",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_dynamic": True,
        "is_active": True,
        "aliases": ["HOTLINE", "TEL"],
    },
]

# Build lookup dictionaries from the single canonical list
CANONICAL_CHANNEL_META_MAP: Dict[str, Dict[str, Any]] = {
    item["channel_code"]: item for item in POPPIES_STANDARD_CHANNELS
}

CHANNEL_CODE_ALIASES: Dict[str, str] = {}
for _meta in POPPIES_STANDARD_CHANNELS:
    _canon = _meta["channel_code"]
    CHANNEL_CODE_ALIASES[_canon] = _canon
    for _alias in _meta.get("aliases", []):
        CHANNEL_CODE_ALIASES[_alias.upper()] = _canon


def normalize_channel_code(raw_code: Optional[str]) -> str:
    """Normalize any canonical or legacy alias channel code to the 14 Canonical codes."""
    cleaned = (raw_code or "").strip().upper()
    return CHANNEL_CODE_ALIASES.get(cleaned, cleaned)


def get_canonical_channel_meta(raw_code: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return canonical metadata dictionary for a channel or alias."""
    canon = normalize_channel_code(raw_code)
    return CANONICAL_CHANNEL_META_MAP.get(canon)


def get_channel_family_codes(raw_code: Optional[str]) -> List[str]:
    """
    Return the ordered family of codes `[canonical_code, *aliases]` for DB resolution.
    Canonical code is always index 0.
    """
    canon = normalize_channel_code(raw_code)
    meta = CANONICAL_CHANNEL_META_MAP.get(canon)
    if not meta:
        return [canon] if canon else []
    return [canon, *meta.get("aliases", [])]


def consolidate_channel_records(raw_channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Consolidate physical channel records into the 14 Canonical channels (Section 2.2):
    - Groups records by Canonical `channel_code` (supporting dynamic new channels from Poppies too).
    - Canonical record holds supreme authority: if the Canonical record has
      `is_active = False`, active legacy alias records NEVER reopen the channel.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    canonical_explicit_active: Dict[str, bool] = {}

    for meta in POPPIES_STANDARD_CHANNELS:
        grouped[meta["channel_code"]] = dict(meta)

    for row in raw_channels or []:
        raw_code = str(row.get("channel_code") or row.get("code") or "").strip().upper()
        if not raw_code:
            continue
        canon = normalize_channel_code(raw_code)
        is_canonical_row = (raw_code == canon)
        row_active = bool(row.get("is_active", True))

        target = grouped.get(canon)
        if target is None:
            target = dict(row)
            target["channel_code"] = canon
            grouped[canon] = target

        if is_canonical_row:
            canonical_explicit_active[canon] = row_active
            target["is_active"] = row_active
            if "has_partner_pricing" in row:
                target["has_partner_pricing"] = bool(row["has_partner_pricing"])
                target["inherited_from"] = (
                    None if target["has_partner_pricing"] else (row.get("inherited_from") or "STORE")
                )
            if row.get("channel_name"):
                target["channel_name"] = row["channel_name"]
            if row.get("arito_channel_code"):
                target["arito_channel_code"] = row["arito_channel_code"]
                target["ma_kenh"] = row["arito_channel_code"]
            for extra_key in (
                "legacy_channel_code",
                "channel_group",
                "earn_enabled",
                "default_markup_percent",
                "packaging_fee_vnd",
                "rounding_rule",
                "restrict_to_priced_items",
                "is_dynamic",
                "version",
                "updated_at",
            ):
                if extra_key in row and row[extra_key] is not None:
                    target[extra_key] = row[extra_key]
            leg_code = str(row.get("legacy_channel_code") or "").strip().upper()
            if leg_code and leg_code != canon:
                existing_aliases = list(target.get("aliases") or [])
                if leg_code not in existing_aliases:
                    existing_aliases.append(leg_code)
                    target["aliases"] = existing_aliases
                CHANNEL_CODE_ALIASES[leg_code] = canon
        else:
            if canon not in canonical_explicit_active:
                target["is_active"] = row_active

    for canon, canon_active in canonical_explicit_active.items():
        if not canon_active:
            grouped[canon]["is_active"] = False

    ordered_codes = [m["channel_code"] for m in POPPIES_STANDARD_CHANNELS]
    extra_codes = [k for k in grouped.keys() if k not in ordered_codes]
    return [grouped[c] for c in [*ordered_codes, *extra_codes]]


async def resolve_channel_from_db_result(res: Any, raw_code: Optional[str]) -> Any:
    """
    Pick the authoritative Channel instance from a SQLAlchemy execute() result
    when querying `Channel.code.in_(get_channel_family_codes(raw_code))`.
    Prioritizes Canonical `channel_code` over legacy aliases, and supports
    both real AsyncSession results (`scalars().all()`) and unit-test mocks
    (`scalar_one_or_none()`).
    """
    if inspect.isawaitable(res):
        res = await res
    if res is None:
        return None

    canon = normalize_channel_code(raw_code)
    scalars_fn = getattr(res, "scalars", None)
    if callable(scalars_fn):
        scalars_obj = scalars_fn()
        if inspect.isawaitable(scalars_obj):
            scalars_obj = await scalars_obj
        all_fn = getattr(scalars_obj, "all", None)
        if callable(all_fn):
            rows = all_fn()
            if inspect.isawaitable(rows):
                rows = await rows
            if isinstance(rows, list) and len(rows) > 0:
                for ch in rows:
                    if str(getattr(ch, "code", "")).upper() == canon:
                        return ch
                return rows[0]

    scalar_one_fn = getattr(res, "scalar_one_or_none", None)
    if callable(scalar_one_fn):
        val = scalar_one_fn()
        if inspect.isawaitable(val):
            val = await val
        if val is not None and (hasattr(val, "code") or hasattr(val, "id")):
            return val
    return None


def slugify_category_code(name: str) -> str:
    """Generate a clean uppercase alphanumeric category code."""
    if not name:
        return "GENERAL"
    cleaned = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE)
    slug = re.sub(r"[-\s]+", "_", cleaned).strip("_").upper()
    return slug[:50] or "GENERAL"
