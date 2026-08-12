/* ===========================================================================
   Grant one account access to the dashboard databases.

   Run this once per account that needs it. You will need it at least twice:

     1. YOUR OWN account, while you are running update-and-run.cmd by hand.
     2. The SERVICE account, once it runs under NSSM.

   With Windows authentication the connection is made by whichever account the
   app process runs as. That is the whole of the 18456 / 4060 error: the login
   reached the server but has no user inside the requested database. Setting up
   `svc_backupdash` does nothing for a dashboard you are launching interactively
   as yourself.

   HOW TO USE: replace the account on the next line, then run the whole file.
   =========================================================================== */

USE [master];
GO

-- ▼▼▼ CHANGE THIS ▼▼▼  ------------------------------------------------------
--   Windows account:  LBFOSTERCO\tscott   or   LBFOSTERCO\svc_backupdash
--   Machine account:  LBFOSTERCO\AZUSCCM01$     (service as Local System)
--   SQL login:        svc_backupdash
DECLARE @account sysname = N'LBFOSTERCO\tscott';
-- ▲▲▲ CHANGE THIS ▲▲▲  ------------------------------------------------------

DECLARE @sql nvarchar(max);
DECLARE @quoted nvarchar(300) = QUOTENAME(@account);

IF DB_ID(N'BackupDashboard') IS NULL
BEGIN
    RAISERROR(N'BackupDashboard does not exist yet — run create-database.sql first.', 16, 1);
    RETURN;
END

/* --- server-level login ---------------------------------------------------
   Only for Windows accounts. A SQL login has to be created with a password, so
   create-database.sql handles that case. */
IF SUSER_ID(@account) IS NULL
BEGIN
    IF @account LIKE N'%\%'
    BEGIN
        SET @sql = N'CREATE LOGIN ' + @quoted + N' FROM WINDOWS;';
        EXEC sp_executesql @sql;
        PRINT 'Created Windows login ' + @account;
    END
    ELSE
    BEGIN
        RAISERROR(N'No such login, and it is not a Windows account — create the SQL login first (see create-database.sql section 2C).', 16, 1);
        RETURN;
    END
END
ELSE
    PRINT 'Login already exists: ' + @account;

/* --- the app's own database -----------------------------------------------
   db_ddladmin is included because the app creates its tables on first start
   and applies additive column migrations on upgrade. Drop that one line if you
   pre-created the schema and want a strictly read/write account. */
SET @sql = N'
USE [BackupDashboard];
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = ' + QUOTENAME(@account, '''') + N')
    CREATE USER ' + @quoted + N' FOR LOGIN ' + @quoted + N';
ALTER ROLE [db_datareader] ADD MEMBER ' + @quoted + N';
ALTER ROLE [db_datawriter] ADD MEMBER ' + @quoted + N';
ALTER ROLE [db_ddladmin]   ADD MEMBER ' + @quoted + N';';
EXEC sp_executesql @sql;
PRINT 'Granted read/write/ddl on BackupDashboard to ' + @account;

/* --- the legacy database, read only ---------------------------------------
   Only needed for `python -m app.cli collect legacy`. The app never writes
   here, and the PowerShell scripts keep working untouched. */
IF DB_ID(N'BackupReporting') IS NULL
    PRINT 'BackupReporting not found on this instance — skipping the legacy grant.';
ELSE
BEGIN
    SET @sql = N'
USE [BackupReporting];
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = ' + QUOTENAME(@account, '''') + N')
    CREATE USER ' + @quoted + N' FOR LOGIN ' + @quoted + N';
GRANT SELECT ON dbo.BackupEvents TO ' + @quoted + N';';
    EXEC sp_executesql @sql;
    PRINT 'Granted SELECT on BackupReporting.dbo.BackupEvents to ' + @account;
END
GO

/* ---------------------------------------------------------------------------
   Verify. Run this AS the account you just granted — in SSMS, connect with
   that login rather than your own, or the answer is about you instead.
   --------------------------------------------------------------------------- */
USE [BackupDashboard];
GO
SELECT
    USER_NAME()                       AS connected_as,
    DB_NAME()                         AS current_database,
    IS_ROLEMEMBER(N'db_datareader')   AS can_read,
    IS_ROLEMEMBER(N'db_datawriter')   AS can_write,
    IS_ROLEMEMBER(N'db_ddladmin')     AS can_create_tables;
GO
