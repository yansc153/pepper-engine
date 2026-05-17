-- 003_drafts_state_machine.sql — UNIFIED_SPEC §16.1 Discord 审批闸门 state machine
CREATE TABLE drafts (
  id INTEGER PRIMARY KEY,
  content TEXT NOT NULL,
  content_length INTEGER NOT NULL,
  content_mode TEXT NOT NULL,                      -- insight | meme | emotional
  optimal_length TEXT NOT NULL,                    -- short | medium | long | article
  topic_lane TEXT NOT NULL,
  persona TEXT NOT NULL,
  pattern_ids TEXT NOT NULL,                       -- JSON: technique_entry ids
  source_observation_ids TEXT NOT NULL,            -- JSON: triggering obs ids
  image_path TEXT,
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  status TEXT DEFAULT 'candidate',                 -- candidate|pushed_to_discord|approved|rejected|published|metrics_collected|learned
  discord_message_id TEXT,
  discord_reaction TEXT,                           -- ✅ ❌ 🔄
  discord_reacted_at TIMESTAMP,
  tweet_url TEXT,
  posted_at TIMESTAMP,
  cross_referenced INTEGER DEFAULT 0,              -- 1 = 6h self-monitor 补绑
  CHECK (status IN ('candidate','pushed_to_discord','approved','rejected','published','metrics_collected','learned'))
);
CREATE INDEX idx_drafts_status ON drafts(status);
CREATE INDEX idx_drafts_discord_msg ON drafts(discord_message_id);
CREATE INDEX idx_drafts_tweet_url ON drafts(tweet_url);
