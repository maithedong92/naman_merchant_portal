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
