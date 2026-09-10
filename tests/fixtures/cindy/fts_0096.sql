-- Cindy, Apache-2.0; upstream 8b512ba5396f83a242838d172dbd7afb19183265
-- scripts/0096_stabilize_messages_fts_rows.ts
CREATE VIRTUAL TABLE messages_fts USING fts5(message_id UNINDEXED, session_id UNINDEXED, role UNINDEXED, content, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS `messages_fts_rows` (
	`fts_rowid` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`message_id` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS `messages_fts_rows_message_id_idx` ON `messages_fts_rows` (`message_id`);

CREATE TRIGGER messages_fts_insert
    AFTER INSERT ON messages
    WHEN new.rewind_at IS NULL AND new.role IN ('user', 'assistant', 'ask_user', 'plan_review')
    BEGIN
      INSERT OR IGNORE INTO messages_fts_rows(message_id) VALUES (new.id);
      INSERT OR REPLACE INTO messages_fts(rowid, message_id, session_id, role, content)
        SELECT fts_rowid, new.id, new.session_id, new.role, new.content
        FROM messages_fts_rows
        WHERE message_id = new.id;
    END;
CREATE TRIGGER messages_fts_delete
    AFTER DELETE ON messages
    BEGIN
      DELETE FROM messages_fts
        WHERE rowid = (
          SELECT fts_rowid FROM messages_fts_rows WHERE message_id = old.id
        );
      DELETE FROM messages_fts_rows WHERE message_id = old.id;
    END;
CREATE TRIGGER messages_fts_update
    AFTER UPDATE OF id, session_id, role, content, rewind_at ON messages
    WHEN old.id IS NOT new.id
      OR old.session_id IS NOT new.session_id
      OR old.role IS NOT new.role
      OR old.content IS NOT new.content
      OR old.rewind_at IS NOT new.rewind_at
    BEGIN
      DELETE FROM messages_fts
        WHERE rowid = (
          SELECT fts_rowid FROM messages_fts_rows WHERE message_id = old.id
        );
      UPDATE messages_fts_rows
        SET message_id = new.id
        WHERE message_id = old.id AND old.id IS NOT new.id;
      INSERT OR IGNORE INTO messages_fts_rows(message_id)
        SELECT new.id
        WHERE new.rewind_at IS NULL AND new.role IN ('user', 'assistant', 'ask_user', 'plan_review');
      INSERT OR REPLACE INTO messages_fts(rowid, message_id, session_id, role, content)
        SELECT fts_rowid, new.id, new.session_id, new.role, new.content
        FROM messages_fts_rows
        WHERE message_id = new.id
          AND new.rewind_at IS NULL
          AND new.role IN ('user', 'assistant', 'ask_user', 'plan_review');
    END;
