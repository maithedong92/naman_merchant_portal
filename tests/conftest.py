import os

# Set test environment defaults BEFORE importing app modules
os.environ["ENVIRONMENT"] = "test"
os.environ["DEBUG"] = "True"
os.environ["JWT_SECRET_KEY"] = "pytest_super_secure_jwt_secret_key_32bytes_min"
os.environ["POPPIES_WEBHOOK_KEY"] = "naman_pos_api_secret_key_2026"
os.environ["SHOPEEFOOD_MENU_USERNAME"] = "namanmarket"
os.environ["SHOPEEFOOD_MENU_PASSWORD"] = "shopeefoodapi"
os.environ["GRABMART_PARTNER_CLIENT_ID"] = "naman_grabmart_pos"
os.environ["GRABMART_PARTNER_CLIENT_SECRET"] = "grabmart_secret_123"
os.environ["GRABMART_CLIENT_SECRET"] = ""

from app.core.config import get_settings

get_settings.cache_clear()
