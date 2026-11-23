from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_current_user, get_db
from app.core.crypto import decrypt_secret, encrypt_secret, write_restricted_json
from app.core.database import get_optional_db
from app.core.exceptions import BadRequestError, ConflictError
from app.main import app
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Product
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole
from app.modules.grabmart.schemas import GrabOrderItem
from app.modules.shopeefood.adapter import (
    MAX_MENU_CACHE_ENTRIES,
    MENU_CACHE_TTL_SECONDS,
    ShopeeFoodChannelAdapter,
    _shopeefood_menu_cache,
    _shopeefood_menu_cache_ts,
)
from app.modules.shopeefood.service import ShopeeFoodClient
from app.modules.shopeemart.router import (
    _CONSUMED_OAUTH_STATES,
    _PROCESSED_WEBHOOK_DELIVERIES,
    _validate_shopee_redirect_url,
)
from app.modules.shopeemart.service import ShopeeMartClient
from app.schemas.channel import ChannelResponse
from app.schemas.inventory import (
    InventoryBatchUpdateRequest,
    InventoryStockUpdateItem,
    InventoryToggleStatusRequest,
)
from app.services.file_service import FileService, file_service
from app.services.haravan_order_service import HaravanOrderService, haravan_order_service
from app.services.inventory_service import InventoryService
from app.services.order_service import OrderService
from app.services.poppies_client import PoppiesClient
from app.services.poppies_service import PoppiesMasterDataSyncService, poppies_service
from app.services.report_service import ReportService


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_user(
    *,
    uid: str = "u-admin",
    username: str = "admin",
    role: str = UserRole.SUPER_ADMIN.value,
    store_id: str | None = None,
    store_code: str | None = None,
    is_superuser: bool = False,
) -> User:
    store_obj = (
        Store(id=store_id or "st-1", code=store_code, name=f"Store {store_code}", is_active=True)
        if store_code
        else None
    )
    return User(
        id=uid,
        username=username,
        email=f"{username}@namanmarket.com",
        role=role,
        store_id=store_id,
        store=store_obj,
        is_active=True,
        is_superuser=is_superuser,
    )


def _scalar_res(val):
    m = MagicMock()
    m.scalar_one_or_none.return_value = val
    s = MagicMock()
    s.first.return_value = val
    s.all.return_value = [val] if val is not None else []
    m.scalars.return_value = s
    return m


def _scalars_res(items):
    m = MagicMock()
    s = MagicMock()
    s.all.return_value = list(items)
    s.first.return_value = items[0] if items else None
    m.scalars.return_value = s
    m.scalar_one_or_none.return_value = items[0] if items else None
    return m


# ==============================================================================
# 1. SEC-C01: Symmetric Encryption & Channel Config Allowlist
# ==============================================================================


def test_crypto_roundtrip_idempotency_and_legacy_plaintext(tmp_path):
    plaintext = "shpk_live_partner_secret_2026"
    encrypted = encrypt_secret(plaintext)
    assert encrypted.startswith("enc:v1:")
    assert plaintext not in encrypted
    assert decrypt_secret(encrypted) == plaintext

    # Encrypting valid ciphertext again stays idempotent
    assert encrypt_secret(encrypted) == encrypted
    # Legacy unencrypted plaintext passes through unchanged
    assert decrypt_secret("legacy_plain_token") == "legacy_plain_token"
    assert encrypt_secret("") == ""
    assert decrypt_secret(None) == ""

    target = tmp_path / "creds.json"
    write_restricted_json(target, {"partner_key": encrypted, "partner_id": "123"})
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["partner_key"] == encrypted


def test_channel_response_strips_secrets_preserves_public_metadata():
    ch = Channel(
        id="ch-sm",
        code="SHOPEEMART",
        name="ShopeeMart",
        is_active=True,
        config={
            "features": {"order_webhook_enabled": True},
            "arito_channel_code": "K12",
            "ma_kenh": "K12",
            "channel_group": "ECOM",
            "has_partner_pricing": True,
            "default_markup_percent": 15.0,
            "packaging_fee_vnd": 3000,
            "inherited_from": None,
            "is_dynamic": False,
            "legacy_channel_code": "SHOPEE_MART",
            "shop_tokens": {"999": {"access_token": "secret-tok", "refresh_token": "secret-ref"}},
            "access_token": "raw-access",
            "refresh_token": "raw-refresh",
            "partner_key": "raw-partner-key",
            "client_secret": "raw-client-secret",
        },
    )
    resp = ChannelResponse.model_validate(ch)
    assert resp.config is not None
    assert resp.config["arito_channel_code"] == "K12"
    assert resp.config["packaging_fee_vnd"] == 3000
    for sensitive_key in ("shop_tokens", "access_token", "refresh_token", "partner_key", "client_secret"):
        assert sensitive_key not in resp.config


# ==============================================================================
# 2. ShopeeMart Security & Concurrency
# ==============================================================================


@pytest.mark.asyncio
async def test_shopeemart_encrypts_credentials_and_tokens_at_rest(tmp_path):
    creds_file = tmp_path / "shopeemart_credentials.json"
    token_file = tmp_path / "shopeemart_tokens.json"

    with (
        patch("app.modules.shopeemart.service.CREDENTIALS_STORE_PATH", creds_file),
        patch("app.modules.shopeemart.service.TOKEN_STORE_PATH", token_file),
    ):
        cli = ShopeeMartClient()
        cli.save_credentials(
            partner_id="201020",
            partner_key="live_partner_secret_xyz",
            shop_id="998877",
        )
        raw_creds = json.loads(creds_file.read_text(encoding="utf-8"))
        assert raw_creds["partner_key"].startswith("enc:v1:")
        assert "live_partner_secret_xyz" not in creds_file.read_text(encoding="utf-8")

        cli._token_cache["998877"] = {
            "access_token": "live_acc_123",
            "refresh_token": "live_ref_456",
            "expire_in": 14400,
            "expires_at": time.time() + 14400,
        }
        cli._write_token_cache_file()
        raw_tokens = json.loads(token_file.read_text(encoding="utf-8"))
        assert raw_tokens["998877"]["access_token"].startswith("enc:v1:")
        assert raw_tokens["998877"]["refresh_token"].startswith("enc:v1:")
        assert "live_acc_123" not in token_file.read_text(encoding="utf-8")

        reloaded = ShopeeMartClient()
        assert reloaded.partner_key == "live_partner_secret_xyz"
        assert reloaded._token_cache["998877"]["access_token"] == "live_acc_123"
        assert reloaded._token_cache["998877"]["refresh_token"] == "live_ref_456"

        # DB persistence encrypts tokens and hydrate_from_db decrypts legacy/encrypted entries
        mock_ch = Channel(id="c-sm", code="SHOPEEMART", name="ShopeeMart", config={})
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=_scalars_res([mock_ch]))
        mock_db.commit = AsyncMock()

        class _DBCtx:
            async def __aenter__(self):
                return mock_db

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with patch("app.core.database.AsyncSessionLocal", return_value=_DBCtx()):
            await cli._persist_tokens_to_db(default_shop_id="998877")
        stored_entry = mock_ch.config["shop_tokens"]["998877"]
        assert stored_entry["access_token"].startswith("enc:v1:")
        assert stored_entry["refresh_token"].startswith("enc:v1:")

        cli2 = ShopeeMartClient()
        cli2._token_cache = {}
        cli2._hydrated_from_db = False
        mock_db.execute = AsyncMock(return_value=_scalars_res([mock_ch]))
        await cli2.hydrate_from_db(mock_db)
        assert cli2._token_cache["998877"]["access_token"] == "live_acc_123"


@pytest.mark.asyncio
async def test_shopeemart_image_ssrf_cgnat_ipv4_mapped_magic_and_return():
    cli = ShopeeMartClient()
    cli.partner_id = "201020"
    cli.partner_key = "live_key"

    for bad_url in (
        "http://cdn.example.com/a.jpg",
        "https://user:pass@cdn.example.com/a.jpg",
        "https://localhost/a.jpg",
        "https://100.64.0.1/a.jpg",
        "https://[::ffff:127.0.0.1]/a.jpg",
    ):
        with pytest.raises(ValueError):
            await cli._validate_external_image_url_async(bad_url)

    with pytest.raises(ValueError):
        cli._detect_image_magic(b"<svg onload=alert(1)>")

    # Verify upload_image_from_url validates PNG magic and returns Shopee image_id (no dead code)
    fake_loop = MagicMock()
    fake_loop.getaddrinfo = AsyncMock(
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    )

    class _FakeImgResp:
        status_code = 200
        headers = {"content-length": "12"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"\x89PNG\r\n\x1a\n1234"

    class _FakeStreamCM:
        async def __aenter__(self):
            return _FakeImgResp()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_http = MagicMock()
    fake_http.stream.return_value = _FakeStreamCM()
    fake_upload_resp = MagicMock()
    fake_upload_resp.status_code = 200
    fake_upload_resp.json.return_value = {"response": {"image_info": {"image_id": "shp_img_999"}}}
    fake_http.post = AsyncMock(return_value=fake_upload_resp)

    with (
        patch("app.modules.shopeemart.service.asyncio.get_running_loop", return_value=fake_loop),
        patch.object(cli, "_get_http_client", return_value=fake_http),
    ):
        img_id = await cli.upload_image_from_url("https://cdn.namanmarket.com/prod.png")
    assert img_id == "shp_img_999"


def test_shopeemart_auth_refresh_superadmin_only_and_hides_tokens(client):
    branch_mgr = _make_user(uid="u-mgr", username="mgr1", role=UserRole.STORE_MANAGER.value, store_id="st-10001")
    super_admin = _make_user(uid="u-adm", username="adm1", role=UserRole.SUPER_ADMIN.value, is_superuser=True)
    mock_db = AsyncMock()
    app.dependency_overrides[get_optional_db] = lambda: mock_db

    # Non-super-admin blocked (403)
    app.dependency_overrides[get_current_user] = lambda: branch_mgr
    app.dependency_overrides[get_current_active_user] = lambda: branch_mgr
    r_forbidden = client.post("/api/v1/shopeemart/auth/refresh?shop_id=999")
    assert r_forbidden.status_code == 403

    # SuperAdmin succeeds, response does NOT leak access_token or refresh_token
    app.dependency_overrides[get_current_user] = lambda: super_admin
    app.dependency_overrides[get_current_active_user] = lambda: super_admin
    from app.modules.shopeemart.schemas import ShopeeMartConnectionStatus, ShopeeMartTokenResponse

    fake_tok = ShopeeMartTokenResponse(
        access_token="SECRET_SHOULD_NOT_APPEAR",
        refresh_token="SECRET_REFRESH_SHOULD_NOT_APPEAR",
        expire_in=14400,
        shop_id=999,
    )
    fake_status = ShopeeMartConnectionStatus(
        is_configured=True,
        has_valid_token=True,
        token_expires_at="2026-11-23T12:00:00Z",
        is_sandbox_mode=False,
        partner_id="201020",
        shop_id="999",
    )
    with (
        patch("app.modules.shopeemart.router.shopeemart_client.hydrate_from_db", new_callable=AsyncMock),
        patch("app.modules.shopeemart.router.shopeemart_client.refresh_access_token", new_callable=AsyncMock, return_value=fake_tok),
        patch("app.modules.shopeemart.router.shopeemart_client.get_connection_status", return_value=fake_status),
    ):
        r_ok = client.post("/api/v1/shopeemart/auth/refresh?shop_id=999")
    assert r_ok.status_code == 200
    body_text = r_ok.text
    assert "SECRET_SHOULD_NOT_APPEAR" not in body_text
    assert "SECRET_REFRESH_SHOULD_NOT_APPEAR" not in body_text
    assert "access_token" not in r_ok.json()["data"]
    assert "refresh_token" not in r_ok.json()["data"]


def test_shopeemart_branch_idor_on_buyer_cancel_tracking_awb_returns(client):
    branch_staff = _make_user(
        uid="u-10001", username="stf10001", role=UserRole.STAFF.value, store_id="st-10001", store_code="10001"
    )
    app.dependency_overrides[get_current_user] = lambda: branch_staff
    app.dependency_overrides[get_current_active_user] = lambda: branch_staff

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    # 1. buyer-cancellation with missing order for branch -> fail closed 404
    mock_db.execute = AsyncMock(return_value=_scalar_res(None))
    r_missing = client.post(
        "/api/v1/shopeemart/orders/SN-UNKNOWN/buyer-cancellation",
        json={"order_sn": "SN-UNKNOWN", "operation": "ACCEPT"},
    )
    assert r_missing.status_code == 404

    # 2. buyer-cancellation for order belonging to another store -> 403
    rival_order = UnifiedOrder(id="o-rival", store_id="st-10004", channel_order_id="SN-10004", status=UnifiedOrderStatus.PENDING)
    mock_db.execute = AsyncMock(return_value=_scalar_res(rival_order))
    r_rival = client.post(
        "/api/v1/shopeemart/orders/SN-10004/buyer-cancellation",
        json={"order_sn": "SN-10004", "operation": "ACCEPT"},
    )
    assert r_rival.status_code == 403

    # 3. tracking / download-awb / returns / return-detail scoped to shop_id mapped to rival store -> 403
    rival_mapping = StoreChannelMapping(store_id="st-10004", channel_id="c-sm", partner_store_id="88804")
    for url in (
        "/api/v1/shopeemart/orders/SN-10004/tracking?shop_id=88804",
        "/api/v1/shopeemart/orders/SN-10004/download-awb?shop_id=88804",
        "/api/v1/shopeemart/returns?shop_id=88804",
        "/api/v1/shopeemart/returns/RET-1?shop_id=88804",
    ):
        mock_db.execute = AsyncMock(side_effect=[_scalar_res(None), _scalar_res(rival_mapping)] if "/orders/" in url else [_scalar_res(rival_mapping)])
        assert client.get(url).status_code == 403


def test_shopeemart_oauth_prod_redirect_replay_cache_headers(client):
    with patch("app.modules.shopeemart.router.get_settings") as mock_gs:
        mock_gs.return_value = MagicMock(ENVIRONMENT="production")
        with pytest.raises(Exception):
            _validate_shopee_redirect_url("http://localhost:3002/api/v1/shopeemart/auth/callback")
        with pytest.raises(Exception):
            _validate_shopee_redirect_url("http://evil.com/cb")
        assert _validate_shopee_redirect_url("https://portal.namanmarket.com/cb").startswith("https://")

    from app.core.security import create_partner_token

    _CONSUMED_OAUTH_STATES.clear()
    oauth_state = create_partner_token("shopeemart_oauth", "admin-1", expires_seconds=300)
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=_scalar_res(None))
    app.dependency_overrides[get_optional_db] = lambda: None

    with (
        patch("app.modules.shopeemart.router.shopeemart_client.exchange_code_for_token", new_callable=AsyncMock) as mock_ex,
        patch("app.modules.shopeemart.router.shopeemart_client.save_credentials"),
    ):
        from app.modules.shopeemart.schemas import ShopeeMartTokenResponse
        mock_ex.return_value = ShopeeMartTokenResponse(access_token="a", refresh_token="r", expire_in=3600, shop_id=10001)
        r1 = client.get(
            f"/api/v1/shopeemart/auth/callback?code=c1&shop_id=10001&state={oauth_state}",
            headers={"Accept": "text/html"},
        )
        assert r1.status_code == 200
        assert r1.headers.get("cache-control") == "no-store"
        assert r1.headers.get("pragma") == "no-cache"
        assert r1.headers.get("referrer-policy") == "no-referrer"

        # Replay same state must fail 401
        r2 = client.get(
            f"/api/v1/shopeemart/auth/callback?code=c1&shop_id=10001&state={oauth_state}",
        )
        assert r2.status_code == 401


def test_shopeemart_webhook_timestamp_skew_dedupe_untrusted_proxy(client):
    _PROCESSED_WEBHOOK_DELIVERIES.clear()
    partner_key = "test_partner_key_hmac"
    body = json.dumps({"msg_id": "m-stale-1", "timestamp": int(time.time()) - 900, "code": 3}).encode("utf-8")
    public_url = "https://portal.namanmarket.com/api/v1/shopeemart/webhooks/order"
    sig = hmac.new(partner_key.encode(), f"{public_url}|{body.decode()}".encode(), hashlib.sha256).hexdigest()

    app.dependency_overrides[get_optional_db] = lambda: AsyncMock()
    with patch("app.modules.shopeemart.router.shopeemart_client.verify_webhook_signature", return_value=True):
        r_stale = client.post(
            "/api/v1/shopeemart/webhooks/order",
            content=body,
            headers={
                "Authorization": sig,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "portal.namanmarket.com",
            },
        )
        assert r_stale.status_code == 401

        # Fresh webhook processed once, second delivery deduped without calling handle_order_webhook twice
        fresh_body = json.dumps({"msg_id": "m-fresh-99", "timestamp": int(time.time()), "code": 3}).encode("utf-8")
        with patch("app.modules.shopeemart.router.adapter.handle_order_webhook", new_callable=AsyncMock, return_value={"code": 0, "message": "success", "order_sn": "SN-99"}) as mock_wh:
            r_one = client.post("/api/v1/shopeemart/webhooks/order", content=fresh_body, headers={"Authorization": "ok"})
            r_two = client.post("/api/v1/shopeemart/webhooks/order", content=fresh_body, headers={"Authorization": "ok"})
            assert r_one.status_code == 200
            assert r_two.status_code == 200
            assert r_two.json().get("duplicate") is True
            assert mock_wh.await_count == 1

        # Error from handler must NOT leak raw exception string
        err_body = json.dumps({"msg_id": "m-err-1", "timestamp": int(time.time()), "code": 3}).encode("utf-8")
        with patch("app.modules.shopeemart.router.adapter.handle_order_webhook", new_callable=AsyncMock, side_effect=RuntimeError("SECRET_DB_DSN_LEAK")):
            r_err = client.post("/api/v1/shopeemart/webhooks/order", content=err_body, headers={"Authorization": "ok"})
            assert r_err.json()["code"] == 400
            assert "SECRET_DB_DSN_LEAK" not in r_err.text


# ==============================================================================
# 3. ShopeeFood S2S Payload Isolation, Canonical HMAC & Cache Bounds
# ==============================================================================


@pytest.mark.asyncio
async def test_shopeefood_s2s_verified_details_ignores_spoofed_payload_fields():
    adapter = ShopeeFoodChannelAdapter()
    spoofed_payload = {
        "order_code": "SF-ORD-101",
        "partner_restaurant_id": "99999-EVIL",
        "status": "CANCELLED",
        "total_amount": 1.0,
        "subtotal_amount": 1.0,
        "delivery_fee": 0.0,
        "discount_amount": 999999.0,
        "customer_name": "Evil Spoofer",
        "items": [{"sku": "FREE-IPHONE", "quantity": 99, "price": 0}],
    }
    verified_details = {
        "code": "SF-ORD-101",
        "status": 1,
        "restaurant": {"partner_restaurant_id": "10001"},
        "order_value": {"value": 250000},
        "total_merchant_discount": {"value": 10000},
        "total_value": {"value": 255000},
        "customer_bill": {"shipping_fee": {"value": 15000}},
        "customer": {"name": "Real Customer", "phone": "0909111222"},
        "deliver_address": {"address": "21 Thao Dien"},
        "assignee": {"name": "Driver A", "phone": "0911000111"},
        "dish_groups": [
            {"dishes": [{"partner_dish_id": "SKU-REAL", "dish_name": "Táo Envy", "quantity": 2, "price": {"value": 125000}, "total": {"value": 250000}}]}
        ],
    }

    fake_order = UnifiedOrder(id="uo-1", order_code="NAM-SF-1", channel_order_id="SF-ORD-101", status=UnifiedOrderStatus.PENDING)
    mock_db = AsyncMock()
    with (
        patch("app.modules.shopeefood.adapter.channel_service.is_feature_enabled", new_callable=AsyncMock, side_effect=[True, False]),
        patch.object(OrderService, "create_or_get_inbound_order", new_callable=AsyncMock, return_value=(fake_order, True)) as mock_create,
    ):
        await adapter.handle_order_webhook(
            headers={},
            raw_body=json.dumps(spoofed_payload).encode("utf-8"),
            db=mock_db,
            verified_order_details=verified_details,
        )

    kw = mock_create.call_args.kwargs
    assert kw["partner_store_id"] == "10001"
    assert kw["order_data"]["total_amount"] == 255000.0
    assert kw["order_data"]["subtotal_amount"] == 250000.0
    assert kw["order_data"]["discount_amount"] == 10000.0
    assert kw["order_data"]["delivery_fee"] == 15000.0
    assert kw["order_data"]["customer_name"] == "Real Customer"
    assert kw["items_data"][0]["sku"] == "SKU-REAL"
    assert kw["items_data"][0]["quantity"] == 2


def test_shopeefood_hmac_rejects_bare_body_and_menu_cache_ttl():
    cli = ShopeeFoodClient()
    cli.app_key = "test_app_key_secret"
    body = b'{"order_code":"SF-1"}'

    # Bare body-only HMAC signature must be REJECTED
    bare_sig = hmac.new(cli.app_key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert cli.verify_hmac_sha256(
        raw_body=body,
        signature=bare_sig,
        method="POST",
        full_url="https://portal.namanmarket.com/api/v1/shopeefood/webhooks/order",
    ) is False

    # Canonical METHOD|URL|BODY passes
    url = "https://portal.namanmarket.com/api/v1/shopeefood/webhooks/order"
    canon_msg = f"POST|{url}|{body.decode('utf-8')}".encode("utf-8")
    canon_sig = hmac.new(cli.app_key.encode("utf-8"), canon_msg, hashlib.sha256).hexdigest()
    assert cli.verify_hmac_sha256(raw_body=body, signature=canon_sig, method="POST", full_url=url) is True

    # Cache TTL expiration & LRU eviction verification
    _shopeefood_menu_cache.clear()
    _shopeefood_menu_cache_ts.clear()
    adapter = ShopeeFoodChannelAdapter()
    _shopeefood_menu_cache["10001"] = {"menu": "stale"}
    _shopeefood_menu_cache_ts["10001"] = time.monotonic() - (MENU_CACHE_TTL_SECONDS + 10)
    assert adapter.get_cached_menu("10001") is None


# ==============================================================================
# 4. GrabMart OAuth Client Validation, 503 Order State, 404 Menu, Schema ge=1
# ==============================================================================


def test_grabmart_security_oauth_state_menu_and_schema(client):
    with pytest.raises(PydanticValidationError):
        GrabOrderItem(id="SKU-1", quantity=0, price=1000)

    from app.modules.grabmart.router import settings as gm_settings
    with (
        patch.object(gm_settings, "GRABMART_PARTNER_CLIENT_ID", "naman_grabmart_pos"),
        patch.object(gm_settings, "GRABMART_PARTNER_CLIENT_SECRET", "partner_secret_1"),
        patch.object(gm_settings, "GRABMART_CLIENT_SECRET", "outbound_secret_2"),
    ):
        assert client.post(
            "/api/v1/grabmart/oauth/token",
            json={"client_id": "wrong_id", "client_secret": "partner_secret_1"},
        ).status_code == 401
        assert client.post(
            "/api/v1/grabmart/oauth/token",
            json={"client_id": "naman_grabmart_pos", "client_secret": "outbound_secret_2"},
        ).status_code == 401
        assert client.post(
            "/api/v1/grabmart/oauth/token",
            json={"client_id": "naman_grabmart_pos", "client_secret": "partner_secret_1"},
        ).status_code == 200

    from app.core.security import create_partner_token
    partner_tok = create_partner_token("grabmart", "naman_grabmart_pos")

    # Offline DB on order state -> 503 (never 204)
    app.dependency_overrides[get_optional_db] = lambda: None
    r_state = client.put(
        "/api/v1/grabmart/webhooks/order/state",
        json={"orderID": "GM-1", "state": "COLLECTED"},
        headers={"Authorization": f"Bearer {partner_tok}"},
    )
    assert r_state.status_code == 503

    # Unmapped merchantID on menu -> 404 (never falls back to Store 1)
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=_scalars_res([]))
    app.dependency_overrides[get_optional_db] = lambda: mock_db
    r_menu = client.get(
        "/api/v1/grabmart/menu?merchantID=UNMAPPED-999&partnerMerchantID=UNMAPPED-999",
        headers={"Authorization": f"Bearer {partner_tok}"},
    )
    assert r_menu.status_code == 404


# ==============================================================================
# 5. FileService SSRF/Magic/Stream & Haravan RBAC/Header/Atomic Order Updates
# ==============================================================================


@pytest.mark.asyncio
async def test_file_service_magic_bytes_traversal_and_stream_size_cap():
    svc = FileService()
    assert svc.clean_file_url("http://namanapi.namanmarket.com/orders/1.pdf") == "https://namanapi.namanmarket.com/orders/1.pdf"
    assert svc.clean_file_url("https://namanapi.namanmarket.com/orders/../../etc/passwd") == ""
    assert svc.clean_file_url("/arbitrary/secret.pdf") == ""
    assert svc.clean_file_url("/files/good.pdf") == "https://namanapi.namanmarket.com/files/good.pdf"

    ok_svg, _ = svc.validate_file("pic.png", 100, b"<svg onload=alert(1)>")
    assert ok_svg is False
    ok_fake_pdf, _ = svc.validate_file("doc.pdf", 10, b"MZ-NOT-PDF")
    assert ok_fake_pdf is False
    ok_real_pdf, _ = svc.validate_file("doc.pdf", 10, b"%PDF-1.5 x")
    assert ok_real_pdf is True

    # Streaming over MAX_FILE_SIZE aborts cleanly
    class _TooBigResp:
        status_code = 200
        headers = {"content-type": "application/pdf", "content-length": str(svc.MAX_FILE_SIZE + 5)}

        async def aiter_bytes(self):
            yield b"%PDF"

    class _TooBigCM:
        async def __aenter__(self):
            return _TooBigResp()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    with patch("httpx.AsyncClient.stream", return_value=_TooBigCM()):
        ok, _, _, _ = await svc.download_file_bytes("https://namanapi.namanmarket.com/orders/huge.pdf")
        assert ok is False


def test_haravan_inventory_check_rbac_and_safe_content_disposition(client):
    branch_staff = _make_user(uid="u-stf", username="stf", role=UserRole.STAFF.value, store_id="st-1", store_code="10001")
    app.dependency_overrides[get_current_user] = lambda: branch_staff
    assert client.get("/api/v1/haravan/inventory-check?skus=001").status_code == 403

    admin = _make_user(uid="u-adm", username="adm", role=UserRole.SUPER_ADMIN.value, is_superuser=True)
    app.dependency_overrides[get_current_user] = lambda: admin
    with patch("app.api.v1.haravan.arito_service.get_inventory_by_skus", new_callable=AsyncMock, return_value={"001": {}}):
        assert client.get("/api/v1/haravan/inventory-check?skus=001").status_code == 200

    with (
        patch("app.api.v1.haravan.haravan_order_service.get_order_detail", new_callable=AsyncMock, return_value={"id": "1", "internal": {"order_file": "https://namanapi.namanmarket.com/orders/a.pdf"}}),
        patch("app.api.v1.haravan.file_service.download_file_bytes", new_callable=AsyncMock, return_value=(True, b"%PDF-1.4", "application/pdf", 'evil"; filename=x\\y.pdf')),
    ):
        r_dl = client.get("/api/v1/haravan/orders/1/files/order/download")
        assert r_dl.status_code == 200
        disp = r_dl.headers.get("content-disposition") or ""
        assert 'filename="evil__ filename=y.pdf"' in disp or ";" not in disp.split('filename="', 1)[1].split('"', 1)[0]


@pytest.mark.asyncio
async def test_haravan_order_service_atomic_conflict_and_completed_guard():
    svc = HaravanOrderService(mongo_uri="mongodb://localhost:27017")
    admin = _make_user(is_superuser=True)

    # Completed order cannot be completed twice
    svc.get_order_detail = AsyncMock(return_value={"id": "1", "internal": {"order_processing_status": "Hoàn thành"}})
    with pytest.raises(BadRequestError):
        await svc.complete_order("1", 100.0, 0.0, "Grab", "", "", admin)

    # Race condition where find_one_and_update returns None raises ConflictError (409)
    mock_col = MagicMock()
    mock_col.find_one_and_update.return_value = None
    svc._get_orders_collection = MagicMock(return_value=mock_col)
    svc.get_order_detail = AsyncMock(return_value={"id": "1", "order_number": "#1", "internal": {"order_processing_status": "Đã phân tuyến", "assigned_store": "10001"}})
    with pytest.raises(ConflictError):
        await svc.pack_order("1", admin)


# ==============================================================================
# 6. Inventory Locking, OrderService Savepoint/Hash, Poppies & CSV Sanitization
# ==============================================================================


@pytest.mark.asyncio
async def test_inventory_toggle_update_for_update_and_ch_code_log():
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    store = Store(id="st-1", code="10001", name="TD", is_active=True)
    prod = Product(id="p-1", sku="SKU-1", name="Item 1", base_price=10000.0, is_active=True)
    inv = StoreInventory(
        store_id="st-1",
        product_id="p-1",
        sku="SKU-1",
        total_stock=100.0,
        reserved_stock=90.0,
        available_stock=10.0,
        safety_stock=2.0,
        is_out_of_stock=False,
    )
    mock_db.execute = AsyncMock(side_effect=[_scalar_res(store), _scalar_res(prod), _scalar_res(inv)])
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch.object(InventoryService, "dispatch_stock_to_channels", new_callable=AsyncMock):
        updated, _ = await InventoryService.toggle_item_status(
            InventoryToggleStatusRequest(store_code="10001", sku="SKU-1", is_out_of_stock=False, available_stock=50.0, sync_to_channels=False),
            mock_db,
        )
    # Query must include FOR UPDATE and computed available_stock = max(0, 100 - 90 - 2) = 8.0 (never bypassing reserved!)
    inv_query_str = str(mock_db.execute.call_args_list[2].args[0])
    assert "FOR UPDATE" in inv_query_str.upper()
    assert updated.available_stock == 8.0

    # update_store_inventory locks with FOR UPDATE ordered by product_id
    mock_db.execute = AsyncMock(side_effect=[_scalar_res(store), _scalars_res([prod]), _scalars_res([inv])])
    await InventoryService.update_store_inventory(
        InventoryBatchUpdateRequest(store_id="10001", items=[InventoryStockUpdateItem(sku="SKU-1", total_stock=120.0)], sync_to_channels=False),
        mock_db,
    )
    batch_inv_query = str(mock_db.execute.call_args_list[2].args[0])
    assert "FOR UPDATE" in batch_inv_query.upper()

    # dispatch_stock_to_channels logs ch_code instead of raising NameError on missing mapping
    bad_adapter = MagicMock()
    bad_adapter.sync_inventory = AsyncMock(side_effect=RuntimeError("boom"))
    ch = Channel(id="c-1", code="GRABMART", name="Grab")
    mapping = StoreChannelMapping(store_id="st-1", channel_id="c-1", partner_store_id="P1", channel=ch)
    mock_db.execute = AsyncMock(return_value=_scalars_res([mapping]))
    with patch("app.services.inventory_service.channel_registry.get", return_value=bad_adapter):
        res = await InventoryService.dispatch_stock_to_channels("st-1", [{"sku": "SKU-1"}], mock_db)
    assert res == []


@pytest.mark.asyncio
async def test_order_service_order_code_hash_avoids_tail_collision_and_uses_savepoint():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        db.add_all([
            Channel(id="c-real", code="SHOPEEFOOD", name="SF", is_active=True),
            Store(id="st-real", code="10001", name="TD", is_active=True),
        ])
        await db.commit()

        with patch.object(db, "begin_nested", wraps=db.begin_nested) as spy_nested:
            order_a, created_a = await OrderService.create_or_get_inbound_order(
                channel_code="SHOPEEFOOD",
                partner_store_id="10001",
                channel_order_id="ORDER-ALPHA-123456",
                order_data={"total_amount": 10000.0},
                items_data=[],
                db=db,
            )
            spy_nested.assert_called_once()

        order_b, created_b = await OrderService.create_or_get_inbound_order(
            channel_code="SHOPEEFOOD",
            partner_store_id="10001",
            channel_order_id="ORDER-BETA-123456",
            order_data={"total_amount": 20000.0},
            items_data=[],
            db=db,
        )
        assert created_a is True and created_b is True
        assert order_a.order_code != order_b.order_code
        assert "123456-" in order_a.order_code
        assert "123456-" in order_b.order_code

    await engine.dispose()


@pytest.mark.asyncio
async def test_poppies_prod_urls_no_localhost_fallback_and_toggle_false(monkeypatch):
    from app.services import poppies_client as pc_mod

    monkeypatch.setattr(pc_mod.settings, "ENVIRONMENT", "production")
    cli = PoppiesClient()
    cli.base_url = "https://poppies.namanmarket.com/api/v1/pos/master-data"
    cli.pricing_base_url = "https://poppies.namanmarket.com/api/v1/pricing"
    cli.pos_pricing_base_url = "https://poppies.namanmarket.com/api/v1/pos/pricing"

    for u in cli._get_ump_urls("health") + cli._get_master_data_urls("snapshot"):
        assert "127.0.0.1:6515" not in u
        assert "host.docker.internal:6515" not in u

    called_urls = []

    async def _fail_post(url, **kwargs):
        called_urls.append(url)
        raise RuntimeError("unreachable")

    fake_http = MagicMock()
    fake_http.post = AsyncMock(side_effect=_fail_post)
    with patch.object(cli, "_get_client", return_value=fake_http):
        res = await cli.toggle_channel_publish_status("SKU-1", "GRAB", True)
    assert res["success"] is False
    assert all("127.0.0.1:6515" not in u and "host.docker.internal:6515" not in u for u in called_urls)


@pytest.mark.asyncio
async def test_poppies_webhook_future_ts_and_integrity_error_skips_propagation():
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    with pytest.raises(BadRequestError):
        await poppies_service.process_price_change_webhook(
            db=mock_db,
            payload={"event_id": "evt-fut", "timestamp": future_ts, "channel_code": "GRAB", "affected_skus": ["SKU-1"]},
        )

    # IntegrityError during commit rolls back and skips propagate_skus_to_external_channel
    ch = Channel(id="c-gm", code="GRAB", name="Grab")
    prod = Product(id="p-1", sku="SKU-1", name="X", base_price=10000.0)
    mock_db.execute = AsyncMock(
        side_effect=[
            _scalar_res(None),
            _scalar_res(ch),
            _scalar_res(prod),
            _scalar_res(None),
        ]
    )
    mock_db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))
    mock_db.rollback = AsyncMock()

    with patch("app.services.poppies_webhook_handler.propagate_skus_to_external_channel", new_callable=AsyncMock) as mock_prop:
        res = await poppies_service.process_price_change_webhook(
            db=mock_db,
            payload={
                "event_id": "evt-race",
                "channel_code": "GRAB",
                "affected_skus": ["SKU-1"],
                "prices": {"SKU-1": 12000.0},
            },
        )
    assert res["duplicate"] is True
    assert res["processed_count"] == 0
    mock_db.rollback.assert_awaited_once()
    mock_prop.assert_not_awaited()


@pytest.mark.asyncio
async def test_poppies_sync_master_data_locks_inventory_after_http_fetch():
    svc = PoppiesMasterDataSyncService()
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    st = Store(id="st-1", code="10001", name="TD", is_active=True)
    mock_db.execute = AsyncMock(
        side_effect=[
            _scalars_result := _scalars_res([st]),
            _scalars_res([]),
            _scalars_res([]),
            _scalars_res([]),
        ]
    )
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()

    order_of_calls = []

    async def _fake_snapshot(**kwargs):
        order_of_calls.append("http")
        return {"items": [{"skuCode": "SKU-Z", "productName": "Z", "pricing": {"baseSalePriceVat": 5000}}], "total": 1}

    orig_exec = mock_db.execute

    async def _tracked_exec(stmt, *args, **kwargs):
        order_of_calls.append(("db", str(stmt)))
        return await orig_exec(stmt, *args, **kwargs)

    mock_db.execute = _tracked_exec
    svc.client = MagicMock()
    svc.client.get_snapshot = AsyncMock(side_effect=_fake_snapshot)

    await svc.sync_master_data_from_poppies(mock_db, branch_code="10001", fetch_all=False)
    assert order_of_calls[0] == "http"
    inv_sql = order_of_calls[4][1].upper()
    assert "FOR UPDATE" in inv_sql
    assert "ORDER BY" in inv_sql


def test_report_sanitize_csv_cell():
    for dangerous in ("=CMD()", "+SUM(1)", "-1+1", "@SUM", "\t=1", "\r=1", "\n=1", "  =1+2", "\n\t +3"):
        assert ReportService._sanitize_csv_cell(dangerous).startswith("'")
    assert ReportService._sanitize_csv_cell("Normal Order") == "Normal Order"
