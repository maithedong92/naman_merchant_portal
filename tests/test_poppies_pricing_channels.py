from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import httpx
import pytest

from app.api.deps import get_current_user, get_db, require_store_manager
from app.main import app
from app.models.channel import Channel
from app.models.product import Category, ChannelProductMapping, Product
from app.models.store import Store
from app.models.sync_log import WebhookAuditLog
from app.models.user import User, UserRole
from app.services.poppies_service import (
    POPPIES_STANDARD_CHANNELS,
    PoppiesUpstreamError,
    consolidate_channel_records,
    normalize_channel_code,
    poppies_service,
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_store_manager():
    return User(
        id="mgr-uuid-1",
        username="store_mgr",
        email="manager@namanmarket.com",
        role=UserRole.STORE_MANAGER.value,
        is_active=True,
    )


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


# ---------------------------------------------------------------------------
# 1. Canonical 14 Channels & Alias Consolidation (Live Poppies Parity)
# ---------------------------------------------------------------------------

def test_normalize_14_canonical_channels_and_shopeemart_separation():
    from app.services.channel_registry import channel_registry
    from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter

    if channel_registry.get("SHOPEEMART") is None:
        channel_registry.register(ShopeeMartChannelAdapter())

    assert len(POPPIES_STANDARD_CHANNELS) == 14
    assert normalize_channel_code("POS_OFFLINE") == "STORE"
    assert normalize_channel_code("STORE") == "STORE"
    assert normalize_channel_code("SHOPEE") == "SHOPEE"
    assert normalize_channel_code("SHOPEE_MALL") == "SHOPEE"
    # SHOPEEMART is its own Canonical channel, strictly separate from SHOPEE (Shopee Mall)!
    assert normalize_channel_code("SHOPEEMART") == "SHOPEEMART"
    assert normalize_channel_code("SHOPEE_MART") == "SHOPEEMART"
    assert channel_registry.get("SHOPEEMART") is not None
    assert channel_registry.get("SHOPEE") is None

    assert normalize_channel_code("GRAB_MART") == "GRAB"
    assert normalize_channel_code("grabmart") == "GRAB"
    assert normalize_channel_code("SHOPEE_FOOD") == "SHOPEEFOOD"
    assert normalize_channel_code("NOW_VN") == "SHOPEEFOOD"
    assert normalize_channel_code("HARAVAN_WEB") == "WEB"
    assert normalize_channel_code("web") == "WEB"
    assert normalize_channel_code("TIKTOK_SHOP") == "TIKTOK"
    assert normalize_channel_code("GOOGLE_SHOP") == "GOOGLESHOP"
    assert normalize_channel_code("IG") == "INSTAGRAM"
    assert normalize_channel_code("HARA_SOCIAL") == "HARASOCIAL"
    assert normalize_channel_code("HOTLINE") == "PHONE"


def test_closed_canonical_channel_not_reopened_by_active_alias():
    """Section 2.2: Closed Canonical record holds supreme authority over active legacy aliases."""
    raw_records = [
        {"channel_code": "GRAB_MART", "is_active": True},
        {"channel_code": "GRAB", "is_active": False, "has_partner_pricing": True},
        {"channel_code": "HARAVAN_WEB", "is_active": False},
        {"channel_code": "WEB", "is_active": True, "has_partner_pricing": False},
    ]
    consolidated = consolidate_channel_records(raw_records)
    by_code = {c["channel_code"]: c for c in consolidated}

    assert len(consolidated) == 14
    assert "SHOPEEMART" in by_code
    assert by_code["GRAB"]["is_active"] is False
    assert by_code["WEB"]["is_active"] is True
    assert by_code["WEB"]["inherited_from"] == "STORE"


@pytest.mark.asyncio
async def test_overview_enforces_canonical_authority_and_store_inheritance():
    """
    Section 2.2 & 3.1:
    - Closed canonical `GRAB` blocks SKU (`allowed_in_context = False`) even if `GRAB_MART` is active.
    - Inherited `WEB` (`has_partner_pricing = False`) uses `STORE` base_price (150,000) instead of stale custom_price.
    """
    mock_db = _make_mock_db()

    grab_canon = Channel(id="ch-grab", code="GRAB", name="GrabMart", is_active=False, config={"has_partner_pricing": True})
    grab_alias = Channel(id="ch-grab-old", code="GRAB_MART", name="Grab Mart Old", is_active=True)
    web_canon = Channel(id="ch-web", code="WEB", name="Website", is_active=True, config={"has_partner_pricing": False})
    shopee_canon = Channel(id="ch-shopee", code="SHOPEE", name="Shopee", is_active=True, config={"has_partner_pricing": True})

    prod = Product(id="p-1", sku="290031798000", name="Việt quất Úc", base_price=150000.0, is_active=True)
    prod.channel_mappings = [
        ChannelProductMapping(id="m1", product_id="p-1", channel_id="ch-grab", channel=grab_canon, custom_price=195000.0, is_enabled=True),
        ChannelProductMapping(id="m2", product_id="p-1", channel_id="ch-web", channel=web_canon, custom_price=999999.0, is_enabled=True),
        ChannelProductMapping(id="m3", product_id="p-1", channel_id="ch-shopee", channel=shopee_canon, custom_price=180000.0, is_enabled=True),
    ]

    mock_db.execute.side_effect = [
        _scalar_result(prod),
        _scalars_result([grab_canon, grab_alias, web_canon, shopee_canon]),
    ]

    overview = await poppies_service.get_sku_channel_publish_overview(db=mock_db, sku="290031798000")
    by_code = {c["channel_code"]: c for c in overview["channels"]}

    assert len(overview["channels"]) == 14
    assert "SHOPEEMART" in by_code
    # GRAB canonical is closed -> blocked regardless of mapping/alias
    assert by_code["GRAB"]["is_channel_active"] is False
    assert by_code["GRAB"]["allowed_in_context"] is False
    assert by_code["GRAB"]["is_published"] is False
    assert by_code["GRAB"]["effective_price"] == 0.0

    # WEB has_partner_pricing=False -> inherits STORE base_price (150,000), ignores 999,999
    assert by_code["WEB"]["has_partner_pricing"] is False
    assert by_code["WEB"]["inherited_from"] == "STORE"
    assert by_code["WEB"]["custom_price"] is None
    assert by_code["WEB"]["effective_price"] == 150000.0

    # SHOPEE has_partner_pricing=True -> uses partner custom_price (180,000)
    assert by_code["SHOPEE"]["has_partner_pricing"] is True
    assert by_code["SHOPEE"]["effective_price"] == 180000.0


# ---------------------------------------------------------------------------
# 2. Pricing Snapshot Sync & Strict Fail-Closed Semantics (Sections 3 & 4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_snapshot_store_inheritance_and_exclusions_parity():
    """
    Section 3.1 & 3.2:
    - Inherited channel (`ZALO`, `has_partner_pricing = False`) clears stale custom prices,
      enforces `exclusions` (`SKU_BLOCKED`), and lifts expired snapshot blocks (`SKU_UNBLOCKED`).
    """
    mock_db = _make_mock_db()
    zalo_ch = Channel(id="ch-zalo", code="ZALO", name="Zalo OA", is_active=True, config={})
    p_blocked = Product(id="p-blk", sku="SKU_BLOCKED", name="Rượu Vang", base_price=500000.0, is_active=True)
    p_unblocked = Product(id="p-unblk", sku="SKU_UNBLOCKED", name="Nước Táo", base_price=80000.0, is_active=True)
    p_manual_off = Product(id="p-man", sku="SKU_MANUAL_OFF", name="Bánh Tươi", base_price=45000.0, is_active=True)

    m_expired_block = ChannelProductMapping(
        id="m-unblk",
        product_id="p-unblk",
        channel_id="ch-zalo",
        custom_price=88000.0,
        is_enabled=False,
        channel_sync_payload={"blocked_by_snapshot": True, "snapshot_version": "v1"},
    )
    m_manual = ChannelProductMapping(
        id="m-man",
        product_id="p-man",
        channel_id="ch-zalo",
        custom_price=None,
        is_enabled=False,
        channel_sync_payload={"manual_override": True, "blocked_by_snapshot": False},
    )

    mock_db.execute.side_effect = [
        _scalar_result(zalo_ch),
        _scalars_result([p_blocked, p_unblocked, p_manual_off]),
        _scalars_result([m_expired_block, m_manual]),
    ]

    snapshot_payload = {
        "success": True,
        "version": "v2",
        "is_active": True,
        "has_partner_pricing": False,
        "inherited_from": "STORE",
        "is_delta": False,
        "prices": {},
        "exclusions": ["SKU_BLOCKED"],
    }

    with patch.object(
        poppies_service.client, "get_pricing_snapshot", new_callable=AsyncMock
    ) as mock_snap:
        mock_snap.return_value = snapshot_payload
        res = await poppies_service.sync_channel_pricing_snapshot(
            db=mock_db, branch_code="10001", channel_code="ZALO_OA"
        )

    assert res["channel_code"] == "ZALO"
    assert res["has_partner_pricing"] is False
    assert res["inherited_from"] == "STORE"
    assert res["blocked_skus_count"] == 1
    assert res["unblocked_skus_count"] == 1
    # Stale custom price cleared + expired snapshot block lifted
    assert m_expired_block.custom_price is None
    assert m_expired_block.is_enabled is True
    # Manual override NOT accidentally reopened
    assert m_manual.is_enabled is False
    # New blocked mapping added for SKU_BLOCKED
    added_objs = [call.args[0] for call in mock_db.add.call_args_list]
    assert any(
        isinstance(o, ChannelProductMapping) and o.product_id == "p-blk" and o.is_enabled is False
        for o in added_objs
    )


@pytest.mark.asyncio
async def test_sync_snapshot_fail_closed_retains_blocked_skus_on_5xx_and_timeout():
    """
    Section 4.3 (U1 Fail-Closed Probe):
    When Poppies returns HTTP 503 or Timeout, `PoppiesUpstreamError` is raised,
    `db.rollback()` executes, and existing `blocked_skus` / cache remain 100% untouched.
    """
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_get.return_value = mock_resp

        with pytest.raises(PoppiesUpstreamError) as exc_info:
            await poppies_service.client.get_pricing_snapshot(
                branch_code="10001", channel_code="GRAB"
            )
        assert exc_info.value.status_code == 503
        assert exc_info.value.details["fail_closed"] is True
        assert exc_info.value.details["cache_retained"] is True

    # Verify service rolls back and never mutates existing mappings on timeout
    mock_db = _make_mock_db()
    existing_blocked = ChannelProductMapping(
        id="m-keep", product_id="p-1", channel_id="ch-grab", custom_price=190000.0, is_enabled=False
    )

    with patch("httpx.AsyncClient.get", side_effect=httpx.ReadTimeout("Upstream timed out")):
        with pytest.raises(PoppiesUpstreamError) as exc_timeout:
            await poppies_service.sync_channel_pricing_snapshot(
                db=mock_db, branch_code="10001", channel_code="GRAB"
            )
        assert exc_timeout.value.status_code == 504

    mock_db.rollback.assert_called_once()
    assert existing_blocked.is_enabled is False
    assert existing_blocked.custom_price == 190000.0


def test_api_sync_snapshot_returns_503_fail_closed_not_200(client, mock_store_manager):
    """API endpoint MUST return HTTP 503 with fail_closed=True on Poppies 5xx failure."""
    app.dependency_overrides[require_store_manager] = lambda: mock_store_manager
    app.dependency_overrides[get_db] = lambda: _make_mock_db()

    with patch("app.main.error_service.log_error", new_callable=AsyncMock), patch.object(
        poppies_service,
        "sync_channel_pricing_snapshot",
        new_callable=AsyncMock,
        side_effect=PoppiesUpstreamError("Poppies DB Down", status_code=503),
    ):
        resp = client.post("/api/v1/pricing/snapshots/sync?branch_code=10001&channel_code=GRAB")
        assert resp.status_code == 503
        body = resp.json()
        assert body["success"] is False
        assert body["error_code"] == "POPPIES_UPSTREAM_UNAVAILABLE"
        assert body["data"]["fail_closed"] is True
        assert body["data"]["cache_retained"] is True

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_snapshot_list_format_and_channel_policy_webhook():
    """Verify list-format snapshot parity (Section 4.2) and CHANNEL_PRICING_STATUS_CHANGED (Section 6.1)."""
    mock_db = _make_mock_db()
    grab_ch = Channel(id="ch-grab", code="GRAB", name="GrabMart", is_active=True, config={})
    prod_a = Product(id="pa", sku="SKU_A", name="Sữa Tươi", base_price=40000.0, is_active=True)
    prod_b = Product(id="pb", sku="SKU_B", name="Bia Nhập", base_price=90000.0, is_active=True)

    mock_db.execute.side_effect = [
        _scalar_result(grab_ch),
        _scalars_result([prod_a, prod_b]),
        _scalars_result([]),
    ]

    list_snap = {
        "success": True,
        "version": "v10",
        "is_active": True,
        "has_partner_pricing": True,
        "inherited_from": None,
        "items": [{"sku": "SKU_A", "effective_price": 52000.0, "allowed_in_context": True}],
        "blocked_skus": ["SKU_B"],
    }

    with patch.object(poppies_service.client, "get_pricing_snapshot", new_callable=AsyncMock, return_value=list_snap):
        res = await poppies_service.sync_channel_pricing_snapshot(
            db=mock_db, branch_code="10004", channel_code="GRAB", fmt="list"
        )

    assert res["has_partner_pricing"] is True
    assert res["prices_updated"] == 1
    assert res["blocked_skus_count"] == 1

    # Now test CHANNEL_PRICING_STATUS_CHANGED closing channel & disabling partner pricing
    mock_db.execute.side_effect = [_scalar_result(None), _scalar_result(grab_ch)]
    wh_res = await poppies_service.process_price_change_webhook(
        db=mock_db,
        payload={
            "event_id": "evt_policy_1",
            "event_type": "CHANNEL_PRICING_STATUS_CHANGED",
            "channel_code": "GRAB_MART",
            "is_active": False,
            "has_partner_pricing": False,
            "timestamp": "2026-11-06T07:00:00Z",
        },
    )
    assert wh_res["processed_count"] == 1
    assert grab_ch.is_active is False
    assert grab_ch.config["has_partner_pricing"] is False
    assert grab_ch.config["inherited_from"] == "STORE"


# ---------------------------------------------------------------------------
# 3. Toggle Publish & Outbox Webhook Guarantees (Sections 4.3, 5.2, 6.1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_toggle_sku_channel_publish_canonical():
    """Verify toggle normalizes GRABMART -> Canonical GRAB and syncs to Poppies."""
    mock_db = _make_mock_db()
    mock_prod = Product(id="prod-123", sku="290031798000", name="Việt quất Úc", base_price=150000.0, is_active=True)
    mock_channel = Channel(id="chan-grab", code="GRAB", name="GrabMart", is_active=True)

    mock_db.execute.side_effect = [
        _scalar_result(mock_prod),
        _scalar_result(mock_channel),
        _scalar_result(None),
    ]

    with patch.object(
        poppies_service.client, "toggle_channel_publish_status", new_callable=AsyncMock
    ) as mock_toggle:
        mock_toggle.return_value = {"success": True, "scopeCode": "GRAB", "isPublished": False}
        result = await poppies_service.toggle_sku_channel_publish(
            db=mock_db, sku="290031798000", channel_code="GRABMART", is_published=False
        )

    assert result["poppies_channel_code"] == "GRAB"
    assert result["is_published"] is False
    mock_toggle.assert_called_once_with(
        sku="290031798000", scope_code="GRAB", is_published=False, scope_type="CHANNEL"
    )


@pytest.mark.asyncio
async def test_webhook_pure_price_changed_preserves_publish_and_blocks_stale_or_duplicate():
    """
    Verify:
    1. Pure `price_changed` without `is_published` updates `custom_price` while keeping `is_enabled = True`.
    2. Out-of-order older `timestamp` is skipped safely.
    3. Duplicate `event_id` is rejected idempotently via `WebhookAuditLog`.
    """
    mock_db = _make_mock_db()
    ch_grab = Channel(id="ch-grab", code="GRAB", name="GrabMart", is_active=True)
    prod = Product(id="p-1", sku="290031798000", name="Việt quất Úc", base_price=150000.0, is_active=True)
    mapping = ChannelProductMapping(
        id="m-1",
        product_id="p-1",
        channel_id="ch-grab",
        custom_price=175000.0,
        is_enabled=True,
        channel_sync_payload={"source_event_timestamp": "2026-11-06T05:00:00+00:00"},
    )

    # Case A: Pure price_changed with newer timestamp (06:00Z)
    mock_db.execute.side_effect = [
        _scalar_result(None),       # WebhookAuditLog lookup -> not duplicate
        _scalar_result(ch_grab),    # Channel lookup
        _scalar_result(prod),       # Product lookup
        _scalar_result(mapping),    # Mapping lookup
    ]
    res_new = await poppies_service.process_price_change_webhook(
        db=mock_db,
        payload={
            "event_id": "evt_price_1",
            "event_type": "price_changed",
            "channel_code": "GRAB_MART",
            "affected_skus": ["290031798000"],
            "prices": {"290031798000": 189000.0},
            "timestamp": "2026-11-06T06:00:00Z",
        },
    )
    assert res_new["processed_count"] == 1
    assert mapping.is_enabled is True  # MUST remain True!
    assert mapping.custom_price == 189000.0

    # Case B: Out-of-order stale event (04:00Z < 06:00Z) -> skipped!
    mock_db.execute.side_effect = [
        _scalar_result(None),
        _scalar_result(ch_grab),
        _scalar_result(prod),
        _scalar_result(mapping),
    ]
    res_stale = await poppies_service.process_price_change_webhook(
        db=mock_db,
        payload={
            "event_id": "evt_stale_0",
            "event_type": "CHANNEL_PUBLISH_STATUS_CHANGED",
            "channel_code": "GRAB",
            "affected_skus": ["290031798000"],
            "is_published": False,
            "timestamp": "2026-11-06T04:00:00Z",
        },
    )
    assert res_stale["processed_count"] == 0
    assert res_stale["skipped_out_of_order"] == 1
    assert mapping.is_enabled is True  # Not overwritten by older event!

    # Case C: Duplicate event_id -> idempotent skip
    mock_db.execute.side_effect = [_scalar_result(WebhookAuditLog(id="aud-1", idempotency_key="evt_price_1"))]
    res_dup = await poppies_service.process_price_change_webhook(
        db=mock_db,
        payload={"event_id": "evt_price_1", "channel_code": "GRAB"},
    )
    assert res_dup["duplicate"] is True
    assert res_dup["processed_count"] == 0


def test_api_endpoints_13_channels_and_webhook(client, mock_store_manager):
    app.dependency_overrides[get_current_user] = lambda: mock_store_manager
    app.dependency_overrides[require_store_manager] = lambda: mock_store_manager
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    with patch.object(
        poppies_service.client,
        "get_pricing_channels",
        new_callable=AsyncMock,
        return_value={"success": True, "total": 14, "channels": POPPIES_STANDARD_CHANNELS},
    ):
        resp_ch = client.get("/api/v1/pricing/channels")
        assert resp_ch.status_code == 200
        codes = [c["channel_code"] for c in resp_ch.json()["data"]["channels"]]
        assert len(codes) == 14
        for expected in ("STORE", "GRAB", "SHOPEEFOOD", "SHOPEE", "SHOPEEMART", "TIKTOK", "LAZADA", "WEB", "ZALO", "GOOGLESHOP"):
            assert expected in codes

    # Verify /by-sku/{sku}/channels/{channel_code}/toggle accepts body without channel_code
    with patch.object(
        poppies_service,
        "toggle_sku_channel_publish",
        new_callable=AsyncMock,
        return_value={
            "sku": "290031798000",
            "product_name": "Việt quất Úc",
            "channel_code": "GRAB",
            "poppies_channel_code": "GRAB",
            "is_published": False,
            "synced_to_poppies": True,
            "message": "Đã TẮT bán",
            "poppies_response": {"success": True},
        },
    ):
        resp_toggle = client.post(
            "/api/v1/products/by-sku/290031798000/channels/GRAB/toggle",
            json={"is_published": False},
        )
        assert resp_toggle.status_code == 200
        assert resp_toggle.json()["data"]["poppies_channel_code"] == "GRAB"

    # Verify webhook passes X-Event-Id header cleanly
    with patch.object(
        poppies_service,
        "process_price_change_webhook",
        new_callable=AsyncMock,
        return_value={"success": True, "event_id": "evt_hdr_99", "processed_count": 1},
    ) as mock_wh:
        resp_wh = client.post(
            "/api/v1/pricing/webhook/price-changed",
            json={"event_type": "price_changed", "channel_code": "GRAB", "affected_skus": ["290031798000"]},
            headers={"X-Poppies-Webhook-Key": "naman_pos_api_secret_key_2026", "X-Event-Id": "evt_hdr_99"},
        )
        assert resp_wh.status_code == 200
        assert mock_wh.call_args.kwargs["event_id_header"] == "evt_hdr_99"

    # Verify unknown SKU cleanly returns HTTP 404 NotFoundError
    mock_nf_db = _make_mock_db()
    mock_nf_db.execute.return_value = _scalar_result(None)
    app.dependency_overrides[get_db] = lambda: mock_nf_db
    resp_404 = client.get("/api/v1/pricing/products/UNKNOWN_SKU_999/channels")
    assert resp_404.status_code == 404
    assert resp_404.json()["error_code"] == "RESOURCE_NOT_FOUND"

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 4. Real-Time Outbound Propagation to External Adapters (GRAB / SHOPEEFOOD / SHOPEE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webhook_auto_propagates_to_grabmart_and_shopeefood_adapters():
    """
    When Poppies pushes CHANNEL_PUBLISH_STATUS_CHANGED (OFF or ON), NAM-UMP commits DB
    AND automatically calls the corresponding external adapter's `sync_inventory`.
    """
    from app.models.inventory import StoreInventory
    from app.models.store import StoreChannelMapping
    from app.schemas.sync import SyncResult
    from app.services.channel_registry import channel_registry

    mock_db = _make_mock_db()
    ch_grab = Channel(id="ch-grab", code="GRAB", name="GrabMart", is_active=True, config={"has_partner_pricing": True})
    prod = Product(id="p-1", sku="290031798000", name="Việt quất Úc", base_price=150000.0, is_active=True)
    mapping_off = ChannelProductMapping(
        id="m-1", product_id="p-1", channel_id="ch-grab", custom_price=195000.0, is_enabled=True
    )
    scm = StoreChannelMapping(id="scm-1", store_id="st-10001", channel_id="ch-grab", partner_store_id="GM-10001", is_active=True)
    inv = StoreInventory(id="inv-1", store_id="st-10001", product_id="p-1", sku="290031798000", available_stock=20.0, is_out_of_stock=False)

    # Sequence of queries:
    # 1. WebhookAuditLog -> None
    # 2. _get_or_create_channel -> ch_grab
    # 3. Product lookup -> prod
    # 4. ChannelProductMapping lookup -> mapping_off
    # --- Inside propagate_skus_to_external_channel ---
    # 5. StoreChannelMapping -> [scm]
    # 6. Product.sku.in_ -> [prod]
    # 7. Channel.code.in_ -> [ch_grab]
    # 8. ChannelProductMapping -> [mapping_off] (now is_enabled=False!)
    # 9. StoreInventory -> [inv]
    mock_db.execute.side_effect = [
        _scalar_result(None),
        _scalar_result(ch_grab),
        _scalar_result(prod),
        _scalar_result(mapping_off),
        _scalars_result([scm]),
        _scalars_result([prod]),
        _scalars_result([ch_grab]),
        _scalars_result([mapping_off]),
        _scalars_result([inv]),
    ]

    from app.modules.grabmart.adapter import GrabMartChannelAdapter

    grab_adapter = channel_registry.get("GRAB")
    if grab_adapter is None:
        channel_registry.register(GrabMartChannelAdapter())
        grab_adapter = channel_registry.get("GRAB")
    assert grab_adapter is not None

    with patch.object(
        grab_adapter,
        "sync_inventory",
        new_callable=AsyncMock,
        return_value=SyncResult(channel_code="GRABMART", store_id="st-10001", sync_type="STOCK", success=True, total_synced=1, message="OK"),
    ) as mock_grab_sync:
        res = await poppies_service.process_price_change_webhook(
            db=mock_db,
            payload={
                "event_id": "evt_auto_grab_off",
                "event_type": "CHANNEL_PUBLISH_STATUS_CHANGED",
                "channel_code": "GRAB",
                "affected_skus": ["290031798000"],
                "is_published": False,
                "timestamp": "2026-11-06T08:00:00Z",
            },
        )

    assert mapping_off.is_enabled is False
    assert len(res["external_channel_sync"]) == 1
    assert res["external_channel_sync"][0]["success"] is True
    mock_grab_sync.assert_called_once()
    pushed_items = mock_grab_sync.call_args.kwargs["stock_items"]
    assert pushed_items[0]["sku"] == "290031798000"
    assert pushed_items[0]["is_out_of_stock"] is True
    assert pushed_items[0]["available_stock"] == 0.0
    assert pushed_items[0]["price"] == 195000.0


@pytest.mark.asyncio
async def test_shopeefood_and_grabmart_adapters_honor_is_enabled_and_custom_price():
    """
    Verify ShopeeFoodChannelAdapter and GrabMartChannelAdapter both apply:
    - `ChannelProductMapping.custom_price` when `has_partner_pricing=True`
    - `UNAVAILABLE` / `status=2` when `ChannelProductMapping.is_enabled=False` even if physical stock > 0!
    """
    from app.models.inventory import StoreInventory
    from app.models.product import Category
    from app.modules.grabmart.adapter import GrabMartChannelAdapter
    from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
    from app.modules.shopeefood.service import shopeefood_client
    from app.services.channel_service import channel_service

    cat = Category(id="cat-f", code="FRUITS_VEGGIES", name="Trái cây", sequence=1)
    p_open = Product(id="p-open", sku="SKU-OPEN", name="Táo Envy", base_price=150000.0, category=cat, is_active=True)
    p_off = Product(id="p-off", sku="SKU-OFF", name="Nho Mẫu Đơn", base_price=300000.0, category=cat, is_active=True)
    inv_open = StoreInventory(store_id="s1", product_id="p-open", available_stock=12.0, is_out_of_stock=False)
    inv_off = StoreInventory(store_id="s1", product_id="p-off", available_stock=15.0, is_out_of_stock=False)

    ch_sf = Channel(id="ch-sf", code="SHOPEEFOOD", name="ShopeeFood", is_active=True, config={"has_partner_pricing": True})
    m_sf_open = ChannelProductMapping(id="msf1", product_id="p-open", channel_id="ch-sf", custom_price=185000.0, is_enabled=True)
    m_sf_off = ChannelProductMapping(id="msf2", product_id="p-off", channel_id="ch-sf", custom_price=360000.0, is_enabled=False)

    mock_db = _make_mock_db()
    rows_res = MagicMock()
    rows_res.all.return_value = [(p_open, inv_open), (p_off, inv_off)]
    mock_db.execute.side_effect = [
        rows_res,
        _scalars_result([ch_sf]),
        _scalars_result([m_sf_open, m_sf_off]),
    ]

    sf_adapter = ShopeeFoodChannelAdapter()
    sf_menu = await sf_adapter.build_catalog_menu(store_id="s1", partner_store_id="10001", db=mock_db)
    sf_items = {it["id"]: it for it in sf_menu["sections"][0]["categories"][0]["items"]}

    assert sf_items["SKU-OPEN"]["availableStatus"] == "AVAILABLE"
    assert sf_items["SKU-OPEN"]["price"] == 185000.0  # Uses ShopeeFood custom_price, not 150,000!
    assert sf_items["SKU-OFF"]["availableStatus"] == "UNAVAILABLE"  # Disabled on channel despite stock=15!
    assert sf_items["SKU-OFF"]["price"] == 360000.0

    # Also verify routine stock sync CANNOT accidentally reopen SKU-OFF on ShopeeFood
    mock_db_sync = _make_mock_db()
    mock_db_sync.execute.side_effect = [
        _scalars_result([p_off]),
        _scalars_result([ch_sf]),
        _scalars_result([m_sf_off]),
    ]
    with patch.object(channel_service, "is_feature_enabled", new_callable=AsyncMock, return_value=True), \
         patch.object(shopeefood_client, "update_dish_status", new_callable=AsyncMock, return_value={"result": "ok"}) as mock_sf_upd:
        await sf_adapter.sync_inventory(
            store_id="s1",
            partner_store_id="10001",
            stock_items=[{"sku": "SKU-OFF", "available_stock": 15.0, "is_out_of_stock": False}],
            db=mock_db_sync,
        )
    assert mock_sf_upd.call_args.kwargs["dishes"] == [{"partner_dish_id": "SKU-OFF", "status": 2}]

    # Verify GrabMart build_catalog_menu also applies custom_price & is_enabled
    ch_gm = Channel(id="ch-gm", code="GRAB", name="GrabMart", is_active=True, config={"has_partner_pricing": True})
    m_gm_open = ChannelProductMapping(id="mgm1", product_id="p-open", channel_id="ch-gm", custom_price=192000.0, is_enabled=True)
    m_gm_off = ChannelProductMapping(id="mgm2", product_id="p-off", channel_id="ch-gm", custom_price=375000.0, is_enabled=False)

    mock_db_gm = _make_mock_db()
    mock_db_gm.execute.side_effect = [
        rows_res,
        _scalars_result([ch_gm]),
        _scalars_result([m_gm_open, m_gm_off]),
    ]
    gm_adapter = GrabMartChannelAdapter()
    gm_menu = await gm_adapter.build_catalog_menu(store_id="s1", partner_store_id="GM-10001", db=mock_db_gm)
    gm_items = {it["id"]: it for it in gm_menu["categories"][0]["subCategories"][0]["items"]}

    assert gm_items["SKU-OPEN"]["availableStatus"] == "AVAILABLE"
    assert gm_items["SKU-OPEN"]["price"] == 192000
    assert gm_items["SKU-OFF"]["availableStatus"] == "UNAVAILABLE"
    assert gm_items["SKU-OFF"]["maxStock"] == 0
    assert gm_items["SKU-OFF"]["price"] == 375000


# ---------------------------------------------------------------------------
# 6. Poppies Channel Reconciliation, Resilient Catalog Fallback & Matching APIs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_channels_from_poppies_renames_and_merges_legacy_aliases():
    """
    Verify `reconcile_channels_from_poppies`:
    - Renames legacy `GRABMART` -> Canonical `GRAB` in-place while preserving UUID & Admin feature flags.
    - Merges duplicate legacy `POS_OFFLINE` into Canonical `STORE` without violating constraints.
    - Creates remaining Canonical channels so all 14 Poppies channels are matched.
    """
    from app.modules.grabmart.adapter import GrabMartChannelAdapter
    from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
    from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter
    from app.services.channel_registry import channel_registry

    for adp in (GrabMartChannelAdapter(), ShopeeFoodChannelAdapter(), ShopeeMartChannelAdapter()):
        if not channel_registry.get(adp.channel_code):
            channel_registry.register(adp)

    legacy_grabmart = Channel(
        id="uuid-grab-keep",
        code="GRABMART",
        name="GrabMart Legacy",
        is_active=True,
        config={"auto_confirm_enabled": True, "client_id": "grab-secret-id"},
    )
    canon_store = Channel(id="uuid-store", code="STORE", name="POS", is_active=True, config={})
    dup_pos_offline = Channel(
        id="uuid-pos-dup",
        code="POS_OFFLINE",
        name="Old POS",
        is_active=True,
        config={"menu_sync_enabled": False},
    )
    st_10001 = Store(id="st-1", code="10001", name="Thảo Điền", is_active=True)

    mock_db = _make_mock_db()
    mock_db.delete = AsyncMock()
    upd_res = MagicMock()
    upd_res.rowcount = 2

    added_channels: list[Channel] = []

    def _track_add(obj):
        if isinstance(obj, Channel):
            if not obj.id:
                obj.id = f"gen-{obj.code}"
            added_channels.append(obj)

    mock_db.add.side_effect = _track_add

    # Sequence of db.execute calls in reconcile_channels_from_poppies + get_poppies_channel_matching_report
    count_scalar = MagicMock()
    count_scalar.scalar_one.return_value = 30685

    mock_db.execute.side_effect = [
        _scalars_result([legacy_grabmart, canon_store, dup_pos_offline]),  # initial select(Channel)
        # _merge_alias_channel_into_canonical(dup_pos_offline -> canon_store):
        _scalars_result([]),  # canon StoreChannelMapping
        _scalars_result([]),  # alias StoreChannelMapping
        _scalars_result([]),  # canon ChannelProductMapping
        _scalars_result([]),  # alias ChannelProductMapping
        upd_res,              # update(UnifiedOrder)
        # sync_store_outlets:
        _scalars_result([st_10001]),  # select(Store)
        _scalars_result([]),          # select(StoreChannelMapping)
        # get_poppies_channel_matching_report:
        _scalars_result([legacy_grabmart, canon_store]),  # select(Channel) - check merged list below
        _scalars_result([]),  # select(StoreChannelMapping)
        _scalars_result([]),  # select(ChannelProductMapping)
        count_scalar,         # select(func.count(Product.id))
    ]

    with patch.object(
        poppies_service.client,
        "get_pricing_channels",
        new_callable=AsyncMock,
        return_value={
            "success": True,
            "source": "poppies_live",
            "total": 14,
            "channels": POPPIES_STANDARD_CHANNELS,
        },
    ):
        res = await poppies_service.reconcile_channels_from_poppies(db=mock_db, sync_store_outlets=True)

    assert res["success"] is True
    assert res["total_poppies_channels"] == 14
    # GRABMART was renamed in-place to GRAB, preserving its original UUID & auto_confirm_enabled!
    assert legacy_grabmart.code == "GRAB"
    assert legacy_grabmart.id == "uuid-grab-keep"
    assert legacy_grabmart.config["auto_confirm_enabled"] is True
    assert legacy_grabmart.config["client_id"] == "grab-secret-id"
    assert legacy_grabmart.config["arito_channel_code"] == "K15"
    # Duplicate POS_OFFLINE was merged and deleted
    mock_db.delete.assert_any_call(dup_pos_offline)
    merged_codes = {(m["from_code"], m["to_code"]) for m in res["legacy_channels_merged"]}
    assert ("GRABMART", "GRAB") in merged_codes
    assert ("POS_OFFLINE", "STORE") in merged_codes
    # Remaining 12 channels (including SHOPEEMART K12) created
    created_codes = {c.code for c in added_channels}
    assert "SHOPEEMART" in created_codes
    assert "SHOPEEFOOD" in created_codes
    assert len(created_codes) == 12


@pytest.mark.asyncio
async def test_master_data_sync_falls_back_to_store_pricing_snapshot_on_500():
    """
    When `/api/v1/pos/master-data/snapshot` returns HTTP 500, `sync_master_data_from_poppies`
    automatically falls back to `STORE` pricing snapshot + CTTK stock records without failing.
    """
    mock_db = _make_mock_db()
    st_10001 = Store(id="st-1", code="10001", name="Thảo Điền", is_active=True)

    # Sequence of db.execute calls:
    # 1-4: sync_master_data_from_poppies initial queries before page 1 get_snapshot fails
    # 5-6: sync_catalog_from_store_pricing_snapshot Category & Product queries
    # 7-9: sync_stock_cttk_realtime Store, Product, StoreInventory queries
    added_products: list[Product] = []

    def _capture_add(obj):
        if isinstance(obj, Product):
            obj.id = f"prod-{obj.sku}"
            added_products.append(obj)

    mock_db.add.side_effect = _capture_add
    mock_db.execute.side_effect = [
        _scalars_result([st_10001]),  # sync_master_data stores
        _scalars_result([]),          # sync_master_data categories
        _scalars_result([]),          # sync_master_data products
        _scalars_result([]),          # sync_master_data inventories
        _scalar_result(Category(id="cat-pop", code="POPPIES_MASTER", name="Poppies")),  # fallback category
        _scalars_result([]),          # fallback products
        _scalars_result([st_10001]),  # cttk stores
        _scalars_result(added_products),  # cttk products
        _scalars_result([]),          # cttk inventories
    ]

    store_snapshot = {
        "success": True,
        "version": "v20261106",
        "prices": {
            "290003357000": {
                "effective_price": 214900.0,
                "base_price": 214900.0,
                "allowed_in_context": True,
                "productName": "Quýt Úc 150-200g",
                "barcode": "                    ",
                "is_weight_item": True,
            }
        },
    }
    cttk_payload = {
        "success": True,
        "items": [
            {"sku": "290003357000", "storeCode": "10001", "slTon": 18.5, "unitName": "Kg"}
        ],
    }

    with patch.object(
        poppies_service.client,
        "get_snapshot",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("500 Internal Error", request=MagicMock(), response=MagicMock(status_code=500)),
    ), patch.object(
        poppies_service.client,
        "get_pricing_snapshot",
        new_callable=AsyncMock,
        return_value=store_snapshot,
    ), patch.object(
        poppies_service.client,
        "get_cttk_records",
        new_callable=AsyncMock,
        return_value=cttk_payload,
    ):
        res = await poppies_service.sync_master_data_from_poppies(
            db=mock_db, branch_code="10001", fetch_all=True
        )

    assert res["success"] is True
    assert res["sync_mode"] == "store_pricing_snapshot_fallback"
    assert res["products_created"] == 1
    assert res["inventories_updated"] == 1
    assert added_products[0].sku == "290003357000"
    assert added_products[0].barcode is None  # Whitespace-only barcode cleanly stripped!
    assert added_products[0].base_price == 214900.0


def test_api_channels_sync_poppies_matching_and_ui_buttons(client, mock_store_manager):
    """Verify new REST endpoints & UI buttons for Poppies ERP Sync & Matching."""
    app.dependency_overrides[get_current_user] = lambda: mock_store_manager
    app.dependency_overrides[require_store_manager] = lambda: mock_store_manager
    app.dependency_overrides[get_db] = lambda: _make_mock_db()

    fake_matching = {
        "source": "poppies_live",
        "total_poppies_channels": 14,
        "matched_channels_count": 14,
        "needs_reconciliation": False,
        "total_local_products": 30685,
        "channels": [],
    }
    fake_reconcile = {
        "success": True,
        "source": "poppies_live",
        "total_poppies_channels": 14,
        "channels_created": 11,
        "channels_updated": 3,
        "legacy_channels_merged": [{"from_code": "GRABMART", "to_code": "GRAB", "action": "renamed_in_place"}],
        "store_mappings_created": 4,
        "matching": fake_matching,
    }
    fake_full_sync = {
        "success": True,
        "branch_code": "10001",
        "channel_reconciliation": fake_reconcile,
        "catalog_sync": {"total_fetched": 30685, "products_created": 100, "products_updated": 30585, "inventories_updated": 120000},
        "pricing_snapshots_synced": [
            {"channel_code": "GRAB", "matched_skus_count": 30685, "prices_updated": 45, "blocked_skus_count": 3}
        ],
        "matching": fake_matching,
        "elapsed_seconds": 4.2,
    }

    try:
        with patch.object(
            poppies_service, "get_poppies_channel_matching_report", new_callable=AsyncMock, return_value=fake_matching
        ), patch.object(
            poppies_service, "reconcile_channels_from_poppies", new_callable=AsyncMock, return_value=fake_reconcile
        ), patch.object(
            poppies_service, "sync_and_match_all_from_poppies", new_callable=AsyncMock, return_value=fake_full_sync
        ):
            r_match = client.get("/api/v1/channels/poppies-matching")
            assert r_match.status_code == 200
            assert r_match.json()["data"]["matched_channels_count"] == 14

            r_sync_ch = client.post("/api/v1/channels/sync-poppies")
            assert r_sync_ch.status_code == 200
            assert r_sync_ch.json()["data"]["total_poppies_channels"] == 14

            r_sync_all = client.post("/api/v1/inventory/poppies/sync-and-match?branch_code=10001")
            assert r_sync_all.status_code == 200
            assert r_sync_all.json()["data"]["catalog_sync"]["total_fetched"] == 30685

            r_sync_pr = client.post("/api/v1/pricing/sync-poppies?branch_code=10001")
            assert r_sync_pr.status_code == 200
            assert r_sync_pr.json()["success"] is True

        # Verify UI pages render the new Poppies Sync & Matching buttons cleanly
        inv_html = client.get("/inventory").text
        assert "Đồng Bộ & Matching Poppies" in inv_html
        assert "Matching Kênh & Giá" in inv_html
        assert "modal-sku-channels" in inv_html

        adm_html = client.get("/admin").text
        assert "Đồng Bộ & Matching Kênh Poppies" in adm_html
        assert "Bảng Đối Soát & Matching Kênh Chuẩn Poppies ERP" in adm_html
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_price_book_fallback_and_store_base_price_propagation():
    """
    Verify:
    1. When `/snapshots` returns 500 ProgrammingError, `get_pricing_snapshot` seamlessly falls back
       to `/price-book` and returns normalized pricing & exclusions.
    2. `sync_channel_pricing_snapshot(channel_code='STORE')` updates `Product.base_price`
       (`27900 -> 29900`, `50900 -> 54900`) and propagates changed SKUs to external channels.
    3. `sync_stock_cttk_realtime` accumulates positive + negative (`LOCHUNG`) rows across pages.
    """
    def _fake_get(url, params=None, headers=None):
        resp = MagicMock()
        if "price-book" in url:
            resp.status_code = 200
            resp.json.return_value = {
                "success": True,
                "version": "v20261107-pb",
                "branch_code": "CH10001",
                "channel_code": (params or {}).get("channel_code", "GRAB"),
                "price_book": [
                    {"sku": "290000790000", "name": "Dalat Milk 450ml", "price": 29900.0, "allowed": True},
                    {"sku": "290000791000", "name": "Dalat Milk 950ml", "price": 54900.0, "allowed": True},
                    {"sku": "SKU_DENIED", "name": "Denied Item", "price": 15000.0, "allowed": False},
                ],
            }
            return resp
        resp.status_code = 500
        return resp

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=_fake_get):
        snap = await poppies_service.client.get_pricing_snapshot(branch_code="10001", channel_code="GRAB")
        assert snap["source"] == "price_book_fallback"
        assert snap["has_partner_pricing"] is True
        assert snap["prices"]["290000790000"]["effective_price"] == 29900.0
        assert snap["prices"]["290000791000"]["effective_price"] == 54900.0
        assert "SKU_DENIED" in snap["exclusions"]

        # Sync STORE snapshot -> updates Product.base_price and propagates changed SKUs
        mock_db = _make_mock_db()
        store_ch = Channel(id="ch-store", code="STORE", name="POS", is_active=True, config={})
        p_450 = Product(id="p450", sku="290000790000", name="Dalat Milk 450ml", base_price=27900.0, is_active=True)
        p_950 = Product(id="p950", sku="290000791000", name="Dalat Milk 950ml", base_price=50900.0, is_active=True)
        mock_db.execute.side_effect = [
            _scalar_result(store_ch),
            _scalars_result([p_450, p_950]),
            _scalars_result([]),
        ]
        with patch("app.services.poppies_pricing_sync.propagate_skus_to_external_channel", new_callable=AsyncMock, return_value=[{"success": True}]) as mock_prop:
            sync_res = await poppies_service.sync_channel_pricing_snapshot(
                db=mock_db, branch_code="10001", channel_code="STORE", auto_propagate=True
            )
            assert sync_res["prices_updated"] == 2
            assert p_450.base_price == 29900.0
            assert p_950.base_price == 54900.0
            assert mock_prop.await_count == 3  # GRAB, SHOPEEFOOD, SHOPEEMART

    # Verify multi-page CTTK accumulation across positive lots and negative LOCHUNG rows
    from app.models.inventory import StoreInventory

    mock_cttk_db = _make_mock_db()
    st_10001 = Store(id="st-1", code="10001", name="Thảo Điền", is_active=True)
    inv_950 = StoreInventory(
        store_id="st-1", product_id="p950", sku="290000791000",
        total_stock=0.0, reserved_stock=0.0, available_stock=0.0, is_out_of_stock=True,
    )
    mock_cttk_db.execute.side_effect = [
        _scalars_result([st_10001]),
        _scalars_result([inv_950]),
    ]
    page1 = {"items": [{"sku": "290000791000", "storeCode": "10001", "slTon": 896.0}] + [{"sku": f"FILL_{i}", "storeCode": "10001", "slTon": 1.0} for i in range(4999)]}
    page2 = {"items": [{"sku": "290000791000", "storeCode": "10001", "slTon": -885.0}]}
    with patch.object(poppies_service.client, "get_cttk_records", new_callable=AsyncMock, side_effect=[page1, page2]):
        cttk_res = await poppies_service.sync_stock_cttk_realtime(db=mock_cttk_db, create_missing_inventories=False)
        assert cttk_res["success"] is True
        assert inv_950.available_stock == 11.0
        assert inv_950.is_out_of_stock is False
        assert "290000791000" in cttk_res["availability_changed_skus"]


@pytest.mark.asyncio
async def test_excluded_zero_price_does_not_pollute_custom_price():
    """Verify TIER_1_EXCLUDED `effective_price=0.0` cleans stale 0.0 custom_price to None instead of overwriting 0.0."""
    grab_ch = Channel(id="ch-grab", code="GRAB", name="GrabMart", is_active=True, config={"has_partner_pricing": True})
    p_450 = Product(id="p450", sku="290000790000", name="Dalat Milk 450ml", base_price=29900.0, is_active=True)
    stale_zero_map = ChannelProductMapping(
        product_id="p450", channel_id="ch-grab", custom_price=0.0, is_enabled=False, channel_sync_payload={}
    )
    mock_db = _make_mock_db()
    mock_db.execute.side_effect = [
        _scalar_result(grab_ch),
        _scalars_result([p_450]),
        _scalars_result([stale_zero_map]),
    ]
    snap = {
        "success": True,
        "version": "v-excl",
        "channel_code": "GRAB",
        "has_partner_pricing": True,
        "restrict_to_priced_items": True,
        "prices": {
            "290000790000": {
                "effective_price": 0.0,
                "base_price": 29900.0,
                "allowed": False,
                "allowed_in_context": False,
            }
        },
        "exclusions": ["290000790000"],
    }
    with patch.object(poppies_service.client, "get_pricing_snapshot", new_callable=AsyncMock, return_value=snap), \
         patch("app.services.poppies_pricing_sync.propagate_skus_to_external_channel", new_callable=AsyncMock, return_value=[]):
        res = await poppies_service.sync_channel_pricing_snapshot(
            db=mock_db, branch_code="10001", channel_code="GRAB", auto_propagate=False
        )
        assert res["success"] is True
        assert stale_zero_map.custom_price is None
        assert stale_zero_map.is_enabled is False


@pytest.mark.asyncio
async def test_resource_optimizations_empty_delta_noop_and_unchanged_cttk_skip():
    """
    Verify resource optimizations:
    1. Empty delta snapshot (`is_delta=True`, `prices={}`, `exclusions=[]`) short-circuits
       with `fast_noop=True` without querying `Product` or `ChannelProductMapping`.
    2. Already-blocked mappings (`is_enabled=False`, `blocked_by_snapshot=True`) are NOT
       mutated on full reconciliation (0 dirty payload rewrites).
    3. `sync_stock_cttk_realtime` skips DB writes & commit when stock is unchanged.
    """
    from app.models.inventory import StoreInventory

    grab_ch = Channel(
        id="ch-grab",
        code="GRAB",
        name="GrabMart",
        is_active=True,
        config={"has_partner_pricing": True, "last_snapshot_version": "v100"},
    )

    # 1. Empty delta -> only 1 DB execute (`Channel` lookup), 0 Product/Mapping queries!
    mock_delta_db = _make_mock_db()
    mock_delta_db.execute.side_effect = [_scalar_result(grab_ch)]
    empty_delta = {
        "success": True,
        "version": "v100",
        "is_active": True,
        "has_partner_pricing": True,
        "is_delta": True,
        "prices": {},
        "exclusions": [],
    }
    with patch.object(poppies_service.client, "get_pricing_snapshot", new_callable=AsyncMock, return_value=empty_delta):
        res_delta = await poppies_service.sync_channel_pricing_snapshot(
            db=mock_delta_db,
            branch_code="10001",
            channel_code="GRAB",
            since_version="v100",
            auto_propagate=False,
        )
        assert res_delta["fast_noop"] is True
        assert mock_delta_db.execute.await_count == 1
        assert mock_delta_db.commit.await_count == 0

    # 2. Already-blocked mapping payload reference is untouched on repeat full snapshot
    orig_payload = {"snapshot_version": "v99", "blocked_by_snapshot": True, "timestamp": "2026-11-07T00:00:00Z"}
    already_blocked = ChannelProductMapping(
        product_id="p1",
        channel_id="ch-grab",
        custom_price=None,
        is_enabled=False,
        channel_sync_payload=orig_payload,
    )
    prod1 = Product(id="p1", sku="SKU_LOCKED", name="Locked SKU", base_price=50000.0, is_active=True)
    mock_full_db = _make_mock_db()
    mock_full_db.execute.side_effect = [
        _scalar_result(grab_ch),
        _scalars_result([prod1]),
        _scalars_result([already_blocked]),
    ]
    full_snap = {
        "success": True,
        "version": "v101",
        "is_active": True,
        "has_partner_pricing": True,
        "is_delta": False,
        "prices": {},
        "exclusions": ["SKU_LOCKED"],
    }
    with patch.object(poppies_service.client, "get_pricing_snapshot", new_callable=AsyncMock, return_value=full_snap):
        res_full = await poppies_service.sync_channel_pricing_snapshot(
            db=mock_full_db, branch_code="10001", channel_code="GRAB", auto_propagate=False
        )
        assert res_full["blocked_mappings_applied"] == 1
        assert already_blocked.channel_sync_payload is orig_payload  # Not rewritten!

    # 3. Unchanged CTTK stock -> inventories_updated == 0 and db.commit not called
    mock_cttk_db = _make_mock_db()
    st_1 = Store(id="st-1", code="10001", name="Thảo Điền", is_active=True)
    inv_same = StoreInventory(
        store_id="st-1",
        product_id="p1",
        sku="SKU_LOCKED",
        total_stock=12.0,
        reserved_stock=2.0,
        available_stock=10.0,
        is_out_of_stock=False,
    )
    mock_cttk_db.execute.side_effect = [
        _scalars_result([st_1]),
        _scalars_result([inv_same]),
    ]
    with patch.object(
        poppies_service.client,
        "get_cttk_records",
        new_callable=AsyncMock,
        return_value={"items": [{"sku": "SKU_LOCKED", "storeCode": "10001", "slTon": 12.0}]},
    ):
        res_cttk = await poppies_service.sync_stock_cttk_realtime(db=mock_cttk_db, batch_size=10000)
        assert res_cttk["inventories_updated"] == 0
        assert mock_cttk_db.commit.await_count == 0


