CREATE TABLE IF NOT EXISTS setting (
    group_id BIGINT PRIMARY KEY,
    vote_to_join BOOLEAN NOT NULL,
    vote_to_kick BOOLEAN NOT NULL,
    vote_time INTEGER NOT NULL CHECK (vote_time BETWEEN 30 AND 3600),
    pin_msg BOOLEAN NOT NULL,
    clean_pinned_message BOOLEAN NOT NULL,
    anonymous_vote BOOLEAN NOT NULL,
    advanced_vote BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS join_request (
    uuid UUID PRIMARY KEY,
    group_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    request_time TIMESTAMPTZ(0) NOT NULL,
    waiting BOOLEAN NOT NULL,
    result BOOLEAN NULL,
    admin BIGINT NULL
);