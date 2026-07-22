from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Brink's Nexus"
    app_version: str = "1.0.0"

    database_url_core: str = "sqlite+aiosqlite:///./core.db"
    database_url_vms: str = "sqlite+aiosqlite:///./catalog_vms.db"
    database_url_dayshift: str = "sqlite+aiosqlite:///./catalog_dayshift.db"
    database_url_complete: str = "sqlite+aiosqlite:///./catalog_complete.db"
    database_url_esnf: str = "sqlite+aiosqlite:///./catalog_esnf.db"

    secret_key: str = "change-this-in-production-a-very-long-secret-key-for-jwt"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480

    debug: bool = True
    session_cookie_secure: bool = False

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
