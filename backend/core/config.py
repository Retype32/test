from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Brink's Nexus"
    app_version: str = "1.0.0"

    # Toggle the product's real name and logo throughout the UI. Off = a
    # generic, unbranded interface; the name and the logo templates stay in
    # place, just unused, for whenever this is switched back on.
    branding_enabled: bool = False

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

    # "development" (default) or "production", read from ENVIRONMENT.
    # Every hardening check added for Phase 4 (SECRET_KEY validation,
    # auto-seed gating, CSRF enforcement, HSTS, startup secure-cookie
    # enforcement) is gated on this flag being exactly "production" -- the
    # default keeps today's permissive local/dev/test behavior completely
    # unchanged. See docs/production_readiness/06_consolidated_plan.md §13
    # (rollback strategy): production posture is opt-in, never silently
    # flipped on in dev.
    environment: str = "development"

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def display_name(self) -> str:
        """The name actually shown in the UI -- the real app_name, unless
        branding is deactivated, in which case a generic placeholder."""
        return self.app_name if self.branding_enabled else "Cash Processing"


settings = Settings()
