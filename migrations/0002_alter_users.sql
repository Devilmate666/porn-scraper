-- Manual migration: migrate an OLD users table (email + login_codes) to the new schema
-- (username + password_hash + is_admin). Run ONCE by hand, only when the old schema is present:
--   wrangler d1 execute porn-archive-db --remote --file=migrations/0002_alter_users.sql
-- The live database is already on the new schema, so this file is kept for reference and is a
-- no-op here. The deploy workflow runs it too, but tolerates the failure (|| echo "::warning::").
-- On a fresh database 0001_auth.sql creates the correct schema, so this file is never needed.
SELECT 1 AS already_migrated;