CREATE TABLE placements (
    placement_id TEXT PRIMARY KEY,
    current_placement_epoch INTEGER NOT NULL CHECK (current_placement_epoch >= 1),
    current_instance_id TEXT,
    reusable_stream_id INTEGER,
    updated_at REAL NOT NULL
);

CREATE TABLE streams (
    stream_id INTEGER PRIMARY KEY CHECK (stream_id >= 0),
    current_stream_epoch INTEGER NOT NULL CHECK (current_stream_epoch >= 1),
    current_instance_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('available', 'active', 'draining', 'retired')),
    resume_cursor INTEGER NOT NULL CHECK (resume_cursor >= 0),
    last_receipt_id TEXT REFERENCES cycle_receipts(receipt_id),
    updated_at REAL NOT NULL
);

CREATE TABLE learner_instances (
    instance_id TEXT PRIMARY KEY,
    placement_id TEXT NOT NULL REFERENCES placements(placement_id),
    placement_epoch INTEGER NOT NULL CHECK (placement_epoch >= 1),
    stream_id INTEGER REFERENCES streams(stream_id),
    stream_epoch INTEGER,
    admission_generation INTEGER,
    admission_token_sha256 TEXT,
    launch_request_id TEXT,
    pbs_job_id TEXT,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid >= 0),
    status TEXT NOT NULL CHECK (status IN (
        'registered', 'admitted', 'draining', 'stopped', 'revoked', 'expired', 'rejected'
    )),
    registered_at REAL NOT NULL,
    admitted_at REAL,
    last_seen REAL,
    stopped_at REAL,
    status_reason TEXT,
    final_update_id TEXT,
    admitted_by_epoch INTEGER,
    UNIQUE(placement_id, placement_epoch),
    UNIQUE(stream_id, stream_epoch),
    CHECK ((status IN ('admitted', 'draining', 'stopped', 'revoked', 'expired')
            AND stream_id IS NOT NULL AND stream_epoch >= 1
            AND admission_generation >= 1 AND length(admission_token_sha256) = 64)
        OR status IN ('registered', 'rejected')),
    CHECK ((status = 'draining' AND final_update_id IS NOT NULL)
        OR (status <> 'draining' AND final_update_id IS NULL))
);

CREATE TABLE registration_requests (
    instance_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    launch_request_id TEXT,
    placement_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'admitted', 'rejected', 'expired')),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    processed_by_epoch INTEGER,
    rejection_reason TEXT,
    result_json TEXT,
    CHECK (expires_at > created_at)
);

CREATE TABLE launch_requests (
    request_id TEXT PRIMARY KEY,
    observation_key TEXT UNIQUE,
    bootstrap_slot INTEGER UNIQUE,
    role TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_by_epoch INTEGER NOT NULL CHECK (requested_by_epoch >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'planned', 'submitting', 'submission_unknown', 'submitted', 'started',
        'terminal_uncertain', 'admitted', 'failed', 'expired', 'manual_review'
    )),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    not_before REAL,
    submission_attempts INTEGER NOT NULL CHECK (submission_attempts >= 0),
    pbs_job_id TEXT,
    scheduler_state TEXT,
    scheduler_observed_at REAL,
    first_uncertain_at REAL,
    last_positive_evidence_at REAL,
    uncertainty_deadline REAL,
    evidence_source TEXT,
    manual_reason TEXT,
    admitted_instance_id TEXT UNIQUE REFERENCES learner_instances(instance_id),
    expires_at REAL,
    last_error TEXT,
    authorized_placement_id TEXT,
    authorized_placement_epoch INTEGER
);

CREATE TABLE capacity_observations (
    observation_key TEXT PRIMARY KEY,
    observation_seq INTEGER NOT NULL UNIQUE CHECK (observation_seq >= 1),
    kind TEXT NOT NULL,
    global_version INTEGER NOT NULL CHECK (global_version >= 0),
    observed_at REAL NOT NULL,
    eligible_contributors INTEGER NOT NULL CHECK (eligible_contributors >= 0),
    selected_contributors INTEGER NOT NULL CHECK (selected_contributors >= 0),
    productive_instances INTEGER NOT NULL CHECK (productive_instances >= 0),
    reserved_launch_capacity INTEGER NOT NULL CHECK (reserved_launch_capacity >= 0),
    desired_contributors INTEGER NOT NULL CHECK (desired_contributors >= 0),
    action TEXT NOT NULL,
    command_epoch INTEGER NOT NULL CHECK (command_epoch >= 1)
);

CREATE TABLE admission_history (
    admission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    stream_id INTEGER,
    stream_epoch INTEGER,
    placement_id TEXT NOT NULL,
    placement_epoch INTEGER NOT NULL,
    admission_generation INTEGER,
    event TEXT NOT NULL CHECK (event IN ('admitted', 'draining', 'stopped', 'revoked', 'expired')),
    reason TEXT,
    command_epoch INTEGER NOT NULL CHECK (command_epoch >= 1),
    created_at REAL NOT NULL
);

CREATE INDEX idx_instances_status ON learner_instances(status, last_seen);
CREATE INDEX idx_launch_requests_state ON launch_requests(state, updated_at);
CREATE INDEX idx_registration_requests_state ON registration_requests(state, expires_at);
