# Hướng Dẫn Tích Hợp & Vận Hành Deploy Tự Động Qua OpenSSH
> **Dự án:** Nam An Unified Merchant Portal (NAM-UMP)  
> **Mục tiêu:** Tài liệu ngắn gọn giúp các Developer khác tích hợp và sử dụng cơ chế deploy 1-chạm (One-Click Remote Deployment) lên Production VPS qua OpenSSH.

---

## 1. Tổng Quan Kiến Trúc (Architecture Overview)

Cơ chế deploy tự động giúp Developer có thể đưa code mới từ máy cá nhân lên Production VPS an toàn, nhất quán trong vòng **30–45 giây** mà không cần Remote Desktop (RDP) thủ công.

```mermaid
flowchart LR
    Dev[💻 Developer Local] -- "1. Git Push" --> GitHub[🐙 GitHub Repo (main)]
    Dev -- "2. OpenSSH Remote Exec" --> VPS[🖥️ Windows VPS (103.175.248.250)]
    subgraph VPS Execution
        VPS --> GitPull["git pull origin main"]
        GitPull --> DockerBuild["docker build & compose up -d"]
        DockerBuild --> HealthCheck["Healthcheck :3002 (healthy)"]
    end
    Dev -- "3. Verify Public Health" --> Domain["🌐 https://ump.namanmarket.com/api/v1/health"]
```

---

## 2. Thông Tin Kết Nối (Connection Specs)

* **Host IP:** `103.175.248.250`
* **SSH Port:** `22` (Dịch vụ Windows OpenSSH Server)
* **SSH User:** `Administrator`
* **Authentication:** SSH Key Ed25519 (`id_ed25519_naman_ump`)
* **Thư mục làm việc trên VPS:** `C:\uploadIMG\docker\ump`
* **Domain Production:** `https://ump.namanmarket.com`

---

## 3. Các Bước Cài Đặt Cho Developer Mới (Dev Setup)

### Bước 3.1: Nhận và lưu SSH Private Key
1. Xin file Private Key `id_ed25519_naman_ump` từ Tech Lead / Quản trị viên.
2. Lưu file vào thư mục SSH của người dùng:
   * **Windows:** `%USERPROFILE%\.ssh\id_ed25519_naman_ump` *(ví dụ: `C:\Users\<Username>\.ssh\id_ed25519_naman_ump`)*
   * **macOS / Linux:** `~/.ssh/id_ed25519_naman_ump`

3. Phân quyền bảo mật file key (bắt buộc):
   * **macOS / Linux / Git Bash:**
     ```bash
     chmod 600 ~/.ssh/id_ed25519_naman_ump
     ```
   * **Windows PowerShell:**
     ```powershell
     icacls "$env:USERPROFILE\.ssh\id_ed25519_naman_ump" /inheritance:r /grant:r "$($env:USERNAME):(R)"
     ```

### Bước 3.2: (Tùy chọn) Cấu hình SSH Config
Thêm đoạn sau vào file `%USERPROFILE%\.ssh\config` (hoặc `~/.ssh/config`) để gọi tắt:

```sshconfig
Host naman-vps
    HostName 103.175.248.250
    User Administrator
    Port 22
    IdentityFile ~/.ssh/id_ed25519_naman_ump
    StrictHostKeyChecking no
```

Kiểm tra kết nối thử:
```bash
ssh naman-vps "echo Connected to NAM-AN VPS successfully"
```

---

## 4. Cách Sử Dụng Deploy (How to Deploy)

### Cách 1: Sử dụng Script 1-chạm có sẵn (Khuyến nghị cho Windows)
Trong thư mục gốc của repository, chạy file:
```cmd
deploy_to_vps.bat
```
Script sẽ tự động thực hiện trọn gói 3 bước:
1. Đẩy commit mới nhất từ máy local lên GitHub (`git push origin main`).
2. SSH vào VPS và kích hoạt quy trình `redeploy.bat` (kéo code, rebuild Docker image, khởi chạy container).
3. Kiểm tra Health Check qua URL `https://ump.namanmarket.com/api/v1/health`.

---

### Cách 2: Chạy Lệnh Trực Tiếp Bằng Terminal (PowerShell / macOS / Linux)

Nếu làm việc trên VS Code Terminal, macOS hoặc Linux:

```bash
# 1. Push code lên GitHub
git push origin main

# 2. Gửi lệnh redeploy qua SSH
ssh -i ~/.ssh/id_ed25519_naman_ump -p 22 -o StrictHostKeyChecking=no Administrator@103.175.248.250 "cd /d C:\uploadIMG\docker\ump && cmd.exe /c redeploy.bat"

# 3. Kiểm tra trạng thái hệ thống sau deploy
curl -s https://ump.namanmarket.com/api/v1/health
```

---

### Cách 3: Clean Rebuild (Khi nâng cấp thư viện nặng / xóa cache Docker)
Nếu có cập nhật `requirements.txt` hoặc muốn build lại hoàn toàn không dùng cache:
```bash
ssh naman-vps "cd /d C:\uploadIMG\docker\ump && cmd.exe /c redeploy.bat --clean"
```

---

## 5. Quy Trình Server-Side Thực Hiện (`redeploy.bat` trên VPS)

Khi lệnh SSH được kích hoạt, kịch bản `redeploy.bat` trên server sẽ tự động:
1. Nạp biến môi trường cho phiên headless SSH (`PATH` chứa Git, Docker, `DOCKER_BUILDKIT=0`).
2. `git pull origin main`: Đồng bộ mã nguồn mới nhất từ GitHub.
3. `docker compose down --remove-orphans`: Thu hồi an toàn các container cũ.
4. `docker build -f docker/Dockerfile -t ump-app .`: Build Docker image FastAPI với Python 3.12.
5. `docker compose up -d`: Khởi động lại service `naman_portal_app` (port 3002) và `naman_portal_nginx` (reverse proxy port 3001).
6. Chờ Docker Container đạt trạng thái `healthy` trước khi kết thúc phiên SSH.

---

## 6. Xử Lý Sự Cố Nhanh (Troubleshooting)

| Vấn đề | Nguyên nhân | Cách khắc phục |
| :--- | :--- | :--- |
| **`Permission denied (publickey)`** | Chưa đặt SSH key đúng thư mục hoặc quyền file chưa đúng `600`. | Kiểm tra đường dẫn `%USERPROFILE%\.ssh\id_ed25519_naman_ump` và cấu hình phân quyền ở mục 3.1. |
| **`Git pull conflict` trên VPS** | Có ai đó chỉnh sửa file trực tiếp trên thư mục server. | SSH vào VPS: `ssh naman-vps`, chuyển vào thư mục `cd /d C:\uploadIMG\docker\ump` và chạy `git reset --hard origin/main`. |
| **Container không chuyển sang `healthy`** | Thiếu biến môi trường trong file `.env` trên VPS hoặc cú pháp Python lỗi. | Kiểm tra log thời gian thực: `ssh naman-vps "cd /d C:\uploadIMG\docker\ump && docker compose logs --tail=50 app"`. |
| **Curl Healthcheck timeout** | Nginx hoặc port forwarding chưa nhận diện. | Kiểm tra trạng thái container: `ssh naman-vps "docker ps"`. |

---

## 7. Các URL Kiểm Tra Sau Khi Deploy

* **Portal Đơn Hàng:** [https://ump.namanmarket.com/admin/orders](https://ump.namanmarket.com/admin/orders)
* **Phân Quyền & Quản Lý User:** [https://ump.namanmarket.com/admin/users](https://ump.namanmarket.com/admin/users)
* **Tổng Quan Kho & Thực Đơn:** [https://ump.namanmarket.com/admin/inventory](https://ump.namanmarket.com/admin/inventory)
* **Tài Liệu Swagger API:** [https://ump.namanmarket.com/docs](https://ump.namanmarket.com/docs)
* **Health Check Probe:** [https://ump.namanmarket.com/api/v1/health](https://ump.namanmarket.com/api/v1/health)
