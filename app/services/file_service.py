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
import posixpath
import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple
import httpx

from app.core.config import get_settings

logger = logging.getLogger("naman_portal.file_service")


class FileService:
    """Service xử lý upload, download và chuẩn hóa tệp đính kèm đơn hàng."""

    ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
    ALLOWED_HOSTS = {"namanapi.namanmarket.com"}
    MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB

    def __init__(self):
        self.settings = get_settings()
        self.base_url = "https://namanapi.namanmarket.com"
        self.upload_order_url = f"{self.base_url}/upload_order_file/"
        self.download_order_url = f"{self.base_url}/orders/"
        self.download_image_url = f"{self.base_url}/images/"
        self.download_file_url = f"{self.base_url}/files/"
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Return a shared reusable httpx.AsyncClient instance."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(verify=True, timeout=60.0)
        return self._client

    async def close(self) -> None:
        """Close the shared httpx.AsyncClient on application shutdown."""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except RuntimeError:
                pass
        self._client = None

    def clean_file_url(self, file_url: Optional[str]) -> str:
        """
        Chuẩn hóa URL download từ dữ liệu lưu trong database hoặc path server trả về.
        Tuân thủ 100% logic clean_file_url & get_download_url từ pmdonhang:
          - Nếu là URL tuyệt đối có schema http/https: chỉ cho phép host namanapi.namanmarket.com (chống SSRF)
          - Nếu chứa '/orders/': định dạng https://namanapi.namanmarket.com/orders/{filename}?image_path={filename}
          - Nếu chứa '/uploadIMG/': định dạng https://namanapi.namanmarket.com/images/{filename}
          - Các trường hợp khác: fallback về https://namanapi.namanmarket.com/files/{filename}
        """
        if not file_url:
            return ""

        url_str = str(file_url).strip()
        if not url_str:
            return ""

        # Nếu là URL có scheme hoặc bắt đầu bằng // -> Ép HTTPS và chỉ cho phép namanapi.namanmarket.com
        if url_str.startswith(("http://", "https://", "//")) or "://" in url_str:
            try:
                parsed = urllib.parse.urlparse(url_str)
            except Exception:
                return ""
            if parsed.scheme not in ("http", "https"):
                logger.warning(f"Từ chối URL tệp có scheme không hợp lệ: {url_str}")
                return ""
            if parsed.username or parsed.password:
                logger.warning("Từ chối URL tệp chứa thông tin xác thực nhúng.")
                return ""
            hostname = (parsed.hostname or "").lower()
            if hostname not in self.ALLOWED_HOSTS:
                logger.warning(f"Chặn SSRF: URL tệp trỏ tới host không được phép '{hostname}'")
                return ""
            raw_path = parsed.path or "/"
            if ".." in raw_path.split("/"):
                logger.warning(f"Từ chối URL tệp chứa traversal: {raw_path}")
                return ""
            norm_path = posixpath.normpath(raw_path)
            if raw_path.endswith("/") and not norm_path.endswith("/"):
                norm_path += "/"
            if not norm_path.startswith(("/orders/", "/images/", "/files/", "/uploads/")):
                logger.warning(f"Từ chối URL tệp ngoài whitelist đường dẫn: {raw_path}")
                return ""
            rebuilt = f"https://{hostname}{norm_path}"
            if parsed.query:
                rebuilt = f"{rebuilt}?{parsed.query}"
            return rebuilt

        # Chuẩn hóa dấu phân cách thư mục
        path = url_str.replace("\\", "/")
        path = re.sub(r"^[A-Za-z]:/", "", path)  # Bỏ ổ đĩa Windows C:/ nếu có
        if ".." in path.split("/"):
            return ""
        filename = os.path.basename(path)
        if not filename or filename in (".", ".."):
            return ""

        if "/orders/" in path:
            return f"{self.download_order_url}{filename}?image_path={filename}"
        elif "/uploadIMG/" in path or "/images/" in path:
            return f"{self.download_image_url}{filename}"
        elif path.startswith("/"):
            norm_path = posixpath.normpath(path)
            if not norm_path.startswith(("/orders/", "/images/", "/files/")) or norm_path.startswith("//"):
                logger.warning(f"Từ chối relative path ngoài whitelist: {path}")
                return ""
            return f"{self.base_url}{norm_path}"
        else:
            return f"{self.download_file_url}{filename}"

    @staticmethod
    def _matches_magic_bytes(ext: str, file_bytes: bytes) -> bool:
        head = file_bytes[:512].lstrip().lower()
        if any(marker in head for marker in (b"<svg", b"<html", b"<!doctype html", b"<script")):
            return False
        if ext == ".pdf":
            return file_bytes.startswith(b"%PDF-")
        if ext == ".png":
            return file_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        if ext in (".jpg", ".jpeg"):
            return file_bytes.startswith(b"\xff\xd8\xff")
        if ext == ".webp":
            return len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP"
        if ext == ".gif":
            return file_bytes.startswith((b"GIF87a", b"GIF89a"))
        if ext == ".bmp":
            return file_bytes.startswith(b"BM")
        return False

    def validate_file(
        self,
        filename: str,
        file_size: int,
        file_bytes: Optional[bytes] = None,
    ) -> Tuple[bool, str]:
        """Kiểm tra tính hợp lệ của định dạng, magic bytes và dung lượng file."""
        safe_name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
        if not safe_name:
            return False, "Tên tệp không được để trống."

        _, ext = os.path.splitext(safe_name)
        ext = ext.lower()

        if ext not in self.ALLOWED_EXTENSIONS:
            allowed_str = ", ".join(sorted(self.ALLOWED_EXTENSIONS))
            return False, f"Định dạng '{ext}' không được hỗ trợ. Chỉ chấp nhận: {allowed_str}"

        if file_size <= 0:
            return False, "Tệp tin rỗng (0 bytes)."

        if file_size > self.MAX_FILE_SIZE:
            max_mb = self.MAX_FILE_SIZE / (1024 * 1024)
            return False, f"Dung lượng tệp vượt quá giới hạn cho phép ({max_mb:.0f}MB)."

        if file_bytes is not None and not self._matches_magic_bytes(ext, file_bytes):
            return False, "Nội dung tệp không hợp lệ hoặc không khớp định dạng khai báo."

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
        if file_type not in ("order", "tracking"):
            return False, "", "Loại tệp không hợp lệ. Chỉ chấp nhận 'order' hoặc 'tracking'."

        safe_name = os.path.basename(str(filename or "attachment.bin").replace("\\", "/")).strip() or "attachment.bin"
        valid, err = self.validate_file(safe_name, len(file_bytes), file_bytes=file_bytes)
        if not valid:
            return False, "", err

        mime_type, _ = mimetypes.guess_type(safe_name)
        if not mime_type:
            mime_type = "application/pdf" if safe_name.lower().endswith(".pdf") else "application/octet-stream"

        files = {
            "file": (safe_name, file_bytes, mime_type),
        }
        data = {
            "file_type": file_type,
        }

        try:
            client = self._get_client()
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
                    return False, "", "Máy chủ lưu trữ không trả về đường dẫn tệp."
            else:
                logger.error(f"Máy chủ lưu trữ báo lỗi (HTTP {response.status_code}): {response.text[:200]}")
                return False, "", f"Máy chủ lưu trữ báo lỗi (HTTP {response.status_code})."

        except httpx.RequestError as exc:
            logger.error(f"Lỗi kết nối tới máy chủ lưu trữ file: {exc}")
            return False, "", "Không thể kết nối tới máy chủ lưu trữ file."
        except Exception as exc:
            logger.exception(f"Lỗi không xác định khi upload file: {exc}")
            return False, "", "Lỗi nội bộ khi upload file."

    async def download_file_bytes(self, file_url: str) -> Tuple[bool, bytes, str, str]:
        """
        Tải nội dung nhị phân của file để phục vụ proxy download (stream có giới hạn dung lượng, chặn redirect).
        Returns:
            (success: bool, content: bytes, content_type: str, filename: str)
        """
        target_url = self.clean_file_url(file_url)
        if not target_url:
            return False, b"", "", "URL không hợp lệ"

        try:
            client = self._get_client()
            async with client.stream("GET", target_url, follow_redirects=False) as res:
                if res.status_code != 200:
                    return False, b"", "", f"HTTP {res.status_code}"

                content_length = res.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > self.MAX_FILE_SIZE:
                            return False, b"", "", "Dung lượng tệp vượt quá giới hạn cho phép."
                    except ValueError:
                        pass

                chunks = bytearray()
                total_read = 0
                async for chunk in res.aiter_bytes():
                    if not chunk:
                        continue
                    total_read += len(chunk)
                    if total_read > self.MAX_FILE_SIZE:
                        return False, b"", "", "Dung lượng tệp vượt quá giới hạn cho phép."
                    chunks.extend(chunk)

                content_type = res.headers.get("content-type", "application/octet-stream")
                filename = os.path.basename(target_url.split("?")[0]) or "downloaded_file"
                return True, bytes(chunks), content_type, filename
        except Exception as exc:
            logger.error(f"Lỗi khi tải file từ URL '{target_url}': {exc}")
            return False, b"", "", "Không thể tải tệp từ máy chủ lưu trữ."


# Singleton instance
file_service = FileService()
