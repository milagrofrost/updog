CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    reporter TEXT NOT NULL,
    hour INTEGER NOT NULL,
    PRIMARY KEY (reporter, hour)
);

CREATE INDEX IF NOT EXISTS reports_hour ON reports (hour);
