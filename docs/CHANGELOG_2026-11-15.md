# Nhật Ký Thay Đổi Kỹ Thuật (Changelog) — 2026-11-15

Tài liệu tổng hợp toàn bộ các hạng mục nâng cấp, sửa lỗi, tối ưu hiệu năng, bảo mật và kiểm thử đã hoàn thiện trong ngày **15/11/2026** trên hệ thống **Nam An Unified Merchant Portal (NAM-UMP)**.

---

## 1. Độ tin cậy Đơn hàng, Webhook & Đồng bộ đa kênh (Track 1)

- **Fail-closed 503 khi mất kết nối DB**:
  - Củng cố xử lý lỗi kết nối cơ sở dữ liệu tại tầng dependency/middleware (`app/api/deps.py`, `app/main.py`) và endpoint readiness (`/api/v1/health/db`) để trả về HTTP `503 Service Unavailable` thay vì âm thầm bỏ qua hoặc trả lỗi 500 thiếu ngữ cảnh khi PostgreSQL gián đoạn.
- **Loại bỏ mock menu GrabMart**:
  - Gỡ bỏ dữ liệu menu giả lập (mock) trong luồng adapter/service của GrabMart (`app/modules/grabmart/adapter.py`, `app/modules/grabmart/service.py`, `app/modules/grabmart/router.py`), đảm bảo dữ liệu đồng bộ phản ánh đúng trạng thái thực tế từ DB và API đối tác.
- **Chặn định tuyến nhầm chi nhánh (`UNMAPPED_STORE_ORDER`)**:
  - Tại `OrderService.create_or_get_inbound_order` (`app/services/order_service.py`), khi `partner_store_id` không khớp `StoreChannelMapping` hoặc `Store.code`, hệ thống **không tự gán về chi nhánh fallback**. Thay vào đó, ghi nhận `OperationalErrorLog` mức `CRITICAL` với mã `UNMAPPED_STORE_ORDER` và ném `ValidationError` để bảo vệ dữ liệu đơn hàng giữa các chi nhánh.
  - Chỉ tự tạo `StoreChannelMapping` khi đã khớp chính xác `Store.code`.
- **Poppies ERP Sync đa chi nhánh & hỗ trợ kênh `SHOPEEMART`**:
  - Cập nhật luồng đồng bộ Poppies ERP (`app/services/poppies_service.py`, `app/services/poppies_channel_push.py`, `app/services/poppies_sync_worker.py`, `app/services/poppies_webhook_handler.py`) hỗ trợ đồng bộ tồn kho đa chi nhánh chính xác và đẩy cập nhật trạng thái/tồn kho cho kênh `SHOPEEMART` bên cạnh `SHOPEEFOOD` và `GRABMART`.
- **Haravan Advisory Lock & Graceful Worker Shutdown (`stop_and_wait()`)**:
  - Bổ sung khóa phân tán PostgreSQL (`SELECT pg_try_advisory_lock(:key)`) cho worker đồng bộ Haravan (`app/modules/haravan/sync_worker.py`) và Poppies (`app/services/poppies_sync_worker.py`) để tránh chạy trùng lặp khi có nhiều worker.
  - Bổ sung phương thức `stop_and_wait()` cho worker và gọi qua `_stop_worker()` trong `lifespan` của `app/main.py` để chờ tác vụ nền kết thúc gọn gàng khi shutdown.
- **Dùng chung `httpx.AsyncClient` (Shared HTTP Client)**:
  - Chuẩn hóa tái sử dụng `httpx.AsyncClient` (`_get_http_client()` / `_get_client()` và `aclose()`) trong `app/modules/grabmart/service.py`, `app/modules/shopeefood/service.py`, `app/modules/shopeemart/service.py`, `app/modules/haravan/client.py` và `app/services/poppies_client.py`, giúp tái sử dụng connection pool và đóng kết nối sạch sẽ khi ứng dụng tắt.

---

## 2. Hiệu năng Báo cáo, Tồn kho, Vòng đời Ứng dụng & DB/Alembic (Track 2)

- **Tối ưu Báo cáo & Đối soát (`app/services/report_service.py`)**:
  - Giới hạn khung thời gian truy vấn mặc định tối đa **30 ngày** (`default_days = 30`) khi không truyền `from_date`/`to_date`.
  - Đẩy tính toán tổng hợp về SQL aggregation thay vì tải toàn bộ bản ghi vào bộ nhớ Python.
  - Xuất CSV theo chunk (`StreamingResponse`) kèm làm sạch cell (`_sanitize_csv_cell`) chống CSV formula injection.
  - Bọc các truy vấn đồng bộ `pymongo` (Haravan/Arito/Auth Mongo) trong `asyncio.to_thread(...)` (`app/services/report_service.py`, `app/api/v1/haravan.py`, `app/services/auth_service.py`) để không block event loop của FastAPI.
- **Chuẩn hóa `available_stock` & Batch-load `.in_()` (`app/services/inventory_service.py`)**:
  - Tập trung công thức `InventoryService.compute_available_stock(total_stock, reserved_stock, safety_stock)` cho toàn hệ thống.
  - Tối ưu truy vấn theo lô bằng `.in_()` để giảm N+1 query khi xử lý danh sách sản phẩm/tồn kho lớn.
- **Vòng đời Ứng dụng & Health Check (`app/main.py`, `app/api/v1/health.py`)**:
  - Chuẩn hóa `lifespan` startup/shutdown: chỉ tự động `Base.metadata.create_all` và seed dữ liệu khi `settings.DEBUG` bật; ở chế độ production chỉ kiểm tra `SELECT 1`.
  - Endpoint `GET /api/v1/health/db` trả về HTTP `503` kèm `error_code="DATABASE_CONNECTION_ERROR"` khi mất kết nối PostgreSQL.
- **Khởi tạo Alembic & Bổ sung Index DB**:
  - Khởi tạo cấu hình Alembic (`alembic.ini`, `alembic/env.py`) với 2 revision:
    - `6b7398920f3d` (`alembic/versions/6b7398920f3d_initial_baseline.py`): Baseline schema toàn bộ bảng và index (`ix_orders_store_status`, `ix_orders_created_at`, `ix_channel_order_unique`, `ix_inventory_stock_logs_created_at`, `ix_error_logs_*`, `ix_audit_security_logs_*`, ...).
    - `a1b2c3d4e5f6` (`alembic/versions/a1b2c3d4e5f6_add_stock_reservation_status.py`): Thêm cột `stock_reservation_status` vào bảng `unified_orders`.
- **Chuẩn hóa Cấu hình Triển khai**:
  - Cập nhật `.env.example`, `docker/Dockerfile`, `docker/nginx/conf.d/default.conf` và `docker/nginx/host-nginx.conf.example` (bổ sung `TRUSTED_PROXY_CIDRS`, `POPPIES_*`, `SHOPEEMART_*`, `HARAVAN_*`, cấu hình rate limit webhook & security headers trên Nginx).

---

## 3. Giao diện, Phân quyền RBAC & Chống XSS (Track 3)

- **Khóa phạm vi chi nhánh (Store Scope Isolation)**:
  - Ràng buộc phạm vi dữ liệu theo `store_id` của người dùng trên giao diện và API cho `app/templates/inventory.html`, `app/templates/admin_dashboard.html` và `app/templates/reports.html`, ngăn chặn thao tác hoặc xem dữ liệu chéo chi nhánh trái phép (IDOR).
- **Ẩn tính năng Admin & tài liệu API khi `DEBUG=False`**:
  - Tắt `/docs`, `/redoc`, `/openapi.json` trong `app/main.py` khi `settings.DEBUG` là `False`.
  - Ẩn các menu/chức năng quản trị nhạy cảm trên giao diện (`app/web/router.py`, `app/templates/base.html`) theo vai trò người dùng và cờ `DEBUG`.
- **Cải thiện Auth Redirect & Responsive Mobile Menu**:
  - Chuẩn hóa luồng chuyển hướng khi hết phiên đăng nhập / chưa xác thực và hoàn thiện menu điều hướng trên thiết bị di động (`app/templates/base.html`, `app/web/router.py`).
- **Chống XSS (`escapeHtml` + `data-*`)**:
  - Bổ sung helper `window.escapeHtml` trong `app/templates/base.html` và áp dụng đồng bộ trên `admin_dashboard.html`, `inventory.html`, `orders.html`, `reports.html`, `system_status.html`, `users.html`; truyền tham số qua thuộc tính `data-*` thay vì nội suy chuỗi trực tiếp vào handler `onclick`.
- **Trạng thái Loading / Error / Retry & Export CSV có Bearer Auth**:
  - Bổ sung trạng thái đang tải, thông báo lỗi rõ ràng kèm nút **Thử lại** trên các bảng dữ liệu chính (`inventory.html`, `reports.html`, `system_status.html`, `users.html`).
  - Luồng xuất CSV trên giao diện gửi kèm `Authorization: Bearer ...` header thay vì tải link trần không xác thực.
- **Hiển thị Tồn ảo trên Kho (`app/templates/inventory.html`)**:
  - Hiển thị tồn kho theo quy chuẩn `{total_stock}-{reserved_stock}` (ví dụ `11-3`) kèm tooltip chi tiết (`Thực tế`, `Giữ chỗ`, `An toàn`, `Khả dụng`) và dòng phụ `Khả dụng`.

---

## 4. Cơ chế Trừ tồn ảo khi phát đơn (Track Virtual Stock)

- Triển khai đầy đủ máy trạng thái `stock_reservation_status` (`NONE` -> `RESERVED` -> `RELEASED` / `CONSUMED`) trong `app/models/order.py` và `app/services/order_service.py`.
- Bảo toàn nguyên tắc **Poppies CTTK là nguồn sự thật cho `total_stock`**, Portal chỉ giữ/nhả `reserved_stock` và không trừ đúp `total_stock` khi hoàn tất giao hàng.
- Áp dụng khóa dòng `StoreInventory` theo thứ tự SKU tăng dần (`_lock_inventories_sorted`) và khóa `UnifiedOrder` `.with_for_update()` để chống race condition & deadlock.
- Thêm biến cấu hình `VIRTUAL_STOCK_STRICT_MODE` (mặc định `False`) trong `app/core/config.py`.
- Xem chi tiết kiến trúc và luồng xử lý tại [VIRTUAL_STOCK_MECHANISM.md](file:///C:/python/naman_merchant_portal/docs/VIRTUAL_STOCK_MECHANISM.md).

---

## 5. Kiểm thử & Chất lượng (QA)

Bổ sung hạ tầng fixture dùng chung (`tests/conftest.py`) và 3 bộ test chuyên sâu mới:

- `tests/test_virtual_stock_reservation.py`: Kiểm thử toàn bộ vòng đời trừ tồn ảo:
  - Giữ tồn khi nhận đơn mới (`test_order_ingestion_reserves_virtual_stock`).
  - Idempotency khi trùng đơn và bắt `IntegrityError` đồng thời (`test_duplicate_order_ingestion_is_idempotent`, `test_concurrent_integrity_error_returns_existing_order`).
  - Hoàn tồn khi hủy đơn (`test_cancel_releases_reserved_stock`).
  - Giải phóng giữ chỗ khi `PICKED_UP` / `DELIVERED` mà không trừ `total_stock` (`test_delivered_and_picked_up_release_reserved_without_touching_total_stock`).
  - Xử lý thiếu tồn ở chế độ mặc định vs `VIRTUAL_STOCK_STRICT_MODE=True` (`test_insufficient_virtual_stock_logs_warning_in_default_mode`, `test_insufficient_virtual_stock_raises_in_strict_mode`).
  - Tương tác với Poppies CTTK sync và Quick Update (`test_poppies_cttk_sync_preserves_reserved_and_safety_stock`, `test_quick_update_stock_respects_reserved_and_safety_stock`).
- `tests/test_api_coverage_core.py`: Kiểm thử bao phủ các API lõi: Auth (login, refresh, logout, change-password, lockout), Users RBAC & security logs, Stores & Channel mapping, Orders update status & cross-store isolation, Inventory/Poppies sync, Haravan/Arito status & error handling.
- `tests/test_security_hardening.py`: Kiểm thử bảo mật: Fail-closed `401` khi chưa xác thực, RBAC `403` cho nhân viên trên endpoint Admin/Manager, chống IDOR chéo chi nhánh `403`, chống CSRF/XSS trên ShopeeMart OAuth callback, chặn SSRF URL nội bộ, và che giấu credentials trong header log (`redact_sensitive_headers`).

---

## 6. Ghi chú Vận hành & Checklist trước khi Redeploy

> [!WARNING]
> **Trạng thái hiện tại trên VPS**: Container `naman_portal_app` trên VPS hiện **vẫn đang tạm tắt (stopped)**. Không tự động bật lại cho đến khi hoàn tất rà soát cấu hình `.env` và chạy database migration (`alembic upgrade head`).

### Danh sách biến `.env` cần kiểm tra / cấu hình trước khi redeploy

Tham chiếu theo `.env.example`:

1. **Môi trường & Bảo mật lõi**:
   - `ENVIRONMENT=production` và `DEBUG=False` (bắt buộc trên production để tắt `/docs`, `/redoc`, `/openapi.json` và tắt auto `create_all`).
   - `ALLOWED_ORIGINS` (domain thực tế, ví dụ `["https://portal.namanmarket.com"]`).
   - `TRUSTED_PROXY_CIDRS` (CIDR của Nginx / reverse proxy nội bộ).
   - `INTERNAL_API_SECRET` (chuỗi ngẫu nhiên tối thiểu 32 bytes).
   - `JWT_SECRET_KEY` (chuỗi ngẫu nhiên tối thiểu 64 bytes).
   - `INITIAL_ADMIN_PASSWORD` (mật khẩu mạnh nếu seed lần đầu).
2. **Cơ sở dữ liệu PostgreSQL & MongoDB**:
   - `DATABASE_URL` (chuỗi kết nối PostgreSQL `postgresql+asyncpg://...`).
   - `MONGO_URI`, `MONGO_DB`, `MONGO_ORDERS_COLLECTION`, `ARITO_DB`, `ARITO_INVENTORY_COLLECTION`.
   - `AUTH_MONGO_ENABLED`, `AUTH_MONGO_URI`, `AUTH_MONGO_DB`, `AUTH_MONGO_COLLECTION`.
3. **Cơ chế Trừ tồn ảo**:
   - `VIRTUAL_STOCK_STRICT_MODE=False` (giữ mặc định `False` để không làm rớt đơn sàn khi lệch tồn tạm thời; chỉ bật `True` khi có yêu cầu nghiệp vụ chặn đơn tuyệt đối).
4. **Tích hợp Poppies ERP, Haravan & Các Sàn**:
   - `POPPIES_API_BASE_URL`, `POPPIES_PRICING_BASE_URL`, `POPPIES_POS_PRICING_BASE_URL`, `POPPIES_API_KEY`, `POPPIES_WEBHOOK_KEY`, `POPPIES_TIMEOUT_SECONDS`, `POPPIES_SYNC_ENABLED`, `POPPIES_SYNC_INTERVAL_SECONDS`.
   - `HARAVAN_ENABLED`, `HARAVAN_TOKEN`, `HARAVAN_BASE_URL`, `HARAVAN_SYNC_INTERVAL_SECONDS`, `HARAVAN_SYNC_LIMIT`.
   - `SHOPEEFOOD_*`, `GRABMART_*` (bao gồm `GRABMART_INBOUND_BEARER_TOKEN`), `SHOPEEMART_*` (`SHOPEEMART_PARTNER_ID`, `SHOPEEMART_PARTNER_KEY`, `SHOPEEMART_SHOP_ID`).

### Trình tự khuyến nghị khi Redeploy

1. Cập nhật file `.env` trên VPS theo checklist trên.
2. Chạy migration schema: `alembic upgrade head` (đảm bảo đã lên revision `a1b2c3d4e5f6`).
3. Build lại image và khởi động container `naman_portal_app`.
4. Kiểm tra `/api/v1/health` và `/api/v1/health/db` trả về `200 OK` trước khi mở lưu lượng webhook từ Nginx.
