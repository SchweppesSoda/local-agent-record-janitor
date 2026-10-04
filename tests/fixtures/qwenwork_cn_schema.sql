CREATE TABLE "__drizzle_migrations" (id SERIAL PRIMARY KEY, hash text NOT NULL, created_at numeric);

CREATE TABLE `acp_idempotency` (
	`id` text PRIMARY KEY NOT NULL,
	`runtime_id` text NOT NULL,
	`scope` text NOT NULL,
	`key` text NOT NULL,
	`fingerprint` text NOT NULL,
	`response` text NOT NULL,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL
);

CREATE TABLE `acp_workspace_map` (
	`workspace_id` text PRIMARY KEY NOT NULL,
	`session_id` text NOT NULL,
	`root` text NOT NULL,
	`created_at` integer,
	FOREIGN KEY (`session_id`) REFERENCES `chats`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `agent_turn_inputs` (
	`id` text PRIMARY KEY NOT NULL,
	`message_id` text NOT NULL,
	`chat_id` text NOT NULL,
	`sub_chat_id` text NOT NULL,
	`source` text NOT NULL,
	`delivery_policy` text DEFAULT 'queue-only' NOT NULL,
	`state` text DEFAULT 'queued' NOT NULL,
	`position` integer NOT NULL,
	`version` integer DEFAULT 1 NOT NULL,
	`content` text NOT NULL,
	`prompt_preview` text DEFAULT '' NOT NULL,
	`attachment_count` integer DEFAULT 0 NOT NULL,
	`target_turn_id` text,
	`error_code` text,
	`error_message` text,
	`accepted_sequence` integer,
	`created_at` integer,
	`updated_at` integer,
	`accepted_at` integer,
	`handed_off_at` integer,
	`target_turn_completed_at` integer,
	`blocked_by_pause` integer DEFAULT false NOT NULL,
	`steer_origin` text,
	FOREIGN KEY (`chat_id`) REFERENCES `chats`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`sub_chat_id`) REFERENCES `sub_chats`(`id`) ON UPDATE no action ON DELETE cascade
);

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
	`base_branch` text, `pr_url` text, `pr_number` integer, `additional_directories` text, `output_directory` text, `source` text, `wecom_account_id` text, `wecom_user_id` text, `chat_type` text DEFAULT 'task', `ext` text, `source_chat_id` text, `deleted_at` integer, `local_project_id` text REFERENCES `local_projects`(`id`) ON UPDATE no action ON DELETE set null, `version` integer DEFAULT 0,
	FOREIGN KEY (`project_id`) REFERENCES `projects`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `client_config_module_cache` (
	`source_id` text NOT NULL,
	`scope_fingerprint` text NOT NULL,
	`module_key` text NOT NULL,
	`schema_version` integer NOT NULL,
	`generation` integer NOT NULL,
	`fetched_at` integer NOT NULL,
	`content_hash` text NOT NULL,
	`payload_json` text NOT NULL,
	PRIMARY KEY(`source_id`, `scope_fingerprint`, `module_key`)
);

CREATE TABLE `data_import_records` (
	`id` text PRIMARY KEY NOT NULL,
	`source_instance` text NOT NULL,
	`capability` text NOT NULL,
	`entity_type` text NOT NULL,
	`source_id` text NOT NULL,
	`target_id` text NOT NULL,
	`state` text DEFAULT 'planned' NOT NULL,
	`created_at` integer NOT NULL,
	`imported_at` integer
);

CREATE TABLE `enterprise_managed_resources` (
	`org_id` text NOT NULL,
	`user_id` text NOT NULL,
	`resource_type` text NOT NULL,
	`resource_origin` text NOT NULL,
	`resource_id` text NOT NULL,
	`folder_name` text NOT NULL,
	`resolved_revision_id` text,
	`target_version` text,
	`content_hash` text,
	`source_market_item_id` text,
	`last_state_hash` text,
	`lease_expires_at` integer,
	`installed_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	PRIMARY KEY(`org_id`, `user_id`, `resource_type`, `resource_id`)
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

CREATE TABLE `local_projects` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`root_paths` text DEFAULT '[]' NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	`deleted_at` integer
, `icon` text NOT NULL DEFAULT 'newfolder-close', `icon_color` text NOT NULL DEFAULT 'black');

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

CREATE TABLE `scheduled_task_admission_cursors` (
	`task_id` text PRIMARY KEY NOT NULL,
	`schedule` text NOT NULL,
	`materialized_through_at` integer NOT NULL,
	FOREIGN KEY (`task_id`) REFERENCES `scheduled_tasks`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `scheduled_task_admissions` (
	`id` text PRIMARY KEY NOT NULL,
	`task_id` text NOT NULL,
	`scheduled_at` integer NOT NULL,
	`reserve_at` integer NOT NULL,
	`request_body` text NOT NULL,
	`state` text NOT NULL,
	`occurrence_id` text,
	`execute_at` integer,
	`clock_offset_ms` integer,
	`fallback_at` integer NOT NULL,
	`next_retry_at` integer,
	`retry_count` integer DEFAULT 0 NOT NULL,
	`request_id` text,
	`last_http_status` integer,
	`last_error_code` text,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL, `protocol_version` integer DEFAULT 1 NOT NULL, `account_id` text, `environment_key` text, `execute_at_raw` text, `expires_at_raw` text, `expires_at` integer, `fallback_eligible` integer DEFAULT false NOT NULL, `next_poll_at` integer, `request_started_at` integer, `started_at` integer, `waiting_previous` integer DEFAULT false NOT NULL, `operation_version` integer DEFAULT 0 NOT NULL, `projected_at` integer, `local_initial` integer DEFAULT false NOT NULL,
	FOREIGN KEY (`task_id`) REFERENCES `scheduled_tasks`(`id`) ON UPDATE no action ON DELETE cascade
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
	`updated_at` integer, `source_chat_id` text, `source_sub_chat_id` text, `deleted_at` integer, `local_project_id` text REFERENCES `local_projects`(`id`) ON UPDATE no action ON DELETE set null,
	FOREIGN KEY (`project_id`) REFERENCES `projects`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `session_event_log` (
	`id` text PRIMARY KEY NOT NULL,
	`session_id` text NOT NULL,
	`event_stream_id` text NOT NULL,
	`runtime_id` text NOT NULL,
	`event_id` text NOT NULL,
	`source_event_id` text NOT NULL,
	`seq` integer NOT NULL,
	`seq_ids` text NOT NULL,
	`turn_id` text,
	`payload` blob NOT NULL,
	`occurred_at` text NOT NULL,
	`acked_at` integer,
	`created_at` integer NOT NULL,
	FOREIGN KEY (`session_id`) REFERENCES `chats`(`id`) ON UPDATE no action ON DELETE cascade
);

CREATE TABLE `session_event_streams` (
	`session_id` text PRIMARY KEY NOT NULL,
	`runtime_id` text NOT NULL,
	`event_stream_id` text NOT NULL,
	`high_watermark_seq` integer DEFAULT 0 NOT NULL,
	`last_rotation_reason` text,
	`rotated_at` integer,
	`created_at` integer,
	FOREIGN KEY (`session_id`) REFERENCES `chats`(`id`) ON UPDATE no action ON DELETE cascade
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

CREATE UNIQUE INDEX `acp_idempotency_scope_key_unique` ON `acp_idempotency` (`runtime_id`,`scope`,`key`);

CREATE UNIQUE INDEX `acp_workspace_map_session_root_unique` ON `acp_workspace_map` (`session_id`,`root`);

CREATE UNIQUE INDEX `agent_turn_inputs_sub_chat_message_unique` ON `agent_turn_inputs` (`sub_chat_id`,`message_id`);

CREATE INDEX `agent_turn_inputs_sub_chat_state_position_idx` ON `agent_turn_inputs` (`sub_chat_id`,`state`,`position`);

CREATE INDEX `byok_custom_models_legacy_key_idx` ON `byok_custom_models` (`legacy_key`);

CREATE INDEX `byok_custom_models_source_idx` ON `byok_custom_models` (`source`);

CREATE INDEX `chats_local_project_sidebar_idx` ON `chats` (`local_project_id`,`deleted_at`,`archived_at`,"updated_at" DESC,"id" DESC);

CREATE INDEX `chats_sidebar_active_updated_idx` ON `chats` (`deleted_at`,`archived_at`,"updated_at" DESC,"id" DESC);

CREATE INDEX `chats_worktree_path_idx` ON `chats` (`worktree_path`);

CREATE INDEX `data_import_records_source_capability_idx` ON `data_import_records` (`source_instance`,`capability`,`state`);

CREATE UNIQUE INDEX `data_import_records_source_identity_unique` ON `data_import_records` (`source_instance`,`capability`,`entity_type`,`source_id`);

CREATE INDEX `idx_enterprise_managed_folder` ON `enterprise_managed_resources` (`org_id`,`user_id`,`folder_name`);

CREATE INDEX `idx_pairing_channel_conv` ON `channel_pairings` (`channel_id`,`conversation_id`);

CREATE UNIQUE INDEX `idx_pairing_v2_robot_binding` ON `channel_pairings_v2` (`channel_id`,`robot_id`,`binding_key`);

CREATE INDEX `local_projects_deleted_created_id_idx` ON `local_projects` (`deleted_at`,"created_at" DESC,"id" DESC);

CREATE UNIQUE INDEX `mcp_oauth_tokens_by_user_unique` ON `mcp_oauth_tokens_by_user` (`user_id`,`server_name`);

CREATE INDEX `messages_chat_created_at_idx` ON `messages` (`chat_id`,`created_at`);

CREATE UNIQUE INDEX `messages_sub_chat_message_unique` ON `messages` (`sub_chat_id`,`message_id`);

CREATE INDEX `messages_sub_chat_sequence_idx` ON `messages` (`sub_chat_id`,`sequence`);

CREATE UNIQUE INDEX `messages_sub_chat_sequence_unique` ON `messages` (`sub_chat_id`,`sequence`);

CREATE INDEX `nudge_logs_created_at_idx` ON `nudge_logs` (`created_at`);

CREATE INDEX `nudge_logs_review_id_idx` ON `nudge_logs` (`review_id`);

CREATE UNIQUE INDEX `projects_path_unique` ON `projects` (`path`);

CREATE UNIQUE INDEX `rc_session_mappings_remote_session_id_idx` ON `rc_session_mappings` (`remote_session_id`);

CREATE INDEX `scheduled_task_admissions_state_idx` ON `scheduled_task_admissions` (`state`);

CREATE UNIQUE INDEX `scheduled_task_admissions_task_occurrence_idx` ON `scheduled_task_admissions` (`task_id`,`scheduled_at`);

CREATE INDEX `scheduled_task_admissions_task_state_idx` ON `scheduled_task_admissions` (`task_id`,`state`);

CREATE INDEX `session_event_log_runtime_acked_idx` ON `session_event_log` (`runtime_id`,`acked_at`);

CREATE UNIQUE INDEX `session_event_log_stream_event_id_unique` ON `session_event_log` (`session_id`,`event_stream_id`,`event_id`);

CREATE INDEX `session_event_log_stream_seq_idx` ON `session_event_log` (`session_id`,`event_stream_id`,`seq`);

CREATE UNIQUE INDEX `session_event_log_stream_seq_unique` ON `session_event_log` (`session_id`,`event_stream_id`,`seq`);

CREATE UNIQUE INDEX `session_event_log_stream_source_id_unique` ON `session_event_log` (`session_id`,`event_stream_id`,`source_event_id`);

CREATE INDEX `skill_evo_suggestions_created_at_idx` ON `skill_evolution_suggestions` (`created_at`);

CREATE INDEX `skill_evo_suggestions_skill_name_idx` ON `skill_evolution_suggestions` (`skill_name`);

CREATE INDEX `skill_evo_suggestions_status_idx` ON `skill_evolution_suggestions` (`status`);

CREATE INDEX `task_run_logs_chat_id_idx` ON `task_run_logs` (`chat_id`);

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
