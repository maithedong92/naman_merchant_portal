from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_user, get_db, require_store_manager
from app.main import app
from app.models.channel import Channel
from app.models.product import Category, ChannelProductMapping, Product
from app.models.user import User, UserRole
from app.services.poppies_service import (
    CHANNEL_CODE_ALIASES,
    POPPIES_STANDARD_CHANNELS,
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


def test_normalize_channel_code():
    assert normalize_channel_code("GRAB") == "GRAB_MART"
    assert normalize_channel_code("grabmart") == "GRAB_MART"
    assert normalize_channel_code("SHOPEEFOOD") == "SHOPEE_FOOD"
    assert normalize_channel_code("web") == "HARAVAN_WEB"
    assert normalize_channel_code("POS_OFFLINE") == "POS_OFFLINE"
    assert normalize_channel_code("TIKTOK") == "TIKTOK_SHOP"


@pytest.mark.asyncio
async def test_poppies_client_toggle_channel_publish_status():
    """Verify PoppiesClient properly sends POST to Poppies ERP pricing endpoint."""
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "sku": "290031798000",
            "scopeType": "CHANNEL",
            "scopeCode": "GRAB_MART",
            "isPublished": False,
            "message": "Đã TẮT bán SKU 290031798000 trên GRAB_MART"
        }
        mock_post.return_value = mock_resp

        res = await poppies_service.client.toggle_channel_publish_status(
            sku="290031798000",
            scope_code="GRAB_MART",
            is_published=False
        )

        assert res["success"] is True
        assert res["sku"] == "290031798000"
        assert res["isPublished"] is False
        assert res["scopeCode"] == "GRAB_MART"


@pytest.mark.asyncio
async def test_service_toggle_sku_channel_publish():
    """Verify service toggles ChannelProductMapping and synchronizes to Poppies."""
    mock_db = AsyncMock()

    mock_prod = Product(
        id="prod-123",
        sku="290031798000",
        name="Việt quất úc 200g",
        base_price=150000.0,
        is_active=True
    )

    mock_channel = Channel(
        id="chan-grab",
        code="GRAB_MART",
        name="Grab Mart",
        is_active=True
    )

    # 1. Product lookup returns mock_prod
    mock_prod_res = MagicMock()
    mock_prod_res.scalar_one_or_none.return_value = mock_prod

    # 2. Channel lookup returns mock_channel
    mock_chan_res = MagicMock()
    mock_chan_res.scalar_one_or_none.return_value = mock_channel

    # 3. Mapping lookup returns None initially (new mapping)
    mock_map_res = MagicMock()
    mock_map_res.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [mock_prod_res, mock_chan_res, mock_map_res]

    with patch.object(
        poppies_service.client,
        "toggle_channel_publish_status",
        new_callable=AsyncMock
    ) as mock_toggle:
        mock_toggle.return_value = {
            "success": True,
            "sku": "290031798000",
            "scopeCode": "GRAB_MART",
            "isPublished": False
        }

        result = await poppies_service.toggle_sku_channel_publish(
            db=mock_db,
            sku="290031798000",
            channel_code="GRABMART",
            is_published=False,
            sync_to_poppies=True
        )

        assert result["sku"] == "290031798000"
        assert result["poppies_channel_code"] == "GRAB_MART"
        assert result["is_published"] is False
        assert "TẮT" in result["message"]
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_toggle.assert_called_once_with(
            sku="290031798000",
            scope_code="GRAB_MART",
            is_published=False,
            scope_type="CHANNEL"
        )


def test_api_get_pricing_channels(client, mock_store_manager):
    app.dependency_overrides[get_current_user] = lambda: mock_store_manager

    response = client.get("/api/v1/pricing/channels")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "channels" in body["data"]
    channels = body["data"]["channels"]
    assert len(channels) == 10
    codes = [c["channel_code"] for c in channels]
    assert "POS_OFFLINE" in codes
    assert "GRAB_MART" in codes
    assert "SHOPEE_FOOD" in codes

    app.dependency_overrides.clear()


def test_api_set_channel_publish_status(client, mock_store_manager):
    app.dependency_overrides[get_current_user] = lambda: mock_store_manager
    app.dependency_overrides[require_store_manager] = lambda: mock_store_manager

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch.object(
        poppies_service,
        "toggle_sku_channel_publish",
        new_callable=AsyncMock
    ) as mock_service_toggle:
        mock_service_toggle.return_value = {
            "sku": "290031798000",
            "product_name": "Việt quất úc 200g",
            "channel_code": "GRAB_MART",
            "poppies_channel_code": "GRAB_MART",
            "is_published": False,
            "synced_to_poppies": True,
            "message": "Đã TẮT bán SKU 290031798000 trên Grab Mart (GRAB_MART)",
            "poppies_response": {"success": True}
        }

        payload = {
            "scopeType": "CHANNEL",
            "scopeCode": "GRAB_MART",
            "sku": "290031798000",
            "isPublished": False
        }

        response = client.post("/api/v1/pricing/channel-publish-status", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["is_published"] is False
        assert body["data"]["sku"] == "290031798000"

    app.dependency_overrides.clear()


def test_api_webhook_price_changed(client):
    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch.object(
        poppies_service,
        "process_price_change_webhook",
        new_callable=AsyncMock
    ) as mock_webhook:
        mock_webhook.return_value = {
            "success": True,
            "event_id": "evt_test_123",
            "processed_count": 1
        }

        payload = {
            "event_id": "evt_test_123",
            "event": "price_changed",
            "event_type": "CHANNEL_PUBLISH_STATUS_CHANGED",
            "channel_code": "GRAB_MART",
            "affected_skus": ["290031798000"],
            "timestamp": "2026-11-04T02:25:00Z"
        }

        headers = {
            "X-Poppies-Webhook-Key": "naman_pos_api_secret_key_2026",
            "X-Event-Id": "evt_test_123"
        }

        response = client.post(
            "/api/v1/pricing/webhook/price-changed",
            json=payload,
            headers=headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["event_id"] == "evt_test_123"

    app.dependency_overrides.clear()
