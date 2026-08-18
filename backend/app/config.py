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
    "smtp_password",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application database -------------------------------------------------
    # SQLite by default so the app runs with zero setup; point at SQL Server in
    # production, e.g.:
    #   mssql+pyodbc://@AZUSCCM01/BackupDashboard?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
    # An empty user slot means Windows auth; SQLAlchemy supplies
    # Trusted_Connection itself. This is the app's own database, not the legacy
    # BackupReporting one.
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
    # The one time a day every configured collector runs, HH:MM in
    # DISPLAY_TIMEZONE. Backups finish overnight and the dashboard is read in
    # the morning, so one run before the working day is what the data actually
    # needs; polling hourly re-asks a question whose answer changed once.
    # Blank disables the daily run entirely (manual and interval only).
    # "Run now" on the Collectors page works regardless.
    collect_time: str = "08:00"
    # Extra polling on top of the daily run, per source, in minutes.
    # 0 means no polling — the daily run and manual runs only.
    veeam_interval: int = 0
    azure_interval: int = 0
    # Cove is the exception, and deliberately so. Its API reports only each
    # device's *latest* session, so a poll that does not happen loses that night
    # permanently — there is no history endpoint to go back for it. One missed
    # daily run is one night gone. Polling every four hours means a single
    # failure costs nothing: a later poll the same day still sees the same last
    # session. The call is one cheap paged enumeration, nothing like Azure's
    # thousands of ARM requests, so the extra polls are close to free.
    nable_interval: int = 240
    # The backfill source. Never scheduled: it walks the whole historical table.
    legacy_interval: int = 0
    legacy_scheduled: bool = False

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
    # The single TLS protocol to offer — 1.0, 1.1, 1.2, 1.3, or "auto" to leave
    # OpenSSL's own range alone. Same idea as the PowerShell's
    # `ServicePointManager.SecurityProtocol = Tls12`: name one and only that one
    # is offered, because an older Schannel resets the connection rather than
    # negotiating down from a hello it cannot parse.
    # `python -m app.cli probe veeam` names the one an appliance accepts.
    veeam_tls_version: str = "1.2"

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
    # Azure Backup for SQL Server / SAP HANA inside a VM takes a transaction-log
    # backup every 15 minutes, per database, and each one is a separate job with
    # operation "Backup" — indistinguishable from the nightly run in the ARM
    # filter. Sixty databases produce roughly 23,000 log jobs in a 96-hour
    # window, which is two orders of magnitude more than the nightly backups
    # this dashboard exists to report on, and they would bury them.
    #
    # Set true if a failed log backup is something you want on the dashboard.
    # Expect the event count, and every collection, to grow accordingly.
    azure_include_log_backups: bool = False

    # --- Morning digest --------------------------------------------------------
    # Sent after the daily collection finishes. The dashboard only works if you
    # visit it; the scripts this replaced pushed to an inbox, and that is the
    # capability the rewrite would otherwise have lost.
    #
    # Blank SMTP_HOST or SMTP_TO disables it entirely.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""  # comma-separated
    # STARTTLS on 587 (the usual), implicit TLS on 465, neither on an open relay.
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    # "daily"    — send every morning, clean or not. The email is then also a
    #              heartbeat: one that does not arrive is itself the alert, which
    #              is the same reasoning as materializing "No backup" rows.
    # "problems" — only when something needs attention.
    notify_when: str = "daily"
    # Prefix for the subject line, so a mail rule can catch it.
    notify_subject_prefix: str = "Backups"
    # The URL that reaches this dashboard from a reader's desk. Blank derives it
    # from the machine's own hostname, because "localhost" in an email is a link
    # that works only for the one person who does not need it.
    dashboard_url: str = ""

    # --- Legacy BackupReporting import -----------------------------------------
    # The SQL database the PowerShell scripts wrote to. Read-only, used to
    # backfill history that predates this app.
    #   mssql+pyodbc://@AZUSCCM01/BackupReporting?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
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

    def public_url(self) -> str:
        import socket

        if self.dashboard_url.strip():
            return self.dashboard_url.strip().rstrip("/")
        return f"http://{socket.gethostname()}:8010"

    def notify_recipients(self) -> list[str]:
        return [a.strip() for a in self.smtp_to.replace(";", ",").split(",") if a.strip()]

    def notify_configured(self) -> bool:
        return bool(self.smtp_host and self.notify_recipients())

    def notify_sender(self) -> str:
        """Falls back to the first recipient — some relays reject an empty From,
        and a digest arriving from yourself is better than not arriving."""
        return self.smtp_from.strip() or (self.notify_recipients() or [""])[0]

    def collect_time_parts(self) -> tuple[int, int] | None:
        """(hour, minute) for the daily run, or None if there isn't one.

        A malformed value disables the daily run rather than crashing the app at
        startup — but says so, because silently never collecting is exactly the
        failure this dashboard exists to catch.
        """
        raw = (self.collect_time or "").strip()
        if not raw:
            return None
        try:
            hour, _, minute = raw.partition(":")
            parts = (int(hour), int(minute or 0))
        except ValueError:
            return None
        if not (0 <= parts[0] <= 23 and 0 <= parts[1] <= 59):
            return None
        return parts


@lru_cache
def get_settings() -> Settings:
    return Settings()
