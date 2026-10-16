CREATE ROLE webapp LOGIN PASSWORD 'app';

CREATE TABLE items (
    id SERIAL PRIMARY KEY,
    name TEXT
);

INSERT INTO items(name)
SELECT 'item-' || g
FROM generate_series(1, 100) g;

CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY,
    user_id INT,
    payload TEXT
);

INSERT INTO events(user_id, payload)
SELECT (random() * 10000)::int, md5(g::text)
FROM generate_series(1, 2000000) g;

CREATE INDEX idx_events_user_id ON events(user_id);

ANALYZE events;

GRANT CONNECT ON DATABASE app TO webapp;
GRANT USAGE ON SCHEMA public TO webapp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO webapp;
