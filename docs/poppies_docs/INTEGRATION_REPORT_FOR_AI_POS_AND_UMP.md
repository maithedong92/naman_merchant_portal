# 📑 BÁO CÁO KỸ THUẬT & HƯỚNG DẪN TÍCH HỢP HỆ THỐNG
## DÀNH CHO ĐỘI NGŨ KỸ THUẬT POPPIES ERP, NAM AN AI-POS VÀ NAM-UMP (UNIFIED MERCHANT PORTAL)
**Dự án:** Thống nhất Bộ mã Kênh, Chính sách Bảng giá Đa kênh, Đồng bộ CTKM & Cổng Điều phối Đa kênh (Poppies ERP ↔ Nam An AI-POS ↔ NAM-UMP)  
**Ngày phát hành:** 06/11/2026 (Asia/Ho_Chi_Minh)  
**Tác giả:** NamAn Enterprise Master Orchestrator (Executive Engineering Division)  
**Tình trạng kiểm chứng:** Đạt 100% qua kiểm định độc lập của Codex (75/75 Assertions PASS) & 3/3 Multi-Schedule PASS  
**Phạm vi áp dụng:** Poppies ERP Backend/Frontend, Nam An AI-POS Gateway, NAM-UMP Hub, Mobile PDA  

---

### 1. TỔNG QUAN & PHÂN ĐỊNH VAI TRÒ 3 HỆ THỐNG

Báo cáo này chuẩn hóa toàn bộ hợp đồng giao tiếp (API Contracts), quy tắc xử lý nghiệp vụ, cơ chế phòng vệ lỗi (Fail-Closed) và hướng dẫn tích hợp giữa **Poppies ERP**, **Nam An AI-POS** và **Nam An Unified Merchant Portal (NAM-UMP)**:

| Hệ thống | Định vị vai trò | Trách nhiệm cốt lõi |
| :--- | :--- | :--- |
| **Poppies ERP** | **Bộ não Dữ liệu gốc (Master Data & Pricing Brain / SSOT)** | Quản lý độc quyền: Danh mục SKU, Barcode, Quy cách, Trạng thái kinh doanh (`01`, `02`), Khóa riêng chi nhánh (`tthh.khoaRieng`), Giá bán từng kênh, Phụ phí bao bì, CTKM và Tồn kho sổ sách CTTK. |
| **Nam An AI-POS** | **Tầng Bán lẻ Tại Quầy (In-Store Retail POS)** | Quản lý bán hàng thu ngân trực tiếp tại các siêu thị, quét barcode, tính tiền, áp dụng CTKM theo ca/khung giờ, in hóa đơn nhiệt 80mm và đóng mở ca két thu ngân. |
| **NAM-UMP** (`https://ump.namanmarket.com`) | **Cánh tay Thực thi Đa kênh & Điều phối Sàn (Omnichannel Execution & Marketplace Hub)** | Nhận Master Data & Giá kênh từ Poppies ERP để đồng bộ lên sàn (ShopeeFood, GrabMart, ShopeeMart, Haravan); gom đơn hàng online đa kênh về giao diện duy nhất cho Tổng đài phân tuyến và Siêu thị gom hàng/giao tài xế. |

#### Mục tiêu cốt lõi:
1. Thống nhất danh mục 13 kênh bán hàng Canonical, loại trừ hoàn toàn việc đọc bảng `system_configs` của AI-POS ở runtime.
2. Tối ưu bảng giá đối tác (`has_partner_pricing = False`), kế thừa trực tiếp giá bán lẻ `STORE` mà không nhân bản 92,037 dòng giá thừa, đồng thời bảo toàn 100% cấm bán theo kênh.
3. Chốt chặn an toàn cho POS Snapshot: Không nuốt exception khi CSDL lỗi; POS giữ nguyên cache hợp lệ (Last-Known-Good) khi nhận HTTP 5xx.
4. Đồng bộ CTKM chặt chẽ: Phân biệt absent vs empty `[]`, hỗ trợ cấu hình lồng `thiet_lap_pos_rule`, lọc chi nhánh trước `LIMIT`, và tự động phát tombstone khi CTKM hết hạn tự nhiên.
5. Hỗ trợ CTKM chạy Đa khung giờ (`time_slots`) cho phép gom 01 CTKM duy nhất chạy theo nhiều ca trong ngày.
6. Thiết lập hợp đồng giao tiếp 2 chiều giữa Poppies ERP và NAM-UMP (Pull API & Outbox Webhook).
7. Tuân thủ tuyệt đối chuẩn tính Net Retail Margin (loại trừ VAT) và phân tách trách nhiệm (SoD).

---

### 2. DANH MỤC 13 KÊNH CANONICAL & XỬ LÝ ALIAS TƯƠNG THÍCH NGƯỢC

#### 2.1. Quy chuẩn 13 Kênh Chuẩn (Single Source of Truth)
Poppies PostgreSQL (`pricing.pricing_channel_rules`) là nguồn chân lý duy nhất. Tất cả API giao tiếp giữa Poppies ERP và AI POS sử dụng **mã kênh chuẩn ngắn (Canonical)**:

| STT | Mã Canonical (`channel_code`) | Tên hiển thị Kênh | Nhóm kênh | Mã Arito | Aliases tương thích ngược được nhận diện |
| :---: | :--- | :--- | :--- | :---: | :--- |
| 1 | **`STORE`** | Bán trực tiếp tại cửa hàng (POS) | `store` | `K21` | `POS_OFFLINE`, `OFFLINE`, `STORE_POS`, `TAI_QUAY` |
| 2 | **`SHOPEE`** | Shopee Mall / Marketplace | `marketplace` | `K12` | `SHOPEE_MALL` |
| 3 | **`TIKTOK`** | TikTok Shop | `marketplace` | `K17` | `TIKTOK_SHOP`, `TIKTOKSHOP`, `TTS` |
| 4 | **`LAZADA`** | Lazada Marketplace | `marketplace` | `K10` | `LAZADA_MALL` |
| 5 | **`GRAB`** | GrabMart | `delivery_app` | `K15` | `GRAB_MART`, `GRABMART` |
| 6 | **`SHOPEEFOOD`**| ShopeeFood (Now) | `delivery_app` | `K22` | `SHOPEE_FOOD`, `NOW_VN` |
| 7 | **`WEB`** | Website Haravan E-commerce | `social_web` | `K20` | `HARAVAN_WEB`, `HARAVAN`, `WEBSITE`, `NAMANMARKET_VN` |
| 8 | **`ZALO`** | Zalo OA / Zalo Mini App | `social_web` | `K20` | `ZALO_OA`, `ZALO_MINI_APP`, `ZALO_APP` |
| 9 | **`FACEBOOK`** | Facebook Social Shop | `social_web` | `K20` | `FB`, `FB_SHOP` |
| 10 | **`INSTAGRAM`** | Instagram Social Shop | `social_web` | `K20` | `IG`, `IG_SHOP` |
| 11 | **`GOOGLESHOP`** | Google Shopping / Ads | `social_web` | `K20` | `GOOGLE_SHOP`, `GG_SHOP` |
| 12 | **`HARASOCIAL`** | Harasocial Omnichannel | `social_web` | `K20` | `HARA_SOCIAL`, `HARASOCIAL_CHAT` |
| 13 | **`PHONE`** | Hotline / Đặt hàng qua SĐT | `social_web` | `K20` | `HOTLINE`, `TEL` |

#### 2.2. Quy tắc Xử lý Xung đột Canonical vs Alias
- Khi truy vấn danh mục (`GET /api/v1/pos/pricing/channels`), hệ thống đọc toàn bộ các dòng vật lý và gom nhóm theo mã Canonical.
- **Bản ghi Canonical nắm thẩm quyền tối cao:** Nếu bản ghi Canonical bị tắt (`is_active = False`), các alias cũ còn cờ `is_active = True` **tuyệt đối không được phép mở lại kênh**.
- Khi một kênh bị tắt (`is_active = False`), API giải quyết giá lập tức chặn bán toàn bộ SKU trên kênh đó (`allowed_in_context = False`).

---

### 3. CƠ CHẾ KẾ THỪA STORE & BẢO TOÀN CẤM BÁN

#### 3.1. Kế thừa Giá Bán lẻ STORE (`has_partner_pricing = False`)
- Các kênh có cờ `has_partner_pricing = False` (như `STORE`, `ZALO`, `FACEBOOK`, `PHONE`):
  - **Không duplicate dữ liệu:** Không sinh ra 92,037 dòng giá riêng cho kênh đối tác trong CSDL.
  - Snapshot trả về `has_partner_pricing: false`, `inherited_from: "STORE"`, `prices: {}`, `total_items: 0`.
  - Thiết bị POS và Client tự động dùng chung bảng giá bán lẻ `STORE` chuẩn theo từng chi nhánh siêu thị.

#### 3.2. Bảo toàn Tuyệt đối Quyền Cấm bán Riêng (Exclusions & Publication Deny)
Mặc dù dùng chung bảng giá của `STORE`, chính sách mặt hàng cấm bán (Assortment) của kênh đối tác vẫn độc lập:
1. **Kiểm tra thời gian hiệu lực nghiêm ngặt:** Hệ thống chỉ áp dụng cấm bán từ `pricing.pricing_context_overrides` (`is_excluded = TRUE`) khi nằm trong khoảng thời gian có hiệu lực:
   $$\text{valid\_from} \le \text{CURRENT\_TIMESTAMP} \quad \text{VÀ} \quad (\text{valid\_to IS NULL} \;\lor\; \text{valid\_to} \ge \text{CURRENT\_TIMESTAMP})$$
   *(Loại bỏ hoàn toàn lỗi cấm bán hết hạn trong quá khứ hoặc chưa bắt đầu trong tương lai lọt vào danh sách cấm).*
2. **Hợp nhất danh mục ngưng xuất bản:** Tự động hợp nhất các SKU có `is_published = FALSE` từ bảng `catalog.sku_channel_pricing` theo đúng kênh hoặc alias của kênh.
3. Snapshot trả về danh sách SKU bị cấm qua 2 trường: `exclusions: ["SKU_A", "SKU_B"]` và `blocked_skus: ["SKU_A", "SKU_B"]`.

---

### 4. ĐẶC TẢ API SNAPSHOT & QUY CHUẨN XỬ LÝ SỰ CỐ (FAIL-CLOSED)

#### 4.1. Endpoint Tải Snapshot Giá (`GET /api/v1/pos/pricing/snapshots`)
- **Query Parameters:**
  - `branch_code` (hoặc `branch`): Mã chi nhánh siêu thị (`CH10001`, `10001`, `10004`...).
  - `channel_code` (hoặc `channel`): Mã kênh bán hàng (`STORE`, `TIKTOK`, `ZALO`...).
  - `since_version`: Version cũ để lấy Delta Snapshot (VD: `v20261106-080000`).
  - `format`: Hỗ trợ 2 định dạng:
    + `map` (Mặc định): Trả về dictionary `prices` index theo mã SKU để nạp RAM/SQLite siêu tốc.
    + `list`: Trả về mảng `items`. **Bảo toàn 100% metadata** (`has_partner_pricing`, `inherited_from`, `is_active`, `exclusions`, `blocked_skus`, `version`).

#### 4.2. Response Schema chuẩn cho Kênh kế thừa STORE (`has_partner_pricing: false`)
```json
{
  "success": true,
  "version": "v20261106-103000",
  "snapshotVersion": "v20261106-103000",
  "generated_at": "2026-11-06T10:30:00+07:00",
  "generatedAt": "2026-11-06T10:30:00+07:00",
  "branch_code": "CH10001",
  "branchCode": "CH10001",
  "channel_code": "ZALO",
  "channelCode": "ZALO",
  "has_partner_pricing": false,
  "is_active": true,
  "isActive": true,
  "inherited_from": "STORE",
  "restrict_to_priced_items": false,
  "is_delta": false,
  "isDelta": false,
  "total_items": 0,
  "totalItems": 0,
  "prices": {},
  "items": [],
  "exclusions": ["00100234", "00200456"],
  "blocked_skus": ["00100234", "00200456"],
  "elapsed_ms": 1.25,
  "elapsedMs": 1.25
}
```

#### 4.3. Quy Tắc Fail-Closed Bắt Buộc Đối Với Dev AI POS (U1 Specification)
- **Backend Poppies:** Tuyệt đối không nuốt lỗi `except: pass`. Nếu việc đọc CSDL gặp sự cố (Timeout, OperationalError), Gateway sẽ trả về lỗi HTTP 500 hoặc 503, **tuyệt đối không bao giờ trả về HTTP 200 kèm `blocked_skus: []` giả mạo**.
- **Yêu cầu đối với Dev AI POS:**
  > [!IMPORTANT]
  > Khi POS gọi API Snapshot mà nhận mã phản hồi lỗi HTTP 5xx (500, 502, 503, 504) hoặc timeout mạng:
  > 1. **GIỮ NGUYÊN BỘ ĐỆM CACHE HIỆN TẠI (Last-Known-Good Cache):** Tiếp tục duy trì danh sách cấm bán `blocked_skus` và giá bán hợp lệ của lần đồng bộ thành công trước đó.
  > 2. **CẤM XÓA TRẮNG DANH MỤC CẤM BÁN:** Tuyệt đối không xóa bỏ danh sách SKU bị chặn bán khi request gặp lỗi.
  > 3. **Cơ chế Retry có Backoff:** Thử lại sau 15s, 30s, 60s mà không làm gián đoạn ca bán hàng offline của thu ngân.

---

### 5. ĐẶC TẢ API ĐỒNG BỘ CTKM CHO AI POS (`GET /api/v1/proposals/promotions/pos-rules`)

Endpoint Pull chủ động để AI POS đồng bộ danh sách CTKM đang có hiệu lực phục vụ tính tiền Offline tại quầy:

#### 5.1. Query Parameters
- `branch_code`: Lọc theo mã chi nhánh (VD: `CH10001`). Lọc ngay trong SQL trước khi `LIMIT / OFFSET`.
- `channel_code`: Lọc theo kênh (VD: `TIKTOK`, `STORE`). SQL nhận diện cả mã canonical, toàn bộ alias cũ, và cấu hình lồng trong `thiet_lap_pos_rule`.
- `updated_since`: Mốc thời gian ISO-8601 để kéo Delta Sync (VD: `2026-11-05T00:00:00Z`).
- `limit` (1..500) & `offset` (>=0): Phân trang an toàn.

#### 5.2. Các Quy Tắc Nghiệp Vụ CTKM Cần Lưu Ý
1. **Phân cấp phê duyệt FSM:**
   - Phiếu chỉ mới duyệt TBP (`trang_thai_de_xuat ILIKE '%tbp%'`) và chưa qua BGD: **Bị loại bỏ hoàn toàn** khỏi active rules (`status: 'pending_bgd'`).
   - Phiếu duyệt BGD nhưng chưa IT thực thi: Trả về `status: 'pending_execution'`, POS không được áp dụng giảm giá.
   - Phiếu đã duyệt BGD VÀ đã thực thi (hoặc có `it_executed_at`): Trả về `status: 'active'`.
2. **Quy tắc Phạm vi Kênh (Channel Scope):**
   - Đề xuất mới tạo mặc định: `channel_scope: 'CUSTOM'` và `target_channels: ['STORE']`.
   - Chọn toàn bộ kênh: Chỉ khi người dùng chọn rõ ràng `['ALL']`.
   - Trường hợp cấu hình rỗng `CUSTOM + []`: Tự động fallback an toàn về `['STORE']` và gán cờ `needs_scope_review: true`.
3. **Cơ chế Tombstone Delta cho CTKM hết hạn tự nhiên:**
   - Khi CTKM hết hạn tự nhiên theo ngày kết thúc (`ngay_ket_thuc_km_cho_ntd < TODAY`) dù cột `migrated_at` không được cập nhật: Hệ thống vẫn tự động quét và phát tombstone `status: 'expired'` trong Delta Sync để POS tự động gỡ bỏ chương trình khuyến mại khỏi quầy thu ngân.

---

### 6. PHÂN QUYỀN RBAC & QUẢN LÝ THAY ĐỔI CẤU HÌNH KÊNH

#### 6.1. Endpoint Cập nhật Kênh (`PUT /api/v1/pos/pricing/channels/{channel_code}`)
- **Ma trận thẩm quyền (Segregation of Duties - SoD):**
  - **Cho phép (HTTP 200):** Tài khoản người dùng có vai trò quản trị hệ thống (`pos_admin`, `admin`, `bgd`, `system_admin`, `superadmin`) hoặc có cờ `isAdmin: true`.
  - **Từ chối (HTTP 403 Forbidden):**
    + Khóa API Key của thiết bị máy POS (`auth_client['type'] == 'API_KEY'`).
    + Nhân sự Trưởng bộ phận (`role == 'manager_approver'`).
- **Lưu vết kiểm toán:** Ghi nhận chính xác `actor` thực hiện từ `user.username` (không dùng tài khoản mặc định).
- **Phát sinh Outbox Event:** Khi thay đổi `has_partner_pricing` hoặc `is_active`, hệ thống tự động tăng `version` và ghi bản ghi Transactional Outbox `CHANNEL_PRICING_STATUS_CHANGED`.

---

### 7. QUY CHUẨN TÍNH BIÊN LỢI NHUẬN (NET RETAIL MARGIN - EXCLUDE VAT)

Áp dụng bắt buộc cho toàn bộ logic tính toán và hiển thị trên Poppies ERP và AI POS:

$$\text{Giá Bán Chưa VAT} = \frac{\text{Giá Bán Niêm Yết (POS)}}{1 + \frac{\text{VAT Đầu Ra (\%) }}{100}}$$

$$\text{Tiền Lãi Gộp Chưa VAT} = \text{Giá Bán Chưa VAT} - \text{Giá Mua Chưa VAT (SPPRICE)}$$

$$\text{Margin (\%)} = \frac{\text{Tiền Lãi Gộp Chưa VAT}}{\text{Giá Bán Chưa VAT}} \times 100$$

> [!CAUTION]
> **Tuyệt đối CẤM:** Không tính Margin thô trực tiếp giữa Giá Bán POS (đã gồm VAT) với Giá Mua chưa VAT $\left(\frac{\text{Giá POS} - \text{Giá Mua}}{\text{Giá POS}}\right)$. Sai lệch này vi phạm chuẩn mực kế toán tài chính doanh nghiệp.

---

### 8. TỔNG KẾT BẰNG CHỨNG KIỂM ĐỊNH (75 / 75 ASSERTIONS PASS)

Toàn bộ hệ thống mã nguồn đã được kiểm chứng độc lập bởi Codex qua 6 bộ test probe nghiêm ngặt:

```text
================================================================================
1. Restriction Failure Probe (restriction_failure_probe.py): 3 / 3 PASSED
   - healthy_empty_snapshot_succeeds ..................................... PASS
   - context_read_error_not_successful_snapshot .......................... PASS
   - publication_read_error_not_successful_snapshot ...................... PASS

2. Snapshot Parity Probe (snapshot_probe.py): 4 / 4 PASSED
   - list_format_preserves_policy_and_exclusions ......................... PASS
   - expired_exclusion_not_inherited ..................................... PASS
   - future_exclusion_not_inherited ...................................... PASS
   - snapshot_respects_unpublished_sku ................................... PASS

3. Completion Probe (completion_probe.py): 10 / 10 PASSED
   - canonical_admin_can_edit_channel .................................... PASS
   - closed_canonical_not_reopened_by_active_alias ....................... PASS
   - transaction_readonly ................................................ PASS
   - inherited_snapshot_carries_channel_exclusion ........................ PASS
   - inactive_snapshot_reports_closed .................................... PASS
   - pull_canonical_filter_keeps_legacy_channel .......................... PASS
   - pull_filter_agrees_with_nested_serializer_scope ..................... PASS
   - pull_filter_keeps_explicit_all_list ................................. PASS
   - branch_filter_applies_before_limit .................................. PASS
   - expiry_without_record_update_emits_delta_tombstone .................. PASS

4. Local ASGI Runtime Probe (runtime_probe.py): 14 / 14 PASSED
5. PostgreSQL Synthetic CTE Probe (pg_probe.py): 15 / 15 PASSED
6. Regression & Helper Unit Tests (regression_probe.py): 29 / 29 PASSED
================================================================================
TỔNG CỘNG: 75 / 75 ASSERTIONS PASSED (0 FAILED)
```

#### Tuân thủ Rule 4.1 (< 500 dòng/file):
- `pos_promotion_schedule_service.py`: **112 dòng** (Mới tách chuẩn hóa Multi Time Slots)
- `TimeSlotsEditor.tsx`: **223 dòng** (Component giao diện chuẩn Apple HIG)
- `pos_pricing_restriction_service.py`: **87 dòng**
- `pos_promotion_pull_service.py`: **212 dòng**
- `pos_promotion_sync_service.py`: **465 dòng** (Đã tối ưu hóa giảm từ 494)
- `pos_channel_master_service.py`: **271 dòng**
- `pos_channel_canonical.py`: **257 dòng**
- `pos_promotion_scope_service.py`: **211 dòng**

---

## 4. Chuẩn Hóa Đa Khung Giờ Trong Ngày (`time_slots`)

Để hỗ trợ các chương trình khuyến mãi theo nhiều ca làm việc (ví dụ: Thẻ nhân viên chạy 3 ca `07:00-09:00`, `11:30-13:30`, `17:00-19:00`) trong **01 CTKM DUY NHẤT** mà không cần nhân bản rule:

### 4.1. Contract Payload Đồng Bộ Sang AI-POS
Trường `conditions` trong Webhook `POST /api/v1/promotions/webhook/sync-rule` và Pull API bổ sung mảng `time_slots`:

```json
{
  "code": "KM261106_STAFF_DISCOUNT",
  "conditions": {
    "customer_tier": ["nv"],
    "applicable_channels": ["STORE"],
    "time_slots": [
      { "start": "07:00", "end": "09:00", "label": "Ca Sáng" },
      { "start": "11:30", "end": "13:30", "label": "Ca Trưa" },
      { "start": "17:00", "end": "19:00", "label": "Ca Chiều" }
    ],
    // Tương thích ngược cho các phiên bản AI-POS chưa nâng cấp:
    "start_hour": "07:00",
    "end_hour": "09:00"
  }
}
```

### 4.2. Hướng Dẫn Cập Nhật Đội Kỹ Thuật Nam An AI-POS (`PromotionEngine`)
Trong tầng kiểm tra điều kiện thời gian áp dụng khuyến mãi (`PromotionEngine.ts`), kiểm tra ưu tiên `time_slots`:

```javascript
// Đánh giá thời gian theo giờ hiện tại của quầy thu ngân (HH:mm)
const nowStr = moment().format('HH:mm'); // Ví dụ: "12:15"

// 1. Nếu có time_slots (Mảng nhiều ca trong ngày)
if (Array.isArray(rule.conditions?.time_slots) && rule.conditions.time_slots.length > 0) {
    const isMatchedSlot = rule.conditions.time_slots.some(slot => {
        return nowStr >= slot.start && nowStr <= slot.end;
    });
    if (!isMatchedSlot) {
        return false; // Ngoài tất cả các khung giờ quy định
    }
} 
// 2. Fallback kiểm tra khung giờ đơn truyền thống
else if (rule.conditions?.start_hour && rule.conditions?.end_hour) {
    if (nowStr < rule.conditions.start_hour || nowStr > rule.conditions.end_hour) {
        return false;
    }
}
```

---

## 5. Quy Chuẩn Tích Hợp Kỹ Thuật Poppies ERP ↔ Nam An Unified Merchant Portal (NAM-UMP)

Hệ thống NAM-UMP (`https://ump.namanmarket.com`) kết nối với Poppies ERP theo 2 chiều rõ rệt:

### 5.1. Chiều 1: NAM-UMP Gọi API Poppies ERP (Pull & Sync qua `X-POS-API-Key`)
| STT | Endpoint bên Poppies ERP | Mục đích NAM-UMP sử dụng |
| :---: | :--- | :--- |
| 1 | `GET /api/v1/pos/master-data/health` | Kiểm tra trạng thái kết nối DB, độ trễ và tổng số bản ghi SKU / CTTK. |
| 2 | `GET /api/v1/pos/master-data/snapshot` | Đồng bộ hàng loạt danh mục, SKU, ảnh `ecommerce.imageUrl`, giá bán, cờ `tthh.khoaRieng` và tồn kho `inventoryCttk`. |
| 3 | `GET /api/v1/pos/master-data/sku/{sku_or_barcode}` | Tra cứu tức thời thông tin chi tiết & quyền mở bán của 1 SKU theo `branch_code`. |
| 4 | `GET /api/v1/pos/master-data/cttk-records` | Đồng bộ nhanh số lượng tồn kho `slTon` của SKU trên toàn bộ chi nhánh. |
| 5 | `GET /api/v1/pos/pricing/channels` | Lấy danh sách 10 kênh chuẩn hóa kèm mã kênh kế toán Arito (`ma_kenh`). |
| 6 | `POST /api/v1/pricing/channel-publish-status` | Đồng bộ ngược trạng thái Bật/Tắt bán `isPublished` khi Quản lý thao tác từ giao diện UMP. |

### 5.2. Chiều 2: Poppies ERP Bắn Webhook Sang NAM-UMP (Real-Time Push)
Khi người dùng thay đổi Giá bán theo kênh hoặc Bật/Tắt bán sản phẩm trên kênh tại Poppies ERP, `PricingOutboxDispatcher` bắn thẳng Webhook sang NAM-UMP:
- **Webhook URL:** `POST https://ump.namanmarket.com/api/v1/pricing/webhook/price-changed`
- **Headers:** `X-Poppies-Webhook-Key: <SHARED_SECRET_KEY>`, `X-Event-Id: <event_id>`
- **Payload mẫu:**
  ```json
  {
    "event_id": "evt_outbox_89412a87c3b1",
    "event": "price_changed",
    "event_type": "CHANNEL_PUBLISH_STATUS_CHANGED",
    "channel_code": "GRAB_MART",
    "branch_code": "ALL",
    "affected_skus": ["290031798000"],
    "is_published": false,
    "is_full_catalog": false,
    "timestamp": "2026-11-06T04:00:00Z"
  }
  ```

### 5.3. Quy Chuẩn Đồng Bộ Mã Định Danh Dùng Chung
1. **Hàng hóa:** Khóa chính `skuCode` (12 chữ số) dùng xuyên suốt để map sản phẩm, tồn kho và chi tiết đơn hàng.
2. **Chi nhánh:** `10001` (Thảo Điền), `10004` (Quận 7), `10005` (Mai Chí Thọ), `10006` (Nguyễn Văn Trỗi), `20004` (Kho Tổng).
3. **Mã kênh:** Tuân thủ 100% bảng mã Canonical ở Mục 2 (`STORE`, `GRAB`, `SHOPEEFOOD`, `SHOPEE`, `LAZADA`, `TIKTOK`, `WEB`, `ZALO`, `FACEBOOK`, `PHONE`).
4. **Kiểm soát Tồn kho & Chống Phạt Sàn:**
   - Tự động ngưng nhận đơn trên sàn nếu SKU bị đánh dấu `tthh.khoaRieng[store_code] = true` hoặc `status != "01"` trên Poppies ERP.
   - Thao tác Bật/Tắt nhanh 1-chạm (Out-of-Stock) tại siêu thị khi hết hàng đột xuất đồng bộ 2 chiều ngay lập tức về Poppies ERP.

---

## 6. Cập Nhật Bổ Sung Ngày 18/11/2026: Trường Tỉ Lệ Quy Đổi Chia Cân Dành Riêng Cho `NAM-UMP`

- **AI-POS không tác động / giữ nguyên như cũ:** Toàn bộ endpoint `/api/v1/pos/*` và Webhook gửi sang `AI_POS` giữ nguyên 100%, không thêm field tỉ lệ quy đổi và không nhận webhook `CONVERSION_RATE_UPDATED`.
- **Quy tắc giá nguyên bản (100% ĐVT gốc ERP):** API Pull (`/api/v1/ump/*`) và Webhook (`NAM_UMP`) vẫn trả **giá bán nguyên 100% theo ĐVT gốc ERP**, không tự nhân/chia tỉ lệ quy đổi. Việc nhân/chia giá để đăng lên sàn do bên **`NAM-UMP`** chủ động xử lý.
- **Endpoints & Webhook riêng cho `NAM-UMP`:**
  - Webhook `NAM_UMP`: nhận `conversion_rates` / `conversionRates` khi `is_full_catalog = false` và `affected_skus <= 200`; nhận thêm `event_type = "CONVERSION_RATE_UPDATED"` (`event = "price_changed"`) khi Admin sửa tỉ lệ quy đổi.
  - Dedicated Pull APIs: `/api/v1/ump/conversion-rates`, `/api/v1/ump/conversion-rates/{sku}`, `/api/v1/ump/pricing/resolve`, `/api/v1/ump/pricing/snapshots`, `/api/v1/ump/master-data/*`.
  - **Thứ tự ưu tiên:** `MANUAL` (`catalog.sku_def.conversion_rate` chỉnh tay) $\rightarrow$ `HARAVAN` (`haravan_product_variants.conversion_rate`, ưu tiên bản VI mới nhất) $\rightarrow$ mặc định `1.0` (`DEFAULT`).
- Chi tiết đầy đủ JSON payload & hợp đồng API xem tại: [POPPIES_UMP_AIPOS_CONVERSION_RATE_API_SPEC.md](file:///Users/pabi/Nam%20An%20Mobile/NamAnMarket/docs/api/POPPIES_UMP_AIPOS_CONVERSION_RATE_API_SPEC.md).

---

*Tài liệu này là căn cứ kỹ thuật chính thức để dev cả ba hệ thống: Poppies ERP, Nam An AI-POS và NAM-UMP đối chiếu, hoàn thiện các module kết nối và nghiệm thu bàn giao.*
