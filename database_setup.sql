CREATE TABLE IF NOT EXISTS setting (
    group_id BIGINT PRIMARY KEY,
    vote_to_join BOOLEAN NOT NULL DEFAULT TRUE,
    vote_time INTEGER NOT NULL DEFAULT 600 CHECK (vote_time BETWEEN 30 AND 3600),
    pin_msg BOOLEAN NOT NULL DEFAULT FALSE,
    clean_pinned_message BOOLEAN NOT NULL DEFAULT FALSE,
    anonymous_vote BOOLEAN NOT NULL DEFAULT TRUE,
    advanced_vote BOOLEAN NOT NULL DEFAULT FALSE,
    language VARCHAR(16) NOT NULL DEFAULT 'zh-CN',
    mini_voters INTEGER NOT NULL DEFAULT 3
);

CREATE TABLE IF NOT EXISTS join_request (
    uuid UUID PRIMARY KEY,
    group_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    request_time TIMESTAMPTZ(0) NOT NULL,
    waiting BOOLEAN NOT NULL,
    result BOOLEAN NULL,
    admin BIGINT NULL,
    yes_votes INTEGER NULL,
    no_votes INTEGER NULL
);
