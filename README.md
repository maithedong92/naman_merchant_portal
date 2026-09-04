# Nam An Market - Unified Merchant Portal (NAM-UMP)

> **Cổng Quản Trị Đa Kênh & Tích Hợp Bán Hàng Trực Tuyến Hợp Nhất của Nam An Market**  
> Kết nối, đồng bộ thực đơn, giá cả, tồn kho và xử lý đơn hàng đa kênh từ các đối tác thương mại điện tử & giao nhận nhanh: **ShopeeFood, GrabMart, Shopee,...**

---

## 1. Bối Cảnh & Mục Tiêu Dự Án (Context & Objectives)

**Nam An Market** là chuỗi siêu thị cao cấp chuyên cung cấp thực phẩm sạch, thực phẩm hữu cơ và hàng nhập khẩu chất lượng cao với nhiều chi nhánh (Thảo Điền, An Phú,...). Để mở rộng kênh tiếp cận khách hàng trực tuyến, Nam An kết nối với nhiều nền tảng bán hàng và giao nhận nhanh hàng đầu như:
- **ShopeeFood** (Foody External API v7.0+)
- **GrabMart** (GrabMart Partner POS API v1.1.3+)
- **ShopeeMart / Shopee Fresh** (Shopee Open Platform API v2)
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
- **Các App Kênh (Channel Modules: ShopeeFood, GrabMart, ShopeeMart) hoàn toàn độc lập:** Mỗi module là một "app" tự quản lý schema đối tác, cơ chế xác thực, client gọi API sàn và router webhook riêng biệt.
- **Nguyên tắc "Không chồng lấn" (Zero Coupling Between Modules):** Module ShopeeFood tuyệt đối không phụ thuộc hay import trực tiếp từ Module GrabMart/ShopeeMart và ngược lại. Tất cả giao tiếp thông qua **Core Interfaces & Event Bus**. Khi cần thêm một kênh bán hàng mới, đội ngũ phát triển chỉ cần cắm thêm (plug-in) adapter mới mà không gây ảnh hưởng đến các kênh đang hoạt động ổn định.

```mermaid
flowchart TD
    subgraph External_Channels["Đối Tác Bán Hàng (External Channels)"]
        SF["ShopeeFood API\n(Foody Partner API)"]
        GM["GrabMart API\n(Grab Partner POS)"]
        SM["ShopeeMart API\n(Shopee Open Platform v2)"]
        Future["Future Channels\n(TikTok Shop, Tiki,...)"]
    end

    subgraph Channel_Adapters["Multi-Module Channel Adapters (Độc lập, không chồng lấn)"]
        SF_Mod["app.modules.shopeefood\n- Signature HMAC-SHA256\n- Foody Menu Formatter\n- Order Webhooks"]
        GM_Mod["app.modules.grabmart\n- OAuth2.0 Token Flow\n- Grab POS Menu Sync\n- Order Push Webhooks"]
        SM_Mod["app.modules.shopeemart\n- Shopee Open API v2 Sign\n- Batch Stock Update\n- Order Webhooks & Ship"]
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
    SM <-->|Webhooks / Open API v2| SM_Mod

    SF_Mod -->|Implements| Adapter_Port
    GM_Mod -->|Implements| Adapter_Port
    SM_Mod -->|Implements| Adapter_Port

    Core_Registry --> Core_Services
    Core_Services --> PostgreSQL_DB
    Web_Dashboard <-->|REST API / WebSockets| Core_API
```

### 2.2. Kiến Trúc Hạ Tầng & Triển Khai 3 Lớp (3-Tier Deployment Architecture)

Hệ thống triển khai theo mô hình mạng **3 lớp bảo vệ độc lập**, tối ưu giữa bảo mật biên và hiệu năng cơ sở dữ liệu:

```mermaid
flowchart TD
    subgraph Clients["Khách Hàng & Đối Tác (Internet)"]
        SF_Hook["ShopeeFood Webhooks"]
        GM_Hook["GrabMart Webhooks"]
        SM_Hook["ShopeeMart Webhooks"]
        Admin_Browser["Trình duyệt Quản Trị Viên (Admin)"]
    end

    subgraph Host_Server["MÁY CHỦ VẬT LÝ / VM (HOST SERVER)"]
        subgraph Layer1_Host_Nginx["Lớp 1: Nginx Local Ngoài Cùng (Host-Level Gateway)"]
            Nginx_Host["Host-Level Nginx Reverse Proxy\n- Public Port: 80 / 443 (SSL/TLS Termination)\n- Wildcard SSL: *.namanmarket.com\n- Rate Limiting, DDoS Shield, Security Headers\n- Reverse Proxy -> 127.0.0.1:2222"]
        end

        subgraph Layer2_Docker["Lớp 2: Docker Container Stack (App + Nginx Đóng Gói Cùng Nhau)"]
            subgraph Docker_Nginx["Container Nginx (Nội Bộ)"]
                Nginx_Doc["Internal Nginx Proxy\n- Port nội bộ: 2222 (Expose Host: 127.0.0.1:2222)\n- Buffer Webhook Payload, Gzip, Static Assets\n- Proxy pass -> app:2223"]
            end

            subgraph Docker_App["Container FastAPI App"]
                FastAPI_App["FastAPI Uvicorn ASGI (:2223)\n- Multi-module Monolith Engine\n- Core Services & Channel Adapters\n- Order State Machine"]
            end
        end

        subgraph Layer3_Local_DB["Lớp 3: Local Database (PostgreSQL Chạy Trực Tiếp Trên Host)"]
            Local_PG["PostgreSQL Database (Local Host Service)\n- Host Port: 5432 (Localhost / Host Gateway)\n- KHÔNG CHẠY TRONG DOCKER\n- Tối ưu I/O đĩa cứng, sao lưu độc lập\n- ACID Transactions an toàn cho Đơn hàng & Tồn kho"]
        end
    end

    Clients -->|HTTPS :443 (TLS v1.3)| Nginx_Host
    Nginx_Host -->|proxy_pass http://127.0.0.1:2222| Nginx_Doc
    Nginx_Doc -->|proxy_pass http://app:2223| FastAPI_App
    FastAPI_App -->|TCP host.docker.internal:5432| Local_PG
```

#### Bảng Phân Bổ Cổng Dịch Vụ Độc Quyền (Custom Port Allocation Matrix - 2222+):
> Để tránh xung đột với các ứng dụng khác đang chạy trên máy chủ và trong Docker (tránh các port phổ biến như 80, 443, 3000, 5000, 8000, 8080), toàn bộ các dịch vụ của Nam An Merchant Portal được quy hoạch bắt đầu từ dải **2222**:

| Cổng (Port) | Dịch Vụ / Vai Trò | Môi Trường Lắng Nghe | Mô Tả Chi Tiết |
| :---: | :--- | :--- | :--- |
| **2222** | **Container Nginx Reverse Proxy** | Docker (Expose `127.0.0.1:2222`) | Cổng tiếp nhận từ Nginx ngoài cùng của máy host |
| **2223** | **FastAPI Backend Application** | Container `:2223` / Host Dev | Chạy Uvicorn ASGI backend & Core API |
| **2224** | **Frontend Web Dashboard** | Container / Host Dev | Giao diện Web Quản trị ("The Living Canvas") |
| **2225+**| **Auxiliary Services** | Container / Host Dev | Dành riêng cho Background Task Workers / Celery / Flower / Metrics |

#### Phân Định Rõ Ràng Trách Nhiệm Từng Lớp:
1. **Lớp 1 - Nginx Local Ngoài Cùng (Host-Level Nginx):**
   - Chạy trực tiếp trên Host OS (không qua container).
   - Đóng vai trò là Public Gateway duy nhất tiếp nhận lưu lượng từ Internet (Domain `portal.namanmarket.com`).
   - Xử lý SSL/TLS Termination (chứng chỉ Wildcard SSL hoặc Let's Encrypt), ép buộc HTTP sang HTTPS.
   - Thiết lập Rate Limiting bảo vệ các endpoint webhook khỏi nghẽn tải hoặc tấn công lặp payload.
   - `proxy_pass` chuyển tiếp an toàn vào cổng nội bộ máy host `127.0.0.1:2222`.
2. **Lớp 2 - Docker Container Runtime (App + Nginx đóng gói cùng nhau):**
   - **Container Nginx:** Đóng gói trong Docker cùng stack với backend, lắng nghe cổng `2222` (được map ra host `127.0.0.1:2222:2222`). Chịu trách nhiệm buffer request body cho các payload đồng bộ lớn từ sàn, nén Gzip, phục vụ assets giao diện và forward về Uvicorn (`app:2223`).
   - **Container FastAPI App:** Chạy mã nguồn backend Python 3.12 (Uvicorn ASGI) trên cổng tùy chỉnh `:2223` nội bộ.
   - **Kết nối ra ngoài Container:** Cấu hình `extra_hosts: ["host.docker.internal:host-gateway"]` cho phép container gọi trực tiếp các dịch vụ trên host.
3. **Lớp 3 - Local PostgreSQL Database (Chạy trực tiếp trên Host, KHÔNG chạy trong Docker):**
   - Cơ sở dữ liệu PostgreSQL được cài đặt trực tiếp trên máy chủ Host (hoặc cụm database bare-metal nội bộ của Nam An).
   - **Lý do kiến trúc:**
     - Tối đa hóa hiệu năng I/O đĩa cứng cho các truy vấn giao dịch tồn kho và đơn hàng đa kênh với tần suất cao.
     - Tách biệt hoàn toàn vòng đời dữ liệu (Data Lifecycle) khỏi vòng đời container (Container Lifecycle), giúp việc build, restart, cập nhật container App & Nginx không gây rủi ro downtime database.
     - Tích hợp trực tiếp với các tiến trình sao lưu nội bộ (pg_dump, WAL archiving) và kết nối với các cơ sở dữ liệu nội bộ khác (Arito, Haravan) mà không bị giới hạn bởi lớp mạng ảo của Docker.

---

## 3. Công Nghệ Sử Dụng (Tech Stack)

| Thành phần | Công nghệ lựa chọn | Lý do & Vai trò |
| :--- | :--- | :--- |
| **Cổng Biên (Public Gateway)** | **Host-Level Nginx (Local)** | Chạy trực tiếp trên Host, SSL/TLS Termination, firewall, rate limiting webhook và reverse proxy vào Docker. |
| **Container Reverse Proxy** | **Nginx Container (Alpine)** | Đóng gói cùng app trong Docker, buffer request, nén gzip, proxy pass uvicorn. |
| **Backend Framework** | **FastAPI** (Python 3.12) | Đóng gói trong Docker, Asynchronous ASGI, OpenAPI tự động, Pydantic v2 validation. |
| **Cơ Sở Dữ Liệu (Local DB)** | **PostgreSQL (Local Host)** | **Chạy trực tiếp trên Host (không trong Docker)**, tối ưu I/O, ACID an toàn cho Đơn hàng & Tồn kho. |
| **ORM & Migrations** | **SQLAlchemy 2.0 (Async) + Alembic** | Async/Await native với asyncpg, quản lý schema versioning chặt chẽ, type safety. |
| **HTTP Client** | **HTTPX (Async)** | Non-blocking API calls tới ShopeeFood, GrabMart, ShopeeMart. |
| **Frontend** | **React / Next.js + Tailwind CSS** | Triết lý **"The Living Canvas"** (`docs/design.md`), Soft Editorial UI, No-Line rule. |
| **Quản lý Cấu hình** | **Pydantic-Settings** | Phân định kết nối DB local dev (`localhost:5432`) và container (`host.docker.internal:5432`). |

---

## 4. Cấu Trúc Thư Mục Dự Án (Project Structure)

```
naman_merchant_portal/
├── docs/
│   ├── design.md                  # Triết lý Design System "The Living Canvas"
│   ├── Development_SOP.md         # Quy chuẩn phát triển (SOP), Code Conventions
│   └── channels/                  # Tài liệu & Đặc tả API tích hợp các sàn
│       ├── grabmart/              # GrabMart Partner POS API v1.1.3 Integration Guide
│       ├── shopeefood/            # ShopeeFood (Foody S2S API v7.0) Specs & Mappings
│       └── shopeemart/            # ShopeeMart (Shopee Open Platform v2) Working Sheet
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
│   │   ├── user.py                # User, UserRole, RefreshToken, AuditSecurityLog
│   │   ├── operational_error.py   # OperationalErrorLog, ErrorSeverity, ErrorStatus
│   │   ├── store.py               # Store (Chi nhánh: 10001, 10004,...), StoreChannelMapping
│   │   ├── channel.py             # Channel (SHOPEEFOOD, GRABMART, SHOPEEMART)
│   │   ├── product.py             # Product, Category, ChannelItemMapping
│   │   ├── inventory.py           # Inventory, StoreStockLevel
│   │   ├── order.py               # UnifiedOrder, OrderItem, OrderStatusHistory
│   │   └── sync_log.py            # SyncLog, WebhookAuditLog
│   ├── schemas/                   # Pydantic v2 Validation Schemas
│   │   ├── common.py              # Pagination, Standard APIResponse
│   │   ├── auth.py                # Login, RefreshToken, ChangePassword schemas
│   │   ├── user.py                # UserCreate, UserUpdate, UserResponse schemas
│   │   ├── system.py              # OperationalError, SystemHealth, ComponentHealth
│   │   ├── store.py               # Store schemas
│   │   ├── channel.py             # Channel schemas
│   │   ├── product.py             # Product & Catalog schemas
│   │   ├── inventory.py           # Inventory & Stock update schemas
│   │   └── order.py               # Unified Order schemas & Status Enums
│   ├── interfaces/                # Ports & Abstractions (Interface chuẩn)
│   │   ├── channel_adapter.py     # BaseChannelAdapter (Contract cho mọi channel)
│   │   └── services.py            # Service interfaces
│   ├── services/                  # Business Logic Core Services
│   │   ├── auth_service.py        # Xác thực, Quản lý Token, RBAC, Khóa brute-force
│   │   ├── error_service.py       # Thu thập sự cố, khử trùng lặp, giải quyết lỗi
│   │   ├── channel_registry.py    # Quản lý & nạp các Channel Adapter động
│   │   ├── order_service.py       # Xử lý vòng đời đơn hàng hợp nhất
│   │   ├── inventory_service.py   # Tính toán & điều phối tồn kho
│   │   └── product_service.py     # Quản lý hàng hóa & catalog
│   ├── api/                       # REST API Endpoints
│   │   ├── deps.py                # FastAPI Security Dependencies (OAuth2, RBAC Guards)
│   │   └── v1/
│   │       ├── router.py          # Master router v1
│   │       ├── health.py          # Liveness & Readiness probes
│   │       ├── system.py          # System Health, Operational Errors & Incident Resolution
│   │       ├── auth.py            # Đăng nhập, Gia hạn token, Đổi mật khẩu
│   │       ├── users.py           # Quản lý người dùng & Nhật ký kiểm toán bảo mật
│   │       ├── stores.py          # Quản lý chi nhánh
│   │       ├── channels.py        # Quản lý kênh bán lẻ
│   │       ├── products.py        # Quản lý sản phẩm & giá
│   │       ├── inventory.py       # Quản lý tồn kho & trigger sync
│   │       └── orders.py          # Xử lý & xem đơn hàng tập trung
│   ├── templates/                 # Giao diện Web (The Living Canvas Design System)
│   │   ├── base.html              # Layout chuẩn Glassmorphism, Font, Toast notification
│   │   ├── admin_dashboard.html   # Trang Tổng Quan Quản Trị (/admin)
│   │   ├── system_status.html     # Trang Giám Sát System Status & Xử Lý Sự Cố (/system-status)
│   │   └── login.html             # Trang Đăng Nhập Quản Trị (/admin/login)
│   ├── web/                       # Web UI Routers (HTML Template Rendering)
│   │   └── router.py              # Điều hướng /admin, /system-status, /admin/login


│   └── modules/                   # Multi-Module Apps (Kênh bán hàng)
│       ├── shopeefood/            # Module ShopeeFood
│       │   ├── __init__.py
│       │   ├── adapter.py         # ShopeeFoodChannelAdapter
│       │   ├── router.py          # Webhook receivers (/api/v1/shopeefood/webhooks/...)
│       │   ├── schemas.py         # DTO riêng của ShopeeFood
│       │   └── service.py         # Logic gọi Foody External API (HMAC-SHA256)
│       ├── grabmart/              # Module GrabMart
│       │   ├── __init__.py
│       │   ├── adapter.py         # GrabMartChannelAdapter
│       │   ├── router.py          # Webhook receivers (/api/v1/grabmart/webhooks/...)
│       │   ├── schemas.py         # DTO riêng của GrabMart POS API
│       │   └── service.py         # Logic gọi Grab OAuth2 & POS API
│       └── shopeemart/            # Module ShopeeMart (Shopee Fresh / Supermarket)
│           ├── __init__.py
│           ├── adapter.py         # ShopeeMartChannelAdapter
│           ├── router.py          # Webhook receivers (/api/v1/shopeemart/webhooks/...)
│           ├── schemas.py         # DTO riêng của Shopee Open Platform v2
│           └── service.py         # Logic gọi Shopee Open API v2 (HMAC-SHA256)
├── docker/                        # Hạ tầng triển khai Docker & Nginx
│   ├── Dockerfile                 # Đóng gói FastAPI backend (Multi-stage Python 3.12)
│   └── nginx/
│       ├── conf.d/
│       │   └── default.conf       # Cấu hình Nginx bên trong Docker (buffer request, proxy app:8000)
│       └── host-nginx.conf.example# Cấu hình mẫu Nginx Local ngoài cùng trên máy Host
├── docker-compose.yml             # Docker stack chạy App + Nginx nội bộ (kết nối Local DB)
├── .env.example                   # Mẫu biến môi trường
├── .gitignore                     # Cấu hình bỏ qua git
├── pyproject.toml                 # Cấu hình project & dependencies
├── requirements.txt               # Danh sách thư viện Python
├── README.md                      # Tài liệu tổng quan dự án
└── Development_SOP.md             # Quy chuẩn lập trình & vận hành
```

---

## 5. Hướng Dẫn Cài Đặt & Triển Khai (Deployment Guide)

### 5.1. Chế Độ 1: Chạy Trực Tiếp Trên Máy Host (Local Development)

Phù hợp cho lập trình viên phát triển và kiểm thử cục bộ:
```bash
# 1. Tạo môi trường ảo virtualenv
python -m venv venv
.\venv\Scripts\Activate.ps1  # Windows

# 2. Cài đặt thư viện phụ thuộc
pip install -r requirements.txt

# 3. Cấu hình .env kết nối Local PostgreSQL
copy .env.example .env
# Chỉnh DATABASE_URL=postgresql+asyncpg://postgres:admin@localhost:5432/naman_merchant_portal

# 4. Khởi chạy FastAPI server trên cổng tùy chỉnh 2223
uvicorn app.main:app --host 0.0.0.0 --port 2223 --reload
```

---

### 5.2. Chế Độ 2: Đóng Gói App + Nginx Bằng Docker (Production / Staging)

Ở chế độ này, **App Backend và Nginx nội bộ được đóng gói cùng nhau trong Docker**, kết nối trực tiếp ra **PostgreSQL Local chạy trên máy Host**:

```mermaid
flowchart LR
    A["Host Nginx Local (Port 443/80)"] -->|proxy_pass :2222| B["Container Nginx (Docker :2222)"]
    B -->|proxy_pass :2223| C["Container FastAPI (:2223)"]
    C -->|host.docker.internal:5432| D[("PostgreSQL Local (Host :5432)")]
```

#### Bước 1: Khởi động stack Docker (App + Nginx Container)
File `docker-compose.yml` đã được định cấu hình sẵn với `extra_hosts` để truy cập Local DB máy host:
```bash
docker compose up -d --build
```
Kiểm tra trạng thái các container:
```bash
docker compose ps
```
- Container `naman_portal_app` lắng nghe cổng nội bộ **2223**.
- Container `naman_portal_nginx` mở cổng **`127.0.0.1:2222`** trên máy Host.

#### Bước 2: Cấu hình Nginx Local Ngoài Cùng trên Máy Host
Cài đặt Nginx trực tiếp trên máy Host (nếu chưa có) và áp dụng cấu hình từ file mẫu [`docker/nginx/host-nginx.conf.example`](file:///c:/python/naman_merchant_portal/docker/nginx/host-nginx.conf.example):

1. Sao chép cấu hình vào thư mục Nginx của máy Host:
   ```nginx
   # Cấu hình proxy_pass từ Host Nginx vào cổng Docker Nginx (127.0.0.1:2222):
   location / {
       proxy_pass http://127.0.0.1:2222;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto https;
   }
   ```
2. Kiểm tra và tải lại Nginx Host:
   ```bash
   nginx -t
   nginx -s reload
   ```

---

### 5.3. Giao Diện Quản Trị & Tài Liệu API
- **Admin Dashboard Tổng Quan:** [http://localhost:2222/admin](http://localhost:2222/admin) (hoặc trực tiếp `http://localhost:2223/admin`)
- **System Status & Quản Lý Sự Cố:** [http://localhost:2222/system-status](http://localhost:2222/system-status) (hoặc `http://localhost:2223/system-status`)
- **Đăng Nhập Quản Trị:** [http://localhost:2222/admin/login](http://localhost:2222/admin/login)
- **Interactive Swagger UI:** [http://localhost:2222/docs](http://localhost:2222/docs) (hoặc qua domain https://portal.namanmarket.com/docs)
- **ReDoc Documentation:** [http://localhost:2222/redoc](http://localhost:2222/redoc)
- **Health Check Endpoint:** [http://localhost:2222/api/v1/health](http://localhost:2222/api/v1/health)


---

## 6. Lộ Trình Phát Triển (Roadmap)

- [x] **Giai đoạn 1: Chuẩn bị Kiến Trúc & Quy Chuẩn (Foundation)**
  - Dọn dẹp code cũ, chuẩn hóa cấu trúc tài liệu tích hợp vào `docs/channels/`.
  - Khởi tạo repository, `.gitignore`, `README.md` và `Development_SOP.md`.
  - Thiết kế kiến trúc Multi-Module Monolith (Core + Decoupled Channel Modules).
- [x] **Giai đoạn 2: Xây Dựng Core Module & Bảo Mật (Core Platform & Security)**
  - Cấu hình FastAPI, Async SQLAlchemy 2.0 & PostgreSQL Engine.
  - Xây dựng Domain Models chuẩn: Stores, Channels, Products, Inventories, Orders, Sync Logs.
  - Xây dựng Base Channel Adapter Interface & Service Registry (`ChannelRegistry`).
  - Triển khai Core REST API endpoints cho đơn hàng, sản phẩm và tồn kho.
  - Triển khai Xác thực & Bảo mật: JWT Access/Refresh Token (Token Rotation, SHA-256 hash, Invalidation on reuse).
  - Triển khai Phân quyền RBAC 4 cấp (`SUPER_ADMIN`, `STORE_MANAGER`, `STAFF`, `READ_ONLY`) kết hợp Store-scoping authorization.
  - Cơ chế chống Brute-force & Account Lockout sau 5 lần sai mật khẩu, mã hóa Bcrypt 12 rounds.
  - Ghi nhật ký kiểm toán bảo mật (`audit_security_logs`) và tự động seed tài khoản SuperAdmin mặc định khi khởi chạy.

- [ ] **Giai đoạn 3: Triển khai Module GrabMart (`app/modules/grabmart`)**
  - Tích hợp Grab OAuth2 client credentials flow.
  - Đồng bộ thực đơn theo chuẩn GrabMart POS API v1.1.3.
  - Xử lý Webhooks tiếp nhận đơn hàng, xác nhận chuẩn bị đơn và cập nhật trạng thái giao hàng.
- [ ] **Giai đoạn 4: Triển khai Module ShopeeFood (`app/modules/shopeefood`)**
  - Cơ chế tạo chữ ký số HMAC-SHA256 theo chuẩn Foody External API.
  - API đẩy đồng bộ menu theo Sections, Categories, Dish Items.
  - Webhooks nhận đơn hàng và cập nhật tình trạng tồn món tức thì.
- [ ] **Giai đoạn 5: Triển khai Module ShopeeMart (`app/modules/shopeemart`)**
  - Tích hợp Shopee Open Platform API v2 (HMAC-SHA256 signing, OAuth2 shop authorization).
  - Ánh xạ mã hàng hóa theo file đối soát `[Nam An Market x Shopee Mart] Working sheet`.
  - Đồng bộ tồn kho khả dụng theo lô SKU (`v2.product.update_stock`) và giá bán (`v2.product.update_price`).
  - Tiếp nhận Webhook đơn hàng ShopeeMart, cập nhật trạng thái đóng gói giao hàng (`v2.logistics.ship_order`) và xử lý hủy đơn.
- [ ] **Giai đoạn 6: Frontend Portal (The Living Canvas UI)**
  - Xây dựng giao diện Web Quản trị theo triết lý "The Living Canvas" (`docs/design.md`).
  - Kanban board hiển thị đơn hàng thời gian thực (WebSockets/SSE).
  - Bảng điều khiển tồn kho đa chi nhánh và cảnh báo lệch tồn.

---

## 7. Liên Hệ & Đóng Góp
- **Tổ chức:** Nam An Market IT & Digital Transformation Team.
- **Người phụ trách:** Mai Thế Đồng ([maithedong92@gmail.com](mailto:maithedong92@gmail.com)).
- **Quy chuẩn phát triển:** Vui lòng đọc kỹ [Development_SOP.md](file:///c:/python/naman_merchant_portal/docs/Development_SOP.md) trước khi tạo Pull Request hoặc phát triển tính năng mới.
