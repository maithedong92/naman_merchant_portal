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
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
        "https://portal.namanmarket.com"
    ]

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/naman_merchant_portal",
        description="Async PostgreSQL connection string"
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # Security
    INTERNAL_API_SECRET: str = "naman_secure_internal_secret_change_me"
    WEBHOOK_TIMEOUT_SECONDS: int = 15

    # ShopeeFood Module Settings
    SHOPEEFOOD_ENABLED: bool = True
    SHOPEEFOOD_APP_ID: str = "10045"
    SHOPEEFOOD_APP_KEY: str = "a9756768d72268a6d66ea886031988f0638f03b0036fc63fbe0b86a6aef18546"
    SHOPEEFOOD_BASE_URL: str = "https://gexternalapi.deliverynow.vn"
    SHOPEEFOOD_COUNTRY: str = "VN"
    SHOPEEFOOD_LANGUAGE: str = "vi"

    # GrabMart Module Settings
    GRABMART_ENABLED: bool = True
    GRABMART_CLIENT_ID: str = ""
    GRABMART_CLIENT_SECRET: str = ""
    GRABMART_BASE_URL: str = "https://partner-api.grab.com"
    GRABMART_SCOPE: str = "grabmart.partner.pos"
    GRABMART_COUNTRY: str = "VN"
    GRABMART_CURRENCY: str = "VND"

    # ShopeeMart / Shopee Open Platform Settings
    SHOPEEMART_ENABLED: bool = True
    SHOPEEMART_PARTNER_ID: str = ""
    SHOPEEMART_PARTNER_KEY: str = ""
    SHOPEEMART_SHOP_ID: str = ""
    SHOPEEMART_BASE_URL: str = "https://partner.shopeemobile.com"


@lru_cache()
def get_settings() -> Settings:
    """Returns singleton instance of Settings cached across requests."""
    return Settings()
