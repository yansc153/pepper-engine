-- 004_topic_selector.sql — §16.2 选题引擎 + §16.3 wild_posts
CREATE TABLE topic_candidates (
  id INTEGER PRIMARY KEY,
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  source_observations TEXT NOT NULL,               -- JSON: obs ids
  topic_summary TEXT NOT NULL,
  virality_score REAL NOT NULL,                    -- 0-100
  predicted_content_mode TEXT,                     -- insight | meme | emotional
  predicted_length TEXT,                           -- short | medium | long | article
  predicted_topic_lane TEXT,
  kol_reaction_count INTEGER,
  emotional_intensity REAL,                        -- 0-1
  debate_potential REAL,                           -- 0-1
  status TEXT DEFAULT 'fresh',                     -- fresh | consumed | expired
  consumed_at TIMESTAMP,
  consumed_by_draft_id INTEGER REFERENCES drafts(id),
  CHECK (status IN ('fresh','consumed','expired'))
);
CREATE INDEX idx_topic_fresh ON topic_candidates(status, virality_score DESC);

CREATE TABLE wild_posts (
  tweet_url TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  posted_at TIMESTAMP NOT NULL,
  discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_wild_hash ON wild_posts(content_hash);
