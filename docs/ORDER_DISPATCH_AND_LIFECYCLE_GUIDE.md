# Hướng Dẫn Quy Trình Phân Tuyến & Vòng Đời Đơn Hàng (Order Dispatch & Lifecycle Guide)

> **Tài liệu quy định nghiệp vụ cốt lõi:** Phân biệt rõ ràng giữa **Đơn hàng Haravan Omnichannel (Cần phân tuyến)** và **Đơn hàng từ Platform (Tự động phân bổ, không cần phân tuyến)**.

---

## 1. Nguyên Tắc Cốt Lõi Về Phân Tuyến Đơn Hàng

| Tiêu chí | Đơn từ Platform (ShopeeFood, GrabMart, ShopeeMart) | Đơn từ Haravan Omnichannel (Website, Hotline, Shopee/Lazada via Haravan) |
| :--- | :--- | :--- |
| **Bản chất kênh** | Kênh giao hàng hỏa tốc / theo app địa lý của sàn thương mại điện tử. | Kênh bán hàng tổng thể đa kênh của thương hiệu Nam An Market. |
| **Xác định chi nhánh** | **ĐÃ XÁC ĐỊNH SẴN TỪ PLATFORM.**<br>Khách hàng khi đặt app đã chọn đích danh cửa hàng (hoặc app sàn định vị GPS tới cửa hàng gần nhất). Webhook gửi về kèm mã định danh cửa hàng (`partner_store_id`, `shop_id`). | **CHƯA XÁC ĐỊNH CHI NHÁNH.**<br>Khách đặt trên website chung `namanmarket.com` hoặc qua hotline. Đơn đổ về Haravan Hub trung tâm mà chưa biết siêu thị nào sẽ xử lý và giao hàng. |
| **Yêu cầu Phân tuyến** | ❌ **KHÔNG CẦN PHÂN TUYẾN THỦ CÔNG.**<br>Hệ thống UMP tự động map qua `StoreChannelMapping` gán thẳng vào `Store.id` / `Store.code`. Đơn hàng chuyển thẳng sang trạng thái sẵn sàng chuẩn bị hàng. | ⚡ **BẮT BUỘC PHẢI PHÂN TUYẾN (DISPATCH REQUIRED).**<br>Điều phối viên tổng đài phải mở đơn, tra cứu tồn kho Arito ERP 4 chi nhánh và bấm nút **"Phân Tuyến Cửa Hàng"**. |
| **Nhân viên chi nhánh thấy đơn khi nào?** | Thấy **ngay lập tức** khi sàn bắn Webhook về. | **Chỉ thấy sau khi Điều phối viên đã phân tuyến** về đúng mã chi nhánh của mình (`assigned_store`). |

---

## 2. Quy Trình Chi Tiết Đối Với Đơn Haravan Omnichannel

```mermaid
flowchart TD
    subgraph "1. Tiếp Nhận Đơn Hàng"
        HV["Khách đặt qua Website namanmarket.com / Hotline"] --> Sync["HaravanSyncWorker kéo về MongoDB"]
        Sync --> Queue["Đơn Hàng Mới (assigned_store = null)"]
    end

    subgraph "2. Điều Phối & Phân Tuyến (Tổng Đài / Dispatcher)"
        Queue --> Review["Điều phối viên mở chi tiết đơn hàng"]
        Review --> CheckArito["Tra cứu tồn kho thực tế Arito ERP (4 chi nhánh)"]
        CheckArito --> ChooseStore["Chọn chi nhánh tối ưu (Gần nhất + Còn đủ hàng)"]
        ChooseStore --> AssignAction["Bấm: 'Phân Tuyến Cửa Hàng'\n- Chọn Store: 10001, 10004, 10005, 10006\n- Chọn Lý do: Gần cửa hàng, Có sẵn hàng...\n- Chụp Snapshot tồn kho Arito (assignment_stock)\n- Gán assigned_store"]
    end

    subgraph "3. Siêu Thị Xử Lý Đơn Hàng (Store Staff)"
        AssignAction --> StoreView["Đơn xuất hiện trong màn hình của Chi Nhánh được gán"]
        
        StoreView --> CheckItem{"Kiểm tra thực tế tại kệ"}
        
        CheckItem -->|Đủ hàng| Pack["Chuyển: 'Đã Đóng Gói'"]
        CheckItem -->|Thiếu hàng / Quá tải| Reassign["Chuyển: 'Yêu Cầu Phân Tuyến Lại'\n- Nhập lý do thiếu hàng\n- GỠ BỎ assigned_store = null\n- Đơn biến mất khỏi chi nhánh cũ, quay lại Bước 2"]
        Reassign --> Queue
        
        Pack --> Ship["Chuyển: 'Đã Gửi Vận Đơn'\n- Nhập mã vận đơn (tracking_code)\n- Đính kèm file hóa đơn/vận đơn"]
        Ship --> Complete["Chuyển: 'Hoàn Thành'\n- Chốt tổng tiền thực tế (actual_total)\n- Chốt phí ship thực tế (actual_shipping)\n- Chốt đơn vị ship (Ahamove, GHN, Grab...)"]
    end
```

---

## 3. Quy Trình Đối Với Đơn Platform (ShopeeFood, GrabMart, ShopeeMart)

1. **Tiếp nhận tự động qua Webhook:**
   - ShopeeFood gửi đơn $\rightarrow$ UMP tự động map `store_code = 10001` (Thảo Điền).
   - GrabMart gửi đơn $\rightarrow$ UMP tự động map `store_code = 10004` (Phú Mỹ Hưng).
   - ShopeeMart gửi đơn $\rightarrow$ Outlet Shop ID tương ứng.
2. **Không qua bước phân tuyến:**
   - Đơn hàng đi thẳng vào quy trình xử lý của siêu thị: `ACCEPTED` / `READY_TO_SHIP` / `PICKED_UP` / `DELIVERED`.
   - Nhân viên chi nhánh tiếp nhận và bấm xác nhận / đóng gói ngay cho tài xế của sàn đến lấy.

---

## 4. Cấu Trúc Dữ Liệu `internal` Cho Đơn Phân Tuyến

Đối với các đơn hàng Haravan, toàn bộ thông tin phân tuyến và lịch sử xử lý được lưu trữ trong object `internal` bên trong MongoDB:

```json
{
  "order_number": "#NA229001",
  "source": "checkout_next",
  "total_price": 385000,
  "internal": {
    "order_processing_status": "Đã phân tuyến",
    "assigned_store": "10001",
    "assignment_reason": "Gần cửa hàng",
    "assignment_note": "Giao trước 11h trưa",
    "assigned_by": "admin",
    "assigned_at": "2026-10-12T04:45:00Z",
    "assignment_stock": {
      "001020": { "10001": 5.0, "10004": 2.0, "10005": 0.0, "10006": 1.0 }
    },
    "history": [
      {
        "timestamp": "2026-10-12T04:40:00Z",
        "action": "Đồng bộ đơn hàng từ Haravan",
        "user": "system_haravan_sync",
        "details": "Đơn hàng mới tiếp nhận từ Website"
      },
      {
        "timestamp": "2026-10-12T04:45:00Z",
        "action": "Phân tuyến đơn hàng",
        "user": "admin",
        "details": "Phân tuyến cho 10001 - Lý do: Gần cửa hàng"
      }
    ]
  }
}
```

---

## 5. Nguyên Tắc Bảo Mật & Phân Quyền (Store Isolation)

1. **Vai trò SuperAdmin / Dispatcher (Tổng đài điều phối):**
   - Xem toàn bộ danh sách đơn hàng của tất cả kênh (Haravan, ShopeeFood, GrabMart, ShopeeMart).
   - Có quyền phân tuyến hoặc phân tuyến lại bất kỳ đơn hàng nào chưa phân hoặc cần chuyển chi nhánh.
2. **Vai trò Store Manager / Staff (Cửa hàng trưởng / Nhân viên chi nhánh `10001`):**
   - **Đối với đơn Platform:** Chỉ thấy đơn của `10001` do sàn gửi về.
   - **Đối với đơn Haravan:** **CHỈ THẤY** các đơn có `internal.assigned_store == "10001"`. Tuyệt đối không thấy đơn chưa phân tuyến hoặc đơn của chi nhánh khác (`10004`, `10005`, `10006`).
   - Khi bấm **"Yêu cầu phân tuyến lại"**, hệ thống gỡ bỏ `assigned_store = null`, đơn hàng ngay lập tức biến mất khỏi màn hình của chi nhánh để đảm bảo nhân viên không bị nhầm lẫn.
