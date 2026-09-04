import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, UnauthorizedError, ValidationError
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
    """Service handling User Authentication, Token Lifecycle, RBAC, and Security Auditing."""

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
        Authenticate user credentials with brute-force lockout protection.
        Issues JWT access token and rotating refresh token upon success.
        """
        now = datetime.now(timezone.utc)

        # Lookup user by username or email
        stmt = select(User).where(
            or_(
                User.username == username_or_email.strip(),
                User.email == username_or_email.strip().lower(),
            )
        )
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            # Fake verify password to mitigate timing attacks
            verify_password("fake_password_dummy", "$2b$12$e8YQ3d1hB5u7.wF36wGjveU78cT0fK5G3uB7G1YvD.Zq2hI4uX3Xy")
            await self.log_security_event(
                db=db,
                event_type="LOGIN_FAILED",
                username=username_or_email,
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

        # Check if user is active
        if not user.is_active:
            await self.log_security_event(
                db=db,
                event_type="LOGIN_BLOCKED_INACTIVE",
                user_id=user.id,
                username=user.username,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await db.commit()
            raise UnauthorizedError("Tài khoản người dùng đã bị vô hiệu hóa. Vui lòng liên hệ ban quản trị.")

        # Verify password
        if not verify_password(password, user.hashed_password):
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

        # Generate access and refresh tokens
        token_payload = {
            "sub": user.id,
            "username": user.username,
            "role": user.role,
            "store_id": user.store_id,
            "is_superuser": user.is_superuser,
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

        # 2. Check token in database
        hashed = hash_token(refresh_token_str)
        stmt = select(RefreshToken).where(RefreshToken.token_hash == hashed)
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
            revoke_all_stmt = select(RefreshToken).where(
                RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
            )
            all_tokens_result = await db.execute(revoke_all_stmt)
            for tok in all_tokens_result.scalars().all():
                tok.revoked_at = now

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

        # 3. Fetch user
        user_stmt = select(User).where(User.id == user_id)
        user_result = await db.execute(user_stmt)
        user = user_result.scalar_one_or_none()

        if not user or not user.is_active:
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
            "is_superuser": user.is_superuser,
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
            stmt = select(RefreshToken).where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked_at.is_(None),
            )
            result = await db.execute(stmt)
            for tok in result.scalars().all():
                tok.revoked_at = now

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
        """Change user password, invalidating all other active refresh sessions."""
        if not verify_password(current_password, user.hashed_password):
            await self.log_security_event(
                db=db,
                event_type="PASSWORD_CHANGE_FAILED",
                user_id=user.id,
                username=user.username,
                ip_address=ip_address,
                user_agent=user_agent,
                details={"reason": "INCORRECT_CURRENT_PASSWORD"},
            )
            await db.commit()
            raise ValidationError("Mật khẩu hiện tại không chính xác.")

        if current_password == new_password:
            raise ValidationError("Mật khẩu mới không được trùng với mật khẩu hiện tại.")

        now = datetime.now(timezone.utc)
        user.hashed_password = hash_password(new_password)

        # Invalidate all existing refresh tokens for security
        stmt = select(RefreshToken).where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
        result = await db.execute(stmt)
        for tok in result.scalars().all():
            tok.revoked_at = now

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

    async def create_user(self, db: AsyncSession, user_in: UserCreate) -> User:
        """Create a new portal user."""
        # Check uniqueness of username
        check_user = await db.execute(select(User).where(User.username == user_in.username))
        if check_user.scalar_one_or_none():
            raise ConflictError(f"Tên đăng nhập '{user_in.username}' đã tồn tại trong hệ thống.")

        # Check uniqueness of email
        check_email = await db.execute(select(User).where(User.email == user_in.email.lower()))
        if check_email.scalar_one_or_none():
            raise ConflictError(f"Email '{user_in.email}' đã được đăng ký cho tài khoản khác.")

        # If store_id provided, verify store exists
        if user_in.store_id:
            store_res = await db.execute(select(Store).where(Store.id == user_in.store_id))
            if not store_res.scalar_one_or_none():
                raise NotFoundError("Store", user_in.store_id)

        user = User(
            username=user_in.username.strip(),
            email=user_in.email.strip().lower(),
            hashed_password=hash_password(user_in.password),
            full_name=user_in.full_name.strip(),
            phone_number=user_in.phone_number.strip() if user_in.phone_number else None,
            role=user_in.role.value,
            store_id=user_in.store_id,
            is_active=True,
            is_superuser=(user_in.role == UserRole.SUPER_ADMIN),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info(f"Đã tạo người dùng mới: {user.username} ({user.role})")
        return user

    async def list_users(
        self,
        db: AsyncSession,
        role: Optional[UserRole] = None,
        store_id: Optional[str] = None,
        is_active: Optional[bool] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[User], int]:
        """List users with filtering and pagination."""
        query = select(User)
        if role:
            query = query.where(User.role == role.value)
        if store_id:
            query = query.where(User.store_id == store_id)
        if is_active is not None:
            query = query.where(User.is_active == is_active)

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
        stmt = select(User).where(User.id == user_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            raise NotFoundError("User", user_id)
        return user

    async def update_user(self, db: AsyncSession, user_id: str, user_in: UserUpdate) -> User:
        """Update user profile or permissions."""
        user = await self.get_user_by_id(db, user_id)

        if user_in.email is not None and user_in.email.lower() != user.email:
            check_email = await db.execute(
                select(User).where(User.email == user_in.email.lower(), User.id != user_id)
            )
            if check_email.scalar_one_or_none():
                raise ConflictError(f"Email '{user_in.email}' đã được sử dụng bởi người dùng khác.")
            user.email = user_in.email.lower()

        if user_in.full_name is not None:
            user.full_name = user_in.full_name.strip()

        if user_in.phone_number is not None:
            user.phone_number = user_in.phone_number.strip() if user_in.phone_number else None

        if user_in.role is not None:
            user.role = user_in.role.value
            user.is_superuser = (user_in.role == UserRole.SUPER_ADMIN)

        if user_in.store_id is not None:
            if user_in.store_id:
                store_res = await db.execute(select(Store).where(Store.id == user_in.store_id))
                if not store_res.scalar_one_or_none():
                    raise NotFoundError("Store", user_in.store_id)
                user.store_id = user_in.store_id
            else:
                user.store_id = None

        if user_in.is_active is not None:
            user.is_active = user_in.is_active

        if user_in.password:
            user.hashed_password = hash_password(user_in.password)
            # Revoke refresh tokens on admin password reset
            now = datetime.now(timezone.utc)
            stmt = select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
            result = await db.execute(stmt)
            for tok in result.scalars().all():
                tok.revoked_at = now

        await db.commit()
        await db.refresh(user)
        return user

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

    async def seed_initial_superadmin(self, db: AsyncSession) -> Optional[User]:
        """
        Idempotent function that seeds default initial superadmin if no superadmin exists.
        Invoked automatically in application lifespan startup.
        """
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


auth_service = AuthService()
