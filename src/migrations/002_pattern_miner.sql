-- 002_pattern_miner.sql — UNIFIED_SPEC §5.2 Pattern Miner 增量 + §16.5/§16.6 字段
CREATE TABLE technique_entries (
  id INTEGER PRIMARY KEY,
  observation_id INTEGER NOT NULL REFERENCES reaction_observations(id),
  hook_pattern TEXT NOT NULL,
  hook_example TEXT NOT NULL,
  syntax_signature TEXT NOT NULL,
  sentence_len_avg REAL NOT NULL,
  sentence_len_p90 REAL NOT NULL,
  stance_strength INTEGER NOT NULL,
  emotion_triggers TEXT NOT NULL,
  image_style TEXT NOT NULL,
  post_hour_utc INTEGER NOT NULL,
  topic_lane TEXT NOT NULL,
  applicable_personas TEXT NOT NULL,
  content_mode TEXT NOT NULL,                     -- §16.6: insight | meme | emotional
  optimal_length TEXT NOT NULL,                   -- §16.5: short | medium | long | article
  distilled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  success_score REAL NOT NULL,
  times_retrieved INTEGER DEFAULT 0,
  times_used_in_post INTEGER DEFAULT 0,
  recency_weight REAL DEFAULT 1.0
);
CREATE UNIQUE INDEX idx_te_obs ON technique_entries(observation_id);
CREATE INDEX idx_te_lane_hour ON technique_entries(topic_lane, post_hour_utc);
CREATE INDEX idx_te_mode ON technique_entries(content_mode);

CREATE TABLE technique_edges (
  id INTEGER PRIMARY KEY,
  src_entry_id INTEGER NOT NULL REFERENCES technique_entries(id),
  dst_entry_id INTEGER NOT NULL REFERENCES technique_entries(id),
  edge_type TEXT NOT NULL,
  weight REAL NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CHECK (src_entry_id < dst_entry_id),
  UNIQUE(src_entry_id, dst_entry_id, edge_type)
);
CREATE INDEX idx_edge_src ON technique_edges(src_entry_id, edge_type);

CREATE TABLE retrieval_log (
  id INTEGER PRIMARY KEY,
  post_id INTEGER REFERENCES posts(id),
  retrieved_entry_ids TEXT NOT NULL,
  context_signature TEXT NOT NULL,
  retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE post_metrics_timeseries (
  post_id INTEGER NOT NULL REFERENCES posts(id),
  collected_at TIMESTAMP NOT NULL,
  likes INTEGER NOT NULL,
  retweets INTEGER NOT NULL,
  replies INTEGER NOT NULL,
  impressions INTEGER,
  viral_score REAL NOT NULL,
  PRIMARY KEY (post_id, collected_at)
);
