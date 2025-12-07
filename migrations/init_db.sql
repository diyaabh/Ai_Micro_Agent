-- 🧍 User Table
CREATE TABLE IF NOT EXISTS "user" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    chat_id TEXT UNIQUE,
    timezone TEXT
);


-- 🗓️ Task Table (for reminders, summaries, etc.)
CREATE TABLE IF NOT EXISTS task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT,
    params_json TEXT,
    schedule_rule TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);


-- 🧾 Run Table (for logging task executions)
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    started_at TEXT,
    ended_at TEXT,
    ok INTEGER,
    outputs_json TEXT,
    error_text TEXT,
    attempt INTEGER DEFAULT 1
);

-- 👥 User Registry (Telegram user details)
CREATE TABLE IF NOT EXISTS user_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT UNIQUE,
    name TEXT,
    username TEXT,
    last_seen TEXT
);

-- 🛍️ Order Status (Buyer ↔ Store transactions)
CREATE TABLE IF NOT EXISTS order_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_chat_id TEXT,
    store_chat_id TEXT,
    store_name TEXT,
    item TEXT,
    status TEXT,
    created_at TEXT,
    updated_at TEXT
);

-- 💬 Order Chat Session (Buyer–Store live chat)
CREATE TABLE IF NOT EXISTS order_chat_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    buyer_chat_id TEXT,
    store_chat_id TEXT,
    active INTEGER DEFAULT 1
);

-- Notes table: simple per-user notes stored by chat_id
CREATE TABLE IF NOT EXISTS note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    pinned INTEGER DEFAULT 0
);
