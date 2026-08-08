CREATE TABLE schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 5),
    protocol_version INTEGER NOT NULL CHECK (protocol_version = 4),
    mode TEXT NOT NULL CHECK (mode IN ('static', 'dynamic')),
    features_json TEXT NOT NULL,
    ddl_sha256 TEXT NOT NULL CHECK (length(ddl_sha256) = 64),
    schema_fingerprint TEXT NOT NULL CHECK (length(schema_fingerprint) = 64),
    created_at REAL NOT NULL CHECK (created_at >= 0)
);

CREATE TABLE run_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    run_id TEXT NOT NULL UNIQUE,
    source_fingerprint TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64)
);

CREATE TABLE command_records (
    command_id TEXT PRIMARY KEY,
    command_kind TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
    result_json TEXT NOT NULL,
    committed_at REAL NOT NULL CHECK (committed_at >= 0)
);

CREATE TABLE syncer_leader (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'released')),
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid >= 0),
    acquired_at REAL NOT NULL,
    renewed_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    heartbeat_seq INTEGER NOT NULL CHECK (heartbeat_seq >= 1),
    CHECK (lease_expires_at >= renewed_at),
    CHECK (renewed_at >= acquired_at)
);

CREATE TABLE syncer_epochs (
    epoch INTEGER PRIMARY KEY CHECK (epoch >= 1),
    owner_id TEXT NOT NULL,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid >= 0),
    acquired_at REAL NOT NULL,
    last_renewed_at REAL NOT NULL,
    final_state TEXT CHECK (final_state IN ('released', 'expired', 'terminal', 'error')),
    final_at REAL,
    superseded_by_epoch INTEGER,
    source_fingerprint TEXT NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
    UNIQUE(epoch, owner_id)
);

CREATE TABLE controller_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL CHECK (state IN ('open', 'closing', 'draining', 'finalized', 'error')),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    reason TEXT,
    requested_at REAL,
    hard_crash_cycle_token_budget INTEGER NOT NULL DEFAULT 0
        CHECK (hard_crash_cycle_token_budget >= 0),
    updated_by_epoch INTEGER,
    updated_by_owner_id TEXT
);

CREATE TABLE terminal_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    state TEXT NOT NULL CHECK (state IN ('finalized', 'error')),
    stop_reason TEXT NOT NULL,
    final_version INTEGER NOT NULL CHECK (final_version >= 0),
    direct_weight_tokens_applied INTEGER NOT NULL CHECK (direct_weight_tokens_applied >= 0),
    finalized_by_epoch INTEGER NOT NULL CHECK (finalized_by_epoch >= 1),
    finalized_by_owner_id TEXT NOT NULL,
    finalized_at REAL NOT NULL
);

CREATE TABLE terminal_contributor_fences (
    generation INTEGER NOT NULL CHECK (generation >= 1),
    stable_contributor_key TEXT NOT NULL,
    fence_kind TEXT NOT NULL CHECK (fence_kind IN ('static', 'dynamic')),
    fence_json TEXT NOT NULL,
    close_last_cycle_seq INTEGER NOT NULL CHECK (close_last_cycle_seq >= 0),
    close_data_cursor INTEGER NOT NULL CHECK (close_data_cursor >= 0),
    state TEXT NOT NULL CHECK (state IN ('awaiting_ack', 'acked', 'hard_crash')),
    final_cycle_seq INTEGER,
    final_update_id TEXT,
    hard_crash_gap_tokens_upper_bound INTEGER NOT NULL DEFAULT 0
        CHECK (hard_crash_gap_tokens_upper_bound >= 0),
    acknowledged_at REAL,
    acknowledged_by_epoch INTEGER,
    PRIMARY KEY(generation, stable_contributor_key),
    UNIQUE(generation, fence_json),
    CHECK ((state = 'awaiting_ack' AND final_cycle_seq IS NULL
            AND final_update_id IS NULL AND acknowledged_at IS NULL)
        OR (state = 'acked' AND final_cycle_seq IS NOT NULL
            AND acknowledged_at IS NOT NULL)
        OR (state = 'hard_crash' AND final_cycle_seq IS NULL
            AND final_update_id IS NULL AND acknowledged_at IS NOT NULL))
);

CREATE TABLE static_contributor_bindings (
    learner_id TEXT PRIMARY KEY,
    logical_launch_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL CHECK (binding_generation >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'terminal', 'replaced')),
    bound_by_epoch INTEGER NOT NULL CHECK (bound_by_epoch >= 1),
    bound_at REAL NOT NULL,
    terminal_at REAL,
    UNIQUE(logical_launch_id, attempt_id),
    UNIQUE(learner_id, binding_generation)
);

CREATE TABLE static_binding_history (
    learner_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL CHECK (binding_generation >= 1),
    logical_launch_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    final_status TEXT NOT NULL CHECK (final_status IN ('terminal', 'replaced')),
    bound_by_epoch INTEGER NOT NULL,
    bound_at REAL NOT NULL,
    finalized_by_epoch INTEGER NOT NULL,
    finalized_at REAL NOT NULL,
    PRIMARY KEY(learner_id, binding_generation)
);

CREATE TABLE proposal_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_contributor_key TEXT NOT NULL,
    cycle_seq INTEGER NOT NULL CHECK (cycle_seq >= 1),
    update_id TEXT,
    pointer_sequence INTEGER,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'accepted', 'exact_replay', 'conflict', 'identity_collision', 'malformed',
        'missing', 'identity_mismatch', 'manual_review', 'stale_fence', 'post_fence'
    )),
    diagnostic TEXT,
    source_relative_path TEXT,
    object_sha256 TEXT,
    observed_by_epoch INTEGER NOT NULL CHECK (observed_by_epoch >= 1),
    observed_at REAL NOT NULL
);

CREATE TABLE proposal_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL UNIQUE REFERENCES proposal_observations(observation_id),
    conflict_kind TEXT NOT NULL CHECK (conflict_kind IN ('logical_key', 'identity_collision')),
    existing_update_id TEXT,
    incoming_update_id TEXT,
    bounded_diagnostic TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64)
);

CREATE TABLE proposal_frontiers (
    run_id TEXT NOT NULL,
    stable_contributor_key TEXT NOT NULL,
    last_terminal_cycle_seq INTEGER NOT NULL CHECK (last_terminal_cycle_seq >= 1),
    terminal_observation_id INTEGER NOT NULL
        REFERENCES proposal_observations(observation_id),
    updated_by_epoch INTEGER NOT NULL CHECK (updated_by_epoch >= 1),
    updated_at REAL NOT NULL,
    PRIMARY KEY(run_id, stable_contributor_key)
);

CREATE TABLE proposal_visibility (
    visibility_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_contributor_key TEXT NOT NULL,
    cycle_seq INTEGER NOT NULL CHECK (cycle_seq >= 1),
    update_id TEXT NOT NULL,
    object_identity TEXT NOT NULL,
    pointer_signature TEXT NOT NULL,
    pointer_sequence INTEGER NOT NULL CHECK (pointer_sequence >= 0),
    first_observed_at REAL NOT NULL,
    first_stable_failure_at REAL,
    last_observed_at REAL NOT NULL,
    stable_failure_count INTEGER NOT NULL CHECK (stable_failure_count >= 0),
    last_read_status TEXT NOT NULL CHECK (last_read_status IN (
        'ok', 'not_found', 'transient_io', 'malformed', 'identity_mismatch'
    )),
    last_fingerprint TEXT,
    bounded_diagnostic TEXT,
    terminal_disposition TEXT CHECK (terminal_disposition IN (
        'missing', 'malformed', 'identity_mismatch', 'manual_review'
    )),
    terminal_observation_id INTEGER REFERENCES proposal_observations(observation_id),
    UNIQUE(stable_contributor_key, object_identity, pointer_signature)
);

CREATE TABLE proposal_visibility_archive (
    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_contributor_key TEXT NOT NULL,
    object_identity TEXT NOT NULL,
    pointer_signature TEXT NOT NULL,
    last_read_status TEXT NOT NULL,
    stable_failure_count INTEGER NOT NULL CHECK (stable_failure_count >= 0),
    last_fingerprint TEXT,
    terminal_disposition TEXT,
    archived_at REAL NOT NULL,
    UNIQUE(stable_contributor_key, object_identity, pointer_signature)
);

CREATE TABLE proposal_quarantine (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_contributor_key TEXT NOT NULL,
    cycle_seq INTEGER NOT NULL CHECK (cycle_seq >= 1),
    update_id TEXT,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'conflict', 'identity_collision', 'malformed', 'missing',
        'identity_mismatch', 'manual_review'
    )),
    fingerprint TEXT NOT NULL,
    bounded_diagnostic TEXT NOT NULL,
    source_relative_path TEXT,
    observation_id INTEGER NOT NULL UNIQUE REFERENCES proposal_observations(observation_id),
    quarantined_at REAL NOT NULL,
    UNIQUE(stable_contributor_key, cycle_seq, disposition, fingerprint)
);

CREATE TABLE cycle_receipts (
    receipt_id TEXT PRIMARY KEY,
    receipt_sha256 TEXT NOT NULL UNIQUE CHECK (length(receipt_sha256) = 64),
    run_id TEXT NOT NULL,
    stable_contributor_key TEXT NOT NULL,
    cycle_seq INTEGER NOT NULL CHECK (cycle_seq >= 1),
    cycle_id TEXT NOT NULL,
    previous_receipt_id TEXT,
    previous_receipt_sha256 TEXT,
    processed_tokens_this_cycle INTEGER NOT NULL CHECK (processed_tokens_this_cycle > 0),
    effective_tokens_this_cycle INTEGER NOT NULL CHECK (effective_tokens_this_cycle >= 0),
    local_discarded_tokens_this_cycle INTEGER NOT NULL
        CHECK (local_discarded_tokens_this_cycle >= 0),
    retained_tokens_since_base INTEGER NOT NULL CHECK (retained_tokens_since_base >= 0),
    data_cursor_start INTEGER NOT NULL CHECK (data_cursor_start >= 0),
    data_cursor_end INTEGER NOT NULL CHECK (data_cursor_end > data_cursor_start),
    proposal_expected INTEGER NOT NULL CHECK (proposal_expected IN (0, 1)),
    planned_update_id TEXT,
    planned_payload_sha256 TEXT,
    fence_kind TEXT NOT NULL CHECK (fence_kind IN ('static', 'dynamic')),
    fence_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    ingested_at REAL NOT NULL,
    ingested_by_epoch INTEGER NOT NULL CHECK (ingested_by_epoch >= 1),
    UNIQUE(run_id, stable_contributor_key, cycle_seq),
    CHECK (processed_tokens_this_cycle =
        effective_tokens_this_cycle + local_discarded_tokens_this_cycle),
    CHECK (retained_tokens_since_base >= effective_tokens_this_cycle),
    CHECK ((cycle_seq = 1 AND previous_receipt_id IS NULL AND previous_receipt_sha256 IS NULL)
        OR (cycle_seq > 1 AND previous_receipt_id IS NOT NULL
            AND length(previous_receipt_sha256) = 64)),
    CHECK ((proposal_expected = 0 AND planned_update_id IS NULL
            AND planned_payload_sha256 IS NULL)
        OR (proposal_expected = 1 AND effective_tokens_this_cycle > 0
            AND planned_update_id IS NOT NULL AND length(planned_payload_sha256) = 64))
);

CREATE TABLE contributor_progress (
    stable_contributor_key TEXT PRIMARY KEY,
    last_cycle_seq INTEGER NOT NULL CHECK (last_cycle_seq >= 0),
    last_receipt_id TEXT REFERENCES cycle_receipts(receipt_id),
    last_receipt_sha256 TEXT,
    data_cursor INTEGER NOT NULL CHECK (data_cursor >= 0),
    updated_by_epoch INTEGER NOT NULL CHECK (updated_by_epoch >= 1),
    updated_at REAL NOT NULL,
    CHECK ((last_cycle_seq = 0 AND last_receipt_id IS NULL AND last_receipt_sha256 IS NULL)
        OR (last_cycle_seq > 0 AND last_receipt_id IS NOT NULL
            AND length(last_receipt_sha256) = 64))
);

CREATE TABLE selection_batches (
    batch_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
    target_version INTEGER NOT NULL CHECK (target_version >= 0),
    state TEXT NOT NULL CHECK (state IN ('selected', 'prepared', 'committed', 'abandoned')),
    created_at REAL NOT NULL,
    prepared_at REAL,
    committed_at REAL,
    abandoned_at REAL,
    abandon_reason TEXT
);

CREATE TABLE updates (
    update_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    stable_contributor_key TEXT NOT NULL,
    cycle_seq INTEGER NOT NULL CHECK (cycle_seq >= 1),
    cycle_id TEXT NOT NULL,
    cycle_receipt_id TEXT NOT NULL REFERENCES cycle_receipts(receipt_id),
    cycle_receipt_sha256 TEXT NOT NULL CHECK (length(cycle_receipt_sha256) = 64),
    proposal_sha256 TEXT NOT NULL CHECK (length(proposal_sha256) = 64),
    base_global_version INTEGER NOT NULL CHECK (base_global_version >= 0),
    local_step_start INTEGER NOT NULL CHECK (local_step_start >= 0),
    local_step_end INTEGER NOT NULL,
    inner_steps INTEGER NOT NULL CHECK (inner_steps > 0),
    processed_tokens_this_cycle INTEGER NOT NULL CHECK (processed_tokens_this_cycle > 0),
    effective_tokens_this_update INTEGER NOT NULL CHECK (effective_tokens_this_update > 0),
    local_discarded_tokens_this_cycle INTEGER NOT NULL
        CHECK (local_discarded_tokens_this_cycle >= 0),
    retained_tokens_since_base INTEGER NOT NULL,
    data_cursor_start INTEGER NOT NULL CHECK (data_cursor_start >= 0),
    data_cursor_end INTEGER NOT NULL,
    fence_kind TEXT NOT NULL CHECK (fence_kind IN ('static', 'dynamic')),
    fence_json TEXT NOT NULL,
    payload_relative_path TEXT NOT NULL,
    payload_size INTEGER NOT NULL CHECK (payload_size > 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    tensor_schema_sha256 TEXT NOT NULL CHECK (length(tensor_schema_sha256) = 64),
    tensor_dtype TEXT NOT NULL CHECK (tensor_dtype IN ('float32', 'bfloat16')),
    tensor_numel INTEGER NOT NULL CHECK (tensor_numel > 0),
    created_at REAL NOT NULL,
    ingested_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'selected', 'applied', 'dropped')),
    selected_batch_id TEXT REFERENCES selection_batches(batch_id),
    selected_by_epoch INTEGER,
    applied_version INTEGER,
    applied_by_epoch INTEGER,
    dropped_by_epoch INTEGER,
    drop_reason TEXT,
    UNIQUE(run_id, stable_contributor_key, cycle_seq),
    CHECK (local_step_end = local_step_start + inner_steps),
    CHECK (processed_tokens_this_cycle =
        effective_tokens_this_update + local_discarded_tokens_this_cycle),
    CHECK (retained_tokens_since_base >= effective_tokens_this_update),
    CHECK (data_cursor_end > data_cursor_start),
    CHECK ((status = 'selected' AND selected_batch_id IS NOT NULL)
        OR status <> 'selected')
);

CREATE TABLE selection_batch_updates (
    batch_id TEXT NOT NULL REFERENCES selection_batches(batch_id),
    update_id TEXT NOT NULL REFERENCES updates(update_id),
    stable_contributor_key TEXT NOT NULL,
    reduction_order INTEGER NOT NULL CHECK (reduction_order >= 0),
    raw_weight REAL NOT NULL CHECK (raw_weight > 0),
    PRIMARY KEY(batch_id, update_id),
    UNIQUE(batch_id, stable_contributor_key),
    UNIQUE(batch_id, reduction_order)
);

CREATE TABLE selection_state (
    stable_contributor_key TEXT PRIMARY KEY,
    committed_credit INTEGER NOT NULL CHECK (committed_credit >= 0),
    last_committed_version INTEGER,
    updated_by_epoch INTEGER NOT NULL CHECK (updated_by_epoch >= 1),
    updated_at REAL NOT NULL
);

CREATE TABLE publication_intents (
    publication_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
    target_version INTEGER NOT NULL CHECK (target_version >= 0),
    predecessor_version INTEGER,
    selection_batch_id TEXT UNIQUE REFERENCES selection_batches(batch_id),
    weight_relative_path TEXT NOT NULL,
    weight_size INTEGER NOT NULL CHECK (weight_size > 0),
    weight_sha256 TEXT NOT NULL CHECK (length(weight_sha256) = 64),
    optim_relative_path TEXT NOT NULL,
    optim_size INTEGER NOT NULL CHECK (optim_size > 0),
    optim_sha256 TEXT NOT NULL CHECK (length(optim_sha256) = 64),
    theta_sha256 TEXT NOT NULL CHECK (length(theta_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'committed', 'abandoned')),
    prepared_at REAL NOT NULL,
    committed_at REAL,
    abandoned_at REAL,
    abandon_reason TEXT,
    repairs_publication_id TEXT REFERENCES publication_intents(publication_id),
    CHECK ((target_version = 0 AND predecessor_version IS NULL AND selection_batch_id IS NULL)
        OR (target_version > 0 AND predecessor_version = target_version - 1
            AND selection_batch_id IS NOT NULL))
);

CREATE TABLE global_versions (
    version INTEGER PRIMARY KEY CHECK (version >= 0),
    predecessor_version INTEGER UNIQUE,
    publication_id TEXT NOT NULL UNIQUE REFERENCES publication_intents(publication_id),
    weight_relative_path TEXT NOT NULL,
    weight_size INTEGER NOT NULL CHECK (weight_size > 0),
    weight_sha256 TEXT NOT NULL CHECK (length(weight_sha256) = 64),
    optim_relative_path TEXT NOT NULL,
    optim_size INTEGER NOT NULL CHECK (optim_size > 0),
    optim_sha256 TEXT NOT NULL CHECK (length(optim_sha256) = 64),
    theta_sha256 TEXT NOT NULL CHECK (length(theta_sha256) = 64),
    committed_by_epoch INTEGER NOT NULL CHECK (committed_by_epoch >= 1),
    committed_by_owner_id TEXT NOT NULL,
    committed_at REAL NOT NULL,
    direct_weight_tokens_applied INTEGER NOT NULL CHECK (direct_weight_tokens_applied >= 0),
    CHECK ((version = 0 AND predecessor_version IS NULL)
        OR (version > 0 AND predecessor_version = version - 1))
);

CREATE UNIQUE INDEX idx_publication_target_committed
    ON publication_intents(target_version) WHERE state = 'committed';
-- Pending supersession is an insert-then-terminalize transition inside one
-- authority transaction, so an immediate UNIQUE index cannot represent it.
-- The authority command owns the at-most-one-pending invariant at commit;
-- selected rows remain independently constrained here.
CREATE UNIQUE INDEX idx_selected_update_per_contributor
    ON updates(stable_contributor_key) WHERE status = 'selected';
CREATE INDEX idx_pending_update_per_contributor
    ON updates(stable_contributor_key, cycle_seq) WHERE status = 'pending';
CREATE INDEX idx_updates_status ON updates(status, base_global_version);
CREATE INDEX idx_observations_contributor
    ON proposal_observations(stable_contributor_key, observation_id);
CREATE INDEX idx_visibility_contributor
    ON proposal_visibility(stable_contributor_key, pointer_sequence);
CREATE INDEX idx_visibility_archive_contributor
    ON proposal_visibility_archive(stable_contributor_key, archive_id);

CREATE TABLE token_fates (
    receipt_id TEXT PRIMARY KEY REFERENCES cycle_receipts(receipt_id),
    stable_contributor_key TEXT NOT NULL,
    local_discarded_tokens INTEGER NOT NULL CHECK (local_discarded_tokens >= 0),
    direct_weight_tokens INTEGER NOT NULL CHECK (direct_weight_tokens >= 0),
    direct_fate TEXT NOT NULL CHECK (direct_fate IN (
        'outstanding', 'applied', 'dropped', 'quarantined', 'conflicted', 'unpublished'
    )),
    fate_reason TEXT,
    applied_version INTEGER REFERENCES global_versions(version),
    updated_by_epoch INTEGER NOT NULL CHECK (updated_by_epoch >= 1),
    updated_at REAL NOT NULL
);

CREATE TABLE token_rollups (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    adjudicated_processed INTEGER NOT NULL CHECK (adjudicated_processed >= 0),
    local_discarded INTEGER NOT NULL CHECK (local_discarded >= 0),
    direct_applied INTEGER NOT NULL CHECK (direct_applied >= 0),
    direct_dropped INTEGER NOT NULL CHECK (direct_dropped >= 0),
    direct_quarantined_or_conflicted INTEGER NOT NULL
        CHECK (direct_quarantined_or_conflicted >= 0),
    direct_reported_unpublished INTEGER NOT NULL CHECK (direct_reported_unpublished >= 0),
    direct_outstanding INTEGER NOT NULL CHECK (direct_outstanding >= 0),
    carried_ancestry INTEGER NOT NULL CHECK (carried_ancestry >= 0),
    updated_by_epoch INTEGER NOT NULL CHECK (updated_by_epoch >= 1),
    updated_at REAL NOT NULL
);

CREATE TABLE artifact_publications (
    publication_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    owning_epoch INTEGER NOT NULL CHECK (owning_epoch >= 1),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'committed', 'orphan', 'deleted')),
    created_at REAL NOT NULL,
    PRIMARY KEY(publication_id, artifact_kind),
    UNIQUE(relative_path),
    CHECK (artifact_kind IN ('weight', 'outer_state'))
);

CREATE TABLE control_publications (
    kind TEXT NOT NULL,
    logical_generation INTEGER NOT NULL CHECK (logical_generation >= 0),
    published_by_epoch INTEGER NOT NULL CHECK (published_by_epoch >= 1),
    published_by_owner_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    created_at REAL NOT NULL,
    PRIMARY KEY(kind, logical_generation, published_by_epoch)
);

CREATE TABLE archive_batches (
    archive_batch_id TEXT PRIMARY KEY,
    owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
    record_kind TEXT NOT NULL,
    cutoff_version INTEGER NOT NULL CHECK (cutoff_version >= 0),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('prepared', 'committed', 'deleted')),
    created_at REAL NOT NULL
);

CREATE TABLE archive_partitions (
    partition_id TEXT PRIMARY KEY,
    owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
    record_kind TEXT NOT NULL,
    source_batch_count INTEGER NOT NULL CHECK (source_batch_count >= 1),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    manifest_relative_path TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('committed', 'deleted')),
    created_at REAL NOT NULL
);

CREATE TABLE audit_partition_batches (
    partition_id TEXT NOT NULL REFERENCES archive_partitions(partition_id),
    archive_batch_id TEXT NOT NULL UNIQUE,
    record_kind TEXT NOT NULL,
    cutoff_version INTEGER NOT NULL CHECK (cutoff_version >= 0),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    PRIMARY KEY(partition_id, archive_batch_id)
);

CREATE TABLE audit_gc_candidates (
    relative_path TEXT PRIMARY KEY,
    partition_id TEXT NOT NULL REFERENCES archive_partitions(partition_id),
    archive_batch_id TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'deleted')),
    recorded_by_epoch INTEGER NOT NULL CHECK (recorded_by_epoch >= 1),
    recorded_at REAL NOT NULL,
    claimed_by_epoch INTEGER CHECK (claimed_by_epoch >= 1),
    claimed_at REAL,
    deleted_at REAL
);

CREATE TABLE gc_candidates (
    relative_path TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL,
    owning_epoch INTEGER NOT NULL CHECK (owning_epoch >= 1),
    publication_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'deleted', 'cancelled')),
    not_before REAL NOT NULL,
    recorded_by_epoch INTEGER NOT NULL CHECK (recorded_by_epoch >= 1),
    recorded_at REAL NOT NULL,
    deleted_at REAL
);

CREATE TABLE candidate_launch_outbox (
    request_id TEXT PRIMARY KEY,
    observation_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'planned', 'submitting', 'submission_unknown', 'submitted', 'started',
        'terminal_uncertain', 'admitted', 'failed', 'expired', 'manual_review'
    )),
    owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
    scheduler_job_id TEXT,
    first_uncertain_at REAL,
    last_positive_evidence_at REAL,
    uncertainty_deadline REAL,
    evidence_source TEXT,
    manual_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE scheduler_operator_requests (
    request_id TEXT PRIMARY KEY,
    launch_request_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'confirm_job_id', 'mark_failed', 'mark_expired',
        'record_external_cancel_evidence'
    )),
    expected_state_sha256 TEXT NOT NULL CHECK (length(expected_state_sha256) = 64),
    reason TEXT NOT NULL,
    scheduler_job_id TEXT,
    evidence_source TEXT,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('applied', 'stale_rejected')),
    result_state TEXT NOT NULL,
    processed_by_epoch INTEGER NOT NULL CHECK (processed_by_epoch >= 1),
    processed_at REAL NOT NULL,
    CHECK ((action = 'confirm_job_id' AND scheduler_job_id IS NOT NULL)
        OR (action <> 'confirm_job_id' AND scheduler_job_id IS NULL))
);
