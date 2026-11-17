from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import (
    _validate_cookie_csrf,
    get_client_ip_and_ua,
    get_current_active_user,
    get_current_user,
    get_db,
    require_store_manager,
)
from app.core.database import get_optional_db
from app.core.exceptions import ForbiddenError
from app.core.security import create_access_token, create_partner_token, redact_sensitive_headers
from app.main import app
from app.models.store import Store
from app.models.user import User, UserRole
from app.modules.shopeemart.service import ShopeeMartClient, _redact_url_secrets
from app.services.poppies_client import PoppiesClient


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def staff_user():
    return User(
        id="staff-sec-1",
        username="staff_store1",
        email="staff1@namanmarket.com",
        role=UserRole.STAFF.value,
        store_id="store-uuid-1",
        is_active=True,
        is_superuser=False,
    )


@pytest.fixture
def store1_manager():
    return User(
        id="mgr-sec-1",
        username="mgr_store1",
        email="mgr1@namanmarket.com",
        role=UserRole.STORE_MANAGER.value,
        store_id="store-uuid-1",
        is_active=True,
        is_superuser=False,
    )


def test_unauthenticated_endpoints_fail_closed_401(client):
    """Critical webhooks and internal endpoints must reject unauthenticated requests with 401."""
    app.dependency_overrides.clear()

    assert client.post("/api/v1/pricing/webhook/price-changed", json={"event": "price_update"}).status_code == 401
    assert (
        client.post(
            "/api/v1/pricing/webhook/price-changed",
            json={"event": "price_update"},
            headers={"X-Poppies-Webhook-Key": "wrong-key"},
        ).status_code
        == 401
    )

    assert client.post("/api/v1/grabmart/webhooks/order", json={"orderID": "GM-1"}).status_code == 401
    assert client.get("/api/v1/grabmart/menu?merchantID=GM-1").status_code == 401
    assert (
        client.post(
            "/api/v1/grabmart/oauth/token",
            json={"client_id": "naman_grabmart_pos", "client_secret": "wrong_secret"},
        ).status_code
        == 401
    )

    assert client.post("/api/v1/shopeefood/webhooks/order", json={"order_id": "SF-1"}).status_code == 401
    assert client.post("/shopeemart/webhooks/order", json={"code": 3, "data": {}}).status_code == 401

    assert client.get("/api/v1/pricing/channels").status_code == 401
    assert client.get("/api/v1/system/health").status_code == 401
    assert client.get("/shopeemart/connection-status?shop_id=10001").status_code == 401


def test_rbac_staff_forbidden_on_admin_and_manager_endpoints(client, staff_user):
    """STAFF user must receive 403 on SUPER_ADMIN and STORE_MANAGER endpoints."""
    app.dependency_overrides[get_current_user] = lambda: staff_user
    app.dependency_overrides[get_current_active_user] = lambda: staff_user
    try:
        assert (
            client.patch(
                "/api/v1/channels/GRABMART/features",
                json={"auto_accept": True},
            ).status_code
            == 403
        )
        assert client.post("/api/v1/channels/GRABMART/sync-menu").status_code == 403
        assert client.get("/api/v1/system/errors").status_code == 403
        assert client.post("/api/v1/system/test-error").status_code == 403
        assert (
            client.post(
                "/api/v1/shopeemart/credentials",
                json={"partner_id": 123, "partner_key": "abc", "shop_id": "10001"},
            ).status_code
            == 403
        )
        assert client.post("/api/v1/inventory/seed").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_cross_store_idor_forbidden_403(client, store1_manager):
    """Store manager for store 10001 cannot access or sync another store (10002)."""
    other_store = Store(id="store-uuid-2", code="10002", name="Nam An 10002", is_active=True)

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = other_store
    mock_db.execute.return_value = mock_result

    app.dependency_overrides[get_current_user] = lambda: store1_manager
    app.dependency_overrides[get_current_active_user] = lambda: store1_manager
    app.dependency_overrides[require_store_manager] = lambda: store1_manager
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        r_store = client.get("/api/v1/stores/store-uuid-2")
        assert r_store.status_code == 403

        with patch("app.api.v1.channels.channel_registry.get", return_value=MagicMock()):
            r_sync = client.post("/api/v1/channels/GRABMART/sync-menu?store_id=store-uuid-2")
        assert r_sync.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_shopeemart_oauth_callback_csrf_and_xss_rejected(client):
    """OAuth callback must reject missing/invalid state (401) and non-numeric shop_id (400)."""
    r_no_state = client.get("/api/v1/shopeemart/auth/callback?code=abc&shop_id=10001")
    assert r_no_state.status_code == 401

    r_bad_state = client.get("/api/v1/shopeemart/auth/callback?code=abc&shop_id=10001&state=forged")
    assert r_bad_state.status_code == 401

    valid_state = create_partner_token("shopeemart_oauth", "admin", expires_seconds=300)
    r_xss = client.get(
        f"/api/v1/shopeemart/auth/callback?code=abc&shop_id=%3Cscript%3Ealert(1)%3C/script%3E&state={valid_state}"
    )
    assert r_xss.status_code == 400


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1/secret.png",
        "http://localhost/admin.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal.jpg",
        "file:///etc/passwd",
        "ftp://example.com/image.png",
    ],
)
def test_shopeemart_ssrf_url_validation_blocks_internal_and_non_http(bad_url):
    """_validate_external_image_url must reject loopback, link-local, RFC1918, and non-HTTP schemes."""
    with pytest.raises(ValueError):
        ShopeeMartClient._validate_external_image_url(bad_url)


def test_redact_sensitive_headers_masks_credentials():
    """Sensitive headers must be redacted before logging or audit persistence."""
    raw = {
        "Authorization": "Bearer super-secret-jwt",
        "Cookie": "naman_access_token=xyz",
        "X-Poppies-Webhook-Key": "poppies-secret",
        "X-Api-Key": "api-secret",
        "Content-Type": "application/json",
    }
    redacted = redact_sensitive_headers(raw)
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["Cookie"] == "[REDACTED]"
    assert redacted["X-Poppies-Webhook-Key"] == "[REDACTED]"
    assert redacted["X-Api-Key"] == "[REDACTED]"
    assert redacted["Content-Type"] == "application/json"


def test_access_token_rejects_mismatched_token_version(client, staff_user):
    """Access token with stale `ver` (after logout/password change) must be rejected with 401."""
    app.dependency_overrides.clear()
    staff_user.token_version = 2
    stale_token, _ = create_access_token(
        {"sub": staff_user.id, "username": staff_user.username, "role": staff_user.role, "ver": 1}
    )

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = staff_user
    mock_db.execute.return_value = mock_result

    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {stale_token}"})
        assert res.status_code == 401
        assert res.json()["error_code"] == "UNAUTHORIZED"
    finally:
        app.dependency_overrides.clear()


def test_cookie_auth_post_rejects_missing_or_mismatched_origin_403(client, staff_user):
    """Cookie-authenticated state-changing POST without matching Origin/Referer must fail CSRF check (403)."""
    app.dependency_overrides.clear()
    staff_user.token_version = 0
    cookie_token, _ = create_access_token(
        {"sub": staff_user.id, "username": staff_user.username, "role": staff_user.role, "ver": 0}
    )

    client.cookies.set("naman_access_token", cookie_token)
    try:
        res_no_origin = client.post("/api/v1/auth/logout", json={})
        assert res_no_origin.status_code == 403
        assert res_no_origin.json()["error_code"] == "FORBIDDEN"

        res_bad_origin = client.post(
            "/api/v1/auth/logout",
            json={},
            headers={"Origin": "https://evil.example.com"},
        )
        assert res_bad_origin.status_code == 403
        assert res_bad_origin.json()["error_code"] == "FORBIDDEN"
    finally:
        client.cookies.clear()
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_poppies_client_get_sku_blocks_path_traversal():
    """PoppiesClient.get_sku must raise ValueError on path traversal payloads."""
    poppies = PoppiesClient()
    with pytest.raises(ValueError):
        await poppies.get_sku("../admin")


def test_shopeemart_redact_url_secrets_masks_access_token_and_sign():
    """_redact_url_secrets must mask access_token and sign query parameters."""
    raw_url = "https://partner.shopeemart.vn/api/v2/product/get_item?partner_id=123&access_token=secret_tok_999&sign=hmac_abc_123&shop_id=10001"
    redacted = _redact_url_secrets(raw_url)
    assert "secret_tok_999" not in redacted
    assert "hmac_abc_123" not in redacted
    assert "access_token=[REDACTED]" in redacted
    assert "sign=[REDACTED]" in redacted
    assert "shop_id=10001" in redacted


def test_shopeemart_oauth_callback_single_use_state_and_no_raw_tokens(client):
    """Callback must omit raw tokens in JSON response and reject replayed single-use state (401)."""
    app.dependency_overrides.clear()
    app.dependency_overrides[get_optional_db] = lambda: None
    single_use_state = create_partner_token("shopeemart_oauth", "admin", expires_seconds=300)

    try:
        with (
            patch(
                "app.modules.shopeemart.router.shopeemart_client.exchange_code_for_token",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        access_token="raw_secret_access_token",
                        refresh_token="raw_secret_refresh_token",
                        expire_in=14400,
                    )
                ),
            ),
            patch("app.modules.shopeemart.router.shopeemart_client.save_credentials"),
        ):
            r_ok = client.get(
                f"/api/v1/shopeemart/auth/callback?code=authcode123&shop_id=10001&state={single_use_state}&format=json"
            )
            assert r_ok.status_code == 200
            body = r_ok.json()
            assert body["success"] is True
            assert "raw_secret_access_token" not in r_ok.text
            assert "raw_secret_refresh_token" not in r_ok.text
            assert "access_token" not in body["data"]
            assert "refresh_token" not in body["data"]

            r_replay = client.get(
                f"/api/v1/shopeemart/auth/callback?code=authcode123&shop_id=10001&state={single_use_state}&format=json"
            )
            assert r_replay.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_store_manager_forbidden_on_super_admin_only_endpoints(client, store1_manager):
    """STORE_MANAGER must receive 403 on SUPER_ADMIN-only sync-poppies and system error resolve endpoints."""
    app.dependency_overrides[get_current_user] = lambda: store1_manager
    app.dependency_overrides[get_current_active_user] = lambda: store1_manager
    try:
        r_sync = client.post("/api/v1/channels/sync-poppies")
        assert r_sync.status_code == 403

        r_resolve = client.post(
            "/api/v1/system/errors/err-1/resolve",
            json={"resolution_notes": "Attempted by manager"},
        )
        assert r_resolve.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_staff_without_store_id_gets_empty_stores_list(client):
    """STAFF with store_id=None must receive [] from GET /api/v1/stores."""
    unassigned_staff = User(
        id="staff-no-store",
        username="staff_unassigned",
        email="unassigned@namanmarket.com",
        role=UserRole.STAFF.value,
        store_id=None,
        is_active=True,
        is_superuser=False,
    )
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: unassigned_staff
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        res = client.get("/api/v1/stores")
        assert res.status_code == 200
        assert res.json()["success"] is True
        assert res.json()["data"] == []
        mock_db.execute.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_unassigned_staff_rejected_403_on_channel_sync_endpoints(client):
    """STAFF with store_id=None must receive 403 on GrabMart, ShopeeMart, and ShopeeFood sync endpoints."""
    unassigned_staff = User(
        id="staff-no-store-sync",
        username="staff_unassigned_sync",
        email="unassigned_sync@namanmarket.com",
        role=UserRole.STAFF.value,
        store_id=None,
        is_active=True,
        is_superuser=False,
    )
    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = None
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    app.dependency_overrides[get_current_user] = lambda: unassigned_staff
    app.dependency_overrides[get_current_active_user] = lambda: unassigned_staff
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        assert client.post("/api/v1/grabmart/stores/st-10001/sync-menu").status_code == 403
        assert client.post("/api/v1/shopeemart/sync/menu/st-10001").status_code == 403
        assert client.post("/api/v1/shopeefood/sync/menu/10001").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_read_only_and_staff_rejected_403_on_shopeemart_create_awb_and_add_item(client, staff_user):
    """READ_ONLY and STAFF must receive 403 on ShopeeMart create-awb and add-item."""
    read_only_user = User(
        id="ro-sec-1",
        username="readonly_user",
        email="ro@namanmarket.com",
        role=UserRole.READ_ONLY.value,
        store_id="store-uuid-1",
        is_active=True,
        is_superuser=False,
    )

    awb_payload = {"document_type": "THERMAL_AIR_WAYBILL"}
    item_payload = {
        "item_name": "Test Item",
        "description": "Desc",
        "category_id": 100201,
        "original_price": 50000.0,
        "normal_stock": 10,
        "seller_sku": "SKU-TEST",
    }

    for user in (read_only_user, staff_user):
        app.dependency_overrides[get_current_user] = lambda u=user: u
        app.dependency_overrides[get_current_active_user] = lambda u=user: u
        try:
            assert (
                client.post(
                    "/api/v1/shopeemart/orders/SHP123/create-awb?shop_id=10001",
                    json=awb_payload,
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/v1/shopeemart/product/add-item?shop_id=10001",
                    json=item_payload,
                ).status_code
                == 403
            )
        finally:
            app.dependency_overrides.clear()


def test_haravan_order_file_upload_rejects_unauthorized_staff_before_upload(client, staff_user):
    """POST /api/v1/haravan/orders/{order_id}/files must reject unauthorized STAFF before calling upload_file."""
    app.dependency_overrides[get_current_user] = lambda: staff_user
    try:
        with (
            patch(
                "app.api.v1.haravan.haravan_order_service.get_order_detail",
                new=AsyncMock(side_effect=ForbiddenError("Cross-store forbidden")),
            ),
            patch(
                "app.api.v1.haravan.file_service.upload_file",
                new=AsyncMock(),
            ) as mock_upload,
        ):
            res = client.post(
                "/api/v1/haravan/orders/hrv-ord-999/files/upload",
                data={"file_type": "order"},
                files={"file": ("invoice.pdf", b"%PDF-1.4 test", "application/pdf")},
            )
            assert res.status_code == 403
            mock_upload.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_get_client_ip_and_ua_ignores_malformed_x_forwarded_for():
    """get_client_ip_and_ua must ignore malformed non-IP X-Forwarded-For entries and pick valid untrusted IP."""
    mock_request = MagicMock()
    mock_request.client.host = "127.0.0.1"
    mock_request.headers = {
        "x-forwarded-for": "not-an-ip, 203.0.113.45, garbage_host, 127.0.0.1",
        "user-agent": "pytest-agent",
    }

    ip, ua = get_client_ip_and_ua(mock_request)
    assert ip == "203.0.113.45"
    assert ua == "pytest-agent"

    # When all X-Forwarded-For entries are malformed, fall back to direct peer IP
    mock_request_bad = MagicMock()
    mock_request_bad.client.host = "127.0.0.1"
    mock_request_bad.headers = {"x-forwarded-for": "spoofed-host, <script>", "user-agent": "ua"}
    ip_fallback, _ = get_client_ip_and_ua(mock_request_bad)
    assert ip_fallback == "127.0.0.1"


def test_validate_cookie_csrf_rejects_http_origin_when_only_https_allowed():
    """_validate_cookie_csrf must reject http:// origin when ALLOWED_ORIGINS only allows https://."""
    mock_request = MagicMock()
    mock_request.headers = {"origin": "http://ump.namanmarket.com"}
    mock_request.url.scheme = "https"
    mock_request.url.netloc = "ump.namanmarket.com"

    with (
        patch("app.api.deps.settings.ALLOWED_ORIGINS", ["https://ump.namanmarket.com"]),
        patch("app.api.deps.settings.ENVIRONMENT", "production"),
    ):
        with pytest.raises(ForbiddenError):
            _validate_cookie_csrf(mock_request)
