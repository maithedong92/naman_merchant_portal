from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_user, get_db
from app.main import app
from app.models.channel import Channel
from app.models.user import User, UserRole
from app.services.channel_service import ChannelService, channel_service


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_admin_user():
    return User(
        id="admin-uuid-1",
        username="admin",
        email="admin@namanmarket.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


@pytest.mark.asyncio
async def test_channel_service_extract_features():
    """Verify channel_service.extract_features returns expected standard structure."""
    ch = Channel(
        code="SHOPEEFOOD",
        name="ShopeeFood VN",
        is_active=True,
        config={
            "order_webhook_enabled": True,
            "auto_confirm_enabled": False,
            "stock_sync_enabled": True,
            "menu_sync_enabled": False,
        }
    )
    features = channel_service.extract_features(ch)
    assert features["is_active"] is True
    assert features["order_webhook_enabled"] is True
    assert features["auto_confirm_enabled"] is False
    assert features["stock_sync_enabled"] is True
    assert features["menu_sync_enabled"] is False


@pytest.mark.asyncio
async def test_channel_service_is_feature_enabled():
    """is_feature_enabled respects both master channel is_active and specific flags."""
    mock_session = AsyncMock()

    # Active channel with auto_confirm disabled
    active_ch = Channel(
        code="TEST_CH",
        name="Test Channel",
        is_active=True,
        config={"order_webhook_enabled": True, "auto_confirm_enabled": False}
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ChannelService, "get_channel_by_code", AsyncMock(return_value=active_ch))

        assert await channel_service.is_feature_enabled("TEST_CH", "order_webhook_enabled", mock_session) is True
        assert await channel_service.is_feature_enabled("TEST_CH", "auto_confirm_enabled", mock_session) is False

        # If master channel is inactive, all features return False
        active_ch.is_active = False
        assert await channel_service.is_feature_enabled("TEST_CH", "order_webhook_enabled", mock_session) is False


def test_list_channels_api(client, mock_admin_user):
    """GET /api/v1/channels returns list of channels enriched with feature flags."""
    mock_db = AsyncMock()

    ch1 = Channel(
        id="ch-1",
        code="SHOPEEFOOD",
        name="ShopeeFood VN",
        is_active=True,
        config={"order_webhook_enabled": True, "auto_confirm_enabled": False}
    )
    ch2 = Channel(
        id="ch-2",
        code="GRABMART",
        name="GrabMart VN",
        is_active=True,
        config={"order_webhook_enabled": True, "auto_confirm_enabled": True}
    )

    # Mock DB query
    mock_res = MagicMock()
    mock_res.scalars().all.return_value = [ch1, ch2]
    mock_db.execute.return_value = mock_res

    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(channel_service, "ensure_default_channels", AsyncMock())
            response = client.get("/api/v1/channels")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["data"]) == 2
        shopee = data["data"][0]
        assert shopee["code"] == "SHOPEEFOOD"
        assert shopee["features"]["order_webhook_enabled"] is True
        assert shopee["features"]["auto_confirm_enabled"] is False
    finally:
        app.dependency_overrides.clear()


def test_update_channel_features_api(client, mock_admin_user):
    """PATCH /api/v1/channels/{code}/features updates feature toggles dynamically."""
    mock_db = AsyncMock()

    updated_ch = Channel(
        id="ch-1",
        code="SHOPEEFOOD",
        name="ShopeeFood VN",
        is_active=True,
        config={
            "order_webhook_enabled": False,
            "auto_confirm_enabled": True,
            "stock_sync_enabled": True,
            "menu_sync_enabled": True,
        }
    )

    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(channel_service, "update_feature_flags", AsyncMock(return_value=updated_ch))

            payload = {
                "order_webhook_enabled": False,
                "auto_confirm_enabled": True
            }
            response = client.patch("/api/v1/channels/SHOPEEFOOD/features", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        features = data["data"]["features"]
        assert features["order_webhook_enabled"] is False
        assert features["auto_confirm_enabled"] is True
    finally:
        app.dependency_overrides.clear()
