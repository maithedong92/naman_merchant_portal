from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Application Settings loaded from Environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )

    # General App Config
    APP_NAME: str = "Nam An Merchant Portal"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "production"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 3002
    ALLOWED_ORIGINS: List[str] = [
        "https://ump.namanmarket.com"
    ]
    TRUSTED_PROXY_CIDRS: List[str] = [
        "127.0.0.1/32",
        "::1/128",
        "172.16.0.0/12",
    ]
    PUBLIC_BASE_URL: str = ""
    SECRETS_ENCRYPTION_KEY: str = ""

    # Database
    DATABASE_URL: str = Field(
        default="",
        description="Async PostgreSQL connection string (must be provided via .env)"
    )
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_ECHO: bool = False

    # Security & Authentication (must be supplied via .env)
    INTERNAL_API_SECRET: str = ""
    WEBHOOK_TIMEOUT_SECONDS: int = 15
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    MAX_FAILED_LOGIN_ATTEMPTS: int = 5
    ACCOUNT_LOCKOUT_MINUTES: int = 15

    # Initial SuperAdmin Seed Account (optional; skipped if INITIAL_ADMIN_PASSWORD is empty)
    INITIAL_ADMIN_USERNAME: str = "admin"
    INITIAL_ADMIN_PASSWORD: str = ""
    INITIAL_ADMIN_EMAIL: str = "admin@namanmarket.com"
    INITIAL_ADMIN_FULL_NAME: str = "Nam An System Administrator"

    # ShopeeFood Module Settings
    SHOPEEFOOD_ENABLED: bool = True
    SHOPEEFOOD_APP_ID: str = ""
    SHOPEEFOOD_APP_KEY: str = ""
    SHOPEEFOOD_BASE_URL: str = "https://gexternalapi.deliverynow.vn"
    SHOPEEFOOD_COUNTRY: str = "VN"
    SHOPEEFOOD_LANGUAGE: str = "vi"
    SHOPEEFOOD_MENU_USERNAME: str = ""
    SHOPEEFOOD_MENU_PASSWORD: str = ""
    SHOPEEFOOD_WEBHOOK_S2S_VERIFY_FALLBACK: bool = True

    # GrabMart Module Settings
    GRABMART_ENABLED: bool = True
    GRABMART_CLIENT_ID: str = ""
    GRABMART_CLIENT_SECRET: str = ""
    GRABMART_PARTNER_CLIENT_ID: str = "naman_grabmart_pos"
    GRABMART_PARTNER_CLIENT_SECRET: str = ""
    GRABMART_INBOUND_BEARER_TOKEN: str = ""
    GRABMART_OAUTH_URL: str = "https://api.grab.com/grabid/v1/oauth2/token"
    GRABMART_BASE_URL: str = "https://partner-api.grab.com"
    GRABMART_SCOPE: str = "mart.partner_api"
    GRABMART_COUNTRY: str = "VN"
    GRABMART_CURRENCY: str = "VND"

    # ShopeeMart / Shopee Open Platform Settings
    SHOPEEMART_ENABLED: bool = True
    SHOPEEMART_PARTNER_ID: str = ""
    SHOPEEMART_PARTNER_KEY: str = ""
    SHOPEEMART_SHOP_ID: str = ""
    SHOPEEMART_BASE_URL: str = "https://partner.shopeemobile.com"
    SHOPEEMART_POLL_ENABLED: bool = True
    SHOPEEMART_POLL_INTERVAL_SECONDS: int = 180

    # MongoDB & Haravan Omnichannel Sync Settings
    MONGO_URI: str = ""
    MONGO_DB: str = "pmdonhang"
    MONGO_ORDERS_COLLECTION: str = "donhang"
    ARITO_DB: str = "arito_database"
    ARITO_INVENTORY_COLLECTION: str = "cttk"

    # Internal MongoDB User Authentication (naman_internal_app.user)
    AUTH_MONGO_ENABLED: bool = True
    AUTH_MONGO_URI: str = ""
    AUTH_MONGO_DB: str = "naman_internal_app"
    AUTH_MONGO_COLLECTION: str = "user"

    # Scheduled Internal HR Sync Cronjob (UTC+7 Vietnam Time)
    HR_SYNC_ENABLED: bool = True
    HR_SYNC_SCHEDULE_HOURS_VN: str = "07:00,14:00"
    HR_SYNC_CHECK_INTERVAL_SECONDS: int = 30
    HR_SYNC_PURGE_DEMO_ACCOUNTS: bool = False

    HARAVAN_ENABLED: bool = True
    HARAVAN_TOKEN: str = ""
    HARAVAN_BASE_URL: str = "https://apis.haravan.com/com"
    HARAVAN_SYNC_INTERVAL_SECONDS: int = 60
    HARAVAN_SYNC_LIMIT: int = 100

    # Poppies ERP POS Master Data & Pricing Gateway
    POPPIES_API_BASE_URL: str = "http://host.docker.internal:6515/api/v1/pos/master-data"
    POPPIES_PRICING_BASE_URL: str = "http://host.docker.internal:6515/api/v1/pricing"
    POPPIES_POS_PRICING_BASE_URL: str = "http://host.docker.internal:6515/api/v1/pos/pricing"
    POPPIES_API_KEY: str = ""
    POPPIES_WEBHOOK_KEY: str = ""
    POPPIES_TIMEOUT_SECONDS: float = 120.0
    POPPIES_SYNC_ENABLED: bool = True
    POPPIES_SYNC_INTERVAL_SECONDS: int = 300

    # Virtual Stock Reservation Settings
    VIRTUAL_STOCK_STRICT_MODE: bool = False

    def validate_production_security(self) -> None:
        """Fail fast at startup if production configuration uses missing or placeholder secrets."""
        if self.ENVIRONMENT.lower() != "production" or self.DEBUG:
            return

        errors: List[str] = []
        placeholder_markers = ("change_in_production", "change_me", "your_")

        jwt_secret = (self.JWT_SECRET_KEY or "").strip()
        if (
            not jwt_secret
            or len(jwt_secret) < 32
            or any(marker in jwt_secret.lower() for marker in placeholder_markers)
        ):
            errors.append(
                "JWT_SECRET_KEY must be at least 32 characters and not use placeholder values"
            )

        internal_secret = (self.INTERNAL_API_SECRET or "").strip()
        if not internal_secret or any(
            marker in internal_secret.lower() for marker in ("change_me", "your_")
        ):
            errors.append(
                "INTERNAL_API_SECRET must be configured and not use placeholder values"
            )

        origins = [
            str(origin).strip()
            for origin in (self.ALLOWED_ORIGINS or [])
            if str(origin).strip()
        ]
        if not origins:
            errors.append("ALLOWED_ORIGINS must not be empty in production")
        else:
            for origin in origins:
                lower_origin = origin.lower()
                if "*" in lower_origin:
                    errors.append("ALLOWED_ORIGINS must not contain wildcard '*'")
                    break
                if (
                    "http://localhost" in lower_origin
                    or "http://127.0.0.1" in lower_origin
                ):
                    errors.append(
                        "ALLOWED_ORIGINS must not contain http://localhost or http://127.0.0.1 in production"
                    )
                    break

        if errors:
            raise RuntimeError(
                "Production security validation failed: " + "; ".join(errors)
            )


@lru_cache()
def get_settings() -> Settings:
    """Returns singleton instance of Settings cached across requests."""
    return Settings()
