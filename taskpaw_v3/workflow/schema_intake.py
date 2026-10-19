"""Schema-two intake additions. Version-one business tables stay byte-compatible."""

TABLES = {
    "intake_root_scans": """CREATE TABLE intake_root_scans (
        root_id TEXT PRIMARY KEY NOT NULL,
        root_config_revision INTEGER NOT NULL CHECK(root_config_revision>0),
        version INTEGER NOT NULL CHECK(version>0),
        scan_id TEXT NOT NULL,
        required INTEGER NOT NULL CHECK(required IN (0,1)),
        reason TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        last_complete_scan_at TEXT,
        FOREIGN KEY(root_id,root_config_revision) REFERENCES intake_roots(root_id,config_revision) ON DELETE RESTRICT
    )""",
    "intake_roots": """CREATE TABLE intake_roots (
        root_id TEXT NOT NULL,
        config_revision INTEGER NOT NULL CHECK(config_revision>0),
        current INTEGER NOT NULL CHECK(current IN (0,1)),
        component_id TEXT NOT NULL,
        work_root_id TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        created_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(root_id,config_revision)
    )""",
    "intake_packages": """CREATE TABLE intake_packages (
        package_id TEXT PRIMARY KEY NOT NULL,
        root_id TEXT NOT NULL,
        root_config_revision INTEGER NOT NULL CHECK(root_config_revision>0),
        relative_path TEXT NOT NULL,
        current INTEGER NOT NULL DEFAULT 1 CHECK(current IN (0,1)),
        path_key TEXT NOT NULL,
        native_identity_key TEXT,
        parent_context TEXT,
        source_generation INTEGER NOT NULL CHECK(source_generation>0),
        version INTEGER NOT NULL CHECK(version>0),
        state TEXT NOT NULL CHECK(state IN ('receiving','waiting_stability','snapshotting','snapshot_ready','operational_ready','needs_attention','blocked','waiting_capacity','unknown_owner','missing')),
        reason TEXT NOT NULL,
        dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),
        observation_digest TEXT NOT NULL CHECK(length(observation_digest)=64 AND observation_digest NOT GLOB '*[^0-9a-f]*'),
        observed_at TEXT NOT NULL,
        last_complete_scan_at TEXT,
        stable_since TEXT,
        last_seen_scan_id TEXT,
        grouping_revision INTEGER CHECK(grouping_revision>0),
        observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
        grouping_json TEXT CHECK(grouping_json IS NULL OR json_valid(grouping_json)),
        eligibility_json TEXT CHECK(eligibility_json IS NULL OR json_valid(eligibility_json)),
        active_operation_id TEXT REFERENCES intake_operations(operation_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        active_snapshot_id TEXT REFERENCES intake_snapshots(snapshot_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(root_id,root_config_revision) REFERENCES intake_roots(root_id,config_revision) ON DELETE RESTRICT
    )""",
    "intake_operations": """CREATE TABLE intake_operations (
        operation_id TEXT PRIMARY KEY NOT NULL,
        package_id TEXT NOT NULL REFERENCES intake_packages(package_id) ON DELETE RESTRICT,
        owner_principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        root_id TEXT NOT NULL,
        root_config_revision INTEGER NOT NULL CHECK(root_config_revision>0),
        source_generation INTEGER NOT NULL CHECK(source_generation>0),
        owner_boot_id TEXT NOT NULL,
        owner_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('admitted','running','stopping','completed','cancelled','failed','unknown')),
        unresolved INTEGER NOT NULL CHECK(unresolved IN (0,1)),
        publication_allowed INTEGER NOT NULL CHECK(publication_allowed IN (0,1)),
        version INTEGER NOT NULL CHECK(version>0),
        admission_json TEXT NOT NULL CHECK(json_valid(admission_json)),
        observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
        config_json TEXT NOT NULL CHECK(json_valid(config_json)),
        owner_json TEXT CHECK(owner_json IS NULL OR json_valid(owner_json)),
        exit_json TEXT CHECK(exit_json IS NULL OR json_valid(exit_json)),
        result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(unresolved=1 OR state IN ('completed','cancelled','failed')),
        FOREIGN KEY(root_id,root_config_revision) REFERENCES intake_roots(root_id,config_revision) ON DELETE RESTRICT
    )""",
    "intake_snapshots": """CREATE TABLE intake_snapshots (
        snapshot_id TEXT PRIMARY KEY NOT NULL,
        operation_id TEXT NOT NULL UNIQUE REFERENCES intake_operations(operation_id) ON DELETE RESTRICT,
        package_id TEXT NOT NULL REFERENCES intake_packages(package_id) ON DELETE RESTRICT,
        source_generation INTEGER NOT NULL CHECK(source_generation>0),
        grouping_revision INTEGER NOT NULL CHECK(grouping_revision>0),
        completion_digest TEXT NOT NULL CHECK(length(completion_digest)=64 AND completion_digest NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""",
    "intake_films": """CREATE TABLE intake_films (
        film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
        input_revision INTEGER NOT NULL CHECK(input_revision>0),
        manifest_kind TEXT NOT NULL DEFAULT 'input' CHECK(manifest_kind='input'),
        package_id TEXT NOT NULL REFERENCES intake_packages(package_id) ON DELETE RESTRICT,
        snapshot_id TEXT NOT NULL REFERENCES intake_snapshots(snapshot_id) ON DELETE RESTRICT,
        source_generation INTEGER NOT NULL CHECK(source_generation>0),
        grouping_revision INTEGER NOT NULL CHECK(grouping_revision>0),
        source_invalidated_context_revision INTEGER CHECK(source_invalidated_context_revision>0),
        created_at TEXT NOT NULL,
        PRIMARY KEY(film_id,input_revision),
        FOREIGN KEY(film_id,manifest_kind,input_revision) REFERENCES manifests(film_id,kind,revision) ON DELETE RESTRICT
    )""",
}

INDEXES = {
    "one_enabled_intake_root": """CREATE UNIQUE INDEX one_enabled_intake_root
        ON intake_roots(enabled) WHERE current=1 AND enabled=1""",
    "one_current_intake_root": """CREATE UNIQUE INDEX one_current_intake_root
        ON intake_roots(root_id) WHERE current=1""",
    "one_unresolved_intake_owner": """CREATE UNIQUE INDEX one_unresolved_intake_owner
        ON intake_operations(unresolved) WHERE unresolved=1""",
    "intake_package_state": """CREATE INDEX intake_package_state
        ON intake_packages(root_id,state,package_id)""",
    "intake_package_identity": """CREATE UNIQUE INDEX intake_package_identity
        ON intake_packages(root_id,native_identity_key) WHERE current=1 AND native_identity_key IS NOT NULL""",
    "intake_package_path": """CREATE UNIQUE INDEX intake_package_path
        ON intake_packages(root_id,path_key) WHERE current=1""",
    "intake_film_package": """CREATE INDEX intake_film_package
        ON intake_films(package_id,source_generation)""",
}
