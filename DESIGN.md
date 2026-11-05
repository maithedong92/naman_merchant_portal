# Nam An Unified Merchant Platform (NAM-UMP) - Design System & UI Specification
> **Theme:** Apple Retail & Merchant Pro (Shopify Polaris / Human Interface / High-Contrast Modern Retail)
> **Approved Direction:** Phương án 3 (Apple Retail & Merchant Pro)

---

## 1. Triết lý Thiết kế (Design Philosophy)

Hệ thống giao diện Nam An Unified Merchant Platform được xây dựng dựa trên phong cách **Apple Retail & Merchant Pro** kết hợp tinh hoa từ **Shopify Polaris**:
1. **Trực quan, Sang trọng & Thân thiện (Clean, Sophisticated & Human-Centric):**
   - Loại bỏ phong cách Notion xám nhờ, mờ nhạt và lạm dụng emoji.
   - Sử dụng nền xám ngọc trai dịu mát (`#F8FAFC` / `#F1F5F9`), bề mặt thẻ trắng tinh khiết (`#FFFFFF`) kết hợp bo góc mềm mại (`rounded-2xl` 16px, `rounded-xl` 12px) và hiệu ứng đổ bóng đa tầng đặc trưng của Apple (`apple-shadow`).
2. **Độ tương phản cao & Dễ đọc (High Contrast & Glanceability - WCAG AAA):**
   - Chữ chính sử dụng Slate-900 (`#0F172A`) trên nền trắng, đạt tỷ lệ tương phản `14.2:1` (vượt xa chuẩn WCAG AAA `7:1`).
   - Phụ đề sử dụng Slate-600 (`#475569`) và Slate-500 (`#64748B`), tuyệt đối không dùng màu xám bợt `#787774` gây nhức mắt.
   - Giúp nhân viên điều phối và nhân viên kho dễ dàng nhận diện thông tin đơn hàng trong chớp mắt (0.5 giây) hoặc từ khoảng cách 1-2 mét.
3. **Màu sắc Thương hiệu Hữu cơ Nam An Market (Emerald Accents):**
   - Màu nhận diện chủ đạo là xanh lục bảo hữu cơ (Emerald-600 `#059669`, Emerald-700 `#047857`, Emerald-950 `#064E3B`), phản ánh định vị siêu thị thực phẩm tươi sống cao cấp của Nam An.
4. **Chuẩn mực Biểu tượng & Trình bày Số liệu (Iconography & Tabular Data):**
   - 100% biểu tượng là Crisp SVG Icons với nét vẽ đồng nhất (`stroke-width: 1.8px - 2px`). Không dùng emoji làm biểu tượng hệ thống.
   - Toàn bộ giá tiền (VND), mã đơn hàng (`#HD10928`, `#SPF-882194`), mã SKU và số lượng (`1x`, `2x`) đều dùng font Monospace (`JetBrains Mono / SF Mono`) với thuộc tính `tabular-nums` để các chữ số luôn thẳng hàng hoàn hảo.

---

## 2. Bảng Màu Hệ Thống (Color Tokens)

### 2.1. Màu Nền & Mặt Bề Mặt (Surfaces & Canvas)
| Tên Token | Mã Hex / Class Tailwind | Mục Đích Sử Dụng |
| :--- | :--- | :--- |
| `surface-canvas` | `#F8FAFC` (`slate-50`) / `#F1F5F9` | Nền tổng thể toàn trang, giảm lóa mắt |
| `surface-card` | `#FFFFFF` (`white`) | Thẻ đơn hàng, bảng dữ liệu, modal popup |
| `surface-muted` | `#F1F5F9` (`slate-100`) | Nền ô input, header bảng dữ liệu, thanh tìm kiếm |
| `surface-hover` | `#F8FAFC` (`slate-50/80`) | Trạng thái hover dòng dữ liệu |
| `border-subtle` | `#E2E8F0` (`slate-200`) | Viền thẻ, viền bảng, viền phân cách |
| `border-medium` | `#CBD5E1` (`slate-300`) | Viền ô input, viền nút outline |

### 2.2. Màu Chữ & Phân Cấp (Typography Contrast)
| Cấp độ | Mã Hex / Class Tailwind | Tỷ lệ Tương Phản (WCAG) | Công Dụng |
| :--- | :--- | :--- | :--- |
| **Headline / Primary** | `#0F172A` (`slate-900`) | `14.2:1` (AAA) | Tên khách hàng, mã đơn, tiêu đề modal, giá tiền |
| **Secondary / Body** | `#334155` (`slate-700`) | `10.5:1` (AAA) | Tên món, địa chỉ giao hàng, nội dung thông báo |
| **Muted / Caption** | `#64748B` (`slate-500`) | `4.8:1` (AA) | Thời gian đặt hàng, ghi chú phụ, nhãn cột |
| **Subtle** | `#94A3B8` (`slate-400`) | `3.2:1` | Placeholder, icon trang trí |

### 2.3. Bảng Màu Kênh Bán Hàng (Channel Badges)
Mỗi kênh bán hàng có badge riêng biệt, gồm chấm tròn trạng thái hoặc icon nhận diện:
- **ShopeeFood:** Nền `#FFF7ED` (`orange-50`), Viền `#FDBA74` (`orange-300`), Chữ `#C2410C` (`orange-700`), Chấm `#F97316` (`orange-500`).
- **GrabMart:** Nền `#ECFDF5` (`emerald-50`), Viền `#A7F3D0` (`emerald-300`), Chữ `#047857` (`emerald-700`), Chấm `#10B981` (`emerald-500`).
- **ShopeeMart:** Nền `#FDF2F8` (`pink-50`), Viền `#FBCFE8` (`pink-300`), Chữ `#BE185D` (`pink-700`), Chấm `#EC4899` (`pink-500`).
- **Haravan Omnichannel:** Nền `#EFF6FF` (`blue-50`), Viền `#BFDBFE` (`blue-300`), Chữ `#1D4ED8` (`blue-700`), Chấm `#3B82F6` (`blue-500`).

### 2.4. Trạng Thái Vận Hành & Pipeline (Operational Stages)
| Giai Đoạn | Mã Token | Màu Nền / Viền / Chữ | Ý Nghĩa Vận Hành |
| :--- | :--- | :--- | :--- |
| **1. Cần Xử Lý Ngay** | `URGENT` | `amber-50` / `amber-200` / `amber-900` | Đơn mới chưa duyệt hoặc Haravan chưa phân tuyến (Chấm vàng nhấp nháy `animate-pulse`) |
| **2. Đang Chuẩn Bị** | `PREPARING` | `emerald-50` / `emerald-200` / `emerald-900` | Siêu thị đã nhận đơn, nhân viên đang nhặt hàng tại kệ |
| **3. Sẵn Sàng Giao** | `READY` | `blue-50` / `blue-200` / `blue-900` | Đã đóng gói xong, chờ shipper lấy hàng |
| **4. Đang Vận Chuyển** | `DELIVERING`| `purple-50` / `purple-200` / `purple-900`| Shipper đang giao tới khách hàng |
| **5. Hoàn Tất** | `COMPLETED` | `slate-100` / `slate-200` / `slate-800` | Khách đã nhận hàng thành công |
| **6. Đã Hủy** | `CANCELLED` | `rose-50` / `rose-200` / `rose-900` | Hết hàng, hủy do khách hoặc sự cố |

---

## 3. Kiểu Chữ & Hiệu Ứng (Typography & Shadows)

### 3.1. Font Stack
* **Chữ giao diện:** `'Plus Jakarta Sans', 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`
* **Mã số & Tiền tệ:** `'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace`

### 3.2. Hiệu ứng Chiều Sâu (Apple Elevation)
```css
.apple-shadow {
    box-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.05), 0 2px 6px -1px rgba(0, 0, 0, 0.02);
}
.apple-card {
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}
.apple-card:hover {
    box-shadow: 0 10px 25px -3px rgba(0, 0, 0, 0.08), 0 4px 10px -2px rgba(0, 0, 0, 0.03);
    transform: translateY(-1px);
}
```

---

## 4. Quy Chuẩn Thành Phần Giao Diện (Components)

1. **Top Navigation Bar (`base.html`):**
   - Header cố định kính mờ (`bg-white/90 backdrop-blur-md border-b border-slate-200/80`).
   - Logo Nam An Market bo góc `rounded-2xl` với gradient `from-emerald-800 to-emerald-600`.
   - Tab điều hướng active dùng `bg-slate-900 text-white shadow-sm rounded-xl`.
   - Thông tin tài nguyên và chi nhánh hiển thị với badge kính mờ `bg-slate-100 text-slate-800`.
2. **Pipeline Ribbon Thống Kê (`orders.html`):**
   - 5 thẻ giai đoạn lớn với số đếm to rõ `text-3xl font-black font-mono`.
   - Thẻ "Cần Duyệt Ngay" nổi bật với gradient vàng hổ phách và chấm đèn led cảnh báo.
3. **Bảng Danh Sách Đơn Hàng:**
   - Hàng xen kẽ tinh tế, hover phản hồi tức thì.
   - Cột số lượng món `1x`, `2x` đóng khung badge nhỏ gọn `bg-slate-100 font-mono font-bold`.
   - Cột giá tiền to rõ `text-sm font-black font-mono text-slate-900`.
   - Nút hành động chính (`Phân Tuyến`, `Báo Xong`) có hiệu ứng `hover:scale-105 active:scale-95`.
4. **Loại Bỏ Tính Năng Không Phù Hợp:**
   - Ứng dụng chỉ phục vụ điều phối và quản trị đơn hàng, không phải POS bán hàng.
   - Bãi bỏ hoàn toàn các nút in bill nhiệt 80mm. Chỉ giữ in phiếu vận chuyển (AWB A6) và phiếu giao hàng A4/A5.
