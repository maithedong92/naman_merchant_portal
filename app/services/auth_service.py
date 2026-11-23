import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from pymongo import MongoClient
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.store import Store
from app.models.user import AuditSecurityLog, RefreshToken, User, UserRole
from app.schemas.auth import TokenResponse
from app.schemas.user import UserCreate, UserResponse, UserUpdate

logger = logging.getLogger("naman_portal.auth_service")
settings = get_settings()


class AuthService:
    """Service handling User Authentication (MongoDB naman_internal_app.user + PG shadow), Token Lifecycle, RBAC, and Security Auditing."""

    def __init__(self):
        self._mongo_client: Optional[MongoClient] = None

    def close(self) -> None:
        """Close the MongoDB client connection pool on shutdown."""
        if self._mongo_client is not None:
            try:
                self._mongo_client.close()
            except Exception:
                pass
            self._mongo_client = None

    def _get_mongo_user_collection(self):
        """Return MongoDB collection handle for naman_internal_app.user."""
        if self._mongo_client is None:
            self._mongo_client = MongoClient(
                settings.AUTH_MONGO_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=8000,
            )
        return self._mongo_client[settings.AUTH_MONGO_DB][settings.AUTH_MONGO_COLLECTION]

    def _find_mongo_user_sync(self, identifier: str) -> Optional[Dict[str, Any]]:
        """
        Look up user in MongoDB naman_internal_app.user strictly by username or user_id (never email).
        Prefers active documents if multiple records match.
        """
        ident = identifier.strip()
        if not ident:
            return None
        col = self._get_mongo_user_collection()
        escaped = re.escape(ident)
        ci_exact = {"$regex": f"^{escaped}$", "$options": "i"}
        docs = list(
            col.find(
                {
                    "$or": [
                        {"username": ci_exact},
                        {"user_id": ident},
                    ]
                }
            ).limit(10)
        )
        if not docs:
            return None
        # Prefer active records over inactive/resigned duplicates
        docs.sort(
            key=lambda d: (
                0 if str(d.get("status") or "").strip().lower() == "active" else 1,
                0 if not d.get("MarkForDelete") else 1,
            )
        )
        return docs[0]

    def _list_active_mongo_users_sync(self) -> List[Dict[str, Any]]:
        """Fetch all active, non-deleted user documents from MongoDB naman_internal_app.user."""
        col = self._get_mongo_user_collection()
        return list(
            col.find(
                {
                    "status": {"$regex": "^active$", "$options": "i"},
                    "MarkForDelete": {"$ne": True},
                }
            )
        )

    def _update_mongo_password_sync(self, mongo_id: Any, new_hashed_password: str) -> None:
        """Update password hash in MongoDB naman_internal_app.user."""
        col = self._get_mongo_user_collection()
        col.update_one({"_id": mongo_id}, {"$set": {"password": new_hashed_password}})

    @staticmethod
    def _extract_mongo_fields(doc: Dict[str, Any], store_code_to_id: Dict[str, str]) -> Dict[str, Any]:
        """Map a MongoDB naman_internal_app.user document to NAM-UMP User attributes."""
        raw_username = str(doc.get("username") or doc.get("user_id") or "").strip()[:50]
        user_id_code = str(doc.get("user_id") or str(doc.get("_id") or "")[-6:]).strip()
        eci = doc.get("ECI") if isinstance(doc.get("ECI"), dict) else {}

        full_name = str(
            doc.get("FullName")
            or doc.get("fullname")
            or doc.get("name")
            or raw_username
        ).strip()[:100]

        phone_raw = str(
            doc.get("phone")
            or doc.get("phone_number")
            or eci.get("MobilePhone")
            or ""
        ).strip()
        phone_number = phone_raw[:20] if phone_raw else None

        raw_email = str(doc.get("email") or eci.get("Email") or "").strip().lower()
        safe_slug = re.sub(r"[^a-z0-9._-]", "", raw_username.lower()) or f"user{user_id_code}"
        if not raw_email or "@" not in raw_email:
            raw_email = f"{safe_slug}@namanmarket.com.vn"

        status_str = str(doc.get("status") or "active").strip().lower()
        is_active = (status_str == "active") and not bool(doc.get("MarkForDelete"))

        dept_str = str(doc.get("dept") or doc.get("DeptName") or "").strip()
        # Phần phân quyền & chi nhánh trên NAM-UMP do IT cấu hình thủ công;
        # không tự mapping quyền/chi nhánh từ HR MongoDB để tránh cấp quá quyền.
        store_id = None
        role = UserRole.READ_ONLY.value
        is_superuser = False

        stored_pw = str(doc.get("password") or "").strip()
        if not stored_pw:
            stored_pw = "!DISABLED!"

        return {
            "username": raw_username,
            "user_id_code": user_id_code,
            "safe_slug": safe_slug,
            "email": raw_email[:100],
            "full_name": full_name or raw_username,
            "phone_number": phone_number,
            "role": role,
            "store_id": store_id,
            "is_active": is_active,
            "is_superuser": is_superuser,
            "hashed_password": stored_pw[:255],
        }

    async def _upsert_pg_shadow_user(
        self,
        db: AsyncSession,
        mongo_doc: Dict[str, Any],
        store_code_to_id: Optional[Dict[str, str]] = None,
    ) -> User:
        """Synchronize a MongoDB user document into PostgreSQL users shadow table."""
        if store_code_to_id is None:
            stores_res = await db.execute(select(Store))
            store_code_to_id = {s.code: s.id for s in stores_res.scalars().all()}

        mapped = self._extract_mongo_fields(mongo_doc, store_code_to_id)
        stmt = (
            select(User)
            .where(func.lower(User.username) == mapped["username"].lower())
            .options(selectinload(User.store))
            .with_for_update()
        )
        user = (await db.execute(stmt)).scalar_one_or_none()

        # Resolve unique email safely
        candidate_email = mapped["email"]
        email_conflict_stmt = select(User.id).where(func.lower(User.email) == candidate_email.lower())
        if user:
            email_conflict_stmt = email_conflict_stmt.where(User.id != user.id)
        if (await db.execute(email_conflict_stmt)).scalar_one_or_none():
            candidate_email = f"{mapped['safe_slug']}.{mapped['user_id_code']}@namanmarket.com.vn"[:100]

        if user:
            # IT assigns RBAC role / branch manually in NAM-UMP; HR sync only refreshes profile & status.
            user.username = mapped["username"]
            user.email = candidate_email
            user.hashed_password = mapped["hashed_password"]
            user.full_name = mapped["full_name"]
            user.phone_number = mapped["phone_number"]
            if getattr(user, "portal_disabled", False):
                user.is_active = False
            else:
                user.is_active = mapped["is_active"]
        else:
            user = User(
                username=mapped["username"],
                email=candidate_email,
                hashed_password=mapped["hashed_password"],
                full_name=mapped["full_name"],
                phone_number=mapped["phone_number"],
                role=mapped["role"],
                store_id=mapped["store_id"],
                is_active=mapped["is_active"],
                is_superuser=mapped["is_superuser"],
            )
            db.add(user)

        await db.flush()
        # Reload store relationship cleanly
        reloaded = (
            await db.execute(
                select(User).where(User.id == user.id).options(selectinload(User.store))
            )
        ).scalar_one()
        return reloaded

    async def _revoke_all_refresh_tokens(
        self,
        db: AsyncSession,
        user_id: str,
        now: datetime,
    ) -> None:
        """Mark all unrevoked refresh tokens belonging to a user as revoked."""
        stmt = select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        result = await db.execute(stmt)
        for tok in result.scalars().all():
            tok.revoked_at = now

    async def log_security_event(
        self,
        db: AsyncSession,
        event_type: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        """Helper to write audit security log entries asynchronously."""
        try:
            log_entry = AuditSecurityLog(
                user_id=user_id,
                username=username,
                event_type=event_type,
                ip_address=ip_address,
                user_agent=user_agent,
                details=json.dumps(details, ensure_ascii=False) if details else None,
            )
            db.add(log_entry)
            await db.flush()
        except Exception as e:
            logger.error(f"Lỗi khi ghi nhật ký bảo mật: {str(e)}")

    async def authenticate_user(
        self,
        db: AsyncSession,
        username_or_email: str,
        password: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[User, TokenResponse]:
        """
        Authenticate user credentials directly against MongoDB (103.175.248.250:27011 / naman_internal_app.user)
        with brute-force lockout protection and automatic shadow sync into PostgreSQL.
        Issues JWT access token and rotating refresh token upon success.
        """
        now = datetime.now(timezone.utc)
        ident = username_or_email.strip()

        mongo_doc: Optional[Dict[str, Any]] = None
        if settings.AUTH_MONGO_ENABLED:
            try:
                mongo_doc = await asyncio.to_thread(self._find_mongo_user_sync, ident)
            except Exception as mongo_ex:
                logger.error(f"Lỗi kết nối MongoDB naman_internal_app.user khi đăng nhập '{ident}': {mongo_ex}")
                raise UnauthorizedError("Không thể kết nối máy chủ xác thực nội bộ Nam An (MongoDB). Vui lòng thử lại sau.")

            if not mongo_doc:
                verify_password("fake_password_dummy", "$2b$12$e8YQ3d1hB5u7.wF36wGjveU78cT0fK5G3uB7G1YvD.Zq2hI4uX3Xy")
                await self.log_security_event(
                    db=db,
                    event_type="LOGIN_FAILED",
                    username=ident,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={"reason": "MONGO_USER_NOT_FOUND"},
                )
                await db.commit()
                raise UnauthorizedError("Tên đăng nhập hoặc mật khẩu không chính xác.")

            user = await self._upsert_pg_shadow_user(db, mongo_doc)
        else:
            stmt = (
                select(User)
                .where(func.lower(User.username) == ident.lower())
                .options(selectinload(User.store))
                .with_for_update()
            )
            result = await db.execute(stmt)
            user = result.scalar_one_or_none()

            if not user:
                verify_password("fake_password_dummy", "$2b$12$e8YQ3d1hB5u7.wF36wGjveU78cT0fK5G3uB7G1YvD.Zq2hI4uX3Xy")
                await self.log_security_event(
                    db=db,
                    event_type="LOGIN_FAILED",
                    username=ident,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={"reason": "USER_NOT_FOUND"},
                )
                await db.commit()
                raise UnauthorizedError("Tên đăng nhập hoặc mật khẩu không chính xác.")

        # Check account lockout
        if user.locked_until and user.locked_until > now:
            remaining_mins = int((user.locked_until - now).total_seconds() // 60) + 1
            await self.log_security_event(
                db=db,
                event_type="LOGIN_BLOCKED_LOCKED",
                user_id=user.id,
                username=user.username,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"remaining_minutes": remaining_mins},
            )
            await db.commit()
            raise UnauthorizedError(
                f"Tài khoản đang bị tạm khóa do nhập sai mật khẩu quá nhiều lần. Vui lòng thử lại sau {remaining_mins} phút."
            )

        # Check if user is active (synced from MongoDB status == 'active' and not portal_disabled)
        if not user.is_active or getattr(user, "portal_disabled", False):
            await self.log_security_event(
                db=db,
                event_type="LOGIN_BLOCKED_INACTIVE",
                user_id=user.id,
                username=user.username,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"portal_disabled": bool(getattr(user, "portal_disabled", False))},
            )
            await db.commit()
            if getattr(user, "portal_disabled", False):
                raise UnauthorizedError("Tài khoản đã bị vô hiệu hóa trên cổng Portal.")
            raise UnauthorizedError("Tài khoản nhân sự đã ngừng kích hoạt trên hệ thống nội bộ Nam An.")

        # Verify password directly against MongoDB hash (or PG hash if Mongo auth disabled)
        target_hash = str(mongo_doc.get("password") or "") if mongo_doc is not None else user.hashed_password
        if not verify_password(password, target_hash):
            user.failed_login_attempts += 1
            is_locked = False
            if user.failed_login_attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
                user.locked_until = now + timedelta(minutes=settings.ACCOUNT_LOCKOUT_MINUTES)
                is_locked = True
                await self.log_security_event(
                    db=db,
                    event_type="ACCOUNT_LOCKED",
                    user_id=user.id,
                    username=user.username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={
                        "failed_attempts": user.failed_login_attempts,
                        "lockout_minutes": settings.ACCOUNT_LOCKOUT_MINUTES,
                    },
                )
            else:
                await self.log_security_event(
                    db=db,
                    event_type="LOGIN_FAILED",
                    user_id=user.id,
                    username=user.username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={"failed_attempts": user.failed_login_attempts},
                )
            await db.commit()

            if is_locked:
                raise UnauthorizedError(
                    f"Bạn đã nhập sai mật khẩu {user.failed_login_attempts} lần. Tài khoản tạm thời bị khóa trong {settings.ACCOUNT_LOCKOUT_MINUTES} phút."
                )
            raise UnauthorizedError("Tên đăng nhập hoặc mật khẩu không chính xác.")

        # Successful login: reset failed attempts & lockout
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        # Upgrade legacy unsalted MD5/SHA-256 hash in MongoDB/PostgreSQL to bcrypt on successful login
        if target_hash and not str(target_hash).strip().startswith("$2"):
            upgraded_hash = hash_password(password)
            if mongo_doc is not None and mongo_doc.get("_id") is not None:
                await asyncio.to_thread(self._update_mongo_password_sync, mongo_doc["_id"], upgraded_hash)
            user.hashed_password = upgraded_hash

        # Generate access and refresh tokens
        token_payload = {
            "sub": user.id,
            "username": user.username,
            "role": user.role,
            "store_id": user.store_id,
            "store_code": user.store_code,
            "is_superuser": user.is_superuser,
            "ver": int(getattr(user, "token_version", 0) or 0),
        }
        access_token, access_exp = create_access_token(token_payload)
        refresh_raw, refresh_hash, refresh_exp = create_refresh_token({"sub": user.id})

        # Save refresh token record
        refresh_token_record = RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_exp,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(refresh_token_record)

        await self.log_security_event(
            db=db,
            event_type="LOGIN_SUCCESS",
            user_id=user.id,
            username=user.username,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        await db.commit()
        await db.refresh(user)

        expires_in = int((access_exp - now).total_seconds())
        token_response = TokenResponse(
            access_token=access_token,
            refresh_token=refresh_raw,
            token_type="bearer",
            expires_in=expires_in,
            user=UserResponse.model_validate(user),
        )

        return user, token_response

    async def refresh_access_token(
        self,
        db: AsyncSession,
        refresh_token_str: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> TokenResponse:
        """
        Validate and rotate refresh token.
        Revokes old refresh token and issues a new access/refresh pair.
        """
        now = datetime.now(timezone.utc)

        # 1. Decode JWT
        payload = decode_token(refresh_token_str)
        if payload.get("type") != "refresh":
            raise UnauthorizedError("Token cung cấp không phải là Refresh Token hợp lệ.")

        user_id = payload.get("sub")
        if not user_id:
            raise UnauthorizedError("Payload token không chứa mã người dùng.")

        # 2. Check token in database (lock row to prevent concurrent refresh races)
        hashed = hash_token(refresh_token_str)
        stmt = select(RefreshToken).where(RefreshToken.token_hash == hashed).with_for_update()
        result = await db.execute(stmt)
        token_record = result.scalar_one_or_none()

        if not token_record:
            await self.log_security_event(
                db=db,
                event_type="REFRESH_TOKEN_FAILED",
                user_id=user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"reason": "TOKEN_NOT_FOUND_IN_DB"},
            )
            await db.commit()
            raise UnauthorizedError("Refresh token không tồn tại hoặc đã bị hủy bỏ.")

        if token_record.revoked_at is not None:
            # Token reuse detection! Possible security breach: revoke all tokens for this user
            await self._revoke_all_refresh_tokens(db, user_id, now)

            user_stmt = select(User).where(User.id == user_id).with_for_update()
            user_obj = (await db.execute(user_stmt)).scalar_one_or_none()
            if user_obj is not None:
                user_obj.token_version = int(getattr(user_obj, "token_version", 0) or 0) + 1

            await self.log_security_event(
                db=db,
                event_type="REFRESH_TOKEN_REUSE_DETECTED",
                user_id=user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"reason": "REVOKED_TOKEN_PRESENTED_ALL_SESSIONS_INVALIDATED"},
            )
            await db.commit()
            raise UnauthorizedError("Phát hiện hành vi tái sử dụng Refresh Token đã bị thu hồi. Toàn bộ phiên đăng nhập đã bị vô hiệu để đảm bảo an toàn.")

        if token_record.expires_at <= now:
            token_record.revoked_at = now
            await db.commit()
            raise UnauthorizedError("Refresh token đã hết hạn sử dụng. Vui lòng đăng nhập lại.")

        # 3. Fetch user (and re-sync active status & role from MongoDB if enabled)
        user_stmt = select(User).where(User.id == user_id).options(selectinload(User.store))
        user_result = await db.execute(user_stmt)
        user = user_result.scalar_one_or_none()

        if user and settings.AUTH_MONGO_ENABLED:
            try:
                mongo_doc = await asyncio.to_thread(self._find_mongo_user_sync, user.username)
                if mongo_doc:
                    user = await self._upsert_pg_shadow_user(db, mongo_doc)
                else:
                    user.is_active = False
            except Exception as refresh_mongo_ex:
                logger.error(f"Không thể đối chiếu lại MongoDB khi refresh token cho '{user.username}': {refresh_mongo_ex}")
                raise UnauthorizedError("Không thể xác minh trạng thái tài khoản trên hệ thống nội bộ. Vui lòng đăng nhập lại.")

        if not user or not user.is_active or getattr(user, "portal_disabled", False):
            token_record.revoked_at = now
            await db.commit()
            raise UnauthorizedError("Người dùng không tồn tại hoặc đã bị vô hiệu hóa.")

        # 4. Token Rotation: Revoke current refresh token
        token_record.revoked_at = now

        # 5. Issue new access token and refresh token
        token_payload = {
            "sub": user.id,
            "username": user.username,
            "role": user.role,
            "store_id": user.store_id,
            "store_code": user.store_code,
            "is_superuser": user.is_superuser,
            "ver": int(getattr(user, "token_version", 0) or 0),
        }
        new_access_token, access_exp = create_access_token(token_payload)
        new_refresh_raw, new_refresh_hash, new_refresh_exp = create_refresh_token({"sub": user.id})

        # Save new refresh token
        new_refresh_record = RefreshToken(
            user_id=user.id,
            token_hash=new_refresh_hash,
            expires_at=new_refresh_exp,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.add(new_refresh_record)

        await self.log_security_event(
            db=db,
            event_type="REFRESH_TOKEN_SUCCESS",
            user_id=user.id,
            username=user.username,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        await db.commit()
        await db.refresh(user)

        expires_in = int((access_exp - now).total_seconds())
        return TokenResponse(
            access_token=new_access_token,
            refresh_token=new_refresh_raw,
            token_type="bearer",
            expires_in=expires_in,
            user=UserResponse.model_validate(user),
        )

    async def logout_user(
        self,
        db: AsyncSession,
        user: User,
        refresh_token_str: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """Revoke a refresh token or all user tokens on logout."""
        now = datetime.now(timezone.utc)

        if refresh_token_str:
            hashed = hash_token(refresh_token_str)
            stmt = select(RefreshToken).where(
                RefreshToken.token_hash == hashed,
                RefreshToken.user_id == user.id,
                RefreshToken.revoked_at.is_(None),
            )
            result = await db.execute(stmt)
            token = result.scalar_one_or_none()
            if token:
                token.revoked_at = now
        else:
            # Revoke all active sessions of this user
            await self._revoke_all_refresh_tokens(db, user.id, now)

        user.token_version = int(getattr(user, "token_version", 0) or 0) + 1

        await self.log_security_event(
            db=db,
            event_type="LOGOUT",
            user_id=user.id,
            username=user.username,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await db.commit()

    async def change_password(
        self,
        db: AsyncSession,
        user: User,
        current_password: str,
        new_password: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """Change user password in MongoDB naman_internal_app.user and PG shadow, invalidating active refresh sessions."""
        now = datetime.now(timezone.utc)
        locked_user = (
            await db.execute(select(User).where(User.id == user.id).with_for_update())
        ).scalar_one_or_none()
        if not locked_user:
            raise UnauthorizedError("Người dùng không tồn tại trong hệ thống.")
        user = locked_user

        if user.locked_until and user.locked_until > now:
            remaining_mins = int((user.locked_until - now).total_seconds() // 60) + 1
            await self.log_security_event(
                db=db,
                event_type="PASSWORD_CHANGE_BLOCKED_LOCKED",
                user_id=user.id,
                username=user.username,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"remaining_minutes": remaining_mins},
            )
            await db.commit()
            raise UnauthorizedError(
                f"Tài khoản đang bị tạm khóa do nhập sai mật khẩu quá nhiều lần. Vui lòng thử lại sau {remaining_mins} phút."
            )

        mongo_doc: Optional[Dict[str, Any]] = None
        target_hash = user.hashed_password
        if settings.AUTH_MONGO_ENABLED:
            try:
                mongo_doc = await asyncio.to_thread(self._find_mongo_user_sync, user.username)
            except Exception as mongo_ex:
                logger.error(f"Lỗi kết nối MongoDB naman_internal_app.user khi đổi mật khẩu '{user.username}': {mongo_ex}")
                raise UnauthorizedError("Không thể kết nối máy chủ xác thực nội bộ Nam An (MongoDB). Vui lòng thử lại sau.")
            if not mongo_doc:
                raise UnauthorizedError("Không tìm thấy tài khoản trên hệ thống xác thực nội bộ.")
            target_hash = str(mongo_doc.get("password") or "")

        if not verify_password(current_password, target_hash):
            user.failed_login_attempts = int(getattr(user, "failed_login_attempts", 0) or 0) + 1
            is_locked = False
            if user.failed_login_attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
                user.locked_until = now + timedelta(minutes=settings.ACCOUNT_LOCKOUT_MINUTES)
                is_locked = True
                await self.log_security_event(
                    db=db,
                    event_type="ACCOUNT_LOCKED",
                    user_id=user.id,
                    username=user.username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={
                        "reason": "INCORRECT_CURRENT_PASSWORD",
                        "failed_attempts": user.failed_login_attempts,
                        "lockout_minutes": settings.ACCOUNT_LOCKOUT_MINUTES,
                    },
                )
            else:
                await self.log_security_event(
                    db=db,
                    event_type="PASSWORD_CHANGE_FAILED",
                    user_id=user.id,
                    username=user.username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={
                        "reason": "INCORRECT_CURRENT_PASSWORD",
                        "failed_attempts": user.failed_login_attempts,
                    },
                )
            await db.commit()
            if is_locked:
                raise UnauthorizedError(
                    f"Bạn đã nhập sai mật khẩu {user.failed_login_attempts} lần. Tài khoản tạm thời bị khóa trong {settings.ACCOUNT_LOCKOUT_MINUTES} phút."
                )
            raise ValidationError("Mật khẩu hiện tại không chính xác.")

        if current_password == new_password:
            raise ValidationError("Mật khẩu mới không được trùng với mật khẩu hiện tại.")

        new_hashed = hash_password(new_password)
        if settings.AUTH_MONGO_ENABLED:
            if not mongo_doc or mongo_doc.get("_id") is None:
                raise UnauthorizedError("Không tìm thấy tài khoản trên hệ thống xác thực nội bộ.")
            try:
                await asyncio.to_thread(self._update_mongo_password_sync, mongo_doc["_id"], new_hashed)
            except Exception as mongo_ex:
                logger.error(f"Lỗi cập nhật mật khẩu trên MongoDB cho '{user.username}': {mongo_ex}")
                raise UnauthorizedError("Không thể cập nhật mật khẩu trên máy chủ xác thực nội bộ Nam An (MongoDB). Vui lòng thử lại sau.")
        user.hashed_password = new_hashed
        user.failed_login_attempts = 0
        user.locked_until = None
        user.token_version = int(getattr(user, "token_version", 0) or 0) + 1

        # Invalidate all existing refresh tokens for security
        await self._revoke_all_refresh_tokens(db, user.id, now)

        await self.log_security_event(
            db=db,
            event_type="PASSWORD_CHANGED",
            user_id=user.id,
            username=user.username,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await db.commit()

    # ==========================================================================
    # User Management CRUD (Admin / Supervisor)
    # ==========================================================================

    async def create_user(
        self,
        db: AsyncSession,
        user_in: UserCreate,
        actor_user: Optional[User] = None,
    ) -> User:
        """Create a new portal user."""
        normalized_username = user_in.username.strip()
        normalized_email = user_in.email.strip().lower()

        # Check case-insensitive uniqueness of username
        check_user = await db.execute(
            select(User).where(func.lower(User.username) == normalized_username.lower())
        )
        if check_user.scalar_one_or_none():
            raise ConflictError(f"Tên đăng nhập '{normalized_username}' đã tồn tại trong hệ thống.")

        # Check case-insensitive uniqueness of email
        check_email = await db.execute(
            select(User).where(func.lower(User.email) == normalized_email)
        )
        if check_email.scalar_one_or_none():
            raise ConflictError(f"Email '{user_in.email}' đã được đăng ký cho tài khoản khác.")

        # If store_id provided, verify store exists
        if user_in.store_id:
            store_res = await db.execute(select(Store).where(Store.id == user_in.store_id))
            if not store_res.scalar_one_or_none():
                raise NotFoundError("Store", user_in.store_id)

        user = User(
            username=normalized_username,
            email=normalized_email,
            hashed_password=hash_password(user_in.password),
            full_name=user_in.full_name.strip(),
            phone_number=user_in.phone_number.strip() if user_in.phone_number else None,
            role=user_in.role.value,
            store_id=user_in.store_id,
            is_active=True,
            portal_disabled=False,
            is_superuser=(user_in.role == UserRole.SUPER_ADMIN),
        )
        db.add(user)
        await db.flush()
        await self.log_security_event(
            db=db,
            event_type="USER_CREATED",
            user_id=user.id,
            username=user.username,
            details={
                "role": user.role,
                "store_id": user.store_id,
                "created_by": actor_user.username if actor_user else None,
            },
        )
        await db.commit()
        await db.refresh(user)
        logger.info(f"Đã tạo người dùng mới: {user.username} ({user.role})")
        return user

    async def list_users(
        self,
        db: AsyncSession,
        role: Optional[UserRole] = None,
        store_id: Optional[str] = None,
        store_code: Optional[str] = None,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[User], int]:
        """List users with filtering and pagination."""
        query = select(User).options(selectinload(User.store))
        if role:
            query = query.where(User.role == role.value)
        if store_id:
            query = query.where(User.store_id == store_id)
        if store_code:
            query = query.join(User.store).where(Store.code == store_code)
        if is_active is not None:
            query = query.where(User.is_active == is_active)
        if search:
            search_pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    User.username.ilike(search_pattern),
                    User.full_name.ilike(search_pattern),
                    User.email.ilike(search_pattern),
                    User.phone_number.ilike(search_pattern),
                )
            )

        # Count total
        from sqlalchemy import func
        count_query = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_query)).scalar() or 0

        # Paginate
        offset = (page - 1) * page_size
        stmt = query.order_by(desc(User.created_at)).offset(offset).limit(page_size)
        result = await db.execute(stmt)
        return list(result.scalars().all()), total

    async def get_user_by_id(self, db: AsyncSession, user_id: str) -> User:
        """Fetch user by primary ID."""
        stmt = select(User).where(User.id == user_id).options(selectinload(User.store))
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            raise NotFoundError("User", user_id)
        return user

    async def update_user(
        self,
        db: AsyncSession,
        user_id: str,
        user_in: UserUpdate,
        actor_user: Optional[User] = None,
    ) -> User:
        """Update user profile or permissions with self-lockout, last-admin, and audit guards."""
        stmt = (
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.store))
            .with_for_update()
        )
        user = (await db.execute(stmt)).scalar_one_or_none()
        if not user:
            raise NotFoundError("User", user_id)

        if actor_user is not None and actor_user.id == user.id:
            if user_in.is_active is False or (
                user_in.role is not None and user_in.role != UserRole.SUPER_ADMIN
            ):
                raise ForbiddenError(
                    "Không thể tự vô hiệu hóa hoặc tự hạ quyền tài khoản của chính mình."
                )

        is_target_super_admin = bool(
            user.is_active and (user.is_superuser or user.role == UserRole.SUPER_ADMIN.value)
        )
        will_disable = user_in.is_active is False
        will_demote = user_in.role is not None and user_in.role != UserRole.SUPER_ADMIN
        if is_target_super_admin and (will_disable or will_demote):
            other_admins_stmt = select(func.count(User.id)).where(
                User.id != user.id,
                User.is_active.is_(True),
                or_(
                    User.role == UserRole.SUPER_ADMIN.value,
                    User.is_superuser.is_(True),
                ),
            )
            other_admin_count = (await db.execute(other_admins_stmt)).scalar() or 0
            if other_admin_count == 0:
                raise ConflictError(
                    "Không thể vô hiệu hóa hoặc hạ quyền SUPER_ADMIN đang hoạt động cuối cùng của hệ thống."
                )

        if user_in.password and settings.AUTH_MONGO_ENABLED:
            raise ValidationError(
                "Tài khoản đồng bộ từ HR cần đổi mật khẩu trực tiếp trên hệ thống HR nội bộ."
            )

        old_role = user.role
        old_store_id = user.store_id
        old_is_active = user.is_active

        if user_in.email is not None and user_in.email.strip().lower() != user.email:
            normalized_email = user_in.email.strip().lower()
            check_email = await db.execute(
                select(User).where(func.lower(User.email) == normalized_email, User.id != user_id)
            )
            if check_email.scalar_one_or_none():
                raise ConflictError(f"Email '{user_in.email}' đã được sử dụng bởi người dùng khác.")
            user.email = normalized_email

        if user_in.full_name is not None:
            user.full_name = user_in.full_name.strip()

        if user_in.phone_number is not None:
            user.phone_number = user_in.phone_number.strip() if user_in.phone_number else None

        fields_set = getattr(user_in, "model_fields_set", set())
        if user_in.role is not None:
            user.role = user_in.role.value
            user.is_superuser = (user_in.role == UserRole.SUPER_ADMIN)
            if (
                user_in.role in (UserRole.SUPER_ADMIN, UserRole.CS, UserRole.HQ_AUDITOR)
                and "store_id" not in fields_set
            ):
                user.store_id = None

        if "store_id" in fields_set or user_in.store_id is not None:
            if user_in.store_id:
                store_res = await db.execute(select(Store).where(Store.id == user_in.store_id))
                if not store_res.scalar_one_or_none():
                    raise NotFoundError("Store", user_in.store_id)
                user.store_id = user_in.store_id
            else:
                user.store_id = None

        now = datetime.now(timezone.utc)
        token_revoked = False

        if user_in.is_active is not None:
            if user_in.is_active is False:
                user.is_active = False
                user.portal_disabled = True
                user.token_version = int(getattr(user, "token_version", 0) or 0) + 1
                await self._revoke_all_refresh_tokens(db, user.id, now)
                token_revoked = True
            else:
                user.is_active = True
                user.portal_disabled = False

        if user_in.password:
            user.hashed_password = hash_password(user_in.password)
            if not token_revoked:
                user.token_version = int(getattr(user, "token_version", 0) or 0) + 1
                await self._revoke_all_refresh_tokens(db, user.id, now)
                token_revoked = True

        if (old_role != user.role or old_store_id != user.store_id) and not token_revoked:
            user.token_version = int(getattr(user, "token_version", 0) or 0) + 1

        changes: Dict[str, Any] = {}
        if old_role != user.role:
            changes["role"] = {"from": old_role, "to": user.role}
        if old_store_id != user.store_id:
            changes["store_id"] = {"from": old_store_id, "to": user.store_id}
        if old_is_active != user.is_active:
            changes["is_active"] = {"from": old_is_active, "to": user.is_active}
        if user_in.password:
            changes["password_reset"] = True

        if old_is_active and not user.is_active:
            event_type = "USER_DEACTIVATED"
        elif not old_is_active and user.is_active:
            event_type = "USER_REACTIVATED"
        else:
            event_type = "USER_UPDATED"

        await self.log_security_event(
            db=db,
            event_type=event_type,
            user_id=user.id,
            username=user.username,
            details={
                "changes": changes,
                "portal_disabled": bool(getattr(user, "portal_disabled", False)),
                "updated_by": actor_user.username if actor_user else None,
            },
        )

        await db.commit()
        await db.refresh(user)
        return user

    async def deactivate_user(
        self,
        db: AsyncSession,
        user_id: str,
        actor_user: Optional[User] = None,
    ) -> User:
        """Deactivate a user account on the portal and revoke all active tokens."""
        return await self.update_user(
            db=db,
            user_id=user_id,
            user_in=UserUpdate(is_active=False),
            actor_user=actor_user,
        )

    async def list_security_logs(
        self,
        db: AsyncSession,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditSecurityLog]:
        """Fetch recent security audit logs."""
        query = select(AuditSecurityLog)
        if user_id:
            query = query.where(AuditSecurityLog.user_id == user_id)
        if event_type:
            query = query.where(AuditSecurityLog.event_type == event_type)

        stmt = query.order_by(desc(AuditSecurityLog.created_at)).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # ==========================================================================
    # Seed SuperAdmin
    # ==========================================================================

    async def sync_users_from_mongodb(
        self,
        db: AsyncSession,
        purge_demo_accounts: bool = True,
    ) -> Dict[str, Any]:
        """
        Synchronize all active accounts from MongoDB (103.175.248.250:27011 / naman_internal_app.user)
        into PostgreSQL shadow table, and remove/disable old demo accounts not present in MongoDB.
        """
        from app.services.channel_service import DEFAULT_STORES

        # Ensure all 4 Nam An default stores exist before mapping CH10001..CH10006
        for store_data in DEFAULT_STORES:
            code = store_data["code"]
            existing_store = (await db.execute(select(Store).where(Store.code == code))).scalar_one_or_none()
            if not existing_store:
                db.add(
                    Store(
                        code=code,
                        name=store_data["name"],
                        address=store_data.get("address"),
                        phone=store_data.get("phone"),
                        is_active=True,
                    )
                )
        await db.flush()

        stores_res = await db.execute(select(Store))
        store_code_to_id = {s.code: s.id for s in stores_res.scalars().all()}

        mongo_docs = await asyncio.to_thread(self._list_active_mongo_users_sync)
        synced_usernames_lower = set()
        role_counts = {r.value: 0 for r in UserRole}

        for doc in mongo_docs:
            raw_u = str(doc.get("username") or doc.get("user_id") or "").strip()
            if not raw_u:
                continue
            user_obj = await self._upsert_pg_shadow_user(db, doc, store_code_to_id=store_code_to_id)
            synced_usernames_lower.add(user_obj.username.lower())
            role_counts[user_obj.role] = role_counts.get(user_obj.role, 0) + 1

        purged_count = 0
        deactivated_count = 0
        now = datetime.now(timezone.utc)
        if synced_usernames_lower:
            demo_prefixes = ("manager_1000", "staff_1000")
            demo_exact = {"admin", "shopee_tester"}
            pg_users = (await db.execute(select(User))).scalars().all()
            for u in pg_users:
                ulow = u.username.lower()
                if ulow not in synced_usernames_lower:
                    if purge_demo_accounts and (ulow in demo_exact or ulow.startswith(demo_prefixes)):
                        await db.delete(u)
                        purged_count += 1
                    elif ulow != "admin" and u.is_active:
                        u.is_active = False
                        u.token_version = int(getattr(u, "token_version", 0) or 0) + 1
                        await self._revoke_all_refresh_tokens(db, u.id, now)
                        deactivated_count += 1

        await db.commit()
        return {
            "mongo_active_users": len(synced_usernames_lower),
            "role_breakdown": role_counts,
            "demo_accounts_deleted": purged_count,
            "stale_accounts_deactivated": deactivated_count,
        }

    async def seed_initial_superadmin(self, db: AsyncSession) -> Optional[User]:
        """
        Startup hook: When AUTH_MONGO_ENABLED is True, syncs active users from MongoDB naman_internal_app.user.
        Otherwise falls back to seeding local superadmin.
        """
        if settings.AUTH_MONGO_ENABLED:
            try:
                summary = await self.sync_users_from_mongodb(db=db, purge_demo_accounts=True)
                logger.info(
                    f"✅ Đã đồng bộ tài khoản đăng nhập từ MongoDB naman_internal_app.user: {summary}"
                )
                return None
            except Exception as mongo_ex:
                logger.warning(f"Không thể đồng bộ khởi động từ MongoDB naman_internal_app.user: {mongo_ex}")
                await db.rollback()
                return None

        try:
            stmt = select(User).where(User.role == UserRole.SUPER_ADMIN.value)
            result = await db.execute(stmt)
            existing_admin = result.scalars().first()

            if existing_admin:
                return existing_admin

            # Also verify if initial username exists
            stmt_user = select(User).where(User.username == settings.INITIAL_ADMIN_USERNAME)
            user_res = await db.execute(stmt_user)
            if user_res.scalars().first():
                return None

            if not settings.INITIAL_ADMIN_PASSWORD:
                logger.warning("INITIAL_ADMIN_PASSWORD chưa được cấu hình; bỏ qua bước tạo SuperAdmin mặc định.")
                return None

            admin_user = User(
                username=settings.INITIAL_ADMIN_USERNAME,
                email=settings.INITIAL_ADMIN_EMAIL,
                hashed_password=hash_password(settings.INITIAL_ADMIN_PASSWORD),
                full_name=settings.INITIAL_ADMIN_FULL_NAME,
                role=UserRole.SUPER_ADMIN.value,
                is_active=True,
                is_superuser=True,
            )
            db.add(admin_user)
            await db.commit()
            await db.refresh(admin_user)
            logger.info(
                f"✅ Đã tự động tạo tài khoản SuperAdmin mặc định: username='{settings.INITIAL_ADMIN_USERNAME}'"
            )
            return admin_user
        except Exception as e:
            logger.warning(f"Không thể tạo tài khoản SuperAdmin khởi tạo: {str(e)}")
            await db.rollback()
            return None

    async def seed_branch_staff_users(
        self,
        db: AsyncSession,
        default_password: Optional[str] = None,
    ) -> List[dict]:
        """
        Idempotent function that seeds Store Managers and Staff accounts for all 4 Nam An branches.
        Creates 1 Manager and 1 Staff per branch if they don't already exist.
        """
        from app.services.channel_service import DEFAULT_STORES

        effective_password = default_password or secrets.token_urlsafe(12)
        created_accounts = []
        try:
            # 1. Ensure all default stores exist in DB
            for store_data in DEFAULT_STORES:
                code = store_data["code"]
                stmt = select(Store).where(Store.code == code)
                res = await db.execute(stmt)
                store = res.scalar_one_or_none()
                if not store:
                    store = Store(
                        code=code,
                        name=store_data["name"],
                        address=store_data.get("address"),
                        phone=store_data.get("phone"),
                        is_active=True,
                    )
                    db.add(store)
                    await db.flush()

                # 2. Seed Manager for this store
                mgr_username = f"manager_{code}"
                mgr_stmt = select(User).where(User.username == mgr_username)
                if not (await db.execute(mgr_stmt)).scalar_one_or_none():
                    store_short = store.name.replace("Nam An Market - ", "")
                    mgr_user = User(
                        username=mgr_username,
                        email=f"manager.{code}@namanmarket.com",
                        hashed_password=hash_password(effective_password),
                        full_name=f"Cửa Hàng Trưởng - {store_short}",
                        phone_number=store.phone,
                        role=UserRole.STORE_MANAGER.value,
                        store_id=store.id,
                        is_active=True,
                        is_superuser=False,
                    )
                    db.add(mgr_user)
                    created_accounts.append({
                        "username": mgr_username,
                        "full_name": mgr_user.full_name,
                        "role": mgr_user.role,
                        "store_code": store.code,
                        "store_name": store.name,
                    })

                # 3. Seed Staff for this store
                staff_username = f"staff_{code}"
                staff_stmt = select(User).where(User.username == staff_username)
                if not (await db.execute(staff_stmt)).scalar_one_or_none():
                    store_short = store.name.replace("Nam An Market - ", "")
                    staff_user = User(
                        username=staff_username,
                        email=f"staff.{code}@namanmarket.com",
                        hashed_password=hash_password(effective_password),
                        full_name=f"Nhân Viên - {store_short}",
                        phone_number=store.phone,
                        role=UserRole.STAFF.value,
                        store_id=store.id,
                        is_active=True,
                        is_superuser=False,
                    )
                    db.add(staff_user)
                    created_accounts.append({
                        "username": staff_username,
                        "full_name": staff_user.full_name,
                        "role": staff_user.role,
                        "store_code": store.code,
                        "store_name": store.name,
                    })

            await db.commit()
            logger.info(f"✅ Đã khởi tạo {len(created_accounts)} tài khoản nhân sự chi nhánh mới.")
            return created_accounts
        except Exception as e:
            logger.error(f"Lỗi khi khởi tạo tài khoản nhân viên chi nhánh: {str(e)}")
            await db.rollback()
            raise


auth_service = AuthService()
