# Hướng Dẫn Tích Hợp GrabMart Partner POS API (NAM-UMP)

> **Dự án:** Nam An Market - Unified Merchant Portal (NAM-UMP)  
> **Kênh bán lẻ:** GrabMart VN (Grab POS Integration)  
> **Phiên bản tài liệu:** GrabMart Partner POS API v1.1.3  
> **Cập nhật:** 2026-09-15  

---

## 1. Tổng Quan Quy Trình (Grab Getting Started Guide)

Quy trình tích hợp chính thức từ Grab Developer Portal bao gồm 5 giai đoạn:

```
[01. Onboarding] ➔ [02. Development] ➔ [03. Testing] ➔ [04. Order Type] ➔ [05. Production]
```

---

## 2. Chi Tiết Từng Giai Đoạn Tích Hợp

### 01. Onboarding (Khởi Tạo Thông Tin Xác Thực)
1. **Tạo OAuth 2.0 Credentials:**
   - Đăng nhập vào **Grab Developer Portal**.
   - Vào mục **Project details**.
   - Dưới phần **Credentials**, bấm **Add** để tạo client credentials.
   - Nhập thông tin bắt buộc: Partner Name (*Nam An Market*), Email address, Logo.
   - Bấm **Save**.
   - Lấy **Client ID** và **Client Secret** (bấm vào biểu tượng con mắt để xem Secret).
2. **Cấu hình vào NAM-UMP:**
   - Điền 2 giá trị này vào file `.env` của hệ thống:
     ```ini
     GRABMART_CLIENT_ID=your_grab_client_id_here
     GRABMART_CLIENT_SECRET=your_grab_client_secret_here
     GRABMART_BASE_URL=https://partner-api.grab.com
     GRABMART_OAUTH_URL=https://api.grab.com/grabid/v1/oauth2/token
     GRABMART_SCOPE=mart.partner_api
     ```

---

### 02. Development (Cấu Hình Endpoint & Kết Nối Hệ Thống)

Hệ thống NAM-UMP đã lập trình sẵn toàn bộ các endpoint cần thiết theo đúng chuẩn của Grab:

#### 1. Tạo Cấu Trúc Menu (Create a Menu Structure)
- Grab cho phép tải file JSON hoặc sử dụng công cụ **Menu Preview** để kiểm tra giao diện menu trên ứng dụng Grab.
- NAM-UMP cung cấp cấu trúc menu chuẩn `GrabMart Menu v1.1.3` qua hàm `build_catalog_menu()` trong `app/modules/grabmart/adapter.py`.

#### 2. Ánh Xạ Cửa Hàng (Map Your Store)
- Đảm bảo hệ thống NAM-UMP giao tiếp chính xác với từng chi nhánh trên Grab:
  1. Thêm **Partner Store ID** (Mã chi nhánh của Nam An) vào trang Configuration trên Grab.
  2. Liên kết **GrabMart Store ID** (Mã do Grab cấp) tương ứng với từng chi nhánh.
- Danh sách 4 chi nhánh vật lý Nam An Market:
  - `10001`: Nam An Market - 21 Thảo Điền, TP. Thủ Đức
  - `10004`: Nam An Market - 46 Hưng Phúc, Phú Mỹ Hưng, Q.7
  - `10005`: Nam An Market - 17 Mai Chí Thọ, TP. Thủ Đức
  - `10006`: Nam An Market - 303 Nguyễn Văn Trỗi, Q. Tân Bình

#### 3. Phương Thức Xác Thực (Authentication Method - OAuth 2.0)
- Grab yêu cầu xác thực bằng OAuth 2.0 (Bearer Token).
- **NAM-UMP Endpoint:**
  - `POST https://ump.namanmarket.com/grabmart/oauth/token`
  - GrabMart gọi endpoint này để lấy Bearer token trước khi gửi các webhook đơn hàng hoặc kéo menu.

#### 4. Đồng Bộ Thực Đơn & Tồn Kho (Sync Your Menu)
- **Menu Update Notification endpoint (Outbound từ NAM-UMP ➔ GrabMart):**
  - Khi có sản phẩm hết hàng hoặc thay đổi giá tại kho Nam An:
  - Gọi: `POST https://partner-api.grab.com/partner/v1/merchant/menu/notification`
  - Cập nhật tồn kho SKU: `PUT https://partner-api.grab.com/partner/v1/menu`
- **Get Menu endpoint (Inbound từ GrabMart ➔ NAM-UMP):**
  - GrabMart chủ động gọi để lấy menu và trạng thái còn/hết hàng:
  - Endpoint: `GET https://ump.namanmarket.com/grabmart/menu`

#### 5. Phương Thức Tiếp Nhận Đơn (Select Order Acceptance Method)
- **Quy định của Grab:** Tại các quốc gia Đông Nam Á bao gồm **Việt Nam (VN)**, GrabMart áp dụng mặc định cơ chế **Auto Acceptance** (Tự động nhận đơn ngay khi khách đặt thành công).
- Không yêu cầu gọi thủ công endpoint `Report Prepare State` để chấp nhận đơn.

#### 6. Nhận Đơn Hàng Mới (Submit Order Webhook)
- Đơn hàng mới từ khách trên Grab được bắn trực tiếp về máy chủ Nam An:
  - Endpoint: `POST https://ump.namanmarket.com/grabmart/webhooks/order`
  - Hệ thống tự động ghi nhận vào cơ sở dữ liệu `UnifiedOrder`, chuyển trạng thái `CONFIRMED`, và phát thông báo âm thanh/chuông tại trang `/admin/orders`.

#### 7. Cập Nhật Trạng Thái Đơn Hàng (Push Order State Webhook)
- Grab thông báo các bước luân chuyển đơn hàng: tài xế nhận đơn, tài xế đã đến lấy hàng, giao thành công, hủy đơn:
  - Endpoint: `PUT https://ump.namanmarket.com/grabmart/webhooks/order/state`

---

### 03. Testing (Kiểm Thử Với Công Cụ Testing Tools Của Grab)

1. **Validation Checks (Xác thực Menu):**
   - Grab tự động kiểm tra cấu trúc dữ liệu khi kéo Menu từ `GET https://ump.namanmarket.com/grabmart/menu`.
   - Cần đảm bảo tất cả sản phẩm đều có: tên món, giá bán (VND), danh mục, hình ảnh hợp lệ, trạng thái còn/hết hàng.
2. **Test Case Scenarios:**
   - Thực hiện kiểm thử toàn bộ các kịch bản có sẵn trên trang Developer:
     - Tạo đơn thành công.
     - Điều phối tài xế lấy hàng.
     - Đơn hủy do khách hoặc cửa hàng.
     - Tắt món / bật món tức thì.

---

### 04. Select Order Type (Chọn Loại Hình Đơn Hàng)

Grab cung cấp 3 loại hình:
1. **Delivery by Grab (Khuyến nghị cho Nam An Market):** Tài xế Grab phụ trách đến siêu thị lấy hàng và giao tận tay khách.
2. **Delivery by restaurant:** Cửa hàng tự điều phối shipper riêng.
3. **Self-collection:** Khách hàng tự đến cửa hàng nhận túi hàng.

👉 **Nam An Market sử dụng chế độ:** **`Delivery by Grab`**.

---

### 05. Ready for Production (Triển Khai Môi Trường Thật)

1. Hoàn thành tất cả bài test trên trang Testing Tools Sandbox của Grab.
2. Bấm **Create Production Project** để lấy thông tin xác thực chính thức (`Production Client ID & Secret`).
3. Trỏ các Webhook URL về tên miền chính thức của Nam An:
   - OAuth Token: `https://ump.namanmarket.com/grabmart/oauth/token`
   - Get Menu: `https://ump.namanmarket.com/grabmart/menu`
   - Submit Order: `https://ump.namanmarket.com/grabmart/webhooks/order`
   - Order State: `https://ump.namanmarket.com/grabmart/webhooks/order/state`
4. Khởi động bán hàng chính thức trên GrabMart.

---

## 3. Bảng Tổng Hợp Endpoint Cấu Hình Cho Kỹ Thuật Viên Grab

| Mục Đích | Endpoint Của Nam An Market Cần Khai Báo Lên Grab | Method |
|---|---|---|
| **Lấy Token Xác Thực Webhook** | `https://ump.namanmarket.com/grabmart/oauth/token` | `POST` |
| **Kéo Dữ Liệu Thực Đơn / Menu** | `https://ump.namanmarket.com/grabmart/menu` | `GET` |
| **Bắn Đơn Hàng Mới Sang Nam An** | `https://ump.namanmarket.com/grabmart/webhooks/order` | `POST` |
| **Cập Nhật Trạng Thái Giao Đơn** | `https://ump.namanmarket.com/grabmart/webhooks/order/state` | `PUT` |

---

## 4. Danh Sách File Nguồn Đã Triển Khai Trong Hệ Thống

- **Module Router:** [`app/modules/grabmart/router.py`](file:///c:/python/naman_merchant_portal/app/modules/grabmart/router.py)
- **Module Adapter:** [`app/modules/grabmart/adapter.py`](file:///c:/python/naman_merchant_portal/app/modules/grabmart/adapter.py)
- **Module Service & Client:** [`app/modules/grabmart/service.py`](file:///c:/python/naman_merchant_portal/app/modules/grabmart/service.py)
- **Data Schemas:** [`app/modules/grabmart/schemas.py`](file:///c:/python/naman_merchant_portal/app/modules/grabmart/schemas.py)
- **Automated Tests:** [`tests/test_grabmart.py`](file:///c:/python/naman_merchant_portal/tests/test_grabmart.py)
