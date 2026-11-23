from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import (
    get_client_ip_and_ua,
    get_current_user,
    get_db,
    has_cross_store_read_access,
    has_financial_global_read_access,
    require_financial_reader,
    require_head_office_manager,
    require_store_manager,
)
from app.api.v1.auth import reset_auth_rate_limiter
from app.core.database import Base, get_optional_db
from app.core.exceptions import ConflictError, ForbiddenError, UnauthorizedError, ValidationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    redact_sensitive_headers,
    verify_password,
)
from app.main import app
from app.models.channel import Channel
from app.models.store import Store, StoreChannelMapping
from app.models.user import AuditSecurityLog, RefreshToken, User, UserRole
from app.schemas.auth import LoginRequest
from app.schemas.user import UserCreate, UserUpdate
from app.services.auth_service import auth_service
from app.services.hr_sync_worker import HRSyncWorker


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


async def _make_sqlite_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory


def _user(
    *,
    uid: str = "u-1",
    username: str = "user1",
    role: UserRole = UserRole.STAFF,
    store_id: str | None = None,
    store: Store | None = None,
    is_active: bool = True,
    portal_disabled: bool = False,
    is_superuser: bool = False,
    token_version: int = 0,
) -> User:
    u = User(
        id=uid,
        username=username,
        email=f"{username}@namanmarket.com",
        hashed_password=hash_password("StrongPass123!"),
        full_name=f"User {username}",
        role=role.value,
        store_id=store_id,
        is_active=is_active,
        portal_disabled=portal_disabled,
        is_superuser=is_superuser,
        token_version=token_version,
    )
    if store is not None:
        u.store = store
    return u


# ---------------------------------------------------------------------------
# 1. Model & Alembic Migration
# ---------------------------------------------------------------------------


def test_user_role_hq_auditor_and_migration_chain():
    assert UserRole.HQ_AUDITOR.value == "HQ_AUDITOR"
    assert hasattr(User, "portal_disabled")

    mig_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "d4e5f6a7b8c9_add_user_portal_disabled.py"
    )
    assert mig_path.exists()
    content = mig_path.read_text(encoding="utf-8")
    assert "revision: str = 'd4e5f6a7b8c9'" in content
    assert "down_revision: Union[str, None] = 'c3d4e5f6a7b8'" in content
    assert "portal_disabled" in content
    assert "drop_column" in content


# ---------------------------------------------------------------------------
# 2. Security Core: decode_token & header redaction
# ---------------------------------------------------------------------------


def test_decode_token_hides_internal_exception_details_and_redacts_headers():
    with pytest.raises(UnauthorizedError) as exc_info:
        decode_token("not.a.jwt")
    assert exc_info.value.details == {"reason": "TOKEN_INVALID"}
    assert "detail" not in exc_info.value.details

    redacted = redact_sensitive_headers(
        {
            "X-POS-API-Key": "pos-secret",
            "X-Foody-Signature": "sig-foody",
            "X-Signature": "sig-generic",
            "X-Request-Id": "req-123",
        }
    )
    assert redacted["X-POS-API-Key"] == "[REDACTED]"
    assert redacted["X-Foody-Signature"] == "[REDACTED]"
    assert redacted["X-Signature"] == "[REDACTED]"
    assert redacted["X-Request-Id"] == "req-123"


# ---------------------------------------------------------------------------
# 3. Deps: portal_disabled, RBAC helpers, Trusted Proxy IP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_user_blocks_portal_disabled_and_rbac_helpers():
    engine, session_factory = await _make_sqlite_session()
    try:
        async with session_factory() as db:
            disabled_user = _user(
                uid="u-portal-off",
                username="portal_off",
                role=UserRole.STAFF,
                is_active=True,
                portal_disabled=True,
            )
            db.add(disabled_user)
            await db.commit()

            tok, _ = create_access_token({"sub": disabled_user.id, "ver": 0})
            req = MagicMock()
            req.cookies = {}
            req.headers = {}
            req.method = "GET"

            with pytest.raises(UnauthorizedError) as exc_info:
                await get_current_user(request=req, token=tok, db=db)
            assert "Portal" in exc_info.value.message
    finally:
        await engine.dispose()

    ro_unassigned = _user(uid="ro-0", username="ro0", role=UserRole.READ_ONLY, store_id=None)
    cs_user = _user(uid="cs-0", username="cs0", role=UserRole.CS, store_id=None)
    hq_auditor = _user(uid="aud-0", username="aud0", role=UserRole.HQ_AUDITOR, store_id=None)
    ho_mgr = _user(uid="ho-0", username="ho0", role=UserRole.STORE_MANAGER, store_id=None)
    br_mgr = _user(uid="br-0", username="br0", role=UserRole.STORE_MANAGER, store_id="st-10001")
    admin = _user(uid="adm-0", username="adm0", role=UserRole.SUPER_ADMIN, is_superuser=True)

    # Cross-store operational read: READ_ONLY(None) fails closed!
    assert has_cross_store_read_access(ro_unassigned) is False
    assert has_cross_store_read_access(cs_user) is True
    assert has_cross_store_read_access(hq_auditor) is True
    assert has_cross_store_read_access(ho_mgr) is True
    assert has_cross_store_read_access(br_mgr) is False
    assert has_cross_store_read_access(admin) is True

    # Financial global read: excludes CS & unassigned READ_ONLY
    assert has_financial_global_read_access(ro_unassigned) is False
    assert has_financial_global_read_access(cs_user) is False
    assert has_financial_global_read_access(hq_auditor) is True
    assert has_financial_global_read_access(ho_mgr) is True
    assert has_financial_global_read_access(br_mgr) is False
    assert has_financial_global_read_access(admin) is True

    # require_head_office_manager & require_financial_reader
    assert await require_head_office_manager(ho_mgr) is ho_mgr
    assert await require_head_office_manager(admin) is admin
    with pytest.raises(ForbiddenError):
        await require_head_office_manager(br_mgr)

    assert await require_financial_reader(hq_auditor) is hq_auditor
    assert await require_financial_reader(br_mgr) is br_mgr
    for denied in (cs_user, ro_unassigned):
        with pytest.raises(ForbiddenError):
            await require_financial_reader(denied)


def test_get_client_ip_ignores_untrusted_proxy_xff():
    untrusted_req = MagicMock()
    untrusted_req.client.host = "203.0.113.10"
    untrusted_req.headers = {"x-forwarded-for": "198.51.100.77", "user-agent": "ua"}
    ip, _ = get_client_ip_and_ua(untrusted_req)
    assert ip == "203.0.113.10"


# ---------------------------------------------------------------------------
# 4. Auth Endpoints: CSRF on cookie refresh, unified rate limit, /token prod, LoginRequest
# ---------------------------------------------------------------------------


def test_auth_refresh_cookie_csrf_unified_ratelimit_token_prod_and_schema(client):
    admin = _user(uid="adm-auth", username="admin", role=UserRole.SUPER_ADMIN, is_superuser=True)
    token_resp = SimpleNamespace(
        access_token="acc.jwt",
        refresh_token="ref.jwt",
        token_type="bearer",
        expires_in=3600,
        user=admin,
    )
    from app.schemas.auth import TokenResponse
    from app.schemas.user import UserResponse

    valid_resp = TokenResponse(
        access_token="acc.jwt",
        refresh_token="ref.jwt",
        token_type="bearer",
        expires_in=3600,
        user=UserResponse.model_validate(admin),
    )

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        # 1. Cookie refresh without Origin/Referer -> 403
        client.cookies.set("naman_refresh_token", "cookie-refresh-tok")
        r_no_origin = client.post("/api/v1/auth/refresh", json={})
        assert r_no_origin.status_code == 403

        # 2. Cookie refresh with allowed Origin -> 200
        with patch("app.api.v1.auth.auth_service.refresh_access_token", new=AsyncMock(return_value=valid_resp)):
            r_ok = client.post(
                "/api/v1/auth/refresh",
                json={},
                headers={"Origin": "http://localhost:3002"},
            )
        assert r_ok.status_code == 200
        client.cookies.clear()

        # 3. Unified rate-limit bucket across /login and /token
        reset_auth_rate_limiter()
        with (
            patch("app.api.v1.auth.settings.ENVIRONMENT", "production"),
            patch("app.api.v1.auth.settings.DEBUG", True),
            patch(
                "app.api.v1.auth.auth_service.authenticate_user",
                new=AsyncMock(return_value=(admin, valid_resp)),
            ),
        ):
            for _ in range(6):
                assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"}).status_code == 200
            for _ in range(4):
                assert client.post("/api/v1/auth/token", data={"username": "admin", "password": "pw"}).status_code == 200
            # 11th request hits shared auth_login bucket -> 429
            assert client.post("/api/v1/auth/token", data={"username": "admin", "password": "pw"}).status_code == 429
        reset_auth_rate_limiter()

        # 4. /token disabled (404) in production when not DEBUG
        with (
            patch("app.api.v1.auth.settings.ENVIRONMENT", "production"),
            patch("app.api.v1.auth.settings.DEBUG", False),
        ):
            r_tok_prod = client.post("/api/v1/auth/token", data={"username": "admin", "password": "pw"})
            assert r_tok_prod.status_code == 404
    finally:
        client.cookies.clear()
        app.dependency_overrides.clear()

    # 5. LoginRequest bounds & control chars
    req_ok = LoginRequest(username="  admin  ", password="secret")
    assert req_ok.username == "admin"
    with pytest.raises(PydanticValidationError):
        LoginRequest(username="a" * 101, password="secret")
    with pytest.raises(PydanticValidationError):
        LoginRequest(username="admin\x00", password="secret")
    with pytest.raises(PydanticValidationError):
        LoginRequest(username="admin", password="p" * 129)


# ---------------------------------------------------------------------------
# 5. Users Endpoints: self-disable/demote, last SUPER_ADMIN, seed prod guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_users_self_protection_last_superadmin_and_seed_prod_guard(client):
    engine, session_factory = await _make_sqlite_session()
    try:
        async with session_factory() as db:
            adm1 = _user(uid="adm-1", username="admin1", role=UserRole.SUPER_ADMIN, is_superuser=True)
            db.add(adm1)
            await db.commit()

            async def _override_db():
                yield db

            app.dependency_overrides[get_current_user] = lambda: adm1
            app.dependency_overrides[get_db] = _override_db

            # Self-disable / self-demote blocked (403)
            assert client.delete(f"/api/v1/users/{adm1.id}").status_code == 403
            assert client.put(f"/api/v1/users/{adm1.id}", json={"is_active": False}).status_code == 403
            assert client.put(f"/api/v1/users/{adm1.id}", json={"role": "STAFF"}).status_code == 403

            # Another superadmin trying to disable/demote the last active SUPER_ADMIN -> 409
            adm2 = _user(uid="adm-2", username="admin2", role=UserRole.SUPER_ADMIN, is_superuser=True)
            app.dependency_overrides[get_current_user] = lambda: adm2
            assert client.delete(f"/api/v1/users/{adm1.id}").status_code == 409
            assert client.put(f"/api/v1/users/{adm1.id}", json={"role": "STORE_MANAGER"}).status_code == 409

            # seed-sample-staff blocked in production when not DEBUG
            with (
                patch("app.api.v1.users.settings.ENVIRONMENT", "production"),
                patch("app.api.v1.users.settings.DEBUG", False),
            ):
                assert client.post("/api/v1/users/seed-sample-staff").status_code == 403
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 6 & 7. Orders, Products, Pricing, Inventory, Reports, Stores, System, Health
# ---------------------------------------------------------------------------


def test_unassigned_readonly_orders_fail_closed(client):
    ro = _user(uid="ro-none", username="ro_none", role=UserRole.READ_ONLY, store_id=None)
    app.dependency_overrides[get_current_user] = lambda: ro
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        r_list = client.get("/api/v1/orders")
        assert r_list.status_code == 200
        assert r_list.json()["data"]["items"] == []
        assert r_list.json()["data"]["meta"]["total_items"] == 0

        r_sum = client.get("/api/v1/orders/summary")
        assert r_sum.status_code == 200
        assert r_sum.json()["data"]["pending_count"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_head_office_and_store_isolation_on_products_pricing_inventory(client):
    engine, session_factory = await _make_sqlite_session()
    try:
        async with session_factory() as db:
            st1 = Store(id="st-10001", code="10001", name="Nam An 10001", is_active=True)
            st4 = Store(id="st-10004", code="10004", name="Nam An 10004", is_active=True)
            db.add_all([st1, st4])
            await db.commit()

            async def _override_db():
                yield db

            br1_mgr = _user(
                uid="mgr-1",
                username="mgr_10001",
                role=UserRole.STORE_MANAGER,
                store_id="st-10001",
                store=st1,
            )
            ho_mgr = _user(
                uid="mgr-ho",
                username="mgr_ho",
                role=UserRole.STORE_MANAGER,
                store_id=None,
            )
            admin = _user(uid="adm-sys", username="admin_sys", role=UserRole.SUPER_ADMIN, is_superuser=True)

            app.dependency_overrides[get_db] = _override_db
            app.dependency_overrides[get_current_user] = lambda: br1_mgr

            # Branch manager blocked from head-office catalog/channel/sync mutations
            assert client.post("/api/v1/products/categories", json={"code": "C1", "name": "Cat 1"}).status_code == 403
            assert client.post("/api/v1/products", json={"sku": "S1", "name": "Prod 1", "base_price": 1000}).status_code == 403
            assert client.post("/api/v1/products/by-sku/S1/channels/GRAB/toggle", json={"is_published": True}).status_code == 403
            assert client.post("/api/v1/pricing/channel-publish-status", json={"sku": "S1", "scopeCode": "GRAB", "isPublished": True}).status_code == 403
            assert client.post("/api/v1/pricing/sync-poppies?branch_code=10001").status_code == 403
            assert client.post("/api/v1/inventory/poppies/sync?branch_code=10001").status_code == 403
            assert client.post("/api/v1/inventory/poppies/sync-and-match?branch_code=10001").status_code == 403

            # Branch 10001 manager blocked on 10004 and unknown 99999 via real Store lookup
            assert client.post("/api/v1/pricing/snapshots/sync?branch_code=10004").status_code == 403
            assert client.post("/api/v1/pricing/snapshots/sync?branch_code=99999").status_code == 403
            assert client.post(
                "/api/v1/inventory/batch-update",
                json={"store_id": "10004", "items": [{"sku": "S1", "total_stock": 5}]},
            ).status_code == 403
            assert client.post("/api/v1/inventory/poppies/sync-stock?store_code=10004").status_code == 403

            # Branch 10001 manager allowed on own store 10001
            with (
                patch(
                    "app.api.v1.pricing.poppies_service.sync_channel_pricing_snapshot",
                    new=AsyncMock(
                        return_value={
                            "success": True,
                            "branch_code": "10001",
                            "channel_code": "STORE",
                            "prices_updated": 0,
                            "blocked_skus_count": 0,
                            "has_partner_pricing": False,
                            "is_active": True,
                        }
                    ),
                ),
                patch(
                    "app.api.v1.inventory.poppies_service.sync_stock_cttk_realtime",
                    new=AsyncMock(return_value={"inventories_updated": 1}),
                ),
                patch(
                    "app.api.v1.inventory.InventoryService.update_store_inventory",
                    new=AsyncMock(return_value=[]),
                ),
            ):
                assert client.post("/api/v1/pricing/snapshots/sync?branch_code=10001").status_code == 200
                assert client.post("/api/v1/inventory/poppies/sync-stock?store_code=10001").status_code == 200
                assert client.post(
                    "/api/v1/inventory/batch-update",
                    json={"store_id": "10001", "items": [{"sku": "S1", "total_stock": 5}]},
                ).status_code == 200

            # Head-office manager allowed on HO endpoints
            app.dependency_overrides[get_current_user] = lambda: ho_mgr
            with patch(
                "app.api.v1.inventory.poppies_service.sync_master_data_from_poppies",
                new=AsyncMock(return_value={"total_fetched": 10}),
            ):
                assert client.post("/api/v1/inventory/poppies/sync?branch_code=10001").status_code == 200

            # Inventory seed blocked in production when not DEBUG
            app.dependency_overrides[get_current_user] = lambda: admin
            with (
                patch("app.api.v1.inventory.settings.ENVIRONMENT", "production"),
                patch("app.api.v1.inventory.settings.DEBUG", False),
            ):
                assert client.post("/api/v1/inventory/seed").status_code == 403
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_reports_stores_mappings_system_and_health_hardening(client):
    engine, session_factory = await _make_sqlite_session()
    try:
        async with session_factory() as db:
            st1 = Store(id="st-1", code="10001", name="Thảo Điền", is_active=True)
            ch = Channel(id="ch-1", code="GRAB", name="GrabMart", is_active=True)
            db.add_all([st1, ch])
            await db.flush()
            db.add(StoreChannelMapping(id="m-1", store_id=st1.id, channel_id=ch.id, partner_store_id="P-10001", is_active=True))
            await db.commit()

            async def _override_db():
                yield db

            cs = _user(uid="cs-1", username="cs1", role=UserRole.CS)
            ro = _user(uid="ro-1", username="ro1", role=UserRole.READ_ONLY)
            auditor = _user(uid="aud-1", username="aud1", role=UserRole.HQ_AUDITOR)
            br_mgr = _user(uid="bm-1", username="bm1", role=UserRole.STORE_MANAGER, store_id="st-1", store=st1)

            app.dependency_overrides[get_db] = _override_db

            # CS & READ_ONLY blocked from financial reconciliation/export & system health & mappings leak
            for u in (cs, ro):
                app.dependency_overrides[get_current_user] = lambda usr=u: usr
                assert client.get("/api/v1/reports/reconciliation").status_code == 403
                assert client.get("/api/v1/reports/export").status_code == 403
                assert client.get("/api/v1/system/health").status_code == 403
                r_map = client.get("/api/v1/stores/mappings")
                assert r_map.status_code == 200
                assert r_map.json()["data"] == []

            # HQ_AUDITOR can read financial reports & mappings, but NOT system/health
            app.dependency_overrides[get_current_user] = lambda: auditor
            assert client.get("/api/v1/system/health").status_code == 403
            r_map_aud = client.get("/api/v1/stores/mappings")
            assert r_map_aud.status_code == 200
            assert len(r_map_aud.json()["data"]) == 1

            with (
                patch("app.api.v1.reports.report_service.get_full_analytics", new=AsyncMock(return_value={"summary": {}, "channels": [], "stores": [], "daily_trends": []})) as mock_an,
                patch("app.api.v1.reports.report_service.get_reconciliation_orders", new=AsyncMock(return_value=([], 0))),
                patch("app.api.v1.reports.report_service.export_reconciliation_csv", new=AsyncMock(return_value="\ufeffhdr\n")),
            ):
                assert client.get("/api/v1/reports/analytics").status_code == 200
                assert mock_an.await_args.kwargs["store_code"] is None
                assert client.get("/api/v1/reports/reconciliation").status_code == 200
                assert client.get("/api/v1/reports/export").status_code == 200

            # Branch manager scoped to own store on financial reports, denied on system/health
            app.dependency_overrides[get_current_user] = lambda: br_mgr
            assert client.get("/api/v1/system/health").status_code == 403
            with patch("app.api.v1.reports.report_service.get_reconciliation_orders", new=AsyncMock(return_value=([], 0))) as mock_rec:
                assert client.get("/api/v1/reports/reconciliation?store_code=10004").status_code == 200
                assert mock_rec.await_args.kwargs["store_code"] == "10001"

            # system/test-error disabled in production when not DEBUG
            admin = _user(uid="adm-s", username="admin_s", role=UserRole.SUPER_ADMIN, is_superuser=True)
            app.dependency_overrides[get_current_user] = lambda: admin
            with (
                patch("app.api.v1.system.settings.ENVIRONMENT", "production"),
                patch("app.api.v1.system.settings.DEBUG", False),
            ):
                assert client.post("/api/v1/system/test-error").status_code == 403

            # /api/v1/health minimal payload when DEBUG=False
            with patch("app.api.v1.health.settings.DEBUG", False):
                r_hlth = client.get("/api/v1/health")
                assert r_hlth.status_code == 200
                assert r_hlth.json()["data"] == {"status": "UP"}
            with patch("app.api.v1.health.settings.DEBUG", True):
                r_hlth_dbg = client.get("/api/v1/health")
                assert r_hlth_dbg.status_code == 200
                assert "environment" in r_hlth_dbg.json()["data"]
                assert "registered_channels" in r_hlth_dbg.json()["data"]
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 8. AuthService & HRSyncWorker Hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_service_hardening_portal_disabled_hr_sync_and_audit():
    # Sentinel password check
    mapped = auth_service._extract_mongo_fields({"username": "no_pw", "status": "active"}, {})
    assert mapped["hashed_password"] == "!DISABLED!"
    assert verify_password("anything", mapped["hashed_password"]) is False

    # _list_active_mongo_users_sync filters MarkForDelete
    mock_col = MagicMock()
    mock_col.find.return_value = []
    with patch.object(auth_service, "_get_mongo_user_collection", return_value=mock_col):
        auth_service._list_active_mongo_users_sync()
    mongo_filter = mock_col.find.call_args[0][0]
    assert mongo_filter["MarkForDelete"] == {"$ne": True}

    engine, session_factory = await _make_sqlite_session()
    try:
        async with session_factory() as db:
            # Case-insensitive create_user + AuditSecurityLog
            with patch("app.services.auth_service.settings.AUTH_MONGO_ENABLED", False):
                created = await auth_service.create_user(
                    db,
                    UserCreate(
                        username="  Staff.One  ",
                        email="STAFF.ONE@namanmarket.com",
                        full_name="Staff One",
                        role=UserRole.STAFF,
                        password="Password123!",
                    ),
                )
                assert created.username == "Staff.One"
                with pytest.raises(ConflictError):
                    await auth_service.create_user(
                        db,
                        UserCreate(
                            username="staff.one",
                            email="other@namanmarket.com",
                            full_name="Dup",
                            role=UserRole.STAFF,
                            password="Password123!",
                        ),
                    )

            # Password reset rejected when AUTH_MONGO_ENABLED=True
            with patch("app.services.auth_service.settings.AUTH_MONGO_ENABLED", True):
                with pytest.raises(ValidationError):
                    await auth_service.update_user(db, created.id, UserUpdate(password="NewPass123!"))

            # Add refresh token and deactivate_user -> sets portal_disabled=True, bumps token_version, revokes refresh tokens
            raw_rt, hash_rt, exp_rt = create_refresh_token({"sub": created.id})
            db.add(RefreshToken(user_id=created.id, token_hash=hash_rt, expires_at=exp_rt))
            await db.commit()

            prev_ver = created.token_version
            deactivated = await auth_service.deactivate_user(db, created.id)
            assert deactivated.is_active is False
            assert deactivated.portal_disabled is True
            assert deactivated.token_version == prev_ver + 1
            rt_row = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_rt))).scalar_one()
            assert rt_row.revoked_at is not None

            # HR sync must NOT reactivate a portal_disabled account
            resynced = await auth_service._upsert_pg_shadow_user(
                db,
                {"username": "Staff.One", "status": "active", "fullname": "Staff One HR"},
                store_code_to_id={},
            )
            await db.commit()
            assert resynced.portal_disabled is True
            assert resynced.is_active is False

            # Reactivating clears portal_disabled
            reactivated = await auth_service.update_user(db, created.id, UserUpdate(is_active=True))
            assert reactivated.is_active is True
            assert reactivated.portal_disabled is False

            # Verify AuditSecurityLog entries recorded
            logs = (await db.execute(select(AuditSecurityLog).where(AuditSecurityLog.user_id == created.id))).scalars().all()
            ev_types = {l.event_type for l in logs}
            assert {"USER_CREATED", "USER_DEACTIVATED", "USER_REACTIVATED"}.issubset(ev_types)

            # sync_users_from_mongodb with purge_demo_accounts=False still deactivates resigned staff & revokes tokens
            resigned = _user(uid="u-resigned", username="nv.resigned", role=UserRole.CS, is_active=True)
            local_admin = _user(uid="u-adm-local", username="admin", role=UserRole.SUPER_ADMIN, is_active=True, is_superuser=True)
            db.add_all([resigned, local_admin])
            _, hash_resigned_rt, exp_resigned_rt = create_refresh_token({"sub": resigned.id})
            db.add(RefreshToken(user_id=resigned.id, token_hash=hash_resigned_rt, expires_at=exp_resigned_rt))
            await db.commit()

            with patch.object(
                auth_service,
                "_list_active_mongo_users_sync",
                return_value=[{"username": "Staff.One", "status": "active"}],
            ):
                summary = await auth_service.sync_users_from_mongodb(db, purge_demo_accounts=False)

            assert summary["stale_accounts_deactivated"] >= 1
            await db.refresh(resigned)
            await db.refresh(local_admin)
            assert resigned.is_active is False
            assert resigned.token_version == 1
            assert local_admin.is_active is True
            resigned_rt = (
                await db.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_resigned_rt))
            ).scalar_one()
            assert resigned_rt.revoked_at is not None

            # seed_branch_staff_users works without hardcoded NamAn@2026
            seeded = await auth_service.seed_branch_staff_users(db)
            assert len(seeded) > 0
            assert all("default_password" not in item for item in seeded)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hr_sync_worker_rolls_back_before_advisory_unlock():
    mock_db = AsyncMock()
    mock_db.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    lock_res = MagicMock()
    lock_res.scalar.return_value = True
    mock_db.execute = AsyncMock(return_value=lock_res)

    class _SessionCM:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    worker = HRSyncWorker()
    fake_settings = SimpleNamespace(AUTH_MONGO_ENABLED=True, HR_SYNC_PURGE_DEMO_ACCOUNTS=False)
    with (
        patch("app.services.hr_sync_worker.get_settings", return_value=fake_settings),
        patch("app.services.hr_sync_worker.AsyncSessionLocal", return_value=_SessionCM()),
        patch(
            "app.services.hr_sync_worker.auth_service.sync_users_from_mongodb",
            new=AsyncMock(side_effect=RuntimeError("DB write error")),
        ),
    ):
        res = await worker.sync_once(trigger="test")

    assert res["success"] is False
    mock_db.rollback.assert_awaited_once()
    executed_sql = [str(call.args[0]) for call in mock_db.execute.await_args_list]
    assert any("pg_advisory_unlock" in sql for sql in executed_sql)


# ---------------------------------------------------------------------------
# 9. Web Router Resilience & Role Redirect
# ---------------------------------------------------------------------------


def test_web_router_invalid_cookie_clears_cookie_and_staff_redirects(client):
    # Invalid JWT cookie on /admin/login must render 200 HTML and clear cookie (not 401 JSON)
    app.dependency_overrides[get_optional_db] = lambda: AsyncMock()
    try:
        client.cookies.set("naman_access_token", "invalid.jwt.cookie")
        res_login = client.get("/admin/login")
        assert res_login.status_code == 200
        assert "text/html" in res_login.headers["content-type"]
        set_cookie = res_login.headers.get("set-cookie", "")
        assert "naman_access_token=" in set_cookie

        # Malformed ver / SQLAlchemyError in _get_authenticated_web_user -> gracefully redirects to login
        bad_ver_token, _ = create_access_token({"sub": "u-1"})
        with patch("app.web.router.decode_token", return_value={"sub": "u-1", "type": "access", "ver": "not-an-int"}):
            client.cookies.set("naman_access_token", bad_ver_token)
            res_bad_ver = client.get("/admin", follow_redirects=False)
            assert res_bad_ver.status_code == 302
            assert "/admin/login" in res_bad_ver.headers["location"]

        failing_db = AsyncMock()
        failing_db.execute = AsyncMock(side_effect=SQLAlchemyError("DB down"))
        app.dependency_overrides[get_optional_db] = lambda: failing_db
        client.cookies.set("naman_access_token", bad_ver_token)
        res_db_err = client.get("/admin", follow_redirects=False)
        assert res_db_err.status_code == 302
        assert "/admin/login" in res_db_err.headers["location"]
    finally:
        client.cookies.clear()
        app.dependency_overrides.clear()

    # STAFF & READ_ONLY redirected server-side from /admin -> /admin/orders
    for role in (UserRole.STAFF, UserRole.READ_ONLY):
        usr = _user(uid=f"web-{role.value}", username=f"web_{role.value}", role=role)
        with patch("app.web.router._get_authenticated_web_user", new=AsyncMock(return_value=usr)):
            res_admin = client.get("/admin", follow_redirects=False)
            assert res_admin.status_code == 302
            assert res_admin.headers["location"] == "/admin/orders"
