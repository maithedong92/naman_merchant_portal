# 📘 TÀI LIỆU KỸ THUẬT: API MASTER DATA SKU CHO HỆ THỐNG POS CHUẨN ERP
**(Nam An Market - Poppies ERP to AI POS / Daisy POS Integration Specification)**

---

## 1. Tổng Quan & Phạm Vi Dữ Liệu (Scope & Security Policy)

Cổng API này phục vụ việc đồng bộ Master Data SKU thời gian thực và theo snapshot giữa **Poppies ERP** và **Hệ thống POS tại quầy thu ngân (Daisy POS / Nam An AI POS)**.

### 🛡️ Nguyên Tắc Bảo Mật & Lọc Dữ Liệu:
- ❌ **Loại bỏ 100% dữ liệu thu mua & bảo mật**:
  - Không có Giá vốn (`costPrice`, `cost`).
  - Không có Giá nhập chỉ định (`purchasePrice`, `spprice`).
  - Không có Chiết khấu mua (`DISC_SPPR`), Thuế suất đầu vào (`inTaxRate`, `inTaxCode`).
  - Không có Thông tin Nhà cung cấp (`supplierCode`, `supplierName`, `contractCode`, `buyerInCharge`, `suppliers[]`).
- ✅ **Đầy đủ 100% các trường Master Data hiển thị trên Frontend**:
  - Mã định danh: `skuCode`, `posCode`, `codeType`, Barcode chính & danh sách Barcode phụ (`barcodes[]`).
  - Tên hàng, ĐVT, Ngành hàng, Phân nhóm, Nhóm `isFresh`, Trọng lượng/Quy cách, Xuất xứ.
  - Giá bán lẻ niêm yết POS (`baseSalePriceVat`), Giá trước thuế (`baseNetSalePrice`), Thuế suất bán (`vatRate`, `vatCode`).
  - Bảng giá đa kênh: Giá riêng chi nhánh (`storeOverrides`), Giá sàn GrabMart, ShopeeFood, Haravan Web (`channels[]`).
  - Trạng thái vận hành TTHH: `01` Kinh doanh, `02` Khóa, `03` Khóa Mua.
  - Trạng thái Khóa riêng từng cửa hàng (`khoaRieng`) và Mức trưng bày tối thiểu (`minDisplay`).
  - Hạn sử dụng (`hsd`), HST Min / Max, Quy cách thùng, Đặt về kho tổng, Trạng thái gom (`DH.01`...).
  - **Tồn kho tức thời CTTK đa chi nhánh (`inventoryCttk`)**: Chi tiết theo từng kho, số lô (`maLo`), vị trí kệ (`maViTri`), số lượng tồn thực tế (`slTon`).
  - Dữ liệu E-Commerce Haravan: Link ảnh CDN (`imageUrl`), Link website (`webUrl`), Quy cách web (`packaging`), Tỷ lệ quy đổi web.

---

## 2. Thông Tin Kết Nối & Xác Thực (Authentication & Coordinates)

- **Base URL Production**: `https://api.namanmarket.com/` (hoặc nội bộ VPS `http://103.175.248.250:1155/`)
- **Prefix Gateway**: `/api/v1/pos/master-data` (hoặc alias `/pos/master-data`)
- **Cơ chế xác thực (Authentication)**:
  Máy POS gửi kèm một trong các Header sau:
  - `X-POS-API-Key: naman_pos_api_secret_key_2026`
  - Hoặc `X-API-Key: naman_pos_api_secret_key_2026`
  - Hoặc `Authorization: Bearer <API_KEY>`

---

## 3. Danh Sách Endpoints Chi Tiết

### 3.1. Probe Kiểm Tra Sức Khỏe API (`GET /health`)
- **Endpoint**: `GET /api/v1/pos/master-data/health`
- **Mục đích**: POS kiểm tra latency mạng, kết nối DB và số lượng bản ghi sẵn sàng.
- **Response**:
```json
{
  "status": "healthy",
  "service": "Nam An Poppies POS Master Data Inbound Gateway",
  "version": "2.1.0",
  "client": "Nam An AI POS Engine",
  "server_time": "2026-10-19T09:39:08.340999+00:00",
  "total_sku_count": 30579,
  "total_cttk_records": 149542,
  "database": "connected",
  "db_latency_ms": 165.89
}
```

---

### 3.2. Tra Cứu Master Data Đầy Đủ 1 SKU / Barcode (`GET /sku/{sku_or_barcode}`)
- **Endpoint**: `GET /api/v1/pos/master-data/sku/{sku_or_barcode}?branch_code={branch_code}`
- **Tham số**:
  - `sku_or_barcode` (Path, bắt buộc): Mã SKU (vd: `290030839000`), Mã POS (vd: `F116369`), hoặc Barcode (vd: `93573719`).
  - `branch_code` (Query, tùy chọn): Mã chi nhánh POS (vd: `10001`, `CH10001`, `10004`, `10005`, `10006`, `20004`) để tự động đánh giá quyền mở bán (`allowPosDisplay`) và kiểm tra khóa riêng.
- **Response Thực Tế**:
```json
{
  "success": true,
  "query": "290030839000",
  "branch": "CH10001",
  "item": {
    "skuCode": "290030839000",
    "posCode": "F116369",
    "codeType": "SKU_STANDARD",
    "barcode": "93573719",
    "barcodes": [
      {
        "barcode": "93573719",
        "isDefault": true,
        "type": "01",
        "descript": "Mã vạch mặc định",
        "unitSymb": "HOP",
        "unitConv": 1.0
      }
    ],
    "productName": "Việt quất úc 200g",
    "shortName": "AUSTRALIA BLUEBERRIES 200G",
    "unitName": "Hộp",
    "brandName": "Nam An",
    "categoryName": "FRESH - IMPORT FRUIT",
    "subcategoryName": "Mặt Hàng POS",
    "isFresh": true,
    "weightVolume": "Chuẩn",
    "countryOrigin": "Nhập Khẩu",
    "status": "01",
    "statusLabel": "Kinh doanh",
    "allowPosDisplay": true,
    "isLockedForTargetBranch": false,
    "pricing": {
      "baseSalePriceVat": 269900.0,
      "baseNetSalePrice": 257047.62,
      "vatRate": 5.0,
      "vatCode": "T1",
      "storeOverrides": [],
      "channels": [
        {
          "channelCode": "POS_OFFLINE",
          "channelName": "Bán lẻ tại quầy POS",
          "listedPrice": 269900.0,
          "isPublished": true
        }
      ]
    },
    "tthh": {
      "mdPhuTrach": "tranthibichtram",
      "quyCach": "HOP",
      "hsd": 7,
      "hsdQuyDoi": 7,
      "hstMin": 2,
      "hstMax": 3,
      "ttGom": "DH.01",
      "datVeKhoTong": false,
      "nguyenNhan": "NCC có hàng",
      "ngayDoiTrangThai": "2026-09-11",
      "ngayCanUpdateTiepTheo": "2026-11-10",
      "khoaRieng": {
        "10001": false,
        "10004": false,
        "10005": false,
        "10006": false,
        "20004": false,
        "10008": false,
        "10002": false,
        "10003": false,
        "10007": false
      },
      "minDisplay": {
        "10001": 3.0,
        "10004": 3.0,
        "10005": 3.0,
        "10006": 3.0,
        "20004": 0.0,
        "10008": 0.0,
        "10002": 0.0,
        "10003": 0.0,
        "10007": 0.0
      }
    },
    "inventoryCttk": {
      "totalStock": 0.0,
      "updatedAt": "2026-10-19T16:43:41+07:00",
      "details": [
        {
          "storeCode": "10001",
          "storeName": "Nam An Market - Thảo Điền",
          "shortName": "Thảo Điền",
          "maLo": "20260114",
          "maViTri": "10001A",
          "slTon": 32.0,
          "lastMovement": "2026-10-08T11:13:26+07:00"
        },
        {
          "storeCode": "10004",
          "storeName": "Nam An Market - Quận 7",
          "shortName": "Quận 7",
          "maLo": "20260114",
          "maViTri": "10004A",
          "slTon": 16.0,
          "lastMovement": "2026-10-08T11:13:26+07:00"
        },
        {
          "storeCode": "20004",
          "storeName": "Kho Tổng Trung Tâm",
          "shortName": "Kho Tổng",
          "maLo": "20260905",
          "maViTri": "20004A",
          "slTon": 96.0,
          "lastMovement": "2026-10-08T11:13:26+07:00"
        }
      ]
    },
    "ecommerce": {
      "isOnWebsite": false,
      "webTitle": null,
      "webUrl": null,
      "webPrice": null,
      "imageUrl": null,
      "packaging": null,
      "conversionRate": 1.0
    }
  }
}
```

---

### 3.3. Tra Cứu Nhanh Tồn Kho CTTK Đa Chi Nhánh (`GET /stock-cttk/{sku}`)
- **Endpoint**: `GET /api/v1/pos/master-data/stock-cttk/{sku}`
- **Mục đích**: Cho thu ngân tra cứu siêu tốc xem mặt hàng còn ở chi nhánh nào khác, lô nào, số lượng bao nhiêu.
- **Response**:
```json
{
  "success": true,
  "sku": "290030839000",
  "totalStock": 0.0,
  "stockCount": 18,
  "details": [
    {
      "storeCode": "10001",
      "storeName": "Nam An Market - Thảo Điền",
      "shortName": "Thảo Điền",
      "maLo": "20260114",
      "maViTri": "10001A",
      "slTon": 32.0,
      "lastMovement": "2026-10-08T11:13:26+07:00"
    },
    {
      "storeCode": "10004",
      "storeName": "Nam An Market - Quận 7",
      "shortName": "Quận 7",
      "maLo": "20260114",
      "maViTri": "10004A",
      "slTon": 16.0,
      "lastMovement": "2026-10-08T11:13:26+07:00"
    },
    {
      "storeCode": "20004",
      "storeName": "Kho Tổng Trung Tâm",
      "shortName": "Kho Tổng",
      "maLo": "20260905",
      "maViTri": "20004A",
      "slTon": 96.0,
      "lastMovement": "2026-10-08T11:13:26+07:00"
    }
  ]
}
```

---

### 3.4. Xem Tất Cả Bản Ghi CTTK Của Sản Phẩm Trong Toàn Hệ Thống (`GET /cttk-records`)
- **Endpoint**: `GET /api/v1/pos/master-data/cttk-records`
- **Mục đích**: Truy vấn toàn bộ 149,000+ bản ghi tồn kho tức thời CTTK (Lô, vị trí, số lượng) của các sản phẩm trên toàn bộ các kho & siêu thị.
- **Tham số Query (Filters & Pagination)**:
  - `store_code` (tùy chọn): Lọc theo mã kho / cửa hàng (vd: `10001`, `10004`, `10005`, `10006`, `20004`).
  - `sku` (tùy chọn): Lọc theo mã sản phẩm `ma_vt` / `skuCode`.
  - `only_positive` (boolean, mặc định `false`): Nếu `true`, chỉ lấy các dòng có số lượng tồn `slTon > 0`.
  - `limit` (integer, mặc định `1000`, tối đa `10000`): Giới hạn số bản ghi trả về mỗi trang.
  - `offset` (integer, mặc định `0`): Vị trí bắt đầu phân trang.
- **Response Thực Tế**:
```json
{
  "success": true,
  "totalRecords": 110626,
  "limit": 5,
  "offset": 0,
  "count": 5,
  "items": [
    {
      "sku": "290000066000",
      "productName": "Kem chuối Bella Bellany 450ml",
      "unitName": "Hũ",
      "storeCode": "10001",
      "storeName": "Nam An Market - Thảo Điền",
      "shortName": "Thảo Điền",
      "maLo": "20260416",
      "maViTri": "10001A",
      "slTon": 6.0,
      "lastMovement": "2026-10-08T11:13:26+07:00"
    },
    {
      "sku": "290000066000",
      "productName": "Kem chuối Bella Bellany 450ml",
      "unitName": "Hũ",
      "storeCode": "10001",
      "storeName": "Nam An Market - Thảo Điền",
      "shortName": "Thảo Điền",
      "maLo": "20260505",
      "maViTri": "10001A",
      "slTon": 6.0,
      "lastMovement": "2026-10-08T11:13:26+07:00"
    },
    {
      "sku": "290000067000",
      "productName": "Kem chuối Bella Bellany 125ml",
      "unitName": "Hũ",
      "storeCode": "10001",
      "storeName": "Nam An Market - Thảo Điền",
      "shortName": "Thảo Điền",
      "maLo": "20260127",
      "maViTri": "10001A",
      "slTon": 10.0,
      "lastMovement": "2026-10-08T11:13:26+07:00"
    }
  ]
}
```

---

### 3.5. Tải Snapshot Theo Lô Đồng Bộ Đầu Ca / Offline Cache (`GET /snapshot`)
- **Endpoint**: `GET /api/v1/pos/master-data/snapshot?branch_code=CH10001&status_filter=01&limit=500&offset=0`
- **Tham số**:
  - `branch_code`: Mã chi nhánh (mặc định `CH10001`).
  - `status_filter`: `01` (Kinh doanh) hoặc `all` (Tất cả).
  - `limit`: Số bản ghi / trang (1 đến 5000, mặc định 500).
  - `offset`: Vị trí phân trang.
- **Response**:
```json
{
  "success": true,
  "branch": "CH10001",
  "total": 30579,
  "limit": 500,
  "offset": 0,
  "count": 500,
  "items": [
    { "...(Master Data SKU Object chuẩn)..." }
  ]
}
```

---

## 4. Bảng Tra Cứu Mã Thuế & Mã Chi Nhánh Chuẩn

### Mã Chi Nhánh Chuẩn (Store Master):
- `10001` / `CH10001`: Nam An Market - Thảo Điền (Quận 2)
- `10004` / `CH10004`: Nam An Market - Lê Thánh Tôn / Quận 7
- `10005` / `CH10005`: Nam An Market - An Phú / Mai Chí Thọ
- `10006` / `CH10006`: Nam An Market - Nguyễn Văn Trỗi / NTMK
- `10008` / `CH10008`: Cửa hàng TMĐT / Kho E-Commerce
- `20004` / `CH20004`: Kho Tổng Trung Tâm (Cát Lái)

### Mã Thuế VAT POS:
- `T0`: 0%
- `T1`: 5%
- `T5`: 8%
- `T2`: 10%
