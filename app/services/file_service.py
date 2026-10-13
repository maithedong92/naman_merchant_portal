# -*- coding: utf-8 -*-
"""
Dịch vụ Quản lý Tệp Đính Kèm (File Service) cho Nam An Merchant Portal.
Kế thừa và đồng bộ 100% logic xử lý từ ứng dụng desktop pmdonhang (v2.0.9):
  - Tải lên tệp đơn hàng (order_file: PDF hóa đơn, đơn gốc scan)
  - Tải lên tệp vận đơn (tracking_file: Phiếu gửi hàng, vận đơn các hãng giao hàng)
  - Giao tiếp với API lưu trữ: https://namanapi.namanmarket.com
  - Chuẩn hóa URL download (/orders/, /images/, /files/)
  - Proxy download tệp an toàn tránh lỗi CORS/chứng chỉ SSL
"""

import logging
import mimetypes
import os
import re
from typing import Any, Dict, Optional, Tuple
import httpx

from app.core.config import get_settings

logger = logging.getLogger("naman_portal.file_service")


class FileService:
    """Service xử lý upload, download và chuẩn hóa tệp đính kèm đơn hàng."""

    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
    MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB

    def __init__(self):
        self.settings = get_settings()
        self.base_url = "https://namanapi.namanmarket.com"
        self.upload_order_url = f"{self.base_url}/upload_order_file/"
        self.download_order_url = f"{self.base_url}/orders/"
        self.download_image_url = f"{self.base_url}/images/"
        self.download_file_url = f"{self.base_url}/files/"

    def clean_file_url(self, file_url: Optional[str]) -> str:
        """
        Chuẩn hóa URL download từ dữ liệu lưu trong database hoặc path server trả về.
        Tuân thủ 100% logic clean_file_url & get_download_url từ pmdonhang:
          - Nếu là URL tuyệt đối có schema http/https: giữ nguyên
          - Nếu chứa '/orders/': định dạng https://namanapi.namanmarket.com/orders/{filename}?image_path={filename}
          - Nếu chứa '/uploadIMG/': định dạng https://namanapi.namanmarket.com/images/{filename}
          - Các trường hợp khác: fallback về https://namanapi.namanmarket.com/files/{filename}
        """
        if not file_url:
            return ""

        url_str = str(file_url).strip()
        if not url_str:
            return ""

        # Nếu đã là URL hợp lệ đầy đủ bắt đầu bằng http:// hoặc https://
        if url_str.startswith(("http://", "https://")):
            return url_str

        # Chuẩn hóa dấu phân cách thư mục
        path = url_str.replace("\\", "/")
        path = re.sub(r"^[A-Za-z]:/", "", path)  # Bỏ ổ đĩa Windows C:/ nếu có
        filename = os.path.basename(path)

        if "/orders/" in path:
            return f"{self.download_order_url}{filename}?image_path={filename}"
        elif "/uploadIMG/" in path or "/images/" in path:
            return f"{self.download_image_url}{filename}"
        elif path.startswith("/"):
            return f"{self.base_url}{path}"
        else:
            return f"{self.download_file_url}{filename}"

    def validate_file(self, filename: str, file_size: int) -> Tuple[bool, str]:
        """Kiểm tra tính hợp lệ của định dạng và dung lượng file."""
        if not filename:
            return False, "Tên tệp không được để trống."

        _, ext = os.path.splitext(filename)
        ext = ext.lower()

        if ext not in self.ALLOWED_EXTENSIONS:
            allowed_str = ", ".join(sorted(self.ALLOWED_EXTENSIONS))
            return False, f"Định dạng '{ext}' không được hỗ trợ. Chỉ chấp nhận: {allowed_str}"

        if file_size <= 0:
            return False, "Tệp tin rỗng (0 bytes)."

        if file_size > self.MAX_FILE_SIZE:
            max_mb = self.MAX_FILE_SIZE / (1024 * 1024)
            return False, f"Dung lượng tệp vượt quá giới hạn cho phép ({max_mb:.0f}MB)."

        return True, ""

    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        file_type: str = "order",
    ) -> Tuple[bool, str, str]:
        """
        Upload tệp tin lên server lưu trữ https://namanapi.namanmarket.com
        Args:
            file_bytes: Nội dung file dạng nhị phân
            filename: Tên file gốc
            file_type: Loại tệp ('order' hoặc 'tracking')
        Returns:
            (success: bool, file_url: str, error_message: str)
        """
        valid, err = self.validate_file(filename, len(file_bytes))
        if not valid:
            return False, "", err

        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream"

        files = {
            "file": (filename, file_bytes, mime_type),
        }
        data = {
            "file_type": file_type,
        }

        try:
            async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
                logger.info(f"Đang tải lên tệp '{filename}' ({len(file_bytes)} bytes) loại '{file_type}'...")
                response = await client.post(
                    self.upload_order_url,
                    files=files,
                    data=data,
                )

                if response.status_code == 200:
                    resp_json = response.json()
                    server_url = resp_json.get("order_url") or resp_json.get("image_url") or resp_json.get("file_url")
                    if server_url:
                        cleaned_url = self.clean_file_url(server_url)
                        logger.info(f"✅ Tải lên tệp '{filename}' thành công: {cleaned_url}")
                        return True, cleaned_url, ""
                    else:
                        logger.error(f"Response thiếu trường URL: {resp_json}")
                        return False, "", f"Máy chủ không trả về đường dẫn tệp: {resp_json}"
                else:
                    err_msg = f"Máy chủ lưu trữ báo lỗi (HTTP {response.status_code}): {response.text[:200]}"
                    logger.error(err_msg)
                    return False, "", err_msg

        except httpx.RequestError as exc:
            err_msg = f"Lỗi kết nối tới máy chủ lưu trữ file: {exc}"
            logger.error(err_msg)
            return False, "", err_msg
        except Exception as exc:
            err_msg = f"Lỗi không xác định khi upload file: {exc}"
            logger.exception(err_msg)
            return False, "", err_msg

    async def download_file_bytes(self, file_url: str) -> Tuple[bool, bytes, str, str]:
        """
        Tải nội dung nhị phân của file để phục vụ proxy download.
        Returns:
            (success: bool, content: bytes, content_type: str, filename: str)
        """
        target_url = self.clean_file_url(file_url)
        if not target_url:
            return False, b"", "", "URL không hợp lệ"

        try:
            async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
                res = await client.get(target_url)
                if res.status_code == 200:
                    content_type = res.headers.get("content-type", "application/octet-stream")
                    filename = os.path.basename(target_url.split("?")[0]) or "downloaded_file"
                    return True, res.content, content_type, filename
                else:
                    return False, b"", "", f"HTTP {res.status_code}"
        except Exception as exc:
            logger.error(f"Lỗi khi tải file từ URL '{target_url}': {exc}")
            return False, b"", "", str(exc)


# Singleton instance
file_service = FileService()
