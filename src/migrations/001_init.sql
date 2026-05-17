-- 001_init.sql — UNIFIED_SPEC §5.2 主表
-- posts (snapshot of published drafts, kept as independent table per §16.1)
CREATE TABLE posts (
  id INTEGER PRIMARY KEY,
  content TEXT NOT NULL,
  content_hash TEXT UNIQUE NOT NULL,
  topic_lane TEXT NOT NULL,
  persona TEXT NOT NULL,
  scheduled_for TIMESTAMP,
  posted_at TIMESTAMP,
  tweet_url TEXT,
  image_path TEXT,
  is_dry_run INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending',
  score_information INTEGER,
  score_stance INTEGER,
  score_counter INTEGER,
  score_hook INTEGER,
  score_compliance INTEGER,
  score_total INTEGER
);
CREATE INDEX idx_posts_status ON posts(status);
CREATE INDEX idx_posts_posted_at ON posts(posted_at);

CREATE TABLE reaction_observations (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  author_handle TEXT NOT NULL,
  author_tier INTEGER NOT NULL,
  content TEXT NOT NULL,
  posted_at TIMESTAMP NOT NULL,
  likes INTEGER NOT NULL,
  retweets INTEGER NOT NULL,
  replies INTEGER NOT NULL,
  impressions INTEGER,
  has_image INTEGER NOT NULL,
  raw_url TEXT NOT NULL UNIQUE,
  topic_hint TEXT,
  viral_score REAL NOT NULL,
  is_viral INTEGER NOT NULL,
  observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  distilled_at TIMESTAMP
);
CREATE INDEX idx_obs_viral ON reaction_observations(is_viral, distilled_at);
CREATE INDEX idx_obs_source ON reaction_observations(source, observed_at);

CREATE TABLE strategy_weights (
  topic_lane TEXT PRIMARY KEY,
  weight REAL NOT NULL,
  reason TEXT,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE learning_log (
  id INTEGER PRIMARY KEY,
  ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  window_days INTEGER,
  winning_patterns TEXT,
  losing_patterns TEXT,
  weights_before TEXT,
  weights_after TEXT,
  sample_size INTEGER
);

CREATE TABLE source_health (
  adapter_name TEXT PRIMARY KEY,
  last_success_at TIMESTAMP,
  consecutive_failures INTEGER DEFAULT 0,
  last_error TEXT,
  rate_limit_hit_at TIMESTAMP
);

CREATE TABLE circuit_breaker (
  scope TEXT PRIMARY KEY,
  tripped_at TIMESTAMP,
  reason TEXT,
  reset_after TIMESTAMP
);

CREATE TABLE slop_words (
  word TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  source TEXT
);

CREATE TABLE daily_stats (
  date TEXT PRIMARY KEY,
  posts_published INTEGER DEFAULT 0,
  observations_collected INTEGER DEFAULT 0,
  entries_distilled INTEGER DEFAULT 0,
  edges_woven INTEGER DEFAULT 0
);
