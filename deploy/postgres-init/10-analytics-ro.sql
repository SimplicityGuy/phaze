-- Provision the read-only analytics role (phaze-euo4a). Run as a role that may create roles (the cluster superuser),
-- BEFORE phaze starts on a release at or past migration 095: phaze's migrations grant to this role and never create it,
-- and 095 refuses to run while it is absent. Idempotent: safe to run on every deploy.
--
-- The repo's own compose stack mounts this directory at /docker-entrypoint-initdb.d, so a FRESH data volume gets the
-- role at first start; an existing volume does not (initdb scripts run once) and needs it run by hand. The password is
-- deliberately not set here: set it out of band, `ALTER ROLE analytics_ro PASSWORD '...'`.
-- See docs/analytics-read-only-access.md, "Provisioning the role".
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
        CREATE ROLE analytics_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_db_role_setting s
        JOIN pg_roles r ON r.oid = s.setrole
        WHERE r.rolname = 'analytics_ro' AND s.setdatabase = 0 AND 'default_transaction_read_only=on' = ANY (s.setconfig)
    ) THEN
        ALTER ROLE analytics_ro SET default_transaction_read_only = on;
    END IF;
END
$$;
