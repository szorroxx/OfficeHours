-- Office Hours schema for Tiger Data (Timescale Cloud / Postgres).
--
-- Run with:  python3 db.py --init
--
-- Two kinds of table in here:
--
--   REGULAR TABLES  hold the current state of things (courses, assignments,
--                   the latest schedule). You overwrite rows as data changes.
--
--   HYPERTABLES     hold history. A hypertable is Timescale's special table
--                   type: it looks and acts like a normal table, but it's
--                   automatically split into chunks by time, which makes
--                   "how did this change over the semester" queries fast.
--                   This is the part that justifies using Tiger Data at all.
--
-- NOTE: there are deliberately NO PASSWORD COLUMNS anywhere in this file.
-- See students.auth_token for what we store instead.

-- --------------------------------------------------------------------------
-- Extension (already installed on Timescale Cloud; harmless to re-run)
-- --------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- --------------------------------------------------------------------------
-- Current state
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS students (
    id            TEXT PRIMARY KEY,           -- 'demo-student'
    display_name  TEXT NOT NULL,
    email         TEXT,
    timezone      TEXT        DEFAULT 'America/New_York',
    canvas_base   TEXT,                       -- URL or local folder name
    -- An opaque token, NOT a password. For the demo it's a fake string.
    -- Real Canvas integrations use a scoped access token the user generates
    -- and can revoke; you never see or store their password.
    auth_token    TEXT,
    -- Free-form bucket for "anything else the user wants". Adding a
    -- preference later needs no schema change: just put a new key in here.
    prefs         JSONB       DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS courses (
    id            TEXT PRIMARY KEY,           -- 'demo-student:phys-1361'
    student_id    TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    code          TEXT NOT NULL,              -- 'PHYS 1361'
    title         TEXT,
    instructor    TEXT,
    canvas_id     TEXT,
    source        TEXT,                       -- which file/URL it came from
    -- When did the crawler last successfully read this course? This column is
    -- what lets the agent decide "this data is stale, re-crawl it".
    last_crawled_at TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assignments (
    -- Deterministic id built from student + course + title (see db.make_id).
    -- Crawling the same page twice UPDATES the row instead of duplicating it.
    id            TEXT PRIMARY KEY,
    student_id    TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    course_code   TEXT NOT NULL,
    title         TEXT NOT NULL,
    kind          TEXT,                       -- homework|quiz|exam|lab|project|reading
    due_at        TIMESTAMPTZ,
    points        NUMERIC,
    est_hours     NUMERIC,                    -- expected time to complete
    est_source    TEXT,                       -- 'claude' | 'user' | 'default'
    priority      INT,                        -- 1 = do first
    status        TEXT        DEFAULT 'open',  -- open|submitted|graded
    description   TEXT,
    source_url    TEXT,
    first_seen_at TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS assignments_due_idx
    ON assignments (student_id, status, due_at);

CREATE TABLE IF NOT EXISTS schedule_blocks (
    id            BIGSERIAL PRIMARY KEY,
    student_id    TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    assignment_id TEXT,
    task          TEXT NOT NULL,
    starts_at     TIMESTAMPTZ NOT NULL,
    ends_at       TIMESTAMPTZ,
    est_minutes   INT,
    priority      INT,
    -- Every time Claude makes a schedule we tag all its blocks with one
    -- generation_id. Reading "the current schedule" = reading the newest
    -- generation. Old ones stay around, so you can show before/after.
    generation_id TEXT NOT NULL,
    generated_at  TIMESTAMPTZ DEFAULT now(),
    rationale     TEXT
);

CREATE INDEX IF NOT EXISTS schedule_gen_idx
    ON schedule_blocks (student_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS study_sets (
    id            BIGSERIAL PRIMARY KEY,
    student_id    TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    course_code   TEXT,
    topic         TEXT,
    format        TEXT,                       -- flashcards|outline|practice_problems
    content       JSONB NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campus_events (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    starts_at     TIMESTAMPTZ,
    location      TEXT,
    url           TEXT,
    tags          TEXT[],
    source        TEXT,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- --------------------------------------------------------------------------
-- History  (the actual time-series part)
-- --------------------------------------------------------------------------

-- One row per course per crawl. Lets you answer "how has my workload changed
-- over the semester" with a single time_bucket() query.
CREATE TABLE IF NOT EXISTS workload_snapshots (
    observed_at      TIMESTAMPTZ NOT NULL,
    student_id       TEXT        NOT NULL,
    course_code      TEXT        NOT NULL,
    open_assignments INT,
    total_est_hours  NUMERIC,
    nearest_due      TIMESTAMPTZ
);

SELECT create_hypertable(
    'workload_snapshots', 'observed_at',
    if_not_exists => TRUE, migrate_data => TRUE
);

-- Actual time spent, so you can compare estimated vs. real effort.
CREATE TABLE IF NOT EXISTS study_sessions (
    started_at    TIMESTAMPTZ NOT NULL,
    student_id    TEXT        NOT NULL,
    assignment_id TEXT,
    minutes       INT,
    source        TEXT                        -- 'user' | 'inferred'
);

SELECT create_hypertable(
    'study_sessions', 'started_at',
    if_not_exists => TRUE, migrate_data => TRUE
);

-- --------------------------------------------------------------------------
-- The query to put on screen for the "Best Use of Tiger Data" track.
--
-- time_bucket() is Timescale's signature function: it rounds timestamps into
-- fixed-width buckets (here, one day) so you can graph a trend. Plain Postgres
-- needs clumsy date_trunc + generate_series to do the same thing.
--
--   SELECT time_bucket('1 day', observed_at) AS day,
--          course_code,
--          max(total_est_hours) AS est_hours
--   FROM workload_snapshots
--   WHERE student_id = 'demo-student'
--     AND observed_at > now() - interval '30 days'
--   GROUP BY day, course_code
--   ORDER BY day;
--
-- db.get_workload_history() runs exactly this.
-- --------------------------------------------------------------------------
