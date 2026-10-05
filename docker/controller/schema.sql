-- The controller's placement tables, for when they move from JSON files under the state dir to Postgres beside
-- gittensor-app (vault 26 §4). Not applied anywhere yet: today the same shapes live in deployments.json, boxes.json
-- (cards) and instances.json. Who writes what: deployments = the admin page; cards and instances = the controller only
-- (single writer); the gateway reads instances. The database is a request, not trusted input: the controller re-verifies
-- every registry entry's signature before running anything a deployments row asks for.

-- Blessed entries: digest-pinned image + its author-owned manifest, signed together by the release key.
CREATE TABLE registry_entries (
    entry_id    TEXT PRIMARY KEY,                    -- <name>@<version>
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL CHECK (version >= 1),
    image       TEXT NOT NULL,                       -- repo[:tag]@sha256:<64 hex>
    manifest    JSONB NOT NULL,                      -- verbatim, as signed
    blessed_at  BIGINT NOT NULL,
    payload     BYTEA NOT NULL,                      -- the canonical JSON bytes the signature covers
    signature   TEXT NOT NULL,                       -- OpenSSH SSHSIG, namespace gt-registry
    UNIQUE (name, version)
);

-- Operator-owned deployment settings. Later the autoscaler writes `replicas`.
CREATE TABLE deployments (
    entry_id    TEXT PRIMARY KEY REFERENCES registry_entries (entry_id),
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    replicas    INTEGER NOT NULL DEFAULT 0 CHECK (replicas >= 0),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL DEFAULT ''
);

-- One row per pinned card; the card, not the box, is the unit of placement and pay (23 §4a, §7).
CREATE TABLE cards (
    box_id       TEXT NOT NULL,                      -- the miner hotkey
    uuid         TEXT NOT NULL,                      -- GPU-..., pinned at ADMIT; unique fleet-wide
    state        TEXT NOT NULL CHECK (state IN ('IDLE', 'STARTING', 'LEASED', 'DRAINING', 'CHECKING')),
    instance_id  TEXT,                               -- set while STARTING / LEASED / DRAINING
    since        TIMESTAMPTZ NOT NULL DEFAULT now(),
    card_name    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (box_id, uuid),
    UNIQUE (uuid)
);

CREATE TABLE instances (
    instance_id   TEXT PRIMARY KEY,                  -- also the io.gittensor.instance container label
    entry_id      TEXT NOT NULL REFERENCES registry_entries (entry_id),
    box_id        TEXT NOT NULL,
    uuid          TEXT NOT NULL,
    container_id  TEXT NOT NULL DEFAULT '',          -- what `docker run` returned
    host          TEXT NOT NULL,
    port          INTEGER,                           -- the port the gateway reaches
    healthy       BOOLEAN NOT NULL DEFAULT FALSE,
    draining      BOOLEAN NOT NULL DEFAULT FALSE,    -- the gateway stops routing the moment this is set
    started_at    TIMESTAMPTZ,
    leased_at     TIMESTAMPTZ,
    drain_type    TEXT NOT NULL,
    drain_max_s   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (box_id, uuid) REFERENCES cards (box_id, uuid)
);
CREATE INDEX instances_routable ON instances (entry_id) WHERE healthy AND NOT draining;
