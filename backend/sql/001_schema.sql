-- Interaction ledger schema.
--
-- Design rules:
--   * `events` is the append-only ledger. Nothing about an event is ever updated: orphan / linked
--     status, unknown dispositions and overrides are all DERIVED at read time (see 002_views.sql).
--     That is what makes "ingest drop_1 then drop_2" equal "ingest everything at once".
--   * `leads` is current state (merge = last non-null write wins). It holds no plaintext PII.
--   * `ingested_files` is an immutable manifest (sha256-keyed) that makes re-ingest a no-op and
--     carries the per-file counters behind data_quality.json.

CREATE TABLE workflows (
    workflow_id text PRIMARY KEY,
    schema      jsonb NOT NULL
);

CREATE TABLE dispositions (
    workflow_id text    NOT NULL REFERENCES workflows,
    code        text    NOT NULL,
    label       text    NOT NULL,
    is_terminal boolean NOT NULL,
    is_success  boolean NOT NULL,
    PRIMARY KEY (workflow_id, code)
);

CREATE TABLE ingested_files (
    file_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text   NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id      text   NOT NULL DEFAULT '',   -- '' for events: one file spans workflows
    batch_id         text,                         -- file stem (batch_001); leads only
    drop_name        text   NOT NULL,
    file_name        text   NOT NULL,
    sha256           text   NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    records_total    integer NOT NULL CHECK (records_total >= 0),     -- non-blank rows / non-empty lines
    records_rejected integer NOT NULL CHECK (records_rejected >= 0),  -- rejected rows / malformed lines
    records_accepted integer NOT NULL CHECK (records_accepted >= 0),  -- valid rows / well-formed lines
    field_warnings   integer NOT NULL DEFAULT 0 CHECK (field_warnings >= 0),
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (records_total = records_rejected + records_accepted),
    CHECK ((kind = 'leads'  AND workflow_id <> '' AND batch_id IS NOT NULL)
        OR (kind = 'events' AND workflow_id =  '' AND batch_id IS NULL)),
    -- idempotency key: the same bytes are never ingested twice
    UNIQUE (kind, workflow_id, sha256)
);

CREATE TABLE leads (
    lead_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id   text   NOT NULL REFERENCES workflows,
    -- PII: the phone is never stored in plaintext. HMAC-SHA256 pseudonym for matching ...
    phone_hash    bytea  NOT NULL CHECK (octet_length(phone_hash) = 32),
    phone_last4   char(4) NOT NULL CHECK (phone_last4 ~ '^[0-9]{4}$'),   -- for masked display only
    batch_id      text   NOT NULL,            -- batch of the FIRST valid row; never changes
    external_ref  text   NOT NULL,            -- value of the schema's "reference" field
    attrs         jsonb  NOT NULL DEFAULT '{}',  -- non-PII fields; null values are simply absent
    pii           jsonb  NOT NULL DEFAULT '{}',  -- {field: Fernet token} for every pii:true field
    first_file_id bigint NOT NULL REFERENCES ingested_files,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, phone_hash)          -- lead identity = (workflow, canonical phone)
);
CREATE INDEX leads_batch_idx ON leads (workflow_id, batch_id);

CREATE TABLE lead_rejections (
    file_id    bigint  NOT NULL REFERENCES ingested_files,
    row_number integer NOT NULL CHECK (row_number >= 1),
    reason     text    NOT NULL,              -- never contains row content, so no PII
    PRIMARY KEY (file_id, row_number)
);

CREATE TABLE events (
    ledger_seq  bigint GENERATED ALWAYS AS IDENTITY,   -- arrival order, informational
    event_id    text   PRIMARY KEY,                    -- global dedupe key (across drops)
    workflow_id text   NOT NULL,                       -- deliberately no FK: unknown workflow => orphan
    event_type  text   NOT NULL,
    call_id     text,
    occurred_at timestamptz NOT NULL,                  -- naive inputs already resolved as IST
    phone_hash  bytea  CHECK (phone_hash IS NULL OR octet_length(phone_hash) = 32),
    payload     jsonb  NOT NULL DEFAULT '{}',
    file_id     bigint NOT NULL REFERENCES ingested_files
);
CREATE INDEX events_link_idx ON events (workflow_id, phone_hash);
CREATE INDEX events_call_idx ON events (workflow_id, call_id);

-- Append-only: the ledger can be inserted into, never changed.
CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on "%" is forbidden: the interaction ledger is append-only', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END $$;

CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();

CREATE TRIGGER events_no_truncate
    BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_mutation();
