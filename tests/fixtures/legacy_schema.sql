-- The schema the live Supabase was built from: a copy of
-- Shakti_One_CRM/workflows/supabase_schema.sql (v2.0), with the seed rows and
-- the pgcrypto extension line removed (gen_random_uuid() is core since PG13),
-- followed by the tenant spine (sql/shakti_setup.sql + shakti_multitenant_spine.sql).
-- Kept in the repo so the migration tests never depend on a path outside it.

CREATE TABLE IF NOT EXISTS users (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name             TEXT NOT NULL,
  whatsapp_number  TEXT NOT NULL UNIQUE,
  email            TEXT,
  role             TEXT NOT NULL DEFAULT 'sales',
  team             TEXT NOT NULL DEFAULT 'Sales',
  is_active        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS contacts (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT NOT NULL DEFAULT 'Unknown Lead',
  email             TEXT,
  phone             TEXT,
  company           TEXT,
  job_title         TEXT,
  location          TEXT,
  corporate_domain  TEXT,
  source_first_seen TEXT,
  language          TEXT DEFAULT 'English',
  opt_out           BOOLEAN NOT NULL DEFAULT FALSE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT contact_has_identifier CHECK (email IS NOT NULL OR phone IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_idx ON contacts (LOWER(email)) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS contacts_phone_idx ON contacts (phone)         WHERE phone IS NOT NULL;
CREATE INDEX        IF NOT EXISTS contacts_company_idx ON contacts (company);

CREATE TABLE IF NOT EXISTS conversations (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id         UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  channel            TEXT NOT NULL,
  external_thread_id TEXT,
  status             TEXT NOT NULL DEFAULT 'new',
  bot_paused         BOOLEAN NOT NULL DEFAULT FALSE,
  message_count      INT NOT NULL DEFAULT 0,
  substantive_count  INT NOT NULL DEFAULT 0,
  last_active_at     TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (contact_id, channel)
);
CREATE INDEX IF NOT EXISTS conversations_contact_idx  ON conversations (contact_id);
CREATE INDEX IF NOT EXISTS conversations_status_idx   ON conversations (status);
CREATE INDEX IF NOT EXISTS conversations_external_idx ON conversations (external_thread_id);

CREATE TABLE IF NOT EXISTS messages (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id  UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  direction        TEXT NOT NULL,
  sender           TEXT,
  text             TEXT NOT NULL DEFAULT '',
  message_hash     TEXT,
  raw_payload      JSONB,
  channel          TEXT,
  delivered        BOOLEAN DEFAULT NULL,
  read_at          TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS messages_conv_idx  ON messages (conversation_id);
CREATE INDEX IF NOT EXISTS messages_hash_idx  ON messages (message_hash);
CREATE INDEX IF NOT EXISTS messages_time_idx  ON messages (created_at DESC);

CREATE TABLE IF NOT EXISTS leads (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id          TEXT NOT NULL UNIQUE,
  contact_id       UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  conversation_id  UUID REFERENCES conversations(id),
  owner_id         UUID REFERENCES users(id),
  status           TEXT NOT NULL DEFAULT 'inquiry',
  pipeline_stage   TEXT NOT NULL DEFAULT 'New',
  score            INT NOT NULL DEFAULT 0 CHECK (score BETWEEN 0 AND 100),
  priority         TEXT NOT NULL DEFAULT 'P3 — nurture',
  source           TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS leads_contact_idx  ON leads (contact_id);

CREATE TABLE IF NOT EXISTS tasks (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id       UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  type          TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  scheduled_for TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS activity_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id     UUID REFERENCES leads(id)    ON DELETE SET NULL,
  contact_id  UUID REFERENCES contacts(id) ON DELETE SET NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  detail      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS follow_up_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  contact_id      UUID NOT NULL REFERENCES contacts(id)      ON DELETE CASCADE,
  type            TEXT NOT NULL DEFAULT 're_engagement',
  status          TEXT NOT NULL DEFAULT 'pending',
  scheduled_for   TIMESTAMPTZ NOT NULL,
  sent_at         TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  description TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

DO $$ DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['users','contacts','conversations','leads','tasks','settings']
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER trg_%s_updated_at
       BEFORE UPDATE ON %s
       FOR EACH ROW EXECUTE FUNCTION touch_updated_at()', t, t);
  END LOOP;
END $$;

DO $$ DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','contacts','conversations','messages','leads',
    'tasks','activity_log','follow_up_queue','settings']
  LOOP
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY "service_role_all" ON %s FOR ALL TO service_role USING (TRUE)', t);
  END LOOP;
END $$;
