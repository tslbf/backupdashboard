/* ===========================================================================
   Backup Status Dashboard — SQL Server setup for AZUSCCM01

   Creates the app's own database, its schema, and a least-privilege account,
   and grants that account read access to the legacy BackupReporting table for
   the one-time history import.

   Run it in SSMS against AZUSCCM01 as an account with sysadmin or dbcreator +
   securityadmin. Every step is guarded, so re-running is safe.

   You do NOT have to run the table section: the app calls create_all() at
   startup and will build the schema itself. Doing it here buys one thing —
   the app account then never needs DDL rights (see the note in section 4).

   The DDL below is what SQLAlchemy emits for these models, with three
   deliberate substitutions: NVARCHAR instead of VARCHAR (collation safety),
   DATETIME2(3) instead of DATETIME (wider range, finer precision), and
   NVARCHAR(MAX) instead of the deprecated TEXT. All three read and write
   identically through pyodbc.
   =========================================================================== */

SET NOCOUNT ON;
GO

/* ---------------------------------------------------------------------------
   1. The database

   This is the app's OWN database. It is not BackupReporting — that one stays
   exactly as it is, and this app only ever reads it.
   --------------------------------------------------------------------------- */
IF DB_ID(N'BackupDashboard') IS NULL
BEGIN
    CREATE DATABASE [BackupDashboard];
    PRINT 'Created database BackupDashboard.';
END
ELSE
    PRINT 'Database BackupDashboard already exists — leaving it alone.';
GO

/* Optional. This is a reporting store, rebuildable from the vendor APIs and the
   legacy import, so point-in-time recovery may not be worth the log management.
   Left commented because it changes the backup posture of a database on your
   server — your policy decides, not this script. */
-- ALTER DATABASE [BackupDashboard] SET RECOVERY SIMPLE;
-- GO

/* ---------------------------------------------------------------------------
   2. The login

   Pick ONE of A / B / C and delete the rest.

   A and B are Windows authentication, which is what the three PowerShell
   scripts already use (Integrated Security=True) and what the app's example
   connection string assumes. There is no password to store or rotate.

   Whichever you choose has to be the account the dashboard actually RUNS as.
   Running it by hand from your own session and running it later under NSSM are
   two different accounts — set up the one you will use in production.
   --------------------------------------------------------------------------- */

-- A. Domain service account (recommended if you have one).
--    Matches:  APP_DB_URL=mssql+pyodbc://@AZUSCCM01/BackupDashboard?...
-- IF SUSER_ID(N'LBFOSTERCO\svc_backupdash') IS NULL
--     CREATE LOGIN [LBFOSTERCO\svc_backupdash] FROM WINDOWS;
-- GO

-- B. The machine account — use this when the app runs as a Windows service
--    under Local System or Network Service on AZUSCCM01 itself. The trailing $
--    is part of the name, not a typo.
-- IF SUSER_ID(N'LBFOSTERCO\AZUSCCM01$') IS NULL
--     CREATE LOGIN [LBFOSTERCO\AZUSCCM01$] FROM WINDOWS;
-- GO

-- C. A local SQL login. Only if Windows auth is not an option: the password
--    then has to live in the connection string, and any @ : / ? in it must be
--    percent-encoded in APP_DB_URL (@ becomes %40, : becomes %3A).
--    CHECK_POLICY follows the machine's password policy.
IF SUSER_ID(N'svc_backupdash') IS NULL
BEGIN
    CREATE LOGIN [svc_backupdash]
        WITH PASSWORD = N'CHANGE-THIS-BEFORE-RUNNING',
             DEFAULT_DATABASE = [BackupDashboard],
             CHECK_EXPIRATION = OFF,
             CHECK_POLICY = ON;
    PRINT 'Created SQL login svc_backupdash.';
END
GO

/* NOTE: sections 3 and 7 use the name `svc_backupdash`. If you picked option A
   or B, find/replace that with the Windows account name — e.g.
   [LBFOSTERCO\svc_backupdash] — throughout the rest of this script. */

/* ---------------------------------------------------------------------------
   3. The user, inside BackupDashboard
   --------------------------------------------------------------------------- */
USE [BackupDashboard];
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'svc_backupdash')
BEGIN
    CREATE USER [svc_backupdash] FOR LOGIN [svc_backupdash];
    PRINT 'Created user svc_backupdash in BackupDashboard.';
END
GO

ALTER ROLE [db_datareader] ADD MEMBER [svc_backupdash];
ALTER ROLE [db_datawriter] ADD MEMBER [svc_backupdash];
GO

/* ---------------------------------------------------------------------------
   4. DDL rights — a decision, not a formality

   With the tables pre-created below, the app needs no DDL rights at all:
   create_all() finds them and does nothing.

   But the app also runs additive micro-migrations at startup (db._ensure_columns
   issues ALTER TABLE ... ADD for columns a newer version introduced). Without
   db_ddladmin, a future upgrade that adds a column will fail on startup with a
   permissions error until someone runs the ALTER by hand.

   Uncomment to let the app manage its own schema. Leave it commented to keep
   the account read/write only and handle upgrades deliberately.
   --------------------------------------------------------------------------- */
-- ALTER ROLE [db_ddladmin] ADD MEMBER [svc_backupdash];
-- GO

/* ---------------------------------------------------------------------------
   5. Tables

   Optional — the app creates these itself on first start. Pre-creating them is
   what lets you skip db_ddladmin above.
   --------------------------------------------------------------------------- */

/* One canonical row per protected machine, whichever tool backs it up. */
IF OBJECT_ID(N'dbo.servers', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.servers (
        id                INT            IDENTITY(1,1) NOT NULL,
        name              NVARCHAR(128)  NOT NULL,   -- short, uppercase, domain-stripped
        display_name      NVARCHAR(255)  NULL,       -- what the vendor actually said
        primary_source    NVARCHAR(16)   NULL,       -- decides the inherited timezone
        timezone          NVARCHAR(64)   NULL,       -- NULL = inherit from primary_source
        night_cutoff_hour INT            NULL,       -- NULL = use the app default
        expected          BIT            NOT NULL,
        hidden            BIT            NOT NULL,
        notes             NVARCHAR(MAX)  NULL,
        last_event_utc    DATETIME2(3)   NULL,
        last_success_utc  DATETIME2(3)   NULL,
        first_seen        DATETIME2(3)   NOT NULL,
        updated_at        DATETIME2(3)   NOT NULL,
        CONSTRAINT PK_servers PRIMARY KEY CLUSTERED (id)
    );
    CREATE UNIQUE INDEX ix_servers_name ON dbo.servers (name);
    CREATE INDEX ix_servers_last_event_utc ON dbo.servers (last_event_utc);
    PRINT 'Created dbo.servers.';
END
GO

/* Per-source display settings and the timezone its servers default to. */
IF OBJECT_ID(N'dbo.source_config', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.source_config (
        source           NVARCHAR(16) NOT NULL,
        display_name     NVARCHAR(64) NOT NULL,
        default_timezone NVARCHAR(64) NOT NULL,
        expected         BIT          NOT NULL,
        sort_order       INT          NOT NULL,
        CONSTRAINT PK_source_config PRIMARY KEY CLUSTERED (source)
    );
    PRINT 'Created dbo.source_config.';
END
GO

/* One backup job run against one server.
   The unique constraint is the same natural key the PowerShell MERGE used, so a
   collector run and a legacy import of the same job converge on one row rather
   than duplicating it. */
IF OBJECT_ID(N'dbo.backup_events', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.backup_events (
        id                INT            IDENTITY(1,1) NOT NULL,
        server_id         INT            NOT NULL,
        source            NVARCHAR(16)   NOT NULL,
        job_name          NVARCHAR(512)  NULL,
        start_utc         DATETIME2(3)   NOT NULL,
        end_utc           DATETIME2(3)   NOT NULL,
        duration_sec      INT            NULL,
        outcome           NVARCHAR(16)   NOT NULL,   -- canonical (see outcomes.py)
        result_raw        NVARCHAR(64)   NULL,       -- exactly what the vendor said
        report_date       NVARCHAR(10)   NOT NULL,   -- the backup NIGHT, server-local
        bytes_transferred BIGINT         NULL,
        native_id         NVARCHAR(128)  NULL,
        details           NVARCHAR(MAX)  NULL,       -- JSON
        inserted_utc      DATETIME2(3)   NOT NULL,
        updated_at        DATETIME2(3)   NOT NULL,
        CONSTRAINT PK_backup_events PRIMARY KEY CLUSTERED (id),
        CONSTRAINT uq_event_source_server_start UNIQUE (source, server_id, start_utc),
        CONSTRAINT FK_backup_events_servers FOREIGN KEY (server_id)
            REFERENCES dbo.servers (id)
    );
    CREATE INDEX ix_backup_events_server_id   ON dbo.backup_events (server_id);
    CREATE INDEX ix_backup_events_source      ON dbo.backup_events (source);
    CREATE INDEX ix_backup_events_outcome     ON dbo.backup_events (outcome);
    CREATE INDEX ix_backup_events_end_utc     ON dbo.backup_events (end_utc);
    CREATE INDEX ix_backup_events_report_date ON dbo.backup_events (report_date);
    CREATE INDEX ix_events_report_date_source ON dbo.backup_events (report_date, source);
    CREATE INDEX ix_events_server_end         ON dbo.backup_events (server_id, end_utc);
    PRINT 'Created dbo.backup_events.';
END
GO

/* One server's verdict for one backup night, per source.

   Materialized rather than computed per request for one reason: "no backup"
   cannot be derived from rows that do not exist. This table has a row for every
   (night, server, source) that SHOULD have run, so a silent absence becomes a
   queryable outcome instead of nothing at all. */
IF OBJECT_ID(N'dbo.server_days', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.server_days (
        id           INT          IDENTITY(1,1) NOT NULL,
        report_date  NVARCHAR(10) NOT NULL,
        server_id    INT          NOT NULL,
        source       NVARCHAR(16) NOT NULL,
        outcome      NVARCHAR(16) NOT NULL,
        event_count  INT          NOT NULL,
        duration_sec INT          NULL,        -- longest run of the night
        end_utc      DATETIME2(3) NULL,
        result_raw   NVARCHAR(64) NULL,
        computed_at  DATETIME2(3) NOT NULL,
        CONSTRAINT PK_server_days PRIMARY KEY CLUSTERED (id),
        CONSTRAINT uq_serverday UNIQUE (report_date, server_id, source),
        CONSTRAINT FK_server_days_servers FOREIGN KEY (server_id)
            REFERENCES dbo.servers (id)
    );
    CREATE INDEX ix_server_days_report_date  ON dbo.server_days (report_date);
    CREATE INDEX ix_server_days_server_id    ON dbo.server_days (server_id);
    CREATE INDEX ix_server_days_outcome      ON dbo.server_days (outcome);
    CREATE INDEX ix_serverday_date_outcome   ON dbo.server_days (report_date, outcome);
    PRINT 'Created dbo.server_days.';
END
GO

/* Run log, one row per collector execution — this is what the Collectors page
   reads to answer "is the data on screen current?". */
IF OBJECT_ID(N'dbo.collector_runs', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.collector_runs (
        id          INT           IDENTITY(1,1) NOT NULL,
        source      NVARCHAR(16)  NOT NULL,
        started_at  DATETIME2(3)  NOT NULL,
        finished_at DATETIME2(3)  NULL,
        status      NVARCHAR(16)  NOT NULL,   -- running | success | error
        records     INT           NOT NULL,
        message     NVARCHAR(MAX) NULL,
        CONSTRAINT PK_collector_runs PRIMARY KEY CLUSTERED (id)
    );
    CREATE INDEX ix_collector_runs_source ON dbo.collector_runs (source);
    PRINT 'Created dbo.collector_runs.';
END
GO

/* ---------------------------------------------------------------------------
   6. Seed the source config

   The app seeds these on every startup if they are missing, so this is only
   here to make the script self-sufficient.

   N-able Cove defaults to Europe/London because that estate is in the UK — this
   is what makes a 23:00 London backup land in the same review as a 23:00
   Eastern one. Change it per server from the UI, not here.
   --------------------------------------------------------------------------- */
MERGE dbo.source_config AS target
USING (VALUES
    (N'veeam',  N'Veeam',          N'America/New_York', 1, 1),
    (N'nable',  N'N-able Cove',    N'Europe/London',    1, 2),
    (N'azure',  N'Azure Backup',   N'America/New_York', 1, 3),
    (N'legacy', N'Legacy import',  N'America/New_York', 0, 9)
) AS src (source, display_name, default_timezone, expected, sort_order)
   ON target.source = src.source
WHEN NOT MATCHED BY TARGET THEN
    INSERT (source, display_name, default_timezone, expected, sort_order)
    VALUES (src.source, src.display_name, src.default_timezone, src.expected, src.sort_order);
GO

/* ---------------------------------------------------------------------------
   7. Read access to the legacy database

   For the one-time history import (python -m app.cli collect legacy). Read
   only — the app never writes to BackupReporting, and the three PowerShell
   scripts keep working exactly as they do now until you retire them.
   --------------------------------------------------------------------------- */
USE [BackupReporting];
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'svc_backupdash')
BEGIN
    CREATE USER [svc_backupdash] FOR LOGIN [svc_backupdash];
    PRINT 'Created user svc_backupdash in BackupReporting.';
END
GO

/* Narrower than db_datareader: only the one table the importer reads. */
GRANT SELECT ON dbo.BackupEvents TO [svc_backupdash];
GO

/* ---------------------------------------------------------------------------
   8. Check it

   Run this as the service account (SSMS: connect as that login) to confirm the
   grants actually landed.
   --------------------------------------------------------------------------- */
USE [BackupDashboard];
GO
SELECT
    DB_NAME()                                        AS database_name,
    USER_NAME()                                      AS connected_as,
    HAS_PERMS_BY_NAME(N'dbo.servers', N'OBJECT', N'SELECT') AS can_read,
    HAS_PERMS_BY_NAME(N'dbo.servers', N'OBJECT', N'INSERT') AS can_write;
GO
SELECT name AS created_table FROM sys.tables ORDER BY name;
GO

/* ---------------------------------------------------------------------------
   Then, in backend\.env:

     APP_DB_URL=mssql+pyodbc://@AZUSCCM01/BackupDashboard?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
     LEGACY_DB_URL=mssql+pyodbc://@AZUSCCM01/BackupReporting?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
     LEGACY_TABLE=dbo.BackupEvents

   ...for Windows authentication (options A or B above). For the SQL login in
   option C, put the credentials in instead:

     APP_DB_URL=mssql+pyodbc://svc_backupdash:PASSWORD@AZUSCCM01/BackupDashboard?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes

   Then:  update-and-run.cmd
   Then:  python -m app.cli collect legacy  &&  python -m app.cli refresh
   --------------------------------------------------------------------------- */
