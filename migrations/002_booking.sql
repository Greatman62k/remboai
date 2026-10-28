CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE salons (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text NOT NULL,
    wa_phone_number_id text NOT NULL UNIQUE,
    timezone           text NOT NULL DEFAULT 'Africa/Nairobi',
    open_hours         jsonb NOT NULL DEFAULT '{"mon":["08:00","19:00"],"tue":["08:00","19:00"],"wed":["08:00","19:00"],"thu":["08:00","19:00"],"fri":["08:00","19:00"],"sat":["08:00","19:00"],"sun":null}',
    deposit_kes        integer NOT NULL DEFAULT 200 CHECK (deposit_kes >= 0),
    owner_wa_id        text,
    active             boolean NOT NULL DEFAULT true
);

CREATE TABLE staff (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid NOT NULL REFERENCES salons ON DELETE CASCADE,
    name     text NOT NULL,
    active   boolean NOT NULL DEFAULT true
);

CREATE TABLE services (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id     uuid NOT NULL REFERENCES salons ON DELETE CASCADE,
    name         text NOT NULL,
    aliases      text[] NOT NULL DEFAULT '{}',
    duration_min integer NOT NULL CHECK (duration_min BETWEEN 5 AND 600),
    price_kes    integer NOT NULL CHECK (price_kes >= 0),
    active       boolean NOT NULL DEFAULT true
);

CREATE TABLE staff_services (
    staff_id   uuid NOT NULL REFERENCES staff ON DELETE CASCADE,
    service_id uuid NOT NULL REFERENCES services ON DELETE CASCADE,
    PRIMARY KEY (staff_id, service_id)
);

CREATE TABLE clients (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid NOT NULL REFERENCES salons ON DELETE CASCADE,
    wa_id    text NOT NULL,
    name     text,
    lang     text NOT NULL DEFAULT 'en' CHECK (lang IN ('en','sw')),
    UNIQUE (salon_id, wa_id)
);

-- A booking occupies [starts_at, ends_at) on one stylist's calendar while it is held or confirmed.
-- The exclusion constraint is the last line of defence: the database itself refuses two overlapping bookings.
CREATE TABLE bookings (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id   uuid NOT NULL REFERENCES salons,
    staff_id   uuid NOT NULL REFERENCES staff,
    service_id uuid NOT NULL REFERENCES services,
    client_id  uuid NOT NULL REFERENCES clients,
    starts_at  timestamptz NOT NULL,
    ends_at    timestamptz NOT NULL,
    status     text NOT NULL DEFAULT 'held' CHECK (status IN ('held','confirmed','cancelled','expired')),
    hold_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ends_at > starts_at),
    EXCLUDE USING gist (staff_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (status IN ('held','confirmed'))
);
CREATE INDEX bookings_client ON bookings (client_id, status);

-- Per-customer conversation memory, in the same database so there is one thing to run and back up.
CREATE TABLE conversations (
    salon_id   uuid NOT NULL REFERENCES salons ON DELETE CASCADE,
    wa_id      text NOT NULL,
    state      jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (salon_id, wa_id)
);
