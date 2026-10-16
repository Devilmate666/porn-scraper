-- Admin audit log: who did what in the admin panel. Safe to run repeatedly.
CREATE TABLE IF NOT EXISTS admin_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  admin TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_log_ts ON admin_log(ts);
