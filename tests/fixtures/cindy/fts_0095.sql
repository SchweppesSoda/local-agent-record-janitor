-- Cindy, Apache-2.0; upstream 8b512ba5396f83a242838d172dbd7afb19183265
-- scripts/0095_scope_messages_fts_update_trigger.ts
CREATE VIRTUAL TABLE messages_fts USING fts5(message_id UNINDEXED, session_id UNINDEXED, role UNINDEXED, content, tokenize='porter unicode61');
CREATE TRIGGER messages_fts_insert
    AFTER INSERT ON messages
    WHEN new.rewind_at IS NULL AND new.role IN ('user', 'assistant', 'ask_user', 'plan_review')
    BEGIN
      INSERT INTO messages_fts(message_id, session_id, role, content)
        VALUES (new.id, new.session_id, new.role, new.content);
    END;
CREATE TRIGGER messages_fts_delete
    AFTER DELETE ON messages
    BEGIN
      DELETE FROM messages_fts WHERE message_id = old.id;
    END;
CREATE TRIGGER messages_fts_update
    AFTER UPDATE ON messages
    WHEN old.role IN ('user', 'assistant', 'ask_user', 'plan_review') OR new.role IN ('user', 'assistant', 'ask_user', 'plan_review')
    BEGIN
      DELETE FROM messages_fts WHERE message_id = old.id;
      INSERT INTO messages_fts(message_id, session_id, role, content)
        SELECT new.id, new.session_id, new.role, new.content
        WHERE new.rewind_at IS NULL AND new.role IN ('user', 'assistant', 'ask_user', 'plan_review');
    END;
