-- 005_human_feedback.sql — §16.4 human_rejection_pool + pattern_cooling + §16.5 tokens_spent
CREATE TABLE human_rejection_pool (
  id INTEGER PRIMARY KEY,
  draft_id INTEGER REFERENCES drafts(id),
  scorer_score INTEGER NOT NULL,
  pattern_ids TEXT NOT NULL,
  rejected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  reason TEXT
);
CREATE INDEX idx_rej_draft ON human_rejection_pool(draft_id);

CREATE TABLE pattern_cooling (
  pattern_id INTEGER PRIMARY KEY REFERENCES technique_entries(id),
  cooled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  reset_after TIMESTAMP,
  consecutive_misses INTEGER NOT NULL
);
CREATE INDEX idx_cooling_reset ON pattern_cooling(reset_after);

-- §16.5 cost tracking
ALTER TABLE daily_stats ADD COLUMN tokens_spent INTEGER DEFAULT 0;
