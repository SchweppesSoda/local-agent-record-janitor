-- Source: makecindy/cindy 8b512ba5396f83a242838d172dbd7afb19183265
-- apps/desktop/drizzle/0034_add_chat_embedding_vec.sql (Apache-2.0; see LICENSE)
CREATE TRIGGER `trg_chat_rewind_clean_vec`
AFTER UPDATE OF `rewind_at` ON `messages`
WHEN NEW.rewind_at IS NOT NULL AND OLD.rewind_at IS NULL
BEGIN
	DELETE FROM `chat_messages_vec_v1` WHERE rowid IN (
		SELECT rowid FROM `embedding_jobs` WHERE source = 'chat' AND source_id = NEW.id
	);
	DELETE FROM `embedding_jobs` WHERE source = 'chat' AND source_id = NEW.id;
END;
