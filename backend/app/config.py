"""Application settings, loaded from environment variables / .env file.

A collector is "configured" (and therefore scheduled) when its required settings
are present. Leave a source's settings empty to disable it.
"""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .secrets import resolve_secret

# Settings that may hold credentials — each accepts plaintext, file:<path>, or
# dpapi:<token> (see app/secrets.py; generate tokens with `app.cli protect`).
SECRET_FIELDS = [
    "app_db_url",
    "legacy_db_url",
    "veeam_password",
    "nable_password",
    "azure_client_secret",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application database -------------------------------------------------
    # SQLite by default so the app runs with zero setup; point at SQL Server in
    # production, e.g.:
    #   mssql+pyodbc://svc_backupdash:...@AZUSCCM01/BackupDashboard?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
    app_db_url: str = "sqlite:///./backupdashboard.db"

    # --- Web -------------------------------------------------------------------
    cors_origins: str = "http://localhost:5173"

    # --- Time ------------------------------------------------------------------
    # The timezone the dashboard is read in. Drives which night counts as "last
    # night" and how timestamps render by default.
    display_timezone: str = "America/New_York"
    # Local hour dividing one backup night from the next (see timeframes.py).
    # Servers may override individually.
    night_cutoff_hour: int = 12

    # --- Expectations ----------------------------------------------------------
    # A server with no event in this many days stops being expected nightly, so
    # decommissioned machines don't generate "No backup" rows forever.
    stale_server_days: int = 14
    # Retention for raw events. 0 keeps everything.
    event_retention_days: int = 400

    # --- Scheduler -------------------------------------------------------------
    scheduler_enabled: bool = True
    # Per-source poll intervals (minutes). 0 disables scheduling for a source
    # without removing its credentials (manual runs still work).
    veeam_interval: int = 60
    nable_interval: int = 60
    azure_interval: int = 60
    legacy_interval: int = 0  # backfill source: run it by hand

    # How far back each collector asks for. Generous by default: re-importing an
    # event is a no-op upsert, missing one is a hole in the history.
    veeam_lookback_hours: int = 96
    nable_lookback_hours: int = 96
    azure_lookback_hours: int = 96

    # --- Veeam Backup & Replication (REST API on port 9419) --------------------
    veeam_servers: str = ""  # comma-separated FQDNs
    veeam_username: str = ""
    veeam_password: str = ""
    veeam_api_version: str = "1.2-rev0"
    veeam_port: int = 9419
    # Veeam ships a self-signed cert by default; this is the documented escape.
    veeam_verify_tls: bool = False

    # --- N-able Cove (Backup Manager JSON-RPC) ---------------------------------
    nable_endpoint: str = "https://api.backup.management/jsonapi"
    nable_partner: str = ""
    nable_username: str = ""
    nable_password: str = ""
    nable_partner_id: int = 0

    # --- Azure Backup (ARM REST, client credentials) ---------------------------
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    # Comma-separated subscription IDs or display names; empty = every visible one.
    azure_subscriptions: str = ""

    # --- Legacy BackupReporting import -----------------------------------------
    # The SQL database the PowerShell scripts wrote to. Read-only, used to
    # backfill history that predates this app.
    #   mssql+pyodbc://svc_reader:...@AZUSCCM01/BackupReporting?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
    legacy_db_url: str = ""
    legacy_table: str = "dbo.BackupEvents"
    # Those scripts stored Eastern local time, not UTC. This is the timezone
    # their datetimes are in, so the importer can convert them back to UTC.
    legacy_stored_timezone: str = "America/New_York"

    @model_validator(mode="after")
    def _resolve_secrets(self):
        for field in SECRET_FIELDS:
            setattr(self, field, resolve_secret(getattr(self, field)))
        return self

    def veeam_server_list(self) -> list[str]:
        return [s.strip() for s in self.veeam_servers.split(",") if s.strip()]

    def azure_subscription_list(self) -> list[str]:
        return [s.strip() for s in self.azure_subscriptions.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
