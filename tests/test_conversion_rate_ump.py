"""Comprehensive tests for Poppies-UMP conversion_rate support across client, sync, webhooks, and channel adapters."""

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.product import Category, ChannelProductMapping, Product
from app.models.store import Store
from app.models.sync_log import WebhookAuditLog
from app.modules.grabmart.adapter import GrabMartChannelAdapter
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
from app.services.poppies_channel_push import (
    apply_channel_governance_to_stock_items,
    compute_channel_sale_price,
    normalize_conversion_rate,
)
from app.services.poppies_client import PoppiesClient
from app.services.poppies_service import PoppiesMasterDataSyncService


def _make_mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    return db


def _scalar_result(obj):
    m = MagicMock()
    m.scalar_one_or_none.return_value = obj
    return m


def _scalars_result(items):
    m = MagicMock()
    s = MagicMock()
    s.all.return_value = items
    m.scalars.return_value = s
    return m


def _rows_result(rows):
    m = MagicMock()
    m.all.return_value = rows
    return m


def _http_response(status_code: int, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    if status_code >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=httpx.Request("GET", "http://test"),
            response=httpx.Response(status_code),
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# 1. normalize_conversion_rate & compute_channel_sale_price
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.5, 0.5),
        (0.25, 0.25),
        (0.1, 0.1),
        (1.0, 1.0),
        ("0.5", 0.5),
        (0.33336, 0.3334),
        (0.12344, 0.1234),
        (None, 1.0),
        (True, 1.0),
        (False, 1.0),
        (0, 1.0),
        (0.0, 1.0),
        (-0.5, 1.0),
        ("0", 1.0),
        ("bad", 1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (float("-inf"), 1.0),
        (0.00001, 1.0),  # rounds to 0.0 -> falls back to 1.0
    ],
)
def test_normalize_conversion_rate(raw, expected):
    assert normalize_conversion_rate(raw) == expected


@pytest.mark.parametrize(
    "erp_price,rate,expected",
    [
        (100000, 0.5, 50000.0),
        (100000, 0.25, 25000.0),
        (100000, 0.1, 10000.0),
        (100000, 1.0, 100000.0),
        (100000, None, 100000.0),
        (100000, -0.5, 100000.0),
        (100000, True, 100000.0),
        (0, 0.5, 0.0),
        (-5000, 0.5, 0.0),
        (None, 0.5, 0.0),
        (True, 0.5, 0.0),
        ("bad", 0.5, 0.0),
        (float("nan"), 0.5, 0.0),
        (float("inf"), 0.5, 0.0),
    ],
)
def test_compute_channel_sale_price(erp_price, rate, expected):
    assert compute_channel_sale_price(erp_price, rate) == expected


# ---------------------------------------------------------------------------
# 2. PoppiesClient /api/v1/ump/* + 404 fallback + path traversal rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poppies_client_ump_conversion_methods_and_path_traversal():
    client = PoppiesClient()
    client.pos_pricing_base_url = "http://poppies.local/api/v1/pos/pricing"
    client.base_url = "http://poppies.local/api/v1/pos/master-data"
    client.pricing_base_url = "http://poppies.local/api/v1/pos/pricing"

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(
        return_value=_http_response(200, {"items": [{"sku": "SKU-1", "conversionRate": 0.5}]})
    )
    mock_http.post = AsyncMock(
        return_value=_http_response(200, {"items": [{"sku": "SKU-1", "conversionRate": 0.5}]})
    )
    client._get_client = MagicMock(return_value=mock_http)

    res_list = await client.get_conversion_rates(skus=["SKU-1"])
    assert res_list["items"][0]["conversionRate"] == 0.5
    assert "/api/v1/ump/conversion-rates" in mock_http.get.call_args_list[0].args[0]

    res_one = await client.get_conversion_rate_by_sku("SKU-1")
    assert res_one is not None
    assert "/api/v1/ump/conversion-rates/SKU-1" in mock_http.get.call_args_list[1].args[0]

    res_resolve = await client.resolve_pricing(skus=["SKU-1"], branch_code="10001", channel_code="GRAB")
    assert res_resolve["items"][0]["conversionRate"] == 0.5
    assert "/api/v1/ump/pricing/resolve" in mock_http.post.call_args_list[0].args[0]

    res_changes = await client.get_master_data_changes(since="2026-01-01T00:00:00Z")
    assert "items" in res_changes
    assert "/api/v1/ump/master-data/changes" in mock_http.get.call_args_list[2].args[0]

    # Path traversal / invalid SKU rejection
    for bad_sku in ("../secret", "..", "SKU/1", "SKU 1", ""):
        with pytest.raises(ValueError, match="Invalid sku"):
            await client.get_conversion_rate_by_sku(bad_sku)


@pytest.mark.asyncio
async def test_poppies_client_ump_404_fallback_to_pos():
    client = PoppiesClient()
    client.pos_pricing_base_url = "http://poppies.local/api/v1/pos/pricing"
    client.base_url = "http://poppies.local/api/v1/pos/master-data"
    client.pricing_base_url = "http://poppies.local/api/v1/pos/pricing"

    called_urls = []

    async def fake_get(url, **kwargs):
        called_urls.append(url)
        if "/api/v1/ump/" in url:
            return _http_response(404)
        return _http_response(200, {"success": True, "prices": {"SKU-1": 100000}})

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(side_effect=fake_get)
    client._get_client = MagicMock(return_value=mock_http)

    snap = await client.get_pricing_snapshot(branch_code="10001", channel_code="GRAB")
    assert snap["prices"]["SKU-1"] == 100000
    assert any("/api/v1/ump/pricing/snapshots" in u for u in called_urls)
    assert any("/api/v1/pos/pricing/snapshots" in u for u in called_urls)

    called_urls.clear()
    msnap = await client.get_snapshot(branch_code="10001")
    assert msnap["success"] is True
    assert any("/api/v1/ump/master-data/snapshot" in u for u in called_urls)
    assert any("/api/v1/pos/master-data/snapshot" in u for u in called_urls)


# ---------------------------------------------------------------------------
# 3. sync_master_data_from_poppies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_master_data_from_poppies_reads_conversion_rate_ignores_unit_conv():
    db = _make_mock_db()
    store = Store(id="store-1", code="10001", name="An Phú", is_active=True)
    cat = Category(id="cat-1", code="CAT1", name="Rau củ")
    existing_prod = Product(
        id="prod-2",
        sku="SKU-ECOM",
        name="Bơ",
        base_price=200000.0,
        conversion_rate=1.0,
        is_active=True,
    )

    db.execute = AsyncMock(
        side_effect=[
            _scalars_result([store]),          # Store
            _scalars_result([cat]),            # Category
            _scalars_result([existing_prod]),  # Product
            _scalars_result([]),               # StoreInventory
        ]
    )

    service = PoppiesMasterDataSyncService()
    service.client = MagicMock()
    service.client.get_snapshot = AsyncMock(
        return_value={
            "items": [
                {
                    "skuCode": "SKU-NEW",
                    "productName": "Nho mẫu đơn",
                    "categoryCode": "CAT1",
                    "categoryName": "Rau củ",
                    "pricing": {"baseSalePriceVat": 100000},
                    "unitConv": 12,
                    "conversionRate": 0.5,
                    "conversionRateSource": "UMP_RULE",
                    "inventories": {"10001": 10},
                },
                {
                    "skuCode": "SKU-ECOM",
                    "productName": "Bơ",
                    "categoryCode": "CAT1",
                    "categoryName": "Rau củ",
                    "pricing": {"baseSalePriceVat": 200000},
                    "unitConv": 24,
                    "ecommerce": {
                        "conversionRate": 0.25,
                        "conversionRateSource": "ECOM_OVERRIDE",
                    },
                    "inventories": {"10001": 5},
                },
            ],
            "total": 2,
        }
    )

    res = await service.sync_master_data_from_poppies(db, branch_code="10001", fetch_all=False)
    assert res["success"] is True
    assert res["products_created"] == 1
    assert res["products_updated"] == 1

    # Verify newly created product has base_price=100000 (not *12 or *0.5) and conversion_rate=0.5
    added_products = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], Product)]
    assert len(added_products) == 1
    new_prod = added_products[0]
    assert new_prod.sku == "SKU-NEW"
    assert float(new_prod.base_price) == 100000.0
    assert new_prod.conversion_rate == 0.5
    assert new_prod.conversion_rate_source == "UMP_RULE"

    # Verify existing product updated conversion_rate from ecommerce.conversionRate, kept base_price=200000
    assert float(existing_prod.base_price) == 200000.0
    assert existing_prod.conversion_rate == 0.25
    assert existing_prod.conversion_rate_source == "ECOM_OVERRIDE"


# ---------------------------------------------------------------------------
# 4. sync_channel_pricing_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_channel_pricing_snapshot_updates_conversion_rate_only():
    db = _make_mock_db()
    channel = Channel(
        id="ch-grab",
        code="GRAB",
        name="GrabMart",
        is_active=True,
        config={"has_partner_pricing": True},
    )
    prod = Product(
        id="prod-1",
        sku="SKU-1",
        name="Táo",
        base_price=100000.0,
        conversion_rate=1.0,
        conversion_rate_source="DEFAULT",
        is_active=True,
    )
    mapping = ChannelProductMapping(
        id="map-1",
        product_id="prod-1",
        channel_id="ch-grab",
        custom_price=110000.0,
        is_enabled=True,
    )

    # Sequence of db.execute calls:
    # 1. _get_or_create_channel -> Channel
    # 2. PoppiesPricingSyncMixin: Product.sku.in_(chunk)
    # 3. PoppiesPricingSyncMixin: ChannelProductMapping.channel_id == channel.id
    # 4. PoppiesService.sync_channel_pricing_snapshot: Product.sku.in_(chunk) for conv_map
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(channel),
            _scalars_result([prod]),
            _scalars_result([mapping]),
            _scalars_result([prod]),
        ]
    )

    service = PoppiesMasterDataSyncService()
    service.client = MagicMock()
    service.client.get_pricing_snapshot = AsyncMock(
        return_value={
            "version": "v2",
            "is_active": True,
            "has_partner_pricing": True,
            "prices": {"SKU-1": 110000.0},
            "conversion_rates": {
                "SKU-1": {"conversionRate": 0.5, "conversionRateSource": "UMP_SNAPSHOT"}
            },
        }
    )

    res = await service.sync_channel_pricing_snapshot(
        db=db,
        branch_code="10001",
        channel_code="GRAB",
        auto_propagate=False,
    )

    assert res["conversion_rates_updated"] == 1
    assert prod.conversion_rate == 0.5
    assert prod.conversion_rate_source == "UMP_SNAPSHOT"
    assert float(prod.base_price) == 100000.0
    assert float(mapping.custom_price) == 110000.0


# ---------------------------------------------------------------------------
# 5. process_price_change_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_conversion_rate_updated_single_and_multi_and_invalid_and_missing():
    db = _make_mock_db()
    prod1 = Product(id="p1", sku="SKU-1", name="P1", base_price=100000.0, conversion_rate=1.0, is_active=True)
    prod2 = Product(id="p2", sku="SKU-2", name="P2", base_price=80000.0, conversion_rate=0.5, is_active=True)

    # Single SKU update
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(None),       # WebhookAuditLog idempotency check
            _scalars_result([prod1]),   # Product query
        ]
    )
    service = PoppiesMasterDataSyncService()
    with patch("app.services.poppies_webhook_handler.propagate_skus_to_external_channel", new=AsyncMock()):
        res_single = await service.process_price_change_webhook(
            db,
            payload={
                "event_id": "evt-single-1",
                "event_type": "CONVERSION_RATE_UPDATED",
                "channel_code": "ALL",
                "affected_skus": ["SKU-1", "SKU-MISSING"],
                "conversion_rate": 0.5,
                "conversion_rate_source": "UMP",
            },
        )
    assert res_single["success"] is True
    assert prod1.conversion_rate == 0.5
    assert float(prod1.base_price) == 100000.0

    # Multi-SKU conversionRates map with invalid/negative/NaN/bool fallback to 1.0 & missing SKU skipped
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(None),              # WebhookAuditLog
            _scalars_result([prod1, prod2]),   # Product query
        ]
    )
    with patch("app.services.poppies_webhook_handler.propagate_skus_to_external_channel", new=AsyncMock()):
        res_multi = await service.process_price_change_webhook(
            db,
            payload={
                "event_id": "evt-multi-2",
                "event_type": "CONVERSION_RATE_UPDATED",
                "channel_code": "ALL",
                "conversionRates": {
                    "SKU-1": -0.25,
                    "SKU-2": True,
                    "SKU-UNKNOWN": 0.5,
                },
            },
        )
    assert res_multi["success"] is True
    assert prod1.conversion_rate == 1.0
    assert prod2.conversion_rate == 1.0

    # NaN fallback
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(None),
            _scalars_result([prod1]),
        ]
    )
    with patch("app.services.poppies_webhook_handler.propagate_skus_to_external_channel", new=AsyncMock()):
        await service.process_price_change_webhook(
            db,
            payload={
                "event_id": "evt-nan-3",
                "event_type": "CONVERSION_RATE_UPDATED",
                "affected_skus": ["SKU-1"],
                "conversion_rate": float("nan"),
            },
        )
    assert prod1.conversion_rate == 1.0


@pytest.mark.asyncio
async def test_webhook_conversion_rate_full_catalog_and_gt_200_skus_ump_pull():
    db = _make_mock_db()
    prod1 = Product(id="p1", sku="SKU-1", name="P1", base_price=100000.0, conversion_rate=1.0, is_active=True)

    # is_full_catalog=True without conversion_rates -> pulls from UMP get_conversion_rates
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(None),
            _scalars_result([prod1]),
        ]
    )
    service = PoppiesMasterDataSyncService()
    service.client = MagicMock()
    service.client.get_conversion_rates = AsyncMock(
        return_value={
            "items": [
                {"sku": "SKU-1", "conversionRate": 0.25, "conversionRateSource": "UMP_FULL"}
            ]
        }
    )
    with patch("app.services.poppies_webhook_handler.propagate_channel_menu_to_external_channel", new=AsyncMock()):
        res_full = await service.process_price_change_webhook(
            db,
            payload={
                "event_id": "evt-full-1",
                "event_type": "CONVERSION_RATE_UPDATED",
                "is_full_catalog": True,
            },
        )
    assert res_full["success"] is True
    service.client.get_conversion_rates.assert_awaited_once()
    assert prod1.conversion_rate == 0.25

    # >200 SKUs without conversion_rates -> pulls from UMP get_conversion_rates
    many_skus = [f"SKU-{i}" for i in range(1, 205)]
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(None),
            _scalars_result([prod1]),
        ]
    )
    service.client.get_conversion_rates = AsyncMock(
        return_value={"items": [{"sku": "SKU-1", "conversionRate": 0.1}]}
    )
    with patch("app.services.poppies_webhook_handler.propagate_channel_menu_to_external_channel", new=AsyncMock()):
        res_many = await service.process_price_change_webhook(
            db,
            payload={
                "event_id": "evt-many-2",
                "event_type": "CONVERSION_RATE_UPDATED",
                "affected_skus": many_skus,
            },
        )
    assert res_many["success"] is True
    service.client.get_conversion_rates.assert_awaited_once()
    assert prod1.conversion_rate == 0.1


@pytest.mark.asyncio
async def test_webhook_duplicate_event_id_idempotency():
    db = _make_mock_db()
    existing_log = WebhookAuditLog(
        id="log-1",
        channel_code="POPPIES_ERP",
        event_type="CONVERSION_RATE_UPDATED",
        idempotency_key="evt-dup-1",
    )
    db.execute = AsyncMock(return_value=_scalar_result(existing_log))

    service = PoppiesMasterDataSyncService()
    res = await service.process_price_change_webhook(
        db,
        payload={
            "event_id": "evt-dup-1",
            "event_type": "CONVERSION_RATE_UPDATED",
            "affected_skus": ["SKU-1"],
            "conversion_rate": 0.5,
        },
    )
    assert res["success"] is True
    assert res["duplicate"] is True
    assert res["processed_count"] == 0


# ---------------------------------------------------------------------------
# 6. Channel Adapters & apply_channel_governance_to_stock_items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grabmart_build_catalog_menu_applies_conversion_rate():
    db = _make_mock_db()
    cat = Category(id="c1", code="DEFAULT", name="Bách hóa")
    prod = Product(
        id="p1",
        sku="SKU-GRAB",
        name="Nho 500g",
        base_price=100000.0,
        conversion_rate=0.5,
        is_active=True,
        category=cat,
    )
    inv = StoreInventory(store_id="s1", product_id="p1", available_stock=10, is_out_of_stock=False)
    store = Store(id="s1", code="10001", name="An Phú", is_active=True)

    db.execute = AsyncMock(return_value=_rows_result([(prod, inv)]))

    adapter = GrabMartChannelAdapter()
    with patch(
        "app.modules.grabmart.adapter.load_channel_governance_for_products",
        new=AsyncMock(return_value=(True, False, {})),
    ):
        payload = await adapter.build_catalog_menu(
            store_id="s1",
            partner_store_id="grab-merchant-1",
            db=db,
        )

    assert len(payload["categories"]) == 1
    items = payload["categories"][0]["subCategories"][0]["items"]
    assert len(items) == 1
    assert items[0]["id"] == "SKU-GRAB"
    assert items[0]["price"] == 50000
    assert float(prod.base_price) == 100000.0


@pytest.mark.asyncio
async def test_shopeefood_build_catalog_menu_applies_conversion_rate():
    db = _make_mock_db()
    cat = Category(id="c1", code="CAT1", name="Trái cây", sequence=1)
    prod = Product(
        id="p1",
        sku="SKU-SF",
        name="Nho 500g",
        base_price=100000.0,
        conversion_rate=0.5,
        is_active=True,
        category=cat,
    )
    inv = StoreInventory(store_id="s1", product_id="p1", available_stock=10, is_out_of_stock=False)
    store = Store(id="s1", code="10001", name="An Phú", is_active=True)

    db.execute = AsyncMock(return_value=_rows_result([(prod, inv)]))

    adapter = ShopeeFoodChannelAdapter()
    with patch(
        "app.modules.shopeefood.adapter.load_channel_governance_for_products",
        new=AsyncMock(return_value=(True, False, {})),
    ):
        menu = await adapter.build_catalog_menu(
            store_id="s1",
            partner_store_id="sf-merchant-1",
            db=db,
        )

    items = menu["sections"][0]["categories"][0]["items"]
    assert len(items) == 1
    assert items[0]["id"] == "SKU-SF"
    assert items[0]["price"] == 50000.0
    assert float(prod.base_price) == 100000.0


@pytest.mark.asyncio
async def test_apply_channel_governance_to_stock_items_applies_conversion_rate():
    db = _make_mock_db()
    prod = Product(
        id="p1",
        sku="SKU-1",
        name="Nho 500g",
        base_price=100000.0,
        conversion_rate=0.5,
        is_active=True,
    )
    db.execute = AsyncMock(return_value=_scalars_result([prod]))

    with patch(
        "app.services.poppies_channel_push.load_channel_governance_for_products",
        new=AsyncMock(return_value=(True, False, {})),
    ):
        adjusted = await apply_channel_governance_to_stock_items(
            db=db,
            raw_channel_code="SHOPEEMART",
            stock_items=[{"sku": "SKU-1", "available_stock": 5, "is_out_of_stock": False}],
        )

    assert len(adjusted) == 1
    assert adjusted[0]["price"] == 50000.0
    assert float(prod.base_price) == 100000.0
