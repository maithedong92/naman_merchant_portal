from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_user, require_super_admin, get_db
from app.main import app
from app.models.channel import Channel
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole


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


def test_list_store_mappings_api(client, mock_admin_user):
    """Test GET /api/v1/stores/mappings returns all store-channel links."""
    mock_db = AsyncMock()
    mock_store = Store(id="store-1", code="10001", name="Nam An Thao Dien")
    mock_channel = Channel(id="chan-1", code="SHOPEEMART", name="ShopeeMart (Fresh)")
    mock_mapping = StoreChannelMapping(
        id="map-1",
        store_id="store-1",
        channel_id="chan-1",
        partner_store_id="OUTLET-10001",
        is_active=True,
        channel_config=None,
    )
    mock_mapping.store = mock_store
    mock_mapping.channel = mock_channel

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_mapping]
    mock_db.execute.return_value = mock_result

    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        response = client.get("/api/v1/stores/mappings")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["data"]) == 1
        item = data["data"][0]
        assert item["partner_store_id"] == "OUTLET-10001"
        assert item["store_code"] == "10001"
        assert item["channel_code"] == "SHOPEEMART"
    finally:
        app.dependency_overrides.clear()


def test_update_or_create_store_mapping_api(client, mock_admin_user):
    """Test PUT /api/v1/stores/{store_id}/channels/{channel_id} upsert mapping."""
    mock_db = AsyncMock()
    mock_store = Store(id="store-1", code="10001", name="Nam An Thao Dien")
    mock_channel = Channel(id="chan-1", code="SHOPEEMART", name="ShopeeMart (Fresh)")
    mock_mapping = StoreChannelMapping(
        id="map-1",
        store_id="store-1",
        channel_id="chan-1",
        partner_store_id="OLD-ID",
        is_active=True,
    )

    # First query is store, second is channel, third is mapping
    res_store = MagicMock()
    res_store.scalar_one_or_none.return_value = mock_store
    res_channel = MagicMock()
    res_channel.scalar_one_or_none.return_value = mock_channel
    res_mapping = MagicMock()
    res_mapping.scalar_one_or_none.return_value = mock_mapping

    mock_db.execute.side_effect = [res_store, res_channel, res_mapping]
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    app.dependency_overrides[require_super_admin] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        payload = {
            "partner_store_id": "NEW-SHOPEEMART-OUTLET-999",
            "is_active": True
        }
        response = client.put("/api/v1/stores/10001/channels/SHOPEEMART", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert mock_mapping.partner_store_id == "NEW-SHOPEEMART-OUTLET-999"
    finally:
        app.dependency_overrides.clear()


def test_delete_store_mapping_api(client, mock_admin_user):
    """Test DELETE /api/v1/stores/{store_id}/channels/{channel_id}."""
    mock_db = AsyncMock()
    mock_store = Store(id="store-1", code="10001", name="Nam An Thao Dien")
    mock_channel = Channel(id="chan-1", code="SHOPEEMART", name="ShopeeMart (Fresh)")
    mock_mapping = StoreChannelMapping(
        id="map-1",
        store_id="store-1",
        channel_id="chan-1",
        partner_store_id="OUTLET-10001",
    )

    res_store = MagicMock()
    res_store.scalar_one_or_none.return_value = mock_store
    res_channel = MagicMock()
    res_channel.scalar_one_or_none.return_value = mock_channel
    res_mapping = MagicMock()
    res_mapping.scalar_one_or_none.return_value = mock_mapping

    mock_db.execute.side_effect = [res_store, res_channel, res_mapping]
    mock_db.delete = AsyncMock()
    mock_db.commit = AsyncMock()

    app.dependency_overrides[require_super_admin] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        response = client.delete("/api/v1/stores/10001/channels/SHOPEEMART")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["deleted"] is True
    finally:
        app.dependency_overrides.clear()
