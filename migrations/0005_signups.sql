-- One account per network: remembers (hashed IP, time) of each registration. Safe to run repeatedly.
CREATE TABLE IF NOT EXISTS signups (
  ip_hash TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signups_ip ON signups(ip_hash, ts);
