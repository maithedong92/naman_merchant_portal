# Nam An Market - Unified Merchant Portal (NAM-UMP)

> **Cổng Quản Trị Đa Kênh & Tích Hợp Bán Hàng Trực Tuyến Hợp Nhất của Nam An Market**  
> Kết nối, đồng bộ thực đơn, giá cả, tồn kho và xử lý đơn hàng đa kênh từ các đối tác thương mại điện tử & giao nhận nhanh: **ShopeeFood, GrabMart, Shopee,...**

---

## 1. Bối Cảnh & Mục Tiêu Dự Án (Context & Objectives)

**Nam An Market** là chuỗi siêu thị cao cấp chuyên cung cấp thực phẩm sạch, thực phẩm hữu cơ và hàng nhập khẩu chất lượng cao với nhiều chi nhánh (Thảo Điền, An Phú,...). Để mở rộng kênh tiếp cận khách hàng trực tuyến, Nam An kết nối với nhiều nền tảng bán hàng và giao nhận nhanh hàng đầu như:
- **ShopeeFood** (Foody External API v7.0+)
- **GrabMart** (GrabMart Partner POS API v1.1.3+)
- **Shopee E-Commerce** (Shopee Open Platform)
- Các kênh tiềm năng trong tương lai (TikTok Shop, GrabFood, Baemin, Tiki,...)

Mỗi kênh bán lẻ có quy tắc phân loại danh mục (category taxonomy), định dạng menu (regular menu, service hours, modifiers), cơ chế định danh món/SKU, giao thức chữ ký bảo mật (HMAC-SHA256, OAuth2, App ID/Key) và vòng đời đơn hàng (order lifecycle) hoàn toàn khác nhau.

**Nam An Merchant Portal** được thiết kế để đóng vai trò là **Portal Tổng (Single Source of Truth & Central Hub)**:
1. **Show & Xử lý đơn hàng tập trung (Unified Order Feed & Processing):** Tiếp nhận đơn hàng real-time từ tất cả các kênh thông qua Webhook, chuẩn hóa trạng thái đơn (`PENDING` -> `ACCEPTED` -> `PREPARING` -> `READY` -> `PICKED_UP` -> `DELIVERED`), cập nhật trạng thái ngược về sàn và điều phối nhân viên siêu thị gom hàng.
2. **Đồng bộ Menu & Giá cả (Catalog & Menu Synchronization):** Quản lý danh mục, thông tin sản phẩm, đơn vị tính, hình ảnh và giá bán một nơi; tự động chuyển đổi sang schema chuẩn của từng kênh và đẩy lên sàn.
3. **Đồng bộ Tồn kho Real-Time theo từng Chi nhánh (Multi-Store Inventory Sync):** Phân chia tồn kho theo từng cửa hàng đối tác (Partner Merchant ID: 10001, 10004, 10005, 10006,...), tự động cập nhật tồn kho khả dụng từ ERP nội bộ (Arito / Haravan) sang các kênh và tắt món tức thì khi hết hàng để tránh hủy đơn ngoài ý muốn.

---

## 2. Kiến Trúc Hệ Thống: Multi-Module Architecture

Hệ thống được thiết kế theo mô hình **Multi-Module Monolith (Clean Architecture & Hexagonal/Ports-and-Adapters Pattern)**:
- **Core Module là trung tâm:** Nắm giữ toàn bộ dữ liệu nghiệp vụ chuẩn (Canonical Domain Models: Stores, Channels, Unified Products, Orders, Inventory, Sync Logs), State Machine xử lý đơn hàng và Base Interface / Abstraction Layer.
- **Các App Kênh (Channel Modules: ShopeeFood, GrabMart, Shopee) hoàn toàn độc lập:** Mỗi module là một "app" tự quản lý schema đối tác, cơ chế xác thực, client gọi API sàn và router webhook riêng biệt.
- **Nguyên tắc "Không chồng lấn" (Zero Coupling Between Modules):** Module ShopeeFood tuyệt đối không phụ thuộc hay import trực tiếp từ Module GrabMart và ngược lại. Tất cả giao tiếp thông qua **Core Interfaces & Event Bus**. Khi cần thêm một kênh bán hàng mới, đội ngũ phát triển chỉ cần cắm thêm (plug-in) adapter mới mà không gây ảnh hưởng đến các kênh đang hoạt động ổn định.

```mermaid
flowchart TD
    subgraph External_Channels["Đối Tác Bán Hàng (External Channels)"]
        SF["ShopeeFood API\n(Foody Partner API)"]
        GM["GrabMart API\n(Grab Partner POS)"]
        SP["Shopee E-Commerce API"]
        Future["Future Channels\n(TikTok Shop, Tiki,...)"]
    end

    subgraph Channel_Adapters["Multi-Module Channel Adapters (Độc lập, không chồng lấn)"]
        SF_Mod["app.modules.shopeefood\n- Signature HMAC-SHA256\n- Foody Menu Formatter\n- Order Webhooks"]
        GM_Mod["app.modules.grabmart\n- OAuth2.0 Token Flow\n- Grab POS Menu Sync\n- Order Push Webhooks"]
        SP_Mod["app.modules.shopee\n- Shopee Open API Adapter"]
    end

    subgraph Core_Platform["Core Platform (FastAPI + PostgreSQL)"]
        Core_API["Core REST API & Router"]
        Core_Registry["Channel Adapter Registry & Dispatcher"]
        
        subgraph Core_Services["Core Services"]
            Order_Service["Unified Order State Machine"]
            Menu_Service["Catalog & Menu Engine"]
            Stock_Service["Multi-Store Inventory Engine"]
            Audit_Service["Audit & Sync Log Service"]
        end
        
        subgraph Core_Interfaces["Core Interfaces & Ports"]
            Adapter_Port["BaseChannelAdapter\n- sync_menu()\n- update_stock()\n- handle_order_webhook()\n- update_order_status()"]
        end
        
        subgraph PostgreSQL_DB["PostgreSQL Database"]
            T_Stores["stores & store_channels"]
            T_Products["products, categories, prices"]
            T_Inventory["inventories & stock_levels"]
            T_Orders["unified_orders & order_items"]
            T_Logs["sync_logs & webhook_audits"]
        end
    end

    subgraph Merchant_UI["Merchant Portal Frontend (The Living Canvas)"]
        Web_Dashboard["Next.js / Modern Dashboard\n- Soft Editorial UI\n- Live Orders Kanban\n- Menu & Pricing Center\n- Multi-Store Stock Monitor"]
    end

    SF <-->|Webhooks / S2S API| SF_Mod
    GM <-->|Webhooks / POS API| GM_Mod
    SP <-->|Open API| SP_Mod

    SF_Mod -->|Implements| Adapter_Port
    GM_Mod -->|Implements| Adapter_Port
    SP_Mod -->|Implements| Adapter_Port

    Core_Registry --> Core_Services
    Core_Services --> PostgreSQL_DB
    Web_Dashboard <-->|REST API / WebSockets| Core_API
```

---

## 3. Công Nghệ Sử Dụng (Tech Stack)

| Thành phần | Công nghệ lựa chọn | Lý do & Vai trò |
| :--- | :--- | :--- |
| **Backend Framework** | **FastAPI** (Python 3.12) | Hiệu năng cao (Asynchronous ASGI), tự động sinh OpenAPI/Swagger, dependency injection mạnh mẽ, Pydantic v2 validation nhanh chóng. |
| **Database** | **PostgreSQL** (v15+) | RDBMS mạnh mẽ, hỗ trợ Transaction ACID an toàn cho quản lý đơn hàng & tồn kho, hỗ trợ JSONB cho dữ liệu payload động từ các sàn. |
| **ORM & Migrations** | **SQLAlchemy 2.0 (Async) + Alembic** | Async/Await native với asyncpg, quản lý schema versioning chặt chẽ, type safety. |
| **HTTP Client** | **HTTPX (Async)** | Gọi API ngoại vi non-blocking, hỗ trợ connection pooling và retry timeout cho các API đối tác. |
| **Frontend** | **React / Next.js + Tailwind CSS** | Áp dụng triệt để Design System **"The Living Canvas"** (tại `docs/design.md`), Soft Editorial UI, No-Line rule, Tonal Layering. |
| **Quản lý Cấu hình** | **Pydantic-Settings** | Load `.env` an toàn theo kiểu dữ liệu, tách biệt môi trường Development / Staging / Production. |

---

## 4. Cấu Trúc Thư Mục Dự Án (Project Structure)

```
naman_merchant_portal/
├── docs/
│   ├── design.md                  # Triết lý Design System "The Living Canvas"
│   └── Development_SOP.md         # Quy chuẩn phát triển (SOP), Code Conventions
├── grabmart/
│   └── docs/                      # Tài liệu tích hợp GrabMart Partner API v1.1.3
├── shopeefood/
│   └── docs/                      # Tài liệu tích hợp ShopeeFood (Foody API v7.0) & Certs
├── app/
│   ├── __init__.py
│   ├── main.py                    # Entrypoint ứng dụng FastAPI (Lifespan, Middleware, Router)
│   ├── core/                      # Hạ tầng & thiết lập dùng chung
│   │   ├── config.py              # Pydantic BaseSettings (.env loader)
│   │   ├── database.py            # Async engine, sessionmaker, get_db dependency
│   │   ├── exceptions.py          # Custom domain exceptions & global exception handlers
│   │   ├── security.py            # Hashing, Token, HMAC signatures
│   │   └── responses.py           # Standardized API response format
│   ├── models/                    # PostgreSQL SQLAlchemy Models (Core Domain)
│   │   ├── base.py                # TimestampMixin, UUIDBaseModel
│   │   ├── store.py               # Store (Chi nhánh: 10001, 10004,...), StoreChannelMapping
│   │   ├── channel.py             # Channel (SHOPEEFOOD, GRABMART, SHOPEE)
│   │   ├── product.py             # Product, Category, ChannelItemMapping
│   │   ├── inventory.py           # Inventory, StoreStockLevel
│   │   ├── order.py               # UnifiedOrder, OrderItem, OrderStatusHistory
│   │   └── sync_log.py            # SyncLog, WebhookAuditLog
│   ├── schemas/                   # Pydantic v2 Validation Schemas
│   │   ├── common.py              # Pagination, Standard APIResponse
│   │   ├── store.py               # Store schemas
│   │   ├── channel.py             # Channel schemas
│   │   ├── product.py             # Product & Catalog schemas
│   │   ├── inventory.py           # Inventory & Stock update schemas
│   │   └── order.py               # Unified Order schemas & Status Enums
│   ├── interfaces/                # Ports & Abstractions (Interface chuẩn)
│   │   ├── channel_adapter.py     # BaseChannelAdapter (Contract cho mọi channel)
│   │   └── services.py            # Service interfaces
│   ├── services/                  # Business Logic Core Services
│   │   ├── channel_registry.py    # Quản lý & nạp các Channel Adapter động
│   │   ├── order_service.py       # Xử lý vòng đời đơn hàng hợp nhất
│   │   ├── inventory_service.py   # Tính toán & điều phối tồn kho
│   │   └── product_service.py     # Quản lý hàng hóa & catalog
│   ├── api/                       # REST API Endpoints
│   │   └── v1/
│   │       ├── router.py          # Master router v1
│   │       ├── health.py          # Liveness & Readiness probes
│   │       ├── stores.py          # Quản lý chi nhánh
│   │       ├── channels.py        # Quản lý kênh bán lẻ
│   │       ├── products.py        # Quản lý sản phẩm & giá
│   │       ├── inventory.py       # Quản lý tồn kho & trigger sync
│   │       └── orders.py          # Xử lý & xem đơn hàng tập trung
│   └── modules/                   # Multi-Module Apps (Kênh bán hàng)
│       ├── shopeefood/            # Module ShopeeFood
│       │   ├── __init__.py
│       │   ├── adapter.py         # ShopeeFoodChannelAdapter (Implements BaseChannelAdapter)
│       │   ├── router.py          # Webhook receivers (/api/v1/shopeefood/webhooks/...)
│       │   ├── schemas.py         # DTO riêng của ShopeeFood
│       │   └── service.py         # Logic gọi Foody External API (HMAC-SHA256)
│       └── grabmart/              # Module GrabMart
│           ├── __init__.py
│           ├── adapter.py         # GrabMartChannelAdapter (Implements BaseChannelAdapter)
│           ├── router.py          # Webhook receivers (/api/v1/grabmart/webhooks/...)
│           ├── schemas.py         # DTO riêng của GrabMart POS API
│           └── service.py         # Logic gọi Grab OAuth2 & POS API
├── .env.example                   # Mẫu biến môi trường
├── .gitignore                     # Cấu hình bỏ qua git
├── pyproject.toml                 # Cấu hình project & dependencies
├── requirements.txt               # Danh sách thư viện Python
├── README.md                      # Tài liệu tổng quan dự án
└── Development_SOP.md             # Quy chuẩn lập trình & vận hành
```

---

## 5. Hướng Dẫn Cài Đặt & Chạy Dự Án (Quick Start)

### 5.1. Yêu Cầu Tiên Quyết (Prerequisites)
- **Python:** Phiên bản 3.10 trở lên (khuyến nghị Python 3.12).
- **PostgreSQL:** Phiên bản 14 trở lên.
- **Git** đã được cài đặt và cấu hình.

### 5.2. Cài Đặt Môi Trường
```bash
# 1. Clone repository
git clone https://github.com/maithedong92/naman_merchant_portal.git
cd naman_merchant_portal

# 2. Tạo môi trường ảo virtualenv
python -m venv venv

# Kích hoạt trên Windows:
.\venv\Scripts\Activate.ps1
# Kích hoạt trên macOS/Linux:
source venv/bin/activate

# 3. Cài đặt các thư viện phụ thuộc
pip install -r requirements.txt
```

### 5.3. Cấu Hình Biến Môi Trường (.env)
Tạo file `.env` từ file mẫu `.env.example`:
```bash
copy .env.example .env
```
Điền các tham số kết nối PostgreSQL và thông tin xác thực tích hợp:
```ini
ENVIRONMENT=development
DEBUG=True
PORT=8000

# PostgreSQL Database
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/naman_portal_db

# ShopeeFood Partner Credentials (UAT / Production)
SHOPEEFOOD_APP_ID=10045
SHOPEEFOOD_APP_KEY=a9756768d72268a6d66ea886031988f0638f03b0036fc63fbe0b86a6aef18546
SHOPEEFOOD_BASE_URL=https://gexternalapi.deliverynow.vn

# GrabMart Partner Credentials
GRABMART_CLIENT_ID=your_grab_client_id
GRABMART_CLIENT_SECRET=your_grab_client_secret
GRABMART_BASE_URL=https://partner-api.grab.com
```

### 5.4. Chạy Ứng Dụng Backend
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Truy cập tài liệu API tự động:
- **Interactive Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Documentation:** [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **Health Check Endpoint:** [http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health)

---

## 6. Lộ Trình Phát Triển (Roadmap)

- [x] **Giai đoạn 1: Chuẩn bị Kiến Trúc & Quy Chuẩn (Foundation)**
  - Dọn dẹp code cũ, chuẩn hóa tài liệu tích hợp vào `docs/`.
  - Khởi tạo repository, `.gitignore`, `README.md` và `Development_SOP.md`.
  - Thiết kế kiến trúc Multi-Module Monolith (Core + Channel Modules).
- [x] **Giai đoạn 2: Xây Dựng Core Module (Core Platform)**
  - Cấu hình FastAPI, Async SQLAlchemy 2.0 & PostgreSQL Engine.
  - Xây dựng Domain Models chuẩn: Stores, Channels, Products, Inventories, Orders, Sync Logs.
  - Xây dựng Base Channel Adapter Interface & Service Registry.
  - Triển khai Core REST API endpoints cho đơn hàng, sản phẩm và tồn kho.
- [ ] **Giai đoạn 3: Triển khai Module GrabMart (`app/modules/grabmart`)**
  - Tích hợp Grab OAuth2 client credentials flow.
  - Đồng bộ thực đơn theo chuẩn GrabMart POS API v1.1.3.
  - Xử lý Webhooks tiếp nhận đơn hàng, xác nhận chuẩn bị đơn và cập nhật trạng thái giao hàng.
- [ ] **Giai đoạn 4: Triển khai Module ShopeeFood (`app/modules/shopeefood`)**
  - Cơ chế tạo chữ ký số HMAC-SHA256 theo chuẩn Foody External API.
  - API đẩy đồng bộ menu theo Sections, Categories, Dish Items.
  - Webhooks nhận đơn hàng và cập nhật tình trạng tồn món tức thì.
- [ ] **Giai đoạn 5: Frontend Portal (The Living Canvas UI)**
  - Xây dựng giao diện Web Quản trị theo triết lý "The Living Canvas" (`docs/design.md`).
  - Kanban board hiển thị đơn hàng thời gian thực (WebSockets/SSE).
  - Bảng điều khiển tồn kho đa chi nhánh và cảnh báo lệch tồn.

---

## 7. Liên Hệ & Đóng Góp
- **Tổ chức:** Nam An Market IT & Digital Transformation Team.
- **Người phụ trách:** Mai Thế Đồng ([maithedong92@gmail.com](mailto:maithedong92@gmail.com)).
- **Quy chuẩn phát triển:** Vui lòng đọc kỹ [Development_SOP.md](file:///c:/python/naman_merchant_portal/docs/Development_SOP.md) trước khi tạo Pull Request hoặc phát triển tính năng mới.
