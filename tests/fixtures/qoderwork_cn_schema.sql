CREATE TABLE "__drizzle_migrations" (id SERIAL PRIMARY KEY, hash text NOT NULL, created_at numeric);

CREATE TABLE `app_settings` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL,
	`updated_at` integer
);

CREATE TABLE `byok_custom_models` (
	`key` text PRIMARY KEY NOT NULL,
	`legacy_key` text,
	`source` text DEFAULT 'byok' NOT NULL,
	`display_name` text NOT NULL,
	`provider` text,
	`type` text,
	`url` text,
	`style` text,
	`model` text NOT NULL,
	`format` text,
	`is_vl` integer DEFAULT false NOT NULL,
	`is_reasoning` integer DEFAULT false NOT NULL,
	`max_input_tokens` integer DEFAULT 0 NOT NULL,
	`max_output_tokens` integer DEFAULT 0 NOT NULL,
	`encrypted_parameters` text DEFAULT '{}' NOT NULL,
	`extra_params` text DEFAULT '' NOT NULL,
	`migrated_from` text,
	`created_at` integer,
	`updated_at` integer
);

CREATE TABLE `channel_pairings` (
	`id` text PRIMARY KEY NOT NULL,
	`channel_id` text NOT NULL,
	`conversation_id` text NOT NULL,
	`conversation_type` text,
	`subject_name` text,
	`paired_at` integer NOT NULL,
	`created_at` integer,
	`sender_staff_id` text
);

CREATE TABLE `channel_pairings_v2` (
	`id` text PRIMARY KEY NOT NULL,
	`channel_id` text NOT NULL,
	`robot_id` text NOT NULL,
	`binding_key` text NOT NULL,
	`conversation_type` text NOT NULL,
	`subject_name` text,
	`sender_staff_id` text,
	`paired_at` integer NOT NULL,
	`created_at` integer
);

CREATE TABLE `chats` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text,
	`project_id` text NOT NULL,
	`created_at` integer,
	`updated_at` integer,
	`archived_at` integer,
	`worktree_path` text,
	`branch` text,
	`base_branch` text, `pr_url` text, `pr_number` integer, `additional_directories` text, `output_directory` text, `source` text, `wecom_account_id` text, `wecom_user_id` text, `chat_type` text DEFAULT 'task', `ext` text, `source_chat_id` text, `deleted_at` integer,
	FOREIGN KEY (`project_id`) REFERENCES `projects`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `google_oauth_tokens` (
	`key` text PRIMARY KEY NOT NULL,
	`account` text,
	`client_id` text NOT NULL,
	`scopes` text,
	`expires_at` integer,
	`encrypted_payload` blob NOT NULL,
	`encryption_version` integer DEFAULT 1 NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);

CREATE TABLE `knowledge_bases` (
	`id` text PRIMARY KEY NOT NULL,
	`owner_id` text NOT NULL,
	`title` text NOT NULL,
	`description` text,
	`status` text DEFAULT 'active',
	`source_count` integer DEFAULT 0,
	`auto_search_enabled` integer DEFAULT true,
	`synced_at` integer,
	`created_at` integer,
	`updated_at` integer
);

CREATE TABLE `mcp_oauth_tokens` (
	`server_name` text PRIMARY KEY NOT NULL,
	`expires_at` integer,
	`encrypted_payload` blob NOT NULL,
	`encryption_version` integer DEFAULT 1 NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);

CREATE TABLE `mcp_oauth_tokens_by_user` (
	`user_id` text NOT NULL,
	`server_name` text NOT NULL,
	`expires_at` integer,
	`encrypted_payload` blob NOT NULL,
	`encryption_version` integer DEFAULT 1 NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);

CREATE TABLE `messages` (
	`id` text PRIMARY KEY NOT NULL,
	`message_id` text NOT NULL,
	`chat_id` text NOT NULL,
	`sub_chat_id` text NOT NULL,
	`sequence` integer NOT NULL,
	`role` text NOT NULL,
	`parts` text DEFAULT '[]' NOT NULL,
	`metadata` text DEFAULT '{}' NOT NULL,
	`searchable_text` text,
	`search_status` text DEFAULT 'ready' NOT NULL,
	`created_at` integer,
	`updated_at` integer,
	FOREIGN KEY (`chat_id`) REFERENCES `chats`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`sub_chat_id`) REFERENCES `sub_chats`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
        searchable_text,
        chat_id UNINDEXED,
        sub_chat_id UNINDEXED,
        message_id UNINDEXED,
        role UNINDEXED,
        tokenize='trigram case_sensitive 0'
      );

CREATE TABLE `ms365_auth_states` (
	`key` text PRIMARY KEY NOT NULL,
	`encrypted_payload` blob NOT NULL,
	`encryption_version` integer DEFAULT 1 NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);

CREATE TABLE `nudge_logs` (
	`id` text PRIMARY KEY NOT NULL,
	`type` text NOT NULL,
	`review_type` text NOT NULL,
	`title` text NOT NULL,
	`preview` text,
	`sub_chat_id` text,
	`review_id` text,
	`duration_ms` integer,
	`is_error` integer DEFAULT false NOT NULL,
	`error_message` text,
	`created_at` integer
, `awareness_target` text);

CREATE TABLE `projects` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`path` text NOT NULL,
	`created_at` integer,
	`updated_at` integer
, `git_remote_url` text, `git_provider` text, `git_owner` text, `git_repo` text);

CREATE TABLE `rc_session_mappings` (
	`sub_chat_id` text PRIMARY KEY NOT NULL,
	`chat_id` text NOT NULL,
	`cwd` text NOT NULL,
	`remote_session_id` text NOT NULL,
	`last_broker_event_seq` integer DEFAULT 0 NOT NULL,
	`last_synced_seq` integer DEFAULT 0 NOT NULL,
	`last_published_msg_id` text,
	`import_id` text,
	`import_mode` text,
	`import_status` text,
	`import_error` text,
	`import_lock_expires_at` text,
	`created_at` integer,
	`updated_at` integer,
	FOREIGN KEY (`sub_chat_id`) REFERENCES `sub_chats`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `scheduled_tasks` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`description` text,
	`project_id` text NOT NULL,
	`enabled` integer DEFAULT true NOT NULL,
	`delete_after_run` integer,
	`missed_run_policy` text DEFAULT 'prompt' NOT NULL,
	`schedule` text NOT NULL,
	`payload` text NOT NULL,
	`next_run_at` integer,
	`running_at` integer,
	`last_run_at` integer,
	`last_run_status` text,
	`last_error` text,
	`last_duration_ms` integer,
	`consecutive_errors` integer DEFAULT 0,
	`created_at` integer,
	`updated_at` integer, `source_chat_id` text, `source_sub_chat_id` text, `deleted_at` integer,
	FOREIGN KEY (`project_id`) REFERENCES `projects`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `skill_evolution_suggestions` (
	`id` text PRIMARY KEY NOT NULL,
	`skill_name` text NOT NULL,
	`action` text NOT NULL,
	`summary` text NOT NULL,
	`rationale` text,
	`confidence` real NOT NULL,
	`skill_file_hash` text,
	`source_sub_chat_id` text NOT NULL,
	`source_session_id` text,
	`review_id` text,
	`status` text DEFAULT 'pending' NOT NULL,
	`evolution_chat_id` text,
	`snapshot_path` text,
	`expires_at` integer,
	`created_at` integer,
	`updated_at` integer
);

CREATE TABLE `sub_chats` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text,
	`chat_id` text NOT NULL,
	`session_id` text,
	`mode` text DEFAULT 'agent' NOT NULL,
	`messages` text DEFAULT '[]' NOT NULL,
	`created_at` integer,
	`updated_at` integer, `stream_id` text, `feedback` text DEFAULT 'none', `model_level` text, `ext` text, `message_feedbacks` text DEFAULT '{}',
	FOREIGN KEY (`chat_id`) REFERENCES `chats`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `task_run_logs` (
	`id` text PRIMARY KEY NOT NULL,
	`task_id` text NOT NULL,
	`chat_id` text,
	`sub_chat_id` text,
	`run_at` integer NOT NULL,
	`duration_ms` integer,
	`status` text NOT NULL,
	`error` text,
	`created_at` integer, `trigger` text,
	FOREIGN KEY (`task_id`) REFERENCES `scheduled_tasks`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `voice_input_history` (
	`id` text PRIMARY KEY NOT NULL,
	`raw_text` text NOT NULL,
	`final_text` text NOT NULL,
	`target_app_name` text,
	`insert_method` text,
	`insert_status` text DEFAULT 'inserted' NOT NULL,
	`error_message` text,
	`created_at` integer
);

CREATE TABLE `voice_input_hotwords` (
	`id` text PRIMARY KEY NOT NULL,
	`text` text NOT NULL,
	`normalized_text` text NOT NULL,
	`enabled` integer DEFAULT true NOT NULL,
	`source` text DEFAULT 'manual' NOT NULL,
	`created_at` integer,
	`updated_at` integer
);

CREATE INDEX `byok_custom_models_legacy_key_idx` ON `byok_custom_models` (`legacy_key`);

CREATE INDEX `byok_custom_models_source_idx` ON `byok_custom_models` (`source`);

CREATE INDEX `chats_worktree_path_idx` ON `chats` (`worktree_path`);

CREATE INDEX `idx_pairing_channel_conv` ON `channel_pairings` (`channel_id`,`conversation_id`);

CREATE UNIQUE INDEX `idx_pairing_v2_robot_binding` ON `channel_pairings_v2` (`channel_id`,`robot_id`,`binding_key`);

CREATE UNIQUE INDEX `mcp_oauth_tokens_by_user_unique` ON `mcp_oauth_tokens_by_user` (`user_id`,`server_name`);

CREATE INDEX `messages_chat_created_at_idx` ON `messages` (`chat_id`,`created_at`);

CREATE UNIQUE INDEX `messages_sub_chat_message_unique` ON `messages` (`sub_chat_id`,`message_id`);

CREATE INDEX `messages_sub_chat_sequence_idx` ON `messages` (`sub_chat_id`,`sequence`);

CREATE UNIQUE INDEX `messages_sub_chat_sequence_unique` ON `messages` (`sub_chat_id`,`sequence`);

CREATE INDEX `nudge_logs_created_at_idx` ON `nudge_logs` (`created_at`);

CREATE INDEX `nudge_logs_review_id_idx` ON `nudge_logs` (`review_id`);

CREATE UNIQUE INDEX `projects_path_unique` ON `projects` (`path`);

CREATE UNIQUE INDEX `rc_session_mappings_remote_session_id_idx` ON `rc_session_mappings` (`remote_session_id`);

CREATE INDEX `skill_evo_suggestions_created_at_idx` ON `skill_evolution_suggestions` (`created_at`);

CREATE INDEX `skill_evo_suggestions_skill_name_idx` ON `skill_evolution_suggestions` (`skill_name`);

CREATE INDEX `skill_evo_suggestions_status_idx` ON `skill_evolution_suggestions` (`status`);

CREATE INDEX `task_run_logs_task_id_idx` ON `task_run_logs` (`task_id`);

CREATE INDEX `voice_input_history_created_at_idx` ON `voice_input_history` (`created_at`);

CREATE INDEX `voice_input_hotwords_enabled_idx` ON `voice_input_hotwords` (`enabled`);

CREATE UNIQUE INDEX `voice_input_hotwords_normalized_unique` ON `voice_input_hotwords` (`normalized_text`);

CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
      DELETE FROM messages_fts WHERE rowid = OLD.rowid;
    END;

CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
      INSERT INTO messages_fts(rowid, searchable_text, chat_id, sub_chat_id, message_id, role)
      VALUES (NEW.rowid, NEW.searchable_text, NEW.chat_id, NEW.sub_chat_id, NEW.message_id, NEW.role);
    END;

CREATE TRIGGER messages_fts_au AFTER UPDATE OF searchable_text ON messages BEGIN
      DELETE FROM messages_fts WHERE rowid = OLD.rowid;
      INSERT INTO messages_fts(rowid, searchable_text, chat_id, sub_chat_id, message_id, role)
      VALUES (NEW.rowid, NEW.searchable_text, NEW.chat_id, NEW.sub_chat_id, NEW.message_id, NEW.role);
    END;
