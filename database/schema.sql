CREATE TABLE IF NOT EXISTS eta_sessions (
    message_id       INTEGER PRIMARY KEY,
    channel_id       INTEGER NOT NULL,
    guild_id         INTEGER NOT NULL,
    origin           TEXT NOT NULL,
    destination      TEXT NOT NULL,
    mode             TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    requested_by_id  INTEGER NOT NULL,
    show_stop_button INTEGER NOT NULL DEFAULT 0
);
