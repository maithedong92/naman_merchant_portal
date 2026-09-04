# ShopeeMart / Shopee Open Platform v2 Integration Guide
## Nam An Market - Unified Merchant Portal

> **Tài liệu hướng dẫn tích hợp kênh bán hàng ShopeeMart (Shopee Fresh / Siêu thị Shopee) cho chuỗi Nam An Market.**

---

## 1. Tổng Quan Kênh ShopeeMart
- **Nền tảng:** Shopee Open Platform API v2 (https://open.shopee.com/)
- **Tài liệu tham chiếu:** [`[Nam An Market x Shopee Mart] Working sheet - Google Sheets.pdf`](./[Nam%20An%20Market%20x%20Shopee%20Mart]%20Working%20sheet%20-%20Google%20Sheets.pdf)
- **Mục tiêu tích hợp:**
  1. Đồng bộ danh mục sản phẩm (Product Catalog) và tồn kho hàng hóa (Inventory Stock) từ Nam An lên gian hàng ShopeeMart.
  2. Tiếp nhận đơn đặt hàng trực tiếp từ Shopee thông qua Push Notification Webhook.
  3. Cập nhật trạng thái đơn hàng (Sẵn sàng giao - Ready to Ship, Hủy đơn - Cancel) ngược về hệ thống Shopee.

---

## 2. Cơ Chế Xác Thực & Chữ Ký Số (Authentication & Signing)

### 2.1. Thông số xác thực
- `partner_id`: Mã đối tác do Shopee Open Platform cấp.
- `partner_key`: Khóa bí mật đối tác dùng để sinh chữ ký HMAC-SHA256.
- `shop_id`: Mã định danh gian hàng của Nam An Market trên Shopee.
- `access_token`: Token OAuth2 truy cập API (có hạn sử dụng, được tự động refresh qua `refresh_token`).

### 2.2. Thuật toán sinh chữ ký (HMAC-SHA256)
```python
base_string = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
signature = hmac.new(
    partner_key.encode("utf-8"),
    base_string.encode("utf-8"),
    hashlib.sha256
).hexdigest()
```

---

## 3. Các API Trọng Tâm (Core API Endpoints)

| Nghiệp vụ | Shopee Open API v2 Endpoint | Phương thức | Mô tả |
| :--- | :--- | :--- | :--- |
| **Lấy danh sách sản phẩm** | `/api/v2/product/get_item_list` | GET | Truy vấn danh sách SKU trên shop |
| **Cập nhật tồn kho** | `/api/v2/product/update_stock` | POST | Đẩy tồn kho khả dụng theo lô SKU |
| **Cập nhật giá bán** | `/api/v2/product/update_price` | POST | Đồng bộ giá bán lẻ |
| **Lấy chi tiết đơn hàng** | `/api/v2/order/get_order_detail` | GET | Lấy thông tin khách và chi tiết món |
| **Xác nhận giao hàng** | `/api/v2/logistics/ship_order` | POST | Báo hàng đã đóng gói sẵn sàng chuyển giao |
| **Hủy đơn hàng** | `/api/v2/order/cancel_order` | POST | Hủy đơn khi hết tồn kho tại quầy |

---

## 4. Cấu Trúc Module ShopeeMart trong Hệ Thống
Mã nguồn module được tổ chức độc lập tại [`app/modules/shopeemart/`](file:///c:/python/naman_merchant_portal/app/modules/shopeemart/):
- [`adapter.py`](file:///c:/python/naman_merchant_portal/app/modules/shopeemart/adapter.py): Lớp `ShopeeMartChannelAdapter` kế thừa `BaseChannelAdapter`.
- [`service.py`](file:///c:/python/naman_merchant_portal/app/modules/shopeemart/service.py): Client HTTP bất đồng bộ gọi API Shopee với cơ chế ký số tự động.
- [`schemas.py`](file:///c:/python/naman_merchant_portal/app/modules/shopeemart/schemas.py): Pydantic DTOs cho các request/response Shopee.
- [`router.py`](file:///c:/python/naman_merchant_portal/app/modules/shopeemart/router.py): Endpoint tiếp nhận Webhook push đơn hàng `/api/v1/shopeemart/webhooks/order`.
