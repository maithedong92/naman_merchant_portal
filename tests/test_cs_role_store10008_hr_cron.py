from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import (
    get_current_user,
    has_cross_store_read_access,
    reject_non_channel_mutator,
    require_order_dispatcher,
    require_order_operator,
    require_staff,
    require_store_manager,
    require_super_admin,
)
from app.core.database import Base, get_db
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import hash_password
from app.main import app
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole
from app.schemas.user import UserUpdate
from app.services.auth_service import auth_service
from app.services.channel_service import (
    DEFAULT_STORES,
    NON_DELIVERY_APP_STORE_CODES,
    ChannelService,
    is_delivery_app_store,
)
from app.services.haravan_order_service import haravan_order_service
from app.services.hr_sync_worker import (
    HRSyncWorker,
    VN_TZ,
    compute_next_run_utc,
    find_due_slot_key,
    parse_schedule_slots,
)
from app.services.inventory_service import InventoryService
from app.services.channel_registry import channel_registry
from app.modules.grabmart.adapter import GrabMartChannelAdapter
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter
from app.services.poppies_service import poppies_service
from app.services.poppies_sync_worker import PoppiesSyncWorker


@pytest_asyncio.fixture
async def db_session():
    """Isolated in-memory SQLite async session per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


def _make_user(
    *,
    uid: str = "u-1",
    username: str = "cs_dispatcher",
    role: UserRole = UserRole.CS,
    store: Store | None = None,
    is_superuser: bool = False,
) -> User:
    u = User(
        id=uid,
        username=username,
        email=f"{username}@namanmarket.com.vn",
        hashed_password=hash_password("StrongPass123!"),
        full_name=f"User {username}",
        role=role.value,
        store_id=store.id if store else None,
        is_active=True,
        is_superuser=is_superuser,
    )
    if store is not None:
        u.store = store
    return u


# ---------------------------------------------------------------------------
# 1. CS Role RBAC & Order Dispatcher Scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cs_role_guards_and_channel_mutation_denial():
    assert UserRole.CS.value == "CS"
    cs_user = _make_user(role=UserRole.CS)
    staff_unassigned = _make_user(uid="u-2", username="staff0", role=UserRole.STAFF)
    admin_user = _make_user(uid="u-3", username="root", role=UserRole.SUPER_ADMIN, is_superuser=True)

    assert await require_order_dispatcher(cs_user) is cs_user
    assert await require_order_operator(cs_user) is cs_user
    assert has_cross_store_read_access(cs_user) is True
    assert has_cross_store_read_access(staff_unassigned) is False

    for guard in (require_staff, require_store_manager, require_super_admin):
        with pytest.raises(ForbiddenError):
            await guard(cs_user)

    with pytest.raises(ForbiddenError):
        reject_non_channel_mutator(cs_user)
    with pytest.raises(ForbiddenError):
        reject_non_channel_mutator(_make_user(uid="u-4", username="ro", role=UserRole.READ_ONLY))
    reject_non_channel_mutator(admin_user)


@pytest.mark.asyncio
async def test_cs_api_allowed_reads_and_forbidden_mutations(db_session: AsyncSession):
    await ChannelService.ensure_default_stores(db_session)
    cs_user = _make_user(role=UserRole.CS)

    async def override_db():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: cs_user
    app.dependency_overrides[get_db] = override_db

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            with (
                patch("app.services.order_service.OrderService.list_orders", new_callable=AsyncMock, return_value=([], 0)),
                patch(
                    "app.services.order_service.OrderService.get_order_summary",
                    new_callable=AsyncMock,
                    return_value={
                        "pending_count": 0,
                        "preparing_count": 0,
                        "ready_count": 0,
                        "delivering_count": 0,
                        "delivered_today_count": 0,
                        "cancelled_today_count": 0,
                        "revenue_today": 0.0,
                    },
                ),
                patch(
                    "app.services.inventory_service.InventoryService.get_store_inventory_overview",
                    new_callable=AsyncMock,
                    return_value=(
                        [],
                        0,
                        {
                            "store_code": "10008",
                            "store_name": "Nam An Market - Kho Online Ecom",
                            "total_skus": 0,
                            "in_stock_count": 0,
                            "out_of_stock_count": 0,
                            "low_stock_count": 0,
                            "in_stock_skus": 0,
                            "out_of_stock_skus": 0,
                            "low_stock_skus": 0,
                            "categories_count": 0,
                        },
                    ),
                ),
                patch(
                    "app.services.report_service.report_service.get_full_analytics",
                    new_callable=AsyncMock,
                    return_value={
                        "filter_from": None,
                        "filter_to": None,
                        "summary": {},
                        "channels": [],
                        "stores": [],
                        "daily_trends": [],
                    },
                ),
            ):
                assert (await ac.get("/api/v1/orders")).status_code == 200
                assert (await ac.get("/api/v1/orders/summary")).status_code == 200
                stores_res = await ac.get("/api/v1/stores")
                assert stores_res.status_code == 200
                store_codes = {s["code"] for s in stores_res.json()["data"]}
                assert "10008" in store_codes
                assert (await ac.get("/api/v1/inventory/overview?store_code=10008")).status_code == 200
                assert (await ac.get("/api/v1/reports/analytics")).status_code == 200

            # Forbidden mutations & admin endpoints for CS
            assert (await ac.patch("/api/v1/orders/ord-1/status", json={"new_status": "ACCEPTED"})).status_code == 403
            assert (
                await ac.patch(
                    "/api/v1/inventory/toggle",
                    json={"store_code": "10001", "sku": "SKU-1", "is_out_of_stock": True},
                )
            ).status_code == 403
            assert (
                await ac.patch(
                    "/api/v1/inventory/quick-update",
                    json={"store_code": "10001", "sku": "SKU-1", "new_stock": 5, "sync_to_channels": False},
                )
            ).status_code == 403
            assert (await ac.post("/api/v1/inventory/sync-all?store_code=10001")).status_code == 403
            assert (
                await ac.post("/api/v1/stores", json={"code": "10099", "name": "Blocked Store"})
            ).status_code == 403
            assert (await ac.get("/api/v1/users/hr-sync/status")).status_code == 403
            assert (await ac.post("/api/v1/haravan/orders/ord-1/pack")).status_code == 403
            assert (await ac.post("/api/v1/haravan/orders/ord-1/complete", json={"actual_total": 10000})).status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cs_can_dispatch_haravan_order_to_store_10008():
    cs_user = _make_user(role=UserRole.CS)
    assert haravan_order_service._has_global_access(cs_user, allow_read_only=False) is True

    sample_order = {
        "id": 999001,
        "order_number": "#NA999001",
        "line_items": [{"sku": "001020", "quantity": 1}],
        "internal": {
            "order_processing_status": "Đơn hàng mới",
            "assigned_store": None,
            "history": [],
        },
    }
    mock_col = MagicMock()

    def _find_one(query):
        return dict(sample_order)

    def _update_one(filt, update_doc):
        for k, v in update_doc.get("$set", {}).items():
            if k.startswith("internal."):
                sample_order["internal"][k.split(".", 1)[1]] = v
        return MagicMock(modified_count=1)

    mock_col.find_one.side_effect = _find_one
    mock_col.update_one.side_effect = _update_one

    app.dependency_overrides[get_current_user] = lambda: cs_user
    try:
        with (
            patch.object(haravan_order_service, "_get_orders_collection", return_value=mock_col),
            patch(
                "app.services.haravan_order_service.arito_service.get_inventory_by_skus",
                new_callable=AsyncMock,
                return_value={"001020": {"10008": 15}},
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                assign_res = await ac.post(
                    "/api/v1/haravan/orders/999001/assign",
                    json={"store_code": "10008", "reason": "Khách online", "note": "Giao tu kho 10008"},
                )
                assert assign_res.status_code == 200
                body = assign_res.json()
                assert body["success"] is True
                assert body["data"]["internal"]["assigned_store"] == "10008"
                assert body["data"]["internal"]["order_processing_status"] == "Đã phân tuyến"

                reassign_res = await ac.post(
                    "/api/v1/haravan/orders/999001/reassign",
                    json={"reason": "Khách hàng yêu cầu đổi cửa hàng", "note": "Chuyen cua hang"},
                )
                assert reassign_res.status_code == 200
                assert reassign_res.json()["data"]["internal"]["assigned_store"] is None
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 2. Store 10008 (Receive-Only) Isolation
# ---------------------------------------------------------------------------


def test_store_10008_constants():
    codes = [s["code"] for s in DEFAULT_STORES]
    assert "10008" in codes
    assert "10008" in NON_DELIVERY_APP_STORE_CODES
    assert is_delivery_app_store("10008") is False
    assert is_delivery_app_store("10001") is True
    assert is_delivery_app_store(None) is False


@pytest.mark.asyncio
async def test_store_10008_seeded_without_delivery_app_mappings(db_session: AsyncSession):
    for adp in (ShopeeFoodChannelAdapter(), GrabMartChannelAdapter(), ShopeeMartChannelAdapter()):
        if not channel_registry.get(adp.channel_code):
            channel_registry.register(adp)

    await ChannelService.ensure_default_stores(db_session)
    stores = (await db_session.execute(select(Store).order_by(Store.code))).scalars().all()
    assert [s.code for s in stores] == ["10001", "10004", "10005", "10006", "10008"]

    # Reconcile Grab / ShopeeFood / ShopeeMart: store 10008 must receive 0 channel mappings
    fake_channels_resp = {
        "channels": [
            {"channel_code": "GRAB", "channel_name": "GrabMart", "is_active": True},
            {"channel_code": "SHOPEEFOOD", "channel_name": "ShopeeFood", "is_active": True},
            {"channel_code": "SHOPEEMART", "channel_name": "ShopeeMart", "is_active": True},
        ],
        "source": "mock_poppies",
    }
    with patch.object(
        poppies_service.client,
        "get_pricing_channels",
        new_callable=AsyncMock,
        return_value=fake_channels_resp,
    ):
        summary = await poppies_service.reconcile_channels_from_poppies(db_session, sync_store_outlets=True)

    store_10008 = (await db_session.execute(select(Store).where(Store.code == "10008"))).scalar_one()
    mappings_10008 = (
        await db_session.execute(select(StoreChannelMapping).where(StoreChannelMapping.store_id == store_10008.id))
    ).scalars().all()
    assert mappings_10008 == []
    assert summary["store_mappings_created"] == 12  # 4 retail stores x 3 channels

    # Seed sample catalog also creates inventory for 10008 without creating channel mappings for 10008
    await InventoryService.seed_sample_catalog(db_session)
    mappings_10008_after_seed = (
        await db_session.execute(select(StoreChannelMapping).where(StoreChannelMapping.store_id == store_10008.id))
    ).scalars().all()
    inv_10008 = (
        await db_session.execute(select(StoreInventory).where(StoreInventory.store_id == store_10008.id))
    ).scalars().all()
    assert mappings_10008_after_seed == []
    assert len(inv_10008) > 0


@pytest.mark.asyncio
async def test_poppies_sync_worker_skips_store_10008(db_session: AsyncSession):
    await ChannelService.ensure_default_stores(db_session)
    db_session.add(
        Channel(code="GRAB", name="GrabMart", is_active=True, config={"has_partner_pricing": True})
    )
    await db_session.commit()

    class _SQLiteLockProxy:
        def __init__(self, inner: AsyncSession):
            self._inner = inner

        async def execute(self, stmt, *args, **kwargs):
            if "pg_try_advisory_lock" in str(stmt) or "pg_advisory_unlock" in str(stmt):
                m = MagicMock()
                m.scalar.return_value = True
                return m
            return await self._inner.execute(stmt, *args, **kwargs)

        def __getattr__(self, item):
            return getattr(self._inner, item)

    class _SessionCM:
        async def __aenter__(self):
            return _SQLiteLockProxy(db_session)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    worker = PoppiesSyncWorker()
    worker._channels_reconciled = True
    seen_branches = []

    async def _fake_sync_snapshot(*, branch_code, **kwargs):
        seen_branches.append(branch_code)
        return {"prices_updated": 0, "blocked_skus_count": 0}

    with (
        patch("app.services.poppies_sync_worker.AsyncSessionLocal", return_value=_SessionCM()),
        patch(
            "app.services.poppies_sync_worker.poppies_service.sync_channel_pricing_snapshot",
            side_effect=_fake_sync_snapshot,
        ),
        patch(
            "app.services.poppies_sync_worker.poppies_service.sync_stock_cttk_realtime",
            new_callable=AsyncMock,
            return_value={"total_cttk_records": 0, "inventories_updated": 0, "availability_changed_skus": []},
        ),
        patch(
            "app.services.poppies_sync_worker.propagate_channel_menu_to_external_channel",
            new_callable=AsyncMock,
            return_value={"stores_pushed": 0},
        ),
    ):
        res = await worker.sync_once(force_full=True)

    assert res["success"] is True
    assert "10008" not in seen_branches
    assert set(seen_branches) == {"10001", "10004", "10005", "10006"}


# ---------------------------------------------------------------------------
# 3. HR Sync 2x/Day Cronjob & IT Manual RBAC Preservation
# ---------------------------------------------------------------------------


def test_hr_cron_schedule_slots_and_due_windows():
    assert parse_schedule_slots("07:00, 14:00") == [(7, 0), (14, 0)]
    assert parse_schedule_slots("14:00,07:00,07:00") == [(7, 0), (14, 0)]
    assert parse_schedule_slots("invalid") == [(7, 0), (14, 0)]

    slots = [(7, 0), (14, 0)]
    dt_0630_vn = datetime(2026, 11, 20, 6, 30, tzinfo=VN_TZ).astimezone(timezone.utc)
    dt_0702_vn = datetime(2026, 11, 20, 7, 2, tzinfo=VN_TZ).astimezone(timezone.utc)
    dt_1000_vn = datetime(2026, 11, 20, 10, 0, tzinfo=VN_TZ).astimezone(timezone.utc)
    dt_1403_vn = datetime(2026, 11, 20, 14, 3, tzinfo=VN_TZ).astimezone(timezone.utc)
    dt_1600_vn = datetime(2026, 11, 20, 16, 0, tzinfo=VN_TZ).astimezone(timezone.utc)

    assert compute_next_run_utc(dt_0630_vn, slots).astimezone(VN_TZ).hour == 7
    assert compute_next_run_utc(dt_0702_vn, slots).astimezone(VN_TZ).hour == 14
    next_day = compute_next_run_utc(dt_1600_vn, slots).astimezone(VN_TZ)
    assert (next_day.day, next_day.hour) == (21, 7)

    executed: set[str] = set()
    slot_morning = find_due_slot_key(dt_0702_vn, slots, executed)
    assert slot_morning == "2026-11-20 07:00"
    executed.add(slot_morning)
    assert find_due_slot_key(dt_0702_vn, slots, executed) is None
    assert find_due_slot_key(dt_1000_vn, slots, executed) is None

    slot_afternoon = find_due_slot_key(dt_1403_vn, slots, executed)
    assert slot_afternoon == "2026-11-20 14:00"
    assert find_due_slot_key(dt_1600_vn, slots, executed) is None


@pytest.mark.asyncio
async def test_hr_sync_defaults_least_privilege_and_preserves_it_rbac(db_session: AsyncSession):
    await ChannelService.ensure_default_stores(db_session)
    st_10001 = (await db_session.execute(select(Store).where(Store.code == "10001"))).scalar_one()

    mongo_doc = {
        "user_id": "9001",
        "username": "nv.cs01",
        "email": "nv.cs01@namanmarket.com.vn",
        "fullname": "Nguyen Van CS",
        "phone": "0909111222",
        "dept": "BGD - CH10001 - MKT",
        "user_role": "admin",
        "user_lvl": 40,
        "status": "active",
    }

    extracted = auth_service._extract_mongo_fields(mongo_doc, {"10001": st_10001.id})
    assert extracted["role"] == UserRole.READ_ONLY.value
    assert extracted["store_id"] is None
    assert extracted["is_superuser"] is False

    # First sync creates user as READ_ONLY with no store
    created_user = await auth_service._upsert_pg_shadow_user(db_session, mongo_doc, {"10001": st_10001.id})
    await db_session.commit()
    assert created_user.role == UserRole.READ_ONLY.value
    assert created_user.store_id is None
    assert created_user.is_superuser is False

    # IT manually assigns STORE_MANAGER on branch 10001, then switches user to CS
    await auth_service.update_user(
        db_session,
        created_user.id,
        UserUpdate(role=UserRole.STORE_MANAGER, store_id=st_10001.id),
    )
    assert created_user.store_id == st_10001.id

    await auth_service.update_user(
        db_session,
        created_user.id,
        UserUpdate(role=UserRole.CS),
    )
    assert created_user.role == UserRole.CS.value
    assert created_user.store_id is None
    assert created_user.is_superuser is False

    # Subsequent HR sync updates profile fields without overwriting IT-assigned role/store/superuser
    updated_doc = {**mongo_doc, "fullname": "Nguyen Van CS (Updated)", "phone": "0909888777"}
    resynced_user = await auth_service._upsert_pg_shadow_user(db_session, updated_doc, {"10001": st_10001.id})
    await db_session.commit()
    assert resynced_user.full_name == "Nguyen Van CS (Updated)"
    assert resynced_user.phone_number == "0909888777"
    assert resynced_user.role == UserRole.CS.value
    assert resynced_user.store_id is None
    assert resynced_user.is_superuser is False


@pytest.mark.asyncio
async def test_hr_sync_status_endpoint_for_super_admin():
    admin = _make_user(uid="adm-1", username="admin", role=UserRole.SUPER_ADMIN, is_superuser=True)
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/v1/users/hr-sync/status")
            assert res.status_code == 200
            data = res.json()["data"]
            assert data["runs_per_day"] == 2
            assert data["schedule_vn"] == "07:00, 14:00"
    finally:
        app.dependency_overrides.clear()

    worker = HRSyncWorker()
    worker.record_manual_run({"mongo_active_users": 12, "stale_accounts_deactivated": 0})
    st = worker.get_status()
    assert st["total_runs"] == 1
    assert st["last_result"]["trigger"] == "manual"


# ---------------------------------------------------------------------------
# 4. Username-Only Login Tests (No Email Lookup)
# ---------------------------------------------------------------------------


def test_mongo_login_lookup_excludes_email_fields():
    mock_col = MagicMock()
    cursor = MagicMock()
    cursor.limit.return_value = [
        {"username": "nv.cs01", "user_id": "9001", "status": "active"}
    ]
    mock_col.find.return_value = cursor

    with patch.object(auth_service, "_get_mongo_user_collection", return_value=mock_col):
        found = auth_service._find_mongo_user_sync("nv.cs01")

    assert found is not None
    assert found["username"] == "nv.cs01"
    query_filter = mock_col.find.call_args[0][0]
    or_branches = query_filter.get("$or") or []
    queried_fields = {k for branch in or_branches for k in branch.keys()}
    assert queried_fields == {"username", "user_id"}
    assert "email" not in queried_fields
    assert "ECI.Email" not in queried_fields


@pytest.mark.asyncio
async def test_pg_login_rejects_email_and_accepts_username(db_session: AsyncSession):
    u = User(
        id="u-pg-login-1",
        username="nv.cs01",
        email="cs01@namanmarket.com.vn",
        hashed_password=hash_password("StrongPass123!"),
        full_name="Nhan Vien CS 01",
        role=UserRole.CS.value,
        is_active=True,
        is_superuser=False,
    )
    db_session.add(u)
    await db_session.commit()

    with patch("app.services.auth_service.settings.AUTH_MONGO_ENABLED", False):
        with pytest.raises(UnauthorizedError):
            await auth_service.authenticate_user(
                db_session,
                username_or_email="cs01@namanmarket.com.vn",
                password="StrongPass123!",
            )

        logged_in_user, token_resp = await auth_service.authenticate_user(
            db_session,
            username_or_email="NV.CS01",
            password="StrongPass123!",
        )
        assert logged_in_user.username == "nv.cs01"
        assert token_resp.access_token


# ---------------------------------------------------------------------------
# 5. Template Smoke Verification
# ---------------------------------------------------------------------------


def test_templates_contain_cs_store_10008_and_hr_cron_ui():
    root = Path(__file__).resolve().parents[1] / "app" / "templates"
    base_html = (root / "base.html").read_text(encoding="utf-8")
    users_html = (root / "users.html").read_text(encoding="utf-8")
    orders_html = (root / "orders.html").read_text(encoding="utf-8")
    inventory_html = (root / "inventory.html").read_text(encoding="utf-8")
    reports_html = (root / "reports.html").read_text(encoding="utf-8")
    login_html = (root / "login.html").read_text(encoding="utf-8")

    assert "5 Chi nhánh ERP" in base_html
    assert 'value="CS"' in users_html
    assert "loadHrSyncStatus()" in users_html
    assert "currentUser.role === 'CS'" in orders_html
    assert "data-store=\"10008\"" in inventory_html
    assert "canMutateInventory()" in inventory_html
    assert 'value="10008"' in reports_html
    assert "Tên đăng nhập (Username)" in login_html
    assert "hoặc email" not in login_html


@pytest.mark.asyncio
async def test_lifespan_non_debug_seeds_default_stores(db_session: AsyncSession):
    """Verify DEBUG=False startup path does not hit UnboundLocalError and ensures stores exist."""
    from app.main import lifespan

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _ConnCM:
        async def __aenter__(self):
            m = MagicMock()
            m.execute = AsyncMock(return_value=MagicMock())
            return m

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_engine = MagicMock()
    fake_engine.connect.return_value = _ConnCM()
    fake_engine.dispose = AsyncMock()

    with (
        patch("app.main.settings.DEBUG", False),
        patch("app.main.settings.ENVIRONMENT", "production"),
        patch("app.main.engine", fake_engine),
        patch("app.main.AsyncSessionLocal", return_value=_SessionCM()),
        patch("app.modules.shopeemart.service.shopeemart_client.hydrate_from_db", new_callable=AsyncMock),
        patch("app.modules.haravan.sync_worker.haravan_sync_worker.start_background_worker"),
        patch("app.services.poppies_sync_worker.poppies_sync_worker.start_background_worker"),
        patch("app.services.hr_sync_worker.hr_sync_worker.start_background_worker"),
        patch("app.main._stop_worker", new_callable=AsyncMock),
    ):
        async with lifespan(app):
            pass

    stores = (await db_session.execute(select(Store).order_by(Store.code))).scalars().all()
    assert "10008" in [s.code for s in stores]
