from contextlib import contextmanager
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.database import Base
from app.models.channel import Channel
from app.models.store import Store, StoreChannelMapping
from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter
from app.modules.shopeemart.service import ShopeeMartClient, shopeemart_client


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


def test_pytest_database_url_is_isolated_to_sqlite():
    url = str(get_settings().DATABASE_URL)
    assert url.startswith("sqlite+aiosqlite://"), f"Unexpected non-SQLite DB URL in test env: {url}"
    assert "103.175.248.250" not in url


def test_get_authorization_url_urlencodes_redirect_with_state():
    client = ShopeeMartClient()
    with patch.object(client, "_load_credentials", return_value=None):
        client.base_url = "https://partner.shopeemobile.com"
        client.partner_id = "2045141"
        client.partner_key = "test_partner_key"
        res = client.get_authorization_url(
            "https://ump.namanmarket.com/api/v1/shopeemart/auth/callback?state=tok.123&foo=bar"
        )
    assert (
        "redirect=https%3A%2F%2Fump.namanmarket.com%2Fapi%2Fv1%2Fshopeemart%2Fauth%2Fcallback%3Fstate%3Dtok.123%26foo%3Dbar"
        in res.auth_url
    )


@pytest.mark.asyncio
async def test_refresh_access_token_uses_db_hydrated_token_without_http(db_session: AsyncSession):
    fresh_exp = time.time() + 7200
    ch = Channel(
        code="SHOPEEMART",
        name="ShopeeMart",
        is_active=True,
        config={
            "default_shop_id": "1925848521",
            "shop_tokens": {
                "1925848521": {
                    "access_token": "fresh_from_peer_worker",
                    "refresh_token": "rotated_rt_2",
                    "expires_at": fresh_exp,
                    "unlinked": False,
                }
            },
        },
    )
    db_session.add(ch)
    await db_session.commit()

    client = ShopeeMartClient()
    client._load_credentials = lambda: None
    client._load_token_cache = lambda: None
    client._write_token_cache_file = lambda: None
    client.partner_id = "2045141"
    client.partner_key = "test_partner_key"
    client.default_shop_id = "1925848521"
    client._token_cache = {
        "1925848521": {
            "access_token": "stale_token",
            "refresh_token": "stale_rt_1",
            "expires_at": time.time() - 60,
        }
    }

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    mock_http = MagicMock()
    mock_http.post = AsyncMock()
    client._http_client = mock_http

    with patch("app.core.database.AsyncSessionLocal", return_value=_SessionCM()):
        tok_resp = await client.refresh_access_token("1925848521")

    assert tok_resp.access_token == "fresh_from_peer_worker"
    assert tok_resp.refresh_token == "rotated_rt_2"
    mock_http.post.assert_not_called()


@pytest.mark.asyncio
async def test_poll_recent_orders_deduplicates_resolved_shop_ids(db_session: AsyncSession):
    ch = Channel(
        id="ch-sm",
        code="SHOPEEMART",
        name="ShopeeMart",
        is_active=True,
        config={
            "default_shop_id": "1925848521",
            "shop_tokens": {
                "1925848521": {
                    "access_token": "tok_live",
                    "refresh_token": "rt_live",
                    "expires_at": time.time() + 3600,
                    "unlinked": False,
                }
            },
        },
    )
    db_session.add(ch)
    for code in ("10001", "10004", "10005", "10006"):
        st = Store(id=f"st-{code}", code=code, name=f"Store {code}", is_active=True)
        db_session.add(st)
        db_session.add(
            StoreChannelMapping(
                store_id=st.id,
                channel_id="ch-sm",
                partner_store_id=code,
                is_active=True,
            )
        )
    await db_session.commit()

    adp = ShopeeMartChannelAdapter()
    seen_shop_ids = []
    orig_cache = dict(shopeemart_client._token_cache)
    shopeemart_client._token_cache = {}

    async def _fake_get_order_list(*, shop_id=None, **kwargs):
        seen_shop_ids.append(shop_id)
        return {"error": "", "response": {"order_list": []}}

    try:
        with (
            patch("app.modules.shopeemart.adapter.shopeemart_client._load_credentials", return_value=None),
            patch("app.modules.shopeemart.adapter.shopeemart_client._load_token_cache", return_value=None),
            patch("app.modules.shopeemart.adapter.shopeemart_client._write_token_cache_file", return_value=None),
            patch("app.modules.shopeemart.adapter.shopeemart_client.partner_id", "2045141"),
            patch("app.modules.shopeemart.adapter.shopeemart_client.partner_key", "test_partner_key"),
            patch(
                "app.modules.shopeemart.adapter.shopeemart_client.get_order_list",
                side_effect=_fake_get_order_list,
            ),
        ):
            res = await adp.poll_recent_orders(db=db_session, shop_id=None, hours_back=24)
    finally:
        shopeemart_client._token_cache = orig_cache

    assert seen_shop_ids == ["1925848521"]
    assert res["total_checked"] == 0


@pytest.mark.asyncio
async def test_exchange_code_with_main_account_id_persists_all_shop_tokens(db_session: AsyncSession):
    ch = Channel(
        id="ch-sm-multi",
        code="SHOPEEMART",
        name="ShopeeMart",
        is_active=True,
        config={"shop_tokens": {"1925848521": {"unlinked": True, "expires_at": 0}}},
    )
    db_session.add(ch)
    await db_session.commit()

    client = ShopeeMartClient()
    client._load_credentials = lambda: None
    client._load_token_cache = lambda: None
    client._write_token_cache_file = lambda: None
    client.partner_id = "2045141"
    client.partner_key = "test_partner_key"
    client.base_url = "https://partner.shopeemobile.com"
    client._token_cache = {}

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "access_token": "acc_main_1",
        "refresh_token": "ref_main_1",
        "expire_in": 14400,
        "shop_id_list": [1925848521, 1925850299],
    }
    mock_http = MagicMock()
    mock_http.is_closed = False
    mock_http.post = AsyncMock(return_value=fake_resp)
    client._http_client = mock_http

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    with patch("app.core.database.AsyncSessionLocal", return_value=_SessionCM()):
        tok_resp = await client.exchange_code_for_token(code="auth_code_main", main_account_id="998877")

    assert tok_resp.authorized_shop_ids == ["1925848521", "1925850299"]
    assert "1925848521" in client._token_cache
    assert "1925850299" in client._token_cache
    assert client._token_cache["1925848521"]["unlinked"] is False

    await db_session.refresh(ch)
    saved_tokens = (ch.config or {}).get("shop_tokens") or {}
    assert set(saved_tokens.keys()) == {"1925848521", "1925850299"}
    assert saved_tokens["1925848521"]["access_token"].startswith("enc:v1:")
    assert saved_tokens["1925850299"]["refresh_token"].startswith("enc:v1:")


@pytest.mark.asyncio
async def test_poll_recent_orders_surfaces_errors_without_rolling_back_caller_session(db_session: AsyncSession):
    adp = ShopeeMartChannelAdapter()
    orig_cache = dict(shopeemart_client._token_cache)
    shopeemart_client._token_cache = {}
    recorded_logs = []

    class _IsolatedSessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def _fake_log_error(**kwargs):
        recorded_logs.append(kwargs)

    db_session.rollback = AsyncMock(side_effect=AssertionError("caller db.rollback must not be called"))

    try:
        with (
            patch("app.modules.shopeemart.adapter.shopeemart_client._load_credentials", return_value=None),
            patch("app.modules.shopeemart.adapter.shopeemart_client._load_token_cache", return_value=None),
            patch("app.modules.shopeemart.adapter.shopeemart_client.partner_id", "2045141"),
            patch("app.modules.shopeemart.adapter.shopeemart_client.partner_key", "test_partner_key"),
            patch(
                "app.modules.shopeemart.adapter.shopeemart_client.get_order_list",
                side_effect=RuntimeError("OAuth expired"),
            ),
            patch("app.core.database.AsyncSessionLocal", return_value=_IsolatedSessionCM()),
            patch("app.modules.shopeemart.adapter.error_service.log_error", side_effect=_fake_log_error),
        ):
            res = await adp.poll_recent_orders(db=db_session, shop_id="1925848521", hours_back=24)
    finally:
        shopeemart_client._token_cache = orig_cache

    assert res["has_errors"] is True
    assert len(res["errors"]) == 1
    assert res["errors"][0]["shop_id"] == "1925848521"
    assert "OAuth expired" in res["errors"][0]["error"]
    assert len(recorded_logs) == 1
    assert recorded_logs[0]["error_code"] == "SHOPEEMART_ORDER_POLL_ERROR"


@pytest.mark.asyncio
async def test_propagate_channel_menu_snapshots_outlets_before_loop(db_session: AsyncSession):
    from app.services.poppies_channel_push import propagate_channel_menu_to_external_channel

    ch = Channel(id="ch-menu-sm", code="SHOPEEMART", name="ShopeeMart", is_active=True)
    st = Store(id="st-menu-1", code="10001", name="Store 10001", is_active=True)
    db_session.add_all([ch, st])
    await db_session.flush()
    db_session.add(
        StoreChannelMapping(store_id=st.id, channel_id=ch.id, partner_store_id="1925848521", is_active=True)
    )
    await db_session.commit()

    fake_adapter = MagicMock()
    fake_adapter.channel_code = "SHOPEEMART"

    async def _failing_sync_menu(**kwargs):
        await db_session.rollback()
        raise RuntimeError("Menu sync failed")

    fake_adapter.sync_menu = AsyncMock(side_effect=_failing_sync_menu)

    with patch("app.services.poppies_channel_push.channel_registry.get", return_value=fake_adapter):
        summaries = await propagate_channel_menu_to_external_channel(db=db_session, raw_channel_code="SHOPEEMART")

    assert len(summaries) == 1
    assert summaries[0]["success"] is False
    assert summaries[0]["partner_store_id"] == "1925848521"


@pytest.mark.asyncio
async def test_shopeemart_sync_worker_keepalive_refresh_and_poll(db_session: AsyncSession):
    from app.services.shopeemart_sync_worker import ShopeeMartSyncWorker

    ch = Channel(
        id="ch-worker-sm",
        code="SHOPEEMART",
        name="ShopeeMart",
        is_active=True,
        config={
            "default_shop_id": "1925848521",
            "shop_tokens": {
                "1925848521": {
                    "access_token": "expiring_soon_tok",
                    "refresh_token": "rt_worker_1",
                    "expires_at": time.time() + 120,
                    "unlinked": False,
                }
            },
        },
    )
    db_session.add(ch)
    await db_session.commit()

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    worker = ShopeeMartSyncWorker()
    orig_cache = dict(shopeemart_client._token_cache)
    shopeemart_client._token_cache = {}

    async def _fake_refresh(shop_id=None):
        shopeemart_client._token_cache[str(shop_id)] = {
            "access_token": "fresh_tok_2",
            "refresh_token": "rt_worker_2",
            "expires_at": time.time() + 14400,
            "unlinked": False,
        }
        return MagicMock(access_token="fresh_tok_2", refresh_token="rt_worker_2", expire_in=14400, shop_id=1925848521)

    try:
        with (
            patch("app.services.shopeemart_sync_worker.AsyncSessionLocal", return_value=_SessionCM()),
            patch("app.modules.shopeemart.service.shopeemart_client._load_credentials", return_value=None),
            patch("app.modules.shopeemart.service.shopeemart_client._load_token_cache", return_value=None),
            patch("app.modules.shopeemart.service.shopeemart_client._write_token_cache_file", return_value=None),
            patch("app.modules.shopeemart.service.shopeemart_client.partner_id", "2045141"),
            patch("app.modules.shopeemart.service.shopeemart_client.partner_key", "test_partner_key"),
            patch(
                "app.modules.shopeemart.service.shopeemart_client.refresh_access_token",
                side_effect=_fake_refresh,
            ) as mock_refresh,
            patch(
                "app.services.shopeemart_sync_worker.ShopeeMartChannelAdapter.poll_recent_orders",
                new=AsyncMock(return_value={"shops_polled": ["1925848521"], "total_checked": 2, "newly_created": 1, "status_synced": 1, "errors": [], "has_errors": False}),
            ) as mock_poll,
        ):
            summary = await worker.sync_once(hours_back=24)
    finally:
        shopeemart_client._token_cache = orig_cache

    assert summary["success"] is True
    assert summary["refreshed_shops"] == ["1925848521"]
    assert summary["valid_shops"] == ["1925848521"]
    assert summary["newly_created"] == 1
    mock_refresh.assert_awaited_once()
    mock_poll.assert_awaited_once()
