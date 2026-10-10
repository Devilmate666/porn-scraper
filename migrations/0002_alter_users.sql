-- Migrate the OLD users table (email + login_codes) to the NEW one (username + password_hash + is_admin).
-- Safe to run on a fresh database (all columns / tables are missing -> ALTER TABLE fails silently, DROP TABLE IF EXISTS is a no-op).
-- Run it once against the live database after the schema in 0001_auth.sql changed:
--   wrangler d1 execute porn-archive-db --remote --file=migrations/0002_alter_users.sql
ALTER TABLE users ADD COLUMN username TEXT;
ALTER TABLE users ADD COLUMN password_hash TEXT;
ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;
DROP TABLE IF EXISTS login_codes;
-- the unique index on username is created by 0001_auth.sql on a fresh DB; make sure the old one is gone too
DROP INDEX IF EXISTS idx_users_username;