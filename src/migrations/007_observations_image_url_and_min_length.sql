-- Migration 007: image_url for observations + content_length for filtering.
--
-- Pipeline contract change (user direction 2026-05-18):
--   * Source observations MUST have an image to be ingested. The image URL is
--     captured here so the writer can later download it and attach to the draft.
--   * Long-form filter: only posts with content_length >= some threshold are
--     suitable as "rewritable original" for the X-style condenser. Storing
--     content_length lets the topic-scorer filter cheaply.

ALTER TABLE reaction_observations ADD COLUMN image_url TEXT;
ALTER TABLE reaction_observations ADD COLUMN content_length INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_obs_image_url ON reaction_observations(image_url) WHERE image_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_obs_content_length ON reaction_observations(content_length);
