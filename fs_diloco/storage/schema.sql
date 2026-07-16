CREATE TABLE IF NOT EXISTS run_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS global_versions (
    version INTEGER PRIMARY KEY,
    weight_path TEXT NOT NULL,
    optim_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    num_updates INTEGER NOT NULL,
    total_update_tokens INTEGER NOT NULL,
    total_seen_tokens INTEGER NOT NULL,
    outer_optimizer TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS learners (
    learner_id TEXT PRIMARY KEY,
    hostname TEXT,
    pid INTEGER,
    last_seen REAL,
    last_loaded_global_version INTEGER,
    last_local_step INTEGER,
    last_update_id TEXT,
    tokens_per_sec REAL,
    last_heartbeat_path TEXT,
    status TEXT NOT NULL,
    status_reason TEXT
);

CREATE TABLE IF NOT EXISTS updates (
    update_id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    hostname TEXT,
    base_global_version INTEGER NOT NULL,
    local_step_start INTEGER NOT NULL,
    local_step_end INTEGER NOT NULL,
    inner_steps INTEGER NOT NULL,
    tokens_this_update INTEGER NOT NULL,
    tokens_since_global_load INTEGER NOT NULL,
    num_examples_this_update INTEGER,
    train_loss REAL,
    grad_norm REAL,
    param_norm REAL,
    delta_norm REAL,
    training_cpu_utilization_peak_percent REAL,
    training_gpu_utilization_peak_percent REAL,
    local_cycle_cpu_utilization_peak_percent REAL,
    local_cycle_gpu_utilization_peak_percent REAL,
    local_cycle_step_time_seconds_mean REAL,
    local_cycle_step_count INTEGER,
    local_cycle_resource_sample_count INTEGER,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER,
    sha256 TEXT,
    created_at REAL NOT NULL,
    committed_at REAL NOT NULL,
    ingested_at REAL,
    selected_at REAL,
    applied_at REAL,
    status TEXT NOT NULL,
    selected_by_run TEXT,
    applied_version INTEGER,
    staleness_versions INTEGER,
    effective_weight REAL,
    drop_reason TEXT,
    UNIQUE(learner_id, local_step_end, base_global_version)
);

CREATE TABLE IF NOT EXISTS fragments (
    fragment_id INTEGER PRIMARY KEY,
    strategy TEXT NOT NULL,
    numel INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    slices_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fragment_versions (
    fragment_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    global_merge_event INTEGER NOT NULL,
    weight_path TEXT NOT NULL,
    optim_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    num_updates INTEGER NOT NULL,
    total_update_tokens INTEGER NOT NULL,
    total_seen_tokens INTEGER NOT NULL,
    outer_optimizer TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT,
    PRIMARY KEY (fragment_id, version)
);

CREATE TABLE IF NOT EXISTS fragment_updates (
    update_id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    hostname TEXT,
    fragment_id INTEGER NOT NULL,
    base_fragment_version INTEGER NOT NULL,
    base_global_merge_event INTEGER NOT NULL,
    local_step_start INTEGER NOT NULL,
    local_step_end INTEGER NOT NULL,
    inner_steps INTEGER NOT NULL,
    tokens_this_update INTEGER NOT NULL,
    tokens_since_fragment_load INTEGER NOT NULL,
    num_examples_this_update INTEGER,
    train_loss REAL,
    grad_norm REAL,
    param_norm REAL,
    fragment_norm REAL,
    training_cpu_utilization_peak_percent REAL,
    training_gpu_utilization_peak_percent REAL,
    local_cycle_cpu_utilization_peak_percent REAL,
    local_cycle_gpu_utilization_peak_percent REAL,
    local_cycle_step_time_seconds_mean REAL,
    local_cycle_step_count INTEGER,
    local_cycle_resource_sample_count INTEGER,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER,
    sha256 TEXT,
    created_at REAL NOT NULL,
    committed_at REAL NOT NULL,
    ingested_at REAL,
    selected_at REAL,
    applied_at REAL,
    status TEXT NOT NULL,
    selected_by_run TEXT,
    applied_fragment_version INTEGER,
    applied_global_merge_event INTEGER,
    staleness_fragment_versions INTEGER,
    staleness_global_events INTEGER,
    effective_weight REAL,
    drop_reason TEXT,
    UNIQUE(learner_id, fragment_id, local_step_end, base_fragment_version)
);

CREATE TABLE IF NOT EXISTS db_dumps (
    dump_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    global_version INTEGER NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER
);

CREATE INDEX IF NOT EXISTS idx_updates_status ON updates(status);
CREATE INDEX IF NOT EXISTS idx_updates_learner_status ON updates(learner_id, status);
CREATE INDEX IF NOT EXISTS idx_updates_base_global_version ON updates(base_global_version);
CREATE INDEX IF NOT EXISTS idx_learners_status ON learners(status);
CREATE INDEX IF NOT EXISTS idx_fragment_updates_status ON fragment_updates(status);
CREATE INDEX IF NOT EXISTS idx_fragment_updates_target
  ON fragment_updates(fragment_id, status, base_fragment_version);
CREATE INDEX IF NOT EXISTS idx_fragment_versions_event ON fragment_versions(global_merge_event);
