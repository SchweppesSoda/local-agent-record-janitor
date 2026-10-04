-- Schema-only fixture from installed Cindy, observed 2026-09-29. No user records.
CREATE TABLE `schedule_runs` (
	`id` text PRIMARY KEY NOT NULL,
	`schedule_id` text NOT NULL,
	`session_id` text,
	`fired_at` integer NOT NULL,
	`finished_at` integer,
	`status` text NOT NULL,
	`error_msg` text, `read_at` integer, `result_text` text, heartbeat_at integer, pre_run_hook_result text, cost_usd real DEFAULT 0 NOT NULL, estimated_value_usd real DEFAULT 0 NOT NULL, cost_attribution text DEFAULT 'legacy' NOT NULL, cost_amount real DEFAULT 0 NOT NULL, estimated_value_amount real DEFAULT 0 NOT NULL, cost_currency text, cost_is_approximate integer DEFAULT 0 NOT NULL,
	FOREIGN KEY (`schedule_id`) REFERENCES `schedules`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`session_id`) REFERENCES `sessions`(`id`) ON UPDATE no action ON DELETE set null
);
CREATE TABLE schedule_session_latest_runs (
        session_id text PRIMARY KEY NOT NULL,
        run_id text NOT NULL,
        fired_at integer NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON UPDATE no action ON DELETE cascade,
        FOREIGN KEY (run_id) REFERENCES schedule_runs(id) ON UPDATE no action ON DELETE cascade
      );
CREATE INDEX idx_schedule_runs_running_heartbeat
        ON schedule_runs (heartbeat_at)
        WHERE status = 'running' AND heartbeat_at IS NOT NULL;
CREATE INDEX idx_schedule_runs_running_legacy
        ON schedule_runs (fired_at)
        WHERE status = 'running' AND heartbeat_at IS NULL;
CREATE INDEX idx_schedule_runs_running_schedule
        ON schedule_runs (schedule_id) WHERE status = 'running';
CREATE INDEX `idx_schedule_runs_schedule` ON `schedule_runs` (`schedule_id`,`fired_at`);
CREATE INDEX idx_schedule_runs_session_latest
        ON schedule_runs (session_id, fired_at, id) WHERE session_id IS NOT NULL;
CREATE INDEX idx_schedule_runs_unread_terminal
        ON schedule_runs (schedule_id, status, fired_at)
        WHERE read_at IS NULL
          AND status IN ('success', 'failed', 'aborted', 'interrupted');
CREATE UNIQUE INDEX idx_schedule_session_latest_runs_run
        ON schedule_session_latest_runs (run_id);
CREATE TRIGGER schedule_session_latest_run_delete
    AFTER DELETE ON schedule_runs
    WHEN OLD.session_id IS NOT NULL
    BEGIN
      DELETE FROM schedule_session_latest_runs
      WHERE session_id = OLD.session_id AND run_id = OLD.id;
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      SELECT OLD.session_id, id, fired_at
      FROM schedule_runs
      WHERE session_id = OLD.session_id
      ORDER BY fired_at DESC, id DESC
      LIMIT 1
      ON CONFLICT(session_id) DO UPDATE SET
        run_id = excluded.run_id,
        fired_at = excluded.fired_at;
    END;
CREATE TRIGGER schedule_session_latest_run_insert
    AFTER INSERT ON schedule_runs
    WHEN NEW.session_id IS NOT NULL
    BEGIN
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      VALUES (NEW.session_id, NEW.id, NEW.fired_at)
      ON CONFLICT(session_id) DO UPDATE SET
        run_id = excluded.run_id,
        fired_at = excluded.fired_at
      WHERE excluded.fired_at > schedule_session_latest_runs.fired_at
        OR (excluded.fired_at = schedule_session_latest_runs.fired_at
          AND excluded.run_id > schedule_session_latest_runs.run_id);
    END;
CREATE TRIGGER schedule_session_latest_run_update
    AFTER UPDATE OF session_id, fired_at ON schedule_runs
    BEGIN
      DELETE FROM schedule_session_latest_runs
      WHERE session_id = OLD.session_id AND run_id = OLD.id;
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      SELECT OLD.session_id, id, fired_at
      FROM schedule_runs
      WHERE OLD.session_id IS NOT NULL AND session_id = OLD.session_id
      ORDER BY fired_at DESC, id DESC
      LIMIT 1
      ON CONFLICT(session_id) DO UPDATE SET
        run_id = excluded.run_id,
        fired_at = excluded.fired_at;
      INSERT INTO schedule_session_latest_runs (session_id, run_id, fired_at)
      SELECT NEW.session_id, NEW.id, NEW.fired_at
      WHERE NEW.session_id IS NOT NULL
      ON CONFLICT(session_id) DO UPDATE SET
        run_id = excluded.run_id,
        fired_at = excluded.fired_at
      WHERE excluded.fired_at > schedule_session_latest_runs.fired_at
        OR (excluded.fired_at = schedule_session_latest_runs.fired_at
          AND excluded.run_id > schedule_session_latest_runs.run_id);
    END;
