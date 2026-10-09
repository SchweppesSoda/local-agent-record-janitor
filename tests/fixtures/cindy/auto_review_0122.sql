-- Cindy, Apache-2.0; installed Cindy 0.1.99 migration 0122.
-- drizzle/0122_auto_review_projections.sql SHA256: ba343dd5f5e0cd7286bb2662f712746980ef987133cb51434b45b0a5d7fc0f66
-- drizzle/scripts/0122_auto_review_projections.ts SHA256: 5946b614e43d29e2c0eeaa1f20aa6530523cf84dae8640e41bea805ef3e0b078
CREATE TABLE `auto_review_projections` (
	`session_id` text NOT NULL,
	`lead_id` text NOT NULL,
	`revision` integer DEFAULT 0 NOT NULL,
	`projected_revision` integer DEFAULT -1 NOT NULL,
	`version` integer DEFAULT 1 NOT NULL,
	`payload` text,
	PRIMARY KEY(`session_id`, `lead_id`),
	FOREIGN KEY (`session_id`) REFERENCES `sessions`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`lead_id`) REFERENCES `sessions`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `auto_review_projections_lead_idx` ON `auto_review_projections` (`lead_id`);
CREATE TRIGGER auto_review_message_insert AFTER INSERT ON messages
WHEN NEW.role IN ('user', 'ask_user', 'plan_review')
 AND NOT (NEW.role = 'user' AND CASE WHEN json_valid(NEW.agent_meta) THEN
   coalesce(json_extract(NEW.agent_meta, '$.autoReviewUserText.kind') IN ('scheduled-continuation','delegated-continuation'),0) ELSE 0 END)
BEGIN
 UPDATE auto_review_projections SET revision = revision + 1,
 payload = json_set(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.appendEvent', json_object(
   'sessionId', NEW.session_id, 'clientId', NEW.client_id, 'role', NEW.role,
   'content', NEW.content, 'createdAt', NEW.created_at, 'agentMeta', NEW.agent_meta,
   'visible', NEW.rewind_at IS NULL AND (SELECT cleared_at IS NULL OR NEW.created_at > cleared_at FROM sessions WHERE id=NEW.session_id)))
 WHERE session_id = NEW.session_id OR lead_id = NEW.session_id;
END;
CREATE TRIGGER auto_review_message_delete AFTER DELETE ON messages
WHEN OLD.role IN ('user', 'ask_user', 'plan_review')
BEGIN
 UPDATE auto_review_projections SET revision = revision + 1, payload = CASE WHEN json_valid(payload) THEN json_remove(payload,'$.appendEvent') ELSE NULL END
 WHERE session_id = OLD.session_id OR lead_id = OLD.session_id;
END;
CREATE TRIGGER auto_review_message_update AFTER UPDATE OF session_id, role, content, agent_meta, created_at, rewind_at ON messages
WHEN (OLD.role IN ('user', 'ask_user', 'plan_review') OR NEW.role IN ('user', 'ask_user', 'plan_review'))
 AND (OLD.session_id IS NOT NEW.session_id OR OLD.role IS NOT NEW.role OR OLD.content IS NOT NEW.content
 OR (CASE WHEN json_valid(OLD.agent_meta) THEN json_array(json_extract(OLD.agent_meta,'$.autoReviewUserText'),json_extract(OLD.agent_meta,'$.delivery'),json_extract(OLD.agent_meta,'$.autoResume'),json_extract(OLD.agent_meta,'$.contextRebuild')) ELSE NULL END)
 IS NOT (CASE WHEN json_valid(NEW.agent_meta) THEN json_array(json_extract(NEW.agent_meta,'$.autoReviewUserText'),json_extract(NEW.agent_meta,'$.delivery'),json_extract(NEW.agent_meta,'$.autoResume'),json_extract(NEW.agent_meta,'$.contextRebuild')) ELSE NULL END)
 OR OLD.created_at IS NOT NEW.created_at OR OLD.rewind_at IS NOT NEW.rewind_at)
BEGIN
 UPDATE auto_review_projections SET revision = revision + 1, payload = CASE WHEN json_valid(payload) THEN json_remove(payload,'$.appendEvent') ELSE NULL END
 WHERE session_id IN (OLD.session_id, NEW.session_id) OR lead_id IN (OLD.session_id, NEW.session_id);
END;
CREATE TRIGGER auto_review_session_clear AFTER UPDATE OF cleared_at ON sessions
WHEN OLD.cleared_at IS NOT NEW.cleared_at
BEGIN
 UPDATE auto_review_projections SET revision = revision + 1, payload = CASE WHEN json_valid(payload) THEN json_remove(payload,'$.appendEvent') ELSE NULL END
 WHERE session_id = NEW.id OR lead_id = NEW.id;
END;
