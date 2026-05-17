-- Migration 006: add content_hash to drafts for robust self_monitor binding.
--
-- self_monitor previously matched X-tweet content to drafts via exact equality
-- (`WHERE content = ?`), which broke on the slightest manual edit before
-- posting (whitespace, punctuation). content_hash stores a sha256 of the
-- normalized content so we get O(1) exact lookup, with the adapter falling
-- back to fuzzy similarity if hash misses.

ALTER TABLE drafts ADD COLUMN content_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_drafts_content_hash ON drafts(content_hash);
