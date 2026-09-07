from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_user, get_db
from app.main import app
from app.models.user import User, UserRole
from app.schemas.report import (
    ChannelAnalyticsItem,
    DailyTrendItem,
    FinancialSummary,
    FullAnalyticsReport,
    ReconciliationOrderItem,
    StoreAnalyticsItem,
)
from app.services.report_service import report_service


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_admin_user():
    return User(
        id="admin-uuid-reports",
        username="admin",
        email="admin@namanmarket.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


def test_admin_reports_html_route(client):
    """GET /admin/reports renders HTML template successfully."""
    response = client.get("/admin/reports")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Báo Cáo Phân Tích & Đối Soát Doanh Thu" in response.text
    assert "chart-daily-trend" in response.text
    assert "chart-channel-share" in response.text


def test_get_analytics_endpoint(client, mock_admin_user):
    """GET /api/v1/reports/analytics returns structured analytics data."""
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_report = FullAnalyticsReport(
        filter_from="2026-09-01",
        filter_to="2026-09-07",
        summary=FinancialSummary(
            total_gross_revenue=15000000.0,
            completed_revenue=12000000.0,
            total_subtotal=14500000.0,
            total_discounts=500000.0,
            total_delivery_fees=1000000.0,
            estimated_commission=3000000.0,
            estimated_net_payout=12000000.0,
            total_orders=30,
            completed_orders=24,
            cancelled_orders=3,
            active_orders=3,
            completion_rate=80.0,
            cancellation_rate=10.0,
            average_order_value=555555.56,
        ),
        channels=[
            ChannelAnalyticsItem(
                channel_code="SHOPEEFOOD",
                channel_name="ShopeeFood VN",
                order_count=18,
                completed_count=15,
                cancelled_count=2,
                revenue=9000000.0,
                share_percentage=60.0,
            ),
            ChannelAnalyticsItem(
                channel_code="GRABMART",
                channel_name="GrabMart VN",
                order_count=12,
                completed_count=9,
                cancelled_count=1,
                revenue=6000000.0,
                share_percentage=40.0,
            ),
        ],
        stores=[
            StoreAnalyticsItem(
                store_code="10001",
                store_name="Nam An Market - 21 Thảo Điền",
                order_count=16,
                completed_count=13,
                cancelled_count=1,
                revenue=8000000.0,
                share_percentage=53.3,
            )
        ],
        daily_trends=[
            DailyTrendItem(
                date="2026-09-07",
                revenue=2500000.0,
                order_count=5,
                completed_count=4,
            )
        ],
    )

    with patch.object(report_service, "get_full_analytics", new=AsyncMock(return_value=fake_report)):
        response = client.get("/api/v1/reports/analytics")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["data"]["summary"]["total_gross_revenue"] == 15000000.0
        assert len(payload["data"]["channels"]) == 2
        assert payload["data"]["channels"][0]["channel_code"] == "SHOPEEFOOD"

    app.dependency_overrides.clear()


def test_get_reconciliation_endpoint(client, mock_admin_user):
    """GET /api/v1/reports/reconciliation returns paginated orders list."""
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_items = [
        ReconciliationOrderItem(
            id="rec-order-1",
            order_code="NAMAN-SPF-001",
            channel_order_id="SPF-9991",
            display_order_id="SPF-991",
            channel_code="SHOPEEFOOD",
            channel_name="ShopeeFood VN",
            store_code="10001",
            store_name="Nam An Market - 21 Thảo Điền",
            status="DELIVERED",
            order_time=datetime.now(timezone.utc),
            customer_name="Trần Văn Nam",
            subtotal_amount=500000.0,
            discount_amount=30000.0,
            delivery_fee=20000.0,
            total_amount=490000.0,
            estimated_commission=98000.0,
            estimated_net_amount=392000.0,
            payment_method="ONLINE",
        )
    ]

    with patch.object(report_service, "get_reconciliation_orders", new=AsyncMock(return_value=(fake_items, 1))):
        response = client.get("/api/v1/reports/reconciliation?page=1&page_size=10")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert len(payload["data"]["items"]) == 1
        assert payload["data"]["meta"]["total_items"] == 1
        assert payload["data"]["items"][0]["order_code"] == "NAMAN-SPF-001"

    app.dependency_overrides.clear()


def test_export_reconciliation_csv(client, mock_admin_user):
    """GET /api/v1/reports/export returns CSV file download with proper headers."""
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_csv = "\ufeffMã Đơn Nội Bộ,Mã Đơn Sàn,Mã Hiển Thị,Kênh Bán\nNAMAN-001,SPF-1,SPF-1,SHOPEEFOOD"

    with patch.object(report_service, "export_reconciliation_csv", new=AsyncMock(return_value=fake_csv)):
        response = client.get("/api/v1/reports/export")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment; filename=naman_doi_soat_" in response.headers["content-disposition"]
        assert "NAMAN-001" in response.text

    app.dependency_overrides.clear()
