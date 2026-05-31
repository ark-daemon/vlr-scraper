-- VLR.gg Scraper - Full Database Schema
-- All tables use CREATE TABLE IF NOT EXISTS for idempotent setup

CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT,
    status      TEXT,
    region      TEXT,
    tier        TEXT,
    prize_pool  TEXT,
    start_date  TEXT,
    end_date    TEXT,
    logo_url    TEXT,
    url         TEXT,
    scraped_at  TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_id       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    abbreviation  TEXT,
    logo_url      TEXT,
    country       TEXT,
    region        TEXT,
    is_active     INTEGER DEFAULT 1,
    url           TEXT,
    scraped_at    TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id        INTEGER PRIMARY KEY,
    ign              TEXT NOT NULL,
    real_name        TEXT,
    country          TEXT,
    country_flag     TEXT,
    current_team_id  INTEGER REFERENCES teams(team_id),
    role             TEXT,
    twitter          TEXT,
    twitch           TEXT,
    url              TEXT,
    scraped_at       TEXT
);

CREATE TABLE IF NOT EXISTS team_rosters (
    roster_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER REFERENCES teams(team_id),
    player_id   INTEGER REFERENCES players(player_id),
    join_date   TEXT,
    leave_date  TEXT,
    is_current  INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS matches (
    match_id       INTEGER PRIMARY KEY,
    event_id       INTEGER REFERENCES events(event_id),
    stage_name     TEXT,
    series_name    TEXT,
    team1_id       INTEGER REFERENCES teams(team_id),
    team2_id       INTEGER REFERENCES teams(team_id),
    team1_score    INTEGER,
    team2_score    INTEGER,
    winner_team_id INTEGER,
    status         TEXT,
    scheduled_at   TEXT,
    unix_timestamp INTEGER,
    best_of        INTEGER,
    vod_url        TEXT,
    url            TEXT,
    scraped_at     TEXT
);

CREATE TABLE IF NOT EXISTS maps_played (
    map_play_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id         INTEGER REFERENCES matches(match_id),
    map_number       INTEGER,
    map_name         TEXT,
    team1_rounds     INTEGER,
    team2_rounds     INTEGER,
    team1_ct_rounds  INTEGER,
    team1_t_rounds   INTEGER,
    team2_ct_rounds  INTEGER,
    team2_t_rounds   INTEGER,
    winner_team_id   INTEGER,
    is_draw          INTEGER DEFAULT 0,
    map_duration     TEXT,
    team1_atk_first  INTEGER
);

CREATE TABLE IF NOT EXISTS match_player_stats (
    stat_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      INTEGER REFERENCES matches(match_id),
    map_play_id   INTEGER,
    player_id     INTEGER REFERENCES players(player_id),
    team_id       INTEGER REFERENCES teams(team_id),
    agent         TEXT,
    rating        REAL,
    acs           REAL,
    kills         INTEGER,
    deaths        INTEGER,
    assists       INTEGER,
    kd_diff       INTEGER,
    kast          REAL,
    adr           REAL,
    hs_pct        REAL,
    fk            INTEGER,
    fd            INTEGER,
    fk_diff       INTEGER,
    rounds_played INTEGER
);

CREATE TABLE IF NOT EXISTS match_performance (
    perf_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id           INTEGER REFERENCES matches(match_id),
    map_play_id        INTEGER,
    player_id          INTEGER REFERENCES players(player_id),
    team_id            INTEGER REFERENCES teams(team_id),
    kills_2k           INTEGER,
    kills_3k           INTEGER,
    kills_4k           INTEGER,
    kills_5k           INTEGER,
    kills_2k_rounds    TEXT,
    kills_3k_rounds    TEXT,
    kills_4k_rounds    TEXT,
    kills_5k_rounds    TEXT,
    clutches_v1        INTEGER,
    clutches_v2        INTEGER,
    clutches_v3        INTEGER,
    clutches_v4        INTEGER,
    clutches_v5        INTEGER,
    clutches_v1_rounds TEXT,
    clutches_v2_rounds TEXT,
    clutches_v3_rounds TEXT,
    clutches_v4_rounds TEXT,
    clutches_v5_rounds TEXT
);

CREATE TABLE IF NOT EXISTS kill_matrix (
    km_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id           INTEGER REFERENCES matches(match_id),
    map_play_id        INTEGER,
    killer_player_id   INTEGER REFERENCES players(player_id),
    victim_player_id   INTEGER REFERENCES players(player_id),
    kill_count         INTEGER
);

CREATE TABLE IF NOT EXISTS match_economy (
    econ_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id                 INTEGER REFERENCES matches(match_id),
    map_play_id              INTEGER,
    team_id                  INTEGER REFERENCES teams(team_id),
    eco_rounds_played        INTEGER,
    eco_rounds_won           INTEGER,
    semi_eco_rounds_played   INTEGER,
    semi_eco_rounds_won      INTEGER,
    semi_buy_rounds_played   INTEGER,
    semi_buy_rounds_won      INTEGER,
    full_buy_rounds_played   INTEGER,
    full_buy_rounds_won      INTEGER
);

CREATE TABLE IF NOT EXISTS match_economy_rounds (
    econ_round_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER REFERENCES matches(match_id),
    map_play_id     INTEGER,
    team_id         INTEGER REFERENCES teams(team_id),
    round_number    INTEGER,
    side            TEXT,
    buy_type        TEXT,
    remaining_bank  INTEGER,
    loadout_value   INTEGER,
    round_won       INTEGER
);

CREATE TABLE IF NOT EXISTS match_logs (
    log_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            INTEGER REFERENCES matches(match_id),
    map_play_id         INTEGER,
    round_number        INTEGER,
    event_order         INTEGER,
    event_type          TEXT,
    killer_player_id    INTEGER REFERENCES players(player_id),
    victim_player_id    INTEGER REFERENCES players(player_id),
    weapon              TEXT,
    is_headshot         INTEGER,
    is_wallbang         INTEGER,
    spike_planted_by    INTEGER,
    round_winner_team   INTEGER,
    round_end_reason    TEXT
);

CREATE TABLE IF NOT EXISTS player_career_stats (
    career_stat_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER REFERENCES players(player_id),
    event_id        INTEGER,
    agent           TEXT,
    maps_played     INTEGER,
    rounds_played   INTEGER,
    rating          REAL,
    acs             REAL,
    kd_ratio        REAL,
    kast            REAL,
    adr             REAL,
    kpr             REAL,
    apr             REAL,
    fkpr            REAL,
    fdpr            REAL,
    hs_pct          REAL,
    cl_pct          REAL,
    cl_won          INTEGER,
    cl_played       INTEGER,
    scraped_at      TEXT
);

CREATE TABLE IF NOT EXISTS team_map_stats (
    map_stat_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id             INTEGER REFERENCES teams(team_id),
    map_name            TEXT,
    maps_played         INTEGER,
    maps_won            INTEGER,
    maps_lost           INTEGER,
    win_pct             REAL,
    atk_rounds_played   INTEGER,
    atk_rounds_won      INTEGER,
    def_rounds_played   INTEGER,
    def_rounds_won      INTEGER,
    scraped_at          TEXT
);

CREATE TABLE IF NOT EXISTS global_player_stats (
    gstat_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id     INTEGER REFERENCES players(player_id),
    team_id       INTEGER REFERENCES teams(team_id),
    region        TEXT,
    timespan      TEXT,
    event_id      INTEGER,
    agents_played TEXT,
    rating        REAL,
    acs           REAL,
    kd_ratio      REAL,
    kast          REAL,
    adr           REAL,
    kpr           REAL,
    apr           REAL,
    fkpr          REAL,
    fdpr          REAL,
    hs_pct        REAL,
    cl_pct        REAL,
    scraped_at    TEXT
);

CREATE TABLE IF NOT EXISTS rankings (
    ranking_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER REFERENCES teams(team_id),
    region      TEXT,
    rank        INTEGER,
    record      TEXT,
    earnings    TEXT,
    scraped_at  TEXT
);

CREATE TABLE IF NOT EXISTS crawl_queue (
    queue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE,
    page_type   TEXT,
    status      TEXT DEFAULT 'pending',
    retries     INTEGER DEFAULT 0,
    last_error  TEXT,
    next_attempt_at TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                    TEXT,
    started_at              TEXT,
    finished_at             TEXT,
    status                  TEXT,
    pages_processed         INTEGER DEFAULT 0,
    pages_failed            INTEGER DEFAULT 0,
    cloudflare_blocks       INTEGER DEFAULT 0,
    cloakbrowser_successes  INTEGER DEFAULT 0,
    last_error              TEXT
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_matches_event_id ON matches(event_id);
CREATE INDEX IF NOT EXISTS idx_match_player_stats_match ON match_player_stats(match_id);
CREATE INDEX IF NOT EXISTS idx_match_player_stats_player ON match_player_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_match_performance_match ON match_performance(match_id);
CREATE INDEX IF NOT EXISTS idx_kill_matrix_match ON kill_matrix(match_id);
CREATE INDEX IF NOT EXISTS idx_match_economy_match ON match_economy(match_id);
CREATE INDEX IF NOT EXISTS idx_match_economy_rounds_match ON match_economy_rounds(match_id);
CREATE INDEX IF NOT EXISTS idx_match_logs_match ON match_logs(match_id);
CREATE INDEX IF NOT EXISTS idx_maps_played_match ON maps_played(match_id);
CREATE INDEX IF NOT EXISTS idx_team_rosters_team ON team_rosters(team_id);
CREATE INDEX IF NOT EXISTS idx_team_rosters_player ON team_rosters(player_id);
CREATE INDEX IF NOT EXISTS idx_player_career_stats_player ON player_career_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_global_player_stats_player ON global_player_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_crawl_queue_status ON crawl_queue(status);
CREATE INDEX IF NOT EXISTS idx_crawl_queue_type ON crawl_queue(page_type);
CREATE INDEX IF NOT EXISTS idx_crawl_queue_ready ON crawl_queue(status, page_type, next_attempt_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status);
CREATE INDEX IF NOT EXISTS idx_matches_scheduled_at ON matches(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_players_current_team ON players(current_team_id);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_started ON scrape_runs(started_at);
