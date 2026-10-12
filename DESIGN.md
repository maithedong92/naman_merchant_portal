# Nam An Unified Merchant Platform (NAM-UMP) - Design System & UI Specification
> **Theme:** Notion Design Language (Minimalist, Typographic, Monospaced Accents, Hairline Borders, Pastel Property Badges)

---

## 1. Triết lý Thiết kế (Design Philosophy)

Hệ thống giao diện được tái cấu trúc (refactor) toàn diện theo ngôn ngữ thiết kế của **Notion** – công cụ quản lý tri thức và cơ sở dữ liệu hàng đầu thế giới:
1. **Tối Giản & Thanh Lịch (Minimalism & Elegance):** Loại bỏ các khối màu đặc, hiệu ứng đổ bóng cồng kềnh, viền bo tròn quá đà (`rounded-full` tròn trịa). Chuyển sang các đường nét thanh mảnh, góc bo chuẩn mực (`rounded-md` 6px, `rounded-lg` 8px).
2. **Màu Nền Trung Tính & Tương Phản Dịu (Soft Contrasts):** Nền trang màu trắng tinh khôi (`#FFFFFF`) kết hợp màu xám nhạt giấy cổ điển Notion (`#F7F6F3`, `#F1F1EF`). Chữ chính mang màu than chì mềm (`#37352F`), không dùng đen thuần `#000000` để tránh mỏi mắt trong ca làm việc dài.
3. **Phân Cấp Dữ Liệu Dạng Bảng Dữ Liệu (Notion Database Aesthetic):** Đơn hàng, tồn kho và danh sách người dùng được hiển thị như một Notion Database trực quan, với các chế độ xem (Views), bộ lọc linh hoạt (Filters), và nhãn trạng thái (Select Tags) chuẩn bảng màu pastel của Notion.
4. **Biểu tượng Cảm Xúc & Ký Hiệu Trực Quan (Icon & Emoji Accents):** Kết hợp hài hòa giữa các icon SVG hairline và emoji đại diện (ví dụ: `📦 Đơn hàng`, `🌐 Haravan`, `⚡ Cần Phân Tuyến`, `⏳ Chưa Hoàn Thành`, `📍 Chi nhánh`), mang lại cảm giác thân thiện, dễ nắm bắt quy trình.

---

## 2. Bảng Màu Chuẩn Notion (Color Palette)

### 2.1. Màu Nền & Khung (Surfaces & Canvas)
| Tên Token | Mã Màu Hex / RGBA | Công Dụng |
| :--- | :--- | :--- |
| `notion-canvas` | `#FFFFFF` | Nền trang chính, thẻ đơn hàng, modal dialog |
| `notion-bg-subtle` | `#F7F6F3` | Nền thanh bên, khối thống kê KPI, callout banner |
| `notion-bg-muted` | `#F1F1EF` | Nền ô tìm kiếm, bảng header, dòng hover phụ |
| `notion-hover` | `rgba(55, 53, 47, 0.08)` | Trạng thái hover chuột của button, menu item |
| `notion-active` | `rgba(55, 53, 47, 0.12)` | Trạng thái active/selected của thẻ hoặc tab |
| `notion-border` | `rgba(55, 53, 47, 0.09)` | Đường viền hairline siêu mảnh giữa các hàng/cột |
| `notion-border-strong` | `rgba(55, 53, 47, 0.16)` | Đường viền khung modal, ô nhập liệu đang focus |

### 2.2. Màu Chữ & Biểu Tượng (Typography Colors)
| Tên Token | Mã Màu Hex / RGBA | Công Dụng |
| :--- | :--- | :--- |
| `notion-text` | `#37352F` | Màu chữ tiêu đề và nội dung chính |
| `notion-text-muted` | `#787774` | Phụ đề, nhãn mô tả, thông tin thời gian |
| `notion-text-subtle` | `#9B9A97` | Placeholder, icon thứ cấp, số đếm không active |

### 2.3. Bảng Màu Nhãn Trạng Thái Pastel Notion (Select & Status Tags)
Các nhãn trạng thái đơn hàng và kênh bán hàng sử dụng đúng tỷ lệ màu nền pastel + chữ đậm nhẹ đặc trưng của Notion:

| Phân Loại | Màu Nền (Background) | Màu Chữ (Text) | Áp Dụng Cho Trạng Thái |
| :--- | :--- | :--- | :--- |
| **Default Gray** | `#F1F1EF` | `#787774` | Đơn hàng mới, Chưa xác định |
| **Brown** | `#F4EEEE` | `#937264` | Ghi chú điều phối, POS tại quầy |
| **Orange** | `#FAECE3` | `#D9730D` | ShopeeFood, Yêu cầu phân tuyến lại |
| **Yellow** | `#FBEDD6` | `#CB912F` | Chờ duyệt (PENDING), Cần phân tuyến |
| **Green** | `#EDF3EC` | `#448361` | Đã nhận (ACCEPTED), GrabMart, Đang soạn hàng |
| **Blue** | `#E7F3F8` | `#2F7EA6` | Đã phân tuyến, Sẵn sàng (READY), Haravan |
| **Purple** | `#F4EFF7` | `#9065B0` | Đã đóng gói, Đã gửi vận đơn, Đang giao |
| **Pink** | `#FBF1F5` | `#C14C8A` | ShopeeMart, Đổi trả hàng |
| **Red** | `#FDEBEC` | `#D44C47` | Đã hủy (CANCELLED), Cảnh báo sự cố |

---

## 3. Kiểu Chữ & Phân Cấp (Typography Hierarchy)

* **Font chữ hệ thống (System Font Stack):**
  `ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, "Apple Color Emoji", Arial, sans-serif, "Segoe UI Emoji", "Segoe UI Symbol"` (kèm Google Fonts `Inter` làm fallback cao cấp).
* **Font Monospace số & mã đơn:**
  `ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace` (cho mã SKU, mã vận đơn, số tiền VND).
* **Quy tắc phân cấp kích thước:**
  * **H1 (Tiêu đề trang):** `text-2xl font-bold tracking-tight text-[#37352F]` (ví dụ: `📦 Đơn hàng & Phân tuyến`).
  * **H2 (Phần/Khối chính):** `text-lg font-semibold text-[#37352F]`.
  * **H3 (Tiêu đề modal/card):** `text-sm font-semibold text-[#37352F]`.
  * **Body chính:** `text-[13px] leading-relaxed text-[#37352F]`.
  * **Small / Caption:** `text-[12px] text-[#787774]`.
  * **Micro / Tag:** `text-[11px] font-medium leading-none px-2 py-0.5 rounded`.

---

## 4. Các Thành Phần Giao Diện Chuẩn (UI Components)

### 4.1. Top Navigation & Breadcrumbs (Thanh Điều Hướng Workspace)
* Thiết kế thanh trắng tinh tế (`h-12` hoặc `h-14`, border dưới `1px solid rgba(55, 53, 47, 0.09)`).
* Breadcrumb bên trái:
  `🏛️ Nam An Market / 📦 Đơn Hàng`
* Menu Tabs ở giữa:
  Hiển thị dạng danh sách phẳng Notion. Tab đang active có gạch chân thanh mảnh hoặc nền `bg-[#EFEFED]` nhẹ nhàng.
* User Pill bên phải:
  Avatar viết tắt, tên nhân sự, nhãn vai trò (`SUPER_ADMIN` hoặc `📍 10001 - Thảo Điền`).

### 4.2. Notion Database View Tabs & Filter Pills
* **Hàng chọn kênh bán:**
  * `📑 Tất cả kênh`
  * `🛍️ ShopeeFood`
  * `🟢 GrabMart`
  * `🛒 ShopeeMart`
  * `🌐 Haravan Omnichannel`
* **Hàng nút tác vụ nhanh & bộ lọc:**
  * `⏳ Chưa Hoàn Thành [N]`: Nút lọc tức thì các đơn đang trong tiến trình xử lý.
  * `⚡ Cần Phân Tuyến [N]`: Nút lọc đơn Haravan trung tâm cần phân về siêu thị.
  * `Dropdown Chi Nhánh`: Lọc theo mã kho (`10001`, `10004`, `10005`, `10006`).
  * `Dropdown Trạng Thái`: Có tùy chọn `⏳ Chưa hoàn thành (Đang xử lý)` đứng đầu tiên.
  * `Search Input`: Thanh tìm kiếm tối giản có icon kính lúp, nền `#F7F6F3`, viền mờ.

### 4.3. KPI Counters (Notion Metric Blocks)
* Các thẻ KPI được thiết kế như ô số liệu Notion Database:
  * Không dùng hiệu ứng đổ bóng cồng kềnh.
  * Nền trắng viền hairline `border border-[rgba(55,53,47,0.09)]`.
  * Tiêu đề nhỏ xám `text-[11px] text-[#787774] font-medium`.
  * Số liệu lớn sắc nét `text-2xl font-bold font-mono text-[#37352F]`.
  * Khi hover: Chuyển nền sang `#F7F6F3` rất mượt.

### 4.4. Thẻ Đơn Hàng (Notion Order Card)
* Header thẻ: Mã đơn dạng monospace `#NA243900`, kênh bán hàng (Notion tag), ngày giờ relative (`5 phút trước`).
* Body:
  * Khách hàng: Tên và SĐT hiển thị gọn gàng.
  * Chi nhánh phụ trách: Tag màu xanh biển Notion `📍 10001 (21 Thảo Điền)`.
  * Danh sách món: Bảng mini tối giản, số lượng nổi bật `x2`, đơn giá rõ ràng.
* Footer thao tác:
  * Nút hành động chính (`Phân Tuyến`, `Đóng Gói`, `Gửi Vận Đơn`, `Hoàn Tất`): Nút bo góc nhẹ `rounded-md`, chữ đen than chì hoặc xanh Notion, hiệu ứng nhấn xúc giác.
  * Nút phụ (`In Phiếu Nhiệt`, `Hủy Đơn`): Nút ghost text với hover xám nhẹ.

### 4.5. Hộp Thoại Modal (Notion Dialog Page)
* Hộp thoại mở ra như một trang Notion nổi (Page Dialog):
  * Header có biểu tượng emoji (ví dụ: `🚚 Gửi Vận Đơn`, `🎯 Phân Tuyến Đơn Hàng`).
  * Bảng đối chiếu tồn kho Arito ERP 4 siêu thị: Thiết kế như bảng cơ sở dữ liệu Notion, cột được căn chỉnh chính xác, số tồn kho dưới định mức hiển thị nhãn đỏ pastel, siêu thị đủ hàng hiển thị nhãn xanh lá pastel.
  * Nút bấm xác nhận: Nút màu đen than chì Notion (`bg-[#2F3437] hover:bg-[#191919] text-white`) hoặc xanh Notion (`#2383E2`).

### 4.6. Notion Callouts (Khung Thông Báo Gợi Ý)
* Dùng cho khung cảnh báo phân quyền chi nhánh (Store Scoped) hoặc gợi ý hệ thống 1-chạm:
  * Nền `#F7F6F3` hoặc `#FBFAF9`.
  * Viền trái hoặc viền toàn phần `border border-[rgba(55,53,47,0.09)]`.
  * Icon emoji kích thước `18px` nằm bên trái.
  * Chữ nội dung `text-[12px] text-[#37352F]`.

---

## 5. Hướng Dẫn Refactor Cho Toàn Bộ Trang (Implementation Guide)

1. **`app/templates/base.html`:**
   * Cập nhật cấu hình Tailwind CSS mở rộng màu Notion (`notion-canvas`, `notion-bg-subtle`, `notion-text`, `notion-border`...).
   * Thay thế toàn bộ Top Navbar sang chuẩn Notion Workspace Navbar.
   * Xóa bỏ style gradient bóng bẩy, bo tròn elip quá mức.
2. **`app/templates/orders.html`:**
   * Tái cấu trúc thanh lọc kênh và trạng thái theo Notion Database View.
   * Cập nhật thẻ hiển thị đơn hàng dạng card/table clean Notion.
   * Tái cấu trúc 4 Modal điều phối (`#haravan-assign-modal`, `#haravan-reassign-modal`, `#haravan-ship-modal`, `#haravan-complete-modal`) theo phong cách Notion Page.
   * Giữ nguyên 100% logic API, WebSocket, Polling, In nhiệt POS 80mm, và các bộ lọc `Chưa hoàn thành` / `Cần phân tuyến`.
3. **`app/templates/users.html`, `admin_dashboard.html`, `inventory.html`, `reports.html`, `system_status.html`, `login.html`:**
   * Áp dụng thống nhất typography, bảng màu Notion, và các thành phần callout/card tương ứng.
