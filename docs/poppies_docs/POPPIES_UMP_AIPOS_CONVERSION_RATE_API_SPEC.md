# Đặc Tả API Tỉ Lệ Quy Đổi Chia Cân (`conversionRate` / `conversion_rates`) — Dành Riêng Cho NAM-UMP

- **Ngày cập nhật:** `18/11/2026` (`Asia/Ho_Chi_Minh`)
- **Phạm vi áp dụng:** Poppies ERP V2 (`https://poppies.namanmarket.com`) $\rightarrow$ **NAM-UMP** (`https://ump.namanmarket.com`)
- **Trạng thái AI-POS:** **KHÔNG TÁC ĐỘNG / KHÔNG THAY ĐỔI** — toàn bộ API & Webhook của **Nam An AI-POS** (`/api/v1/pos/pricing/*`, `/api/v1/pos/master-data/*` và Webhook `AI_POS`) được giữ nguyên 100% như trước.
- **Tài liệu liên quan:**
  - [GIOI_THIEU_HE_THONG_NAM_UMP_CHO_POPPIES_ERP.md](file:///Users/pabi/Nam%20An%20Mobile/NamAnMarket/docs/reference/GIOI_THIEU_HE_THONG_NAM_UMP_CHO_POPPIES_ERP.md)
  - [INTEGRATION_REPORT_FOR_AI_POS_AND_UMP.md](file:///Users/pabi/Nam%20An%20Mobile/NamAnMarket/docs/handover/channel_pricing_promotion_20261105/INTEGRATION_REPORT_FOR_AI_POS_AND_UMP.md)

---

## 1. Lưu Ý Quan Trọng Bắt Buộc Cho Phía Nhận API (NAM-UMP)

> [!IMPORTANT]
> **1. Giá bắn / trả qua API của Poppies ERP VẪN LÀ GIÁ NGUYÊN 100% THEO ĐVT GỐC ERP.**
> - Ví dụ: Sản phẩm *Táo xanh* có ĐVT gốc là `KG`, giá niêm yết / giá kênh từ Poppies là `100.000đ` -> Poppies luôn giữ và trả giá `100000`, **tuyệt đối KHÔNG tự nhân hay chia theo `0.5`**.
> - Việc **nhân/chia giá bán theo quy cách đóng gói bán trên sàn** (VD: bán gói `500g` trên Website/Shopee/Grab từ `1 KG` gốc $\Rightarrow 100.000 \times 0.5 = 50.000\text{đ}$) **do bên phía đối tác nhận API (`NAM-UMP` / sàn TMĐT) chủ động xử lý** dựa trên trường `conversionRate` / `conversion_rate` mà Poppies cung cấp.
>
> **2. Phân biệt rõ `AI-POS` vs `NAM-UMP`:**
> - **AI-POS (`/api/v1/pos/*` và Webhook `AI_POS`)**: Không thêm field tỉ lệ quy đổi, không nhận webhook `CONVERSION_RATE_UPDATED`, giữ nguyên 100% như cũ.
> - **NAM-UMP (`/api/v1/ump/*` và Webhook `NAM_UMP`)**: Chỉ các API/Webhook dành riêng cho UMP mới bổ sung đầy đủ trường tỉ lệ quy đổi.
>
> **3. Phân biệt `conversionRate` (chia cân bán sàn) và `unitConv` (`UNITCONV`):**
> - **`conversionRate` / `conversion_rate`**: Tỉ lệ chia cân/quy đổi sang quy cách bán trên sàn (thường `< 1` như `0.5`, `0.25`, `0.1`, hoặc `1.0`). Dùng **1 giá trị chung cho mỗi SKU**.
> - **`unitConv` (`UNITCONV`)**: Hệ số quy đổi đơn vị nhập/kho nội bộ ERP (VD: 1 Thùng = 12 Lon). Không dùng thay cho `conversionRate`.

---

## 2. Tên Trường, Thứ Tự Ưu Tiên & Giá Trị Mặc Định

### 2.1. Thứ tự ưu tiên xác định giá trị (3 tầng)

Poppies luôn giải quyết giá trị hiệu lực cho từng SKU theo thứ tự ưu tiên từ cao xuống thấp:

| Thứ tự | Mã nguồn (`conversionRateSource` / `conversion_rate_source`) | Nguồn dữ liệu trong PostgreSQL | Quy tắc hoạt động |
| :---: | :--- | :--- | :--- |
| **1 (Cao nhất)** | **`MANUAL`** | `catalog.sku_def.conversion_rate` (khi `IS NOT NULL`) | Giá trị do Admin chỉnh tay & khóa tại màn hình *Giá Kênh & Điểm* hoặc *Chỉnh sửa Master Data*. Các lần đồng bộ tự động từ Haravan sau đó **không được ghi đè**. |
| **2 (Kế thừa)** | **`HARAVAN`** | `growth_ecommerce.haravan_product_variants.conversion_rate` | Khi `catalog.sku_def.conversion_rate IS NULL`, Poppies tự kế thừa động từ Haravan theo thứ tự ưu tiên: bản Tiếng Việt (`hp.lang = 'VI'`) $\rightarrow$ bản đang xuất bản (`published_at IS NOT NULL`) $\rightarrow$ bản đồng bộ mới nhất (`synced_at DESC, id DESC`). |
| **3 (Mặc định)** | **`DEFAULT`** | Fallback mặc định của hệ thống | Nếu sản phẩm chưa từng chỉnh tay **và** không có bản ghi quy đổi trên Haravan, giá trị mặc định luôn trả về là **`1.0`**. |

- Khi Admin chọn **"Khôi phục về kế thừa / Haravan"**, Poppies đặt `catalog.sku_def.conversion_rate = NULL` để SKU tự quay lại tầng **2 (`HARAVAN`)** hoặc **3 (`DEFAULT` = `1.0`)**.
- Giá trị trả ra luôn là số dương `> 0` (tối đa 4 chữ số thập phân, ví dụ `0.5`, `0.25`, `0.1`, `1.0`).

### 2.2. Tên trường chuẩn trên API & Webhook dành cho NAM-UMP

| Nhóm API của NAM-UMP | Tên trường chứa tỉ lệ quy đổi | Kiểu dữ liệu | Trường đi kèm cho biết nguồn gốc |
| :--- | :--- | :--- | :--- |
| **Webhook Push (`NAM_UMP` - `price-changed`)** | `conversion_rates`, `conversionRates` (map nhiều SKU)<br>và `conversion_rate`, `conversionRate` (khi 1 SKU) | `Record<string, number>`<br>và `number` | `conversion_rate_source`, `conversionRateSource` (khi 1 SKU) |
| **UMP Conversion Rates API (`/api/v1/ump/conversion-rates*`)** | `conversionRate` | `number` (mặc định `1.0`) | `conversionRateSource`, `manualConversionRate`, `haravanConversionRate` |
| **UMP Pricing Pull (`/api/v1/ump/pricing/resolve`, `/snapshots`)** | `conversion_rate`, `conversionRate` | `number` (mặc định `1.0`) | `conversion_rate_source` (`"MANUAL"` \| `"HARAVAN"` \| `"DEFAULT"`) |
| **UMP Master Data Pull (`/api/v1/ump/master-data/*`)** | Cấp item: `conversionRate`<br>Trong `ecommerce`: `ecommerce.conversionRate` | `number` (mặc định `1.0`) | `conversionRateSource` và `ecommerce.conversionRateSource` |
| **AI-POS (`/api/v1/pos/*` & Webhook `AI_POS`)** | *(Giữ nguyên 100% như cũ, không thay đổi)* | — | — |

---

## 3. Chi Tiết Từng API Dành Cho NAM-UMP

Tất cả API Pull dưới đây dùng chung header xác thực `X-POS-API-Key` (hoặc `X-API-Key` / `Authorization: Bearer <API_KEY>`).

### 3.1. Webhook Outbox Push tới `NAM_UMP` (`POST https://ump.namanmarket.com/api/v1/pricing/webhook/price-changed`)

Header: `Content-Type: application/json`, `X-Poppies-Webhook-Key`, `X-Event-Id`, `Idempotency-Key`.

#### Trường hợp A — Sự kiện đổi giá / bật tắt bán kênh nhỏ (`is_full_catalog = false` và `1 <= affected_skus.length <= 200`)
Chỉ khi bắn sang đích **`NAM_UMP`**, Poppies nhúng sẵn `conversion_rates` / `conversionRates` (và trỏ `sync_instructions` về `/api/v1/ump/...`). Đích `AI_POS` vẫn nhận payload cũ nguyên bản.

```json
{
  "event_id": "2c8c9f5b-...",
  "event": "price_changed",
  "event_type": "PRICE_EFFECTIVITY_CHANGED",
  "channel": "HARAVAN_WEB",
  "channel_code": "HARAVAN_WEB",
  "branch": "ALL",
  "branch_code": "ALL",
  "affected_skus": ["290000245000", "290031729000"],
  "conversion_rates": {
    "290000245000": 0.5,
    "290031729000": 0.5
  },
  "conversionRates": {
    "290000245000": 0.5,
    "290031729000": 0.5
  },
  "is_full_catalog": false,
  "timestamp": "2026-11-18T09:45:00+00:00",
  "source": "POPPIES_ERP",
  "sync_instructions": {
    "recommended_action": "PULL_AFFECTED_SKUS",
    "pull_url": "https://poppies.namanmarket.com/api/v1/ump/pricing/resolve",
    "snapshot_url": "https://poppies.namanmarket.com/api/v1/ump/pricing/snapshots?branch_code=ALL&channel_code=HARAVAN_WEB",
    "conversion_rates_url": "https://poppies.namanmarket.com/api/v1/ump/conversion-rates"
  }
}
```

#### Trường hợp B — Sự kiện Admin lưu **Tỉ lệ quy đổi** (`event_type = "CONVERSION_RATE_UPDATED"`)
Phát tự động ngay khi Admin chỉnh hoặc khôi phục tỉ lệ quy đổi trên **Giá Kênh & Điểm** hoặc **Master Data ERP**:
- **Chỉ gửi tới `NAM_UMP`** (`target_services = ["NAM_UMP"]`), **không bắn sang `AI_POS`**.
- **Lưu ý:** Giá trên Poppies vẫn giữ nguyên 100% giá ĐVT gốc ERP; `NAM-UMP` dùng `conversion_rate` mới để chia lại giá bán niêm yết trên sàn.

```json
{
  "event_id": "9f1a4d12-...",
  "event": "price_changed",
  "event_type": "CONVERSION_RATE_UPDATED",
  "channel": "HARAVAN_WEB",
  "channel_code": "HARAVAN_WEB",
  "branch": "ALL",
  "branch_code": "ALL",
  "affected_skus": ["290000245000"],
  "conversion_rate": 0.25,
  "conversionRate": 0.25,
  "conversion_rate_source": "MANUAL",
  "conversionRateSource": "MANUAL",
  "conversion_rates": {
    "290000245000": 0.25
  },
  "conversionRates": {
    "290000245000": 0.25
  },
  "is_full_catalog": false,
  "timestamp": "2026-11-18T09:46:10+00:00",
  "source": "POPPIES_ERP",
  "sync_instructions": {
    "recommended_action": "PULL_AFFECTED_SKUS",
    "pull_url": "https://poppies.namanmarket.com/api/v1/ump/pricing/resolve",
    "snapshot_url": "https://poppies.namanmarket.com/api/v1/ump/pricing/snapshots?branch_code=ALL&channel_code=HARAVAN_WEB",
    "conversion_rates_url": "https://poppies.namanmarket.com/api/v1/ump/conversion-rates",
    "master_data_url": "https://poppies.namanmarket.com/api/v1/ump/master-data/changes"
  }
}
```

---

### 3.2. API Dedicated Chuyên Biệt Cho Tỉ Lệ Quy Đổi (`NAM-UMP`)

- **`GET /api/v1/ump/health`**: Kiểm tra kết nối và tổng số SKU `manual_conversion_count` / `haravan_non_unit_count`.
- **`GET /api/v1/ump/conversion-rates`**:
  - Query: `skus` (danh sách SKU cách nhau dấu phẩy), `search`, `source` (`ALL` \| `MANUAL` \| `HARAVAN` \| `NON_UNIT`), `since` (ISO timestamp), `limit` (`1..1000`), `offset`.
  - Trả về `basePrice` (giá nguyên 100% ĐVT gốc ERP) + `conversionRate`, `conversionRateSource`, `manualConversionRate`, `haravanConversionRate`, `haravanPackaging`, `updatedBy`, `updatedAt`.
- **`GET /api/v1/ump/conversion-rates/{sku}`**: Tra cứu nhanh 1 SKU (`404` nếu không tồn tại).

---

### 3.3. API Pull Giá & Master Data Riêng Cho `NAM-UMP`

- **`POST /api/v1/ump/pricing/resolve`** & **`GET /api/v1/ump/pricing/snapshots`**:
  - Giữ nguyên toàn bộ schema giá của Poppies (`effective_price`, `effectivePrice`, `base_price`, `basePrice` là **giá nguyên 100% ĐVT gốc ERP**) và bổ sung `conversion_rate`, `conversionRate`, `conversion_rate_source`.
- **`GET /api/v1/ump/master-data/sku/{sku_or_barcode}`**, **`GET /api/v1/ump/master-data/snapshot`**, **`GET /api/v1/ump/master-data/changes?since=...`**:
  - Giữ nguyên giá trong `pricing` (`baseSalePriceVat`, `channels[].listedPrice`, `storeOverrides`), đồng thời trả `conversionRate` (`MANUAL > HARAVAN > 1.0`) và `conversionRateSource` ở cả cấp gốc và `ecommerce.conversionRate`.
  - Endpoint `/changes?since=...` bắt được ngay các SKU vừa đổi `conversionRate` (do Poppies cập nhật `catalog.sku_def.migrated_at = NOW()`).

---

### 3.4. API Quản Trị Nội Bộ Poppies (Pricing Studio & Master Data)

- **`GET /api/v1/pricing/rules/preview`** *(hỗ trợ `conversion_filter=ALL|NON_UNIT|MANUAL|HARAVAN`)*
- **`GET /api/v1/pricing/sku-conversion-rate/{sku}`**, **`PATCH` / `POST /api/v1/pricing/sku-conversion-rate`** *(gửi `"conversionRate": null` để quay về Haravan)*, **`POST /api/v1/pricing/sku-conversion-rate/batch`**
- **`PUT /api/v1/products/{sku}/master-attributes`** *(hỗ trợ `conversionRate` & `resetConversionRate`)*

---

## 4. Hướng Dẫn Xử Lý Đề Xuất Cho Phía Nhận API (NAM-UMP)

1. Đọc tỉ lệ quy đổi từ `payload.conversion_rates[sku]` trong Webhook `price-changed` hoặc từ các endpoint `/api/v1/ump/*` (mặc định an toàn `1.0` nếu vắng mặt hoặc `<= 0`).
2. Tính giá niêm yết đẩy lên sàn đối tác:
   ```text
   gia_ban_tren_san = gia_kenh_tu_poppies * conversion_rate
   ```
   *(Phép nhân chia cân thực hiện tại adapter xuất lên sàn/website của NAM-UMP; luôn giữ nguyên `gia_kenh_tu_poppies` làm giá đối soát ĐVT gốc từ ERP).*
