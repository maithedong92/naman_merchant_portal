from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import Response
from starlette.testclient import TestClient

from app.api.deps import get_current_user
from app.main import app
from app.models.user import User, UserRole
from app.services.file_service import file_service
from app.services.haravan_order_service import haravan_order_service


@pytest.fixture
def mock_admin_user():
    return User(
        id="user-superadmin",
        username="admin",
        email="admin@naman.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


def test_file_service_validate_file():
    """Kiểm tra validation định dạng và dung lượng file đính kèm."""
    # Hợp lệ
    valid, err = file_service.validate_file("invoice.pdf", 1024)
    assert valid is True
    assert err == ""

    valid, err = file_service.validate_file("bill_scan.PNG", 2048)
    assert valid is True

    # Định dạng không hỗ trợ
    valid, err = file_service.validate_file("malicious.exe", 1024)
    assert valid is False
    assert "không được hỗ trợ" in err

    # Rỗng
    valid, err = file_service.validate_file("empty.pdf", 0)
    assert valid is False
    assert "rỗng" in err

    # Quá dung lượng cho phép (> 15MB)
    valid, err = file_service.validate_file("huge.pdf", 20 * 1024 * 1024)
    assert valid is False
    assert "vượt quá giới hạn" in err


def test_file_service_clean_file_url():
    """Kiểm tra chuẩn hóa URL tương thích 100% với pmdonhang."""
    # URL rỗng
    assert file_service.clean_file_url(None) == ""
    assert file_service.clean_file_url("") == ""

    # URL tuyệt đối
    url = "https://namanapi.namanmarket.com/orders/2026/10/test.pdf"
    assert file_service.clean_file_url(url) == url

    # Windows path chứa /orders/
    win_path = "C:\\uploadIMG\\orders\\2026\\10\\invoice.pdf"
    cleaned = file_service.clean_file_url(win_path)
    assert "https://namanapi.namanmarket.com/orders/invoice.pdf?image_path=invoice.pdf" in cleaned

    # Path chứa /uploadIMG/
    img_path = "/uploadIMG/sample.png"
    assert file_service.clean_file_url(img_path) == "https://namanapi.namanmarket.com/images/sample.png"

    # Path tương đối
    rel_path = "/custom/file.pdf"
    assert file_service.clean_file_url(rel_path) == "https://namanapi.namanmarket.com/custom/file.pdf"


@pytest.mark.asyncio
async def test_file_service_upload_file_mocked():
    """Kiểm tra upload file lên server lưu trữ."""
    mock_resp = Response(
        200,
        json={"status": "success", "order_url": "https://namanapi.namanmarket.com/orders/2026/10/mock.pdf"},
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        success, url, err = await file_service.upload_file(b"%PDF-1.4 test", "mock.pdf", "order")
        assert success is True
        assert url == "https://namanapi.namanmarket.com/orders/2026/10/mock.pdf"
        assert err == ""


@pytest.mark.asyncio
async def test_file_service_download_bytes_mocked():
    """Kiểm tra proxy download file bytes."""
    mock_resp = Response(
        200,
        content=b"%PDF-1.4 sample content",
        headers={"content-type": "application/pdf"},
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        success, content, ctype, fname = await file_service.download_file_bytes(
            "https://namanapi.namanmarket.com/orders/sample.pdf"
        )
        assert success is True
        assert content == b"%PDF-1.4 sample content"
        assert ctype == "application/pdf"
        assert fname == "sample.pdf"


@pytest.mark.asyncio
async def test_haravan_order_service_attach_and_remove_file(mock_admin_user):
    """Kiểm tra HaravanOrderService cập nhật đúng trường internal và lịch sử audit."""
    sample_order = {
        "id": 999111,
        "order_number": "#NA999111",
        "internal": {
            "order_processing_status": "Đã phân tuyến",
            "history": [],
        },
    }

    mock_col = MagicMock()
    with patch.object(haravan_order_service, "_get_orders_collection", return_value=mock_col):
        with patch.object(haravan_order_service, "get_order_detail", new_callable=AsyncMock) as mock_get_detail:
            mock_get_detail.return_value = sample_order

            # 1. Attach file đơn hàng
            updated = await haravan_order_service.attach_order_file(
                order_id_or_sn="999111",
                file_type="order",
                file_url="https://namanapi.namanmarket.com/orders/2026/10/invoice.pdf",
                file_name="invoice.pdf",
                current_user=mock_admin_user,
            )
            assert updated is not None
            mock_col.update_one.assert_called()
            args, kwargs = mock_col.update_one.call_args
            update_payload = args[1]
            assert update_payload["$set"]["internal.order_file"] == "https://namanapi.namanmarket.com/orders/2026/10/invoice.pdf"
            assert update_payload["$set"]["internal.order_file_name"] == "invoice.pdf"
            assert update_payload["$set"]["internal.order_file_uploaded_by"] == "admin"
            assert update_payload["$push"]["internal.history"]["action"] == "Upload file đơn hàng"

            # 2. Attach file vận đơn
            await haravan_order_service.attach_order_file(
                order_id_or_sn="999111",
                file_type="tracking",
                file_url="https://namanapi.namanmarket.com/orders/2026/10/tracking.pdf",
                file_name="tracking.pdf",
                current_user=mock_admin_user,
            )
            args, kwargs = mock_col.update_one.call_args
            update_payload = args[1]
            assert update_payload["$set"]["internal.tracking_file"] == "https://namanapi.namanmarket.com/orders/2026/10/tracking.pdf"
            assert update_payload["$push"]["internal.history"]["action"] == "Upload file vận đơn"

            # 3. Remove file
            await haravan_order_service.remove_order_file(
                order_id_or_sn="999111",
                file_type="order",
                current_user=mock_admin_user,
            )
            args, kwargs = mock_col.update_one.call_args
            update_payload = args[1]
            assert "internal.order_file" in update_payload["$unset"]
            assert update_payload["$push"]["internal.history"]["action"] == "Xóa file đơn hàng"


def test_api_upload_order_file_endpoint(mock_admin_user):
    """Kiểm tra API POST /api/v1/haravan/orders/{order_id}/files/upload."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    sample_order = {
        "id": 999111,
        "order_number": "#NA999111",
        "internal": {},
    }

    with patch.object(file_service, "upload_file", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = (True, "https://namanapi.namanmarket.com/orders/test.pdf", "")
        with patch.object(haravan_order_service, "attach_order_file", new_callable=AsyncMock) as mock_attach:
            mock_attach.return_value = sample_order

            response = client.post(
                "/api/v1/haravan/orders/999111/files/upload",
                files={"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")},
                data={"file_type": "order"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["data"]["file_info"]["name"] == "test.pdf"
            assert data["data"]["file_info"]["url"] == "https://namanapi.namanmarket.com/orders/test.pdf"

    app.dependency_overrides.clear()


def test_api_delete_order_file_endpoint(mock_admin_user):
    """Kiểm tra API DELETE /api/v1/haravan/orders/{order_id}/files/{file_type}."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    sample_order = {"id": 999111, "order_number": "#NA999111", "internal": {}}

    with patch.object(haravan_order_service, "remove_order_file", new_callable=AsyncMock) as mock_remove:
        mock_remove.return_value = sample_order

        response = client.delete("/api/v1/haravan/orders/999111/files/order")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "gỡ bỏ" in data["message"].lower()

    app.dependency_overrides.clear()


def test_api_download_proxy_endpoint(mock_admin_user):
    """Kiểm tra API GET /api/v1/haravan/orders/{order_id}/files/{file_type}/download."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    sample_order = {
        "id": 999111,
        "order_number": "#NA999111",
        "internal": {
            "order_file": "https://namanapi.namanmarket.com/orders/test.pdf",
        },
    }

    with patch.object(haravan_order_service, "get_order_detail", new_callable=AsyncMock) as mock_detail:
        mock_detail.return_value = sample_order
        with patch.object(file_service, "download_file_bytes", new_callable=AsyncMock) as mock_dl:
            mock_dl.return_value = (True, b"%PDF-1.4 binary content", "application/pdf", "test.pdf")

            response = client.get("/api/v1/haravan/orders/999111/files/order/download")
            assert response.status_code == 200
            assert response.content == b"%PDF-1.4 binary content"
            assert response.headers["content-type"] == "application/pdf"
            assert "test.pdf" in response.headers["content-disposition"]

    app.dependency_overrides.clear()
