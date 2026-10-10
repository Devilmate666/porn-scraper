-- Accounts, sessions, synced items, rate counters (Cloudflare D1 / SQLite). Safe to run repeatedly.
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created INTEGER NOT NULL,
  last_login INTEGER NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL,
  ua TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created);
CREATE TABLE IF NOT EXISTS user_items (
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  id TEXT NOT NULL,
  data TEXT NOT NULL,
  ts INTEGER NOT NULL,        -- the client's timestamp of the change: last writer wins per item
  deleted INTEGER NOT NULL DEFAULT 0,
  updated INTEGER NOT NULL,   -- server order (ms * 1000 + n): the pull cursor
  PRIMARY KEY (user_id, kind, id)
);
CREATE INDEX IF NOT EXISTS idx_items_pull ON user_items(user_id, kind, updated);
CREATE TABLE IF NOT EXISTS rate (
  k TEXT PRIMARY KEY,
  n INTEGER NOT NULL,
  reset INTEGER NOT NULL
);
