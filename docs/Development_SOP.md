# Quy Chuẩn Phát Triển & Vận Hành Hệ Thống (Development SOP)
## Nam An Market - Unified Merchant Portal (NAM-UMP)

> **Tài liệu này xác lập các tiêu chuẩn kỹ thuật bắt buộc (Standard Operating Procedures - SOP) dành cho toàn bộ kỹ sư phần mềm tham gia thiết kế, phát triển và bảo trì hệ thống Nam An Merchant Portal.**

---

## 1. Nguyên Tắc Kiến Trúc Cốt Lõi (Core Architectural Principles)

### 1.1. Kiến trúc Multi-Module Độc Lập ("Không Chồng Lấn" - Zero Module Coupling)
Hệ thống là một **Modular Monolith** kết hợp với kiến trúc **Ports and Adapters (Hexagonal Architecture)**.
1. **Core Module là Single Source of Truth (SSOT):**
   - Nắm giữ toàn bộ dữ liệu nghiệp vụ chuẩn (Canonical Domain Models): Stores (Chi nhánh), Channels (Kênh), Unified Products (Hàng hóa), Inventories (Tồn kho), Orders (Đơn hàng), Sync Logs (Nhật ký đồng bộ).
   - Core định nghĩa các **Ports / Interfaces** trừu tượng (`BaseChannelAdapter`, `IMenuSyncService`, `IOrderService`).
2. **Channel Modules là các Adapter cắm ngoài (Pluggable Adapters):**
   - Mỗi kênh bán lẻ (`shopeefood`, `grabmart`, `shopeemart`,...) là một module độc lập hoàn toàn nằm trong thư mục `app/modules/<channel_name>/`.
   - Mỗi channel module tự quản lý:
     - DTO/Schemas riêng biệt theo tài liệu API của sàn đó.
     - Cơ chế xác thực (OAuth2 token, HMAC-SHA256 signature, API Key).
     - Router tiếp nhận Webhook riêng (`/api/v1/<channel_name>/webhooks/...`).
     - Logic chuyển đổi 2 chiều (Bidirectional Mapping): Từ Canonical Model của Core sang Channel Payload, và từ Webhook Payload của sàn sang Canonical Order Model.
3. **Quy tắc biên giới (Strict Boundary Rules):**
   - ❌ **CẤM:** Các module `shopeefood`, `grabmart`, `shopeemart` tuyệt đối không import trực tiếp bất kỳ class, function hoặc schema nào của nhau (Zero module-to-module dependencies).
   - ❌ **CẤM:** Lưu logic đặc thù của một sàn (ví dụ: công thức tính signature của Foody/Shopee hay token Grab) vào bên trong `app/core/` hay `app/models/`.
   - ✅ **ĐÚNG:** Mọi giao tiếp giữa các module hoặc giữa Core và Module đều phải thông qua **Core Interfaces** (`BaseChannelAdapter`) hoặc thông qua **Service Registry / Event Bus**.

---

## 2. Quy Chuẩn Lập Trình Python & FastAPI (Code Conventions)

### 2.1. Chuẩn hóa Type Hinting & Pydantic v2
- Mọi hàm, method, endpoint bắt buộc phải có đầy đủ Type Annotations (PEP 484, PEP 585):
  ```python
  # ✅ ĐÚNG:
  async def get_store_inventory(
      store_id: str, 
      sku_list: list[str], 
      db: AsyncSession
  ) -> dict[str, int]: ...

  # ❌ CẤM:
  async def get_store_inventory(store_id, sku_list, db): ...
  ```
- Sử dụng **Pydantic v2** (`BaseModel`, `Field`, `ConfigDict`):
  - Luôn sử dụng `model_config = ConfigDict(from_attributes=True)` khi map từ SQLAlchemy Model sang Pydantic.
  - Sử dụng `Field(..., description="...")` để Swagger UI tự động hiển thị tài liệu rõ ràng.

### 2.2. Lập trình Bất Đồng Bộ (Async-First)
- Toàn bộ các thao tác I/O (truy vấn Database PostgreSQL, gọi HTTP request tới ShopeeFood/GrabMart, đọc ghi Cache Redis) **bắt buộc phải là `async / await`**.
- Tuyệt đối không sử dụng thư viện đồng bộ gây nghẽn Event Loop:
  - ❌ Không dùng `requests` -> ✅ Dùng `httpx.AsyncClient`
  - ❌ Không dùng `psycopg2` -> ✅ Dùng `asyncpg` / `SQLAlchemy AsyncSession`
  - ❌ Không dùng `time.sleep()` -> ✅ Dùng `asyncio.sleep()`

### 2.3. Quy Chuẩn Xử Lý Lỗi & Định Dạng Response Chuẩn (Unified API Response)
Mọi API trả về cho Frontend hoặc Hệ thống ngoài đều phải tuân thủ schema đồng nhất:
```json
{
  "success": true,
  "data": { ... },
  "message": "Thao tác thành công",
  "error_code": null,
  "timestamp": "2026-09-04T08:45:00Z"
}
```
Khi có lỗi xảy ra:
```json
{
  "success": false,
  "data": null,
  "message": "Không tìm thấy chi nhánh với mã 10001",
  "error_code": "STORE_NOT_FOUND",
  "timestamp": "2026-09-04T08:45:00Z"
}
```
- Không bao giờ để lộ traceback hay raw exception ra response của client.
- Tạo các custom exception kế thừa từ `AppException` trong `app/core/exceptions.py`.

---

## 3. Quy Chuẩn Cơ Sở Dữ Liệu PostgreSQL & Alembic (Database SOP)

### 3.1. Quy ước Đặt Tên (Naming Conventions)
- **Tên bảng:** Viết thường số nhiều, nối bằng dấu gạch dưới (snake_case), ví dụ: `stores`, `channels`, `products`, `unified_orders`, `order_items`.
- **Khóa chính:** Luôn là `id` (UUIDv4 hoặc BigInteger tùy yêu cầu hiệu năng). Khuyến nghị dùng UUID string cho các thực thể phân tán, và BigInt cho bảng log lớn.
- **Khóa ngoại:** Dạng `<tên_bảng_số_ít>_id`, ví dụ: `store_id`, `channel_id`, `order_id`.
- **Chỉ mục (Indexes):**
  - Đánh index bắt buộc cho các trường thường xuyên query / filter: `store_id`, `sku`, `status`, `created_at`.
  - Đánh Unique Index phức hợp cho các quan hệ 1-1 logic, ví dụ: `(channel_id, channel_order_id)` để đảm bảo tính Idempotency khi nhận Webhook đơn hàng.

### 3.2. Quản Lý Migration với Alembic
- Tuyệt đối không thay đổi schema trực tiếp trên cơ sở dữ liệu production bằng tay.
- Mọi thay đổi model trong `app/models/` đều phải được tạo migration thông qua Alembic:
  ```bash
  alembic revision --autogenerate -m "create_unified_orders_and_items"
  alembic upgrade head
  ```
- Kiểm tra kỹ file migration được sinh ra trước khi commit vào git. Đảm bảo có đầy đủ hàm `upgrade()` và `downgrade()`.

### 3.3. Xử Lý Transaction & Concurrency (Đơn Hàng & Tồn Kho)
- Thao tác cập nhật tồn kho khi nhận đơn hàng mới phải sử dụng Database Transaction rõ ràng:
  - Sử dụng `SELECT ... FOR UPDATE` khi trừ tồn kho để chống Race Condition khi nhiều đơn hàng về cùng một thời điểm.
  - Sử dụng `AuditLog` ghi lại lịch sử biến động số lượng tồn cho từng SKU theo từng store.

---

## 4. Quy Chuẩn Triển Khai Channel Adapter (Channel Integration SOP)

Mỗi kênh bán lẻ mới khi tích hợp vào hệ thống **bắt buộc** phải tuân thủ quy trình 4 bước:

### Bước 1: Khởi tạo Cấu trúc Module
Tạo thư mục trong `app/modules/<channel_name>/` với các file bắt buộc:
```
app/modules/<channel_name>/
├── __init__.py
├── adapter.py      # Class kế thừa BaseChannelAdapter
├── router.py       # FastAPI router cho Webhook sàn
├── schemas.py      # Pydantic schemas đặc thù của sàn
└── service.py      # Client gọi API ngoài & xử lý thuật toán riêng (signature, auth)
```

### Bước 2: Kế Thừa & Thực Thi `BaseChannelAdapter`
Trong `adapter.py`:
```python
from app.interfaces.channel_adapter import BaseChannelAdapter

class ShopeeFoodChannelAdapter(BaseChannelAdapter):
    @property
    def channel_code(self) -> str:
        return "SHOPEEFOOD"

    async def sync_menu(self, store_id: str, menu_data: UnifiedMenu) -> SyncResult:
        # Chuyển đổi UnifiedMenu -> ShopeeFood format & gọi API
        ...

    async def update_stock(self, store_id: str, stock_updates: list[StockUpdateItem]) -> SyncResult:
        ...

    async def handle_order_webhook(self, payload: dict, headers: dict) -> WebhookProcessResult:
        # Xác thực signature -> Parse payload -> Convert sang UnifiedOrder -> Gọi Core OrderService
        ...

    async def update_order_status(self, channel_order_id: str, new_status: UnifiedOrderStatus) -> bool:
        ...
```

### Bước 3: Đăng Ký Adapter vào `ChannelRegistry`
Đăng ký adapter trong file `app/services/channel_registry.py` hoặc tự động nạp qua lifespan của `app/main.py`.

### Bước 4: Đảm Bảo Tính Idempotency (Chống Trùng Lặp Webhook)
Các sàn giao đồ ăn/bán lẻ thường gửi lại Webhook nhiều lần (retry mechanism) khi mạng chập chờn.
- Channel module phải kiểm tra bảng `sync_logs` / `webhook_audits` theo `request_id` hoặc `(channel_code, event_id, channel_order_id)`.
- Nếu sự kiện đã được xử lý thành công trước đó, trả về HTTP 200 ngay lập tức mà không xử lý lại đơn hàng lần 2.

---

## 5. Quy Chuẩn Giao Diện (Frontend Design SOP)
Tuân thủ nghiêm ngặt tài liệu triết lý thiết kế **"The Living Canvas"** tại [`docs/design.md`](file:///c:/python/naman_merchant_portal/docs/design.md):

1. **The "No-Line" Rule:**
   - Tuyệt đối không dùng đường viền solid 1px (`border-gray-200`) để chia tách các vùng nội dung.
   - Sử dụng chuyển đổi màu nền để tạo phân vùng: `surface` (`#fbf9f8`) -> `surface-container-low` (`#f6f3f2`) -> `surface-container-lowest` (`#ffffff`).
2. **Hệ Thống Màu Sắc (Color Palette):**
   - **Primary:** Deep Organic Green (`#004d37`), Hover: (`#00674b`).
   - **Backgrounds:** `#F5F7FA` (Page), `#FFFFFF` (Card/Form).
   - **Text:** Tuyệt đối không dùng đen tuyệt đối (`#000000`). Luôn dùng `on-surface` Charcoal (`#1b1c1c`) hoặc Slate (`#3f4944`).
   - **Accent / Organic Chip:** Lime Green (`#b8f649`) cho nhãn "Organic", "Fresh", "Local".
3. **Typography:**
   - **Display / Headlines:** Plus Jakarta Sans (Rounded, organic feel).
   - **Body / Content:** Work Sans (Neutral, readable).
   - **Labels / Micro-copy:** Inter.

---

## 6. Quy Chuẩn An Toàn & Bảo Mật (Security & Secret SOP)

1. **Không Hardcode Secrets:**
   - Tuyệt đối cấm commit API Key, App Secret, Private Key, Database Password, JWT Secret vào kho mã nguồn.
   - Mọi thông tin nhạy cảm phải được đặt trong file `.env` và khai báo trong `app/core/config.py` bằng `pydantic-settings`.
2. **Xác Thực Chữ Ký Webhook:**
   - Mọi Webhook từ bên ngoài (ShopeeFood, GrabMart) phải qua middleware hoặc dependency xác thực tính hợp lệ của chữ ký (HMAC-SHA256, Authorization Header) trước khi tiếp nhận vào hệ thống.
3. **Quản Lý Chứng Chỉ SSL (Certs):**
   - Các file chứng chỉ (`.pfx`, `.pem`, `.key`, `.crt`) đã được đưa vào `.gitignore`. Khi triển khai, chứng chỉ được mount thông qua Docker secret hoặc Environment Path.

---

## 7. Quy Trình Git & Quản Lý Mã Nguồn (Git Workflow)

### 7.1. Chiến Lược Nhánh (Branching Strategy)
- `main`: Nhánh production. Chỉ merge thông qua Pull Request sau khi được test kỹ lưỡng.
- `develop`: Nhánh tích hợp chính của đội ngũ dev.
- `feature/<tên-tính-năng>`: Nhánh phát triển tính năng mới. Ví dụ:
  - `feature/core-order-engine`
  - `feature/shopeefood-menu-sync`
  - `feature/grabmart-pos-adapter`
- `bugfix/<tên-lỗi>`: Nhánh sửa lỗi.

### 7.2. Chuẩn Đặt Tên Commit (Conventional Commits)
Mỗi commit message phải tuân theo cấu trúc: `<type>(<scope>): <mô tả ngắn gọn>`:
- `feat(core)`: Thêm tính năng quản lý multi-store inventory.
- `feat(shopeefood)`: Thêm thuật toán tạo chữ ký HMAC-SHA256 cho S2S menu sync.
- `feat(grabmart)`: Triển khai flow lấy OAuth2 access token.
- `fix(order)`: Khắc phục lỗi cập nhật trạng thái đơn hàng khi bị trùng webhook.
- `docs`: Cập nhật tài liệu SOP và README.
- `refactor`: Tái cấu trúc mã nguồn không làm thay đổi hành vi nghiệp vụ.

---

## 8. Danh Sách Kiểm Tra Trước Khi Merge Code (PR Checklist)
Trước khi tạo Pull Request, lập trình viên bắt buộc kiểm tra:
- [ ] Code không vi phạm nguyên tắc "Không chồng lấn" giữa các channel module.
- [ ] Đã có đầy đủ Type Hinting trên toàn bộ functions/methods.
- [ ] Không có secret / password / key nào bị commit vào git.
- [ ] Các thao tác I/O đều là `async / await`.
- [ ] File migration của Alembic (nếu có thay đổi database) đã được test upgrade và downgrade.
- [ ] Swagger Docs (`/docs`) hiển thị đầy đủ mô tả cho endpoint mới.
