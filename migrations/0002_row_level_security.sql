-- Migration 0002_row_level_security
-- Apply with: poetry run inventory-migrate up
-- Session-mode or direct connection only (not transaction pooler :6543).
--
-- Single-operator RLS for the inventory schema.
--   * postgres / service_role still bypass RLS (migrate.py and psycopg2 keep working)
--   * anon has no policies → denied
--   * authenticated is allowed only if auth.uid() is in inventory.operators
--
-- After this applies, insert yourself once (SQL Editor, as postgres):
--   INSERT INTO inventory.operators (user_id, email)
--   VALUES ('<your auth.users id>', 'you@example.com');
-- Then you may expose the "inventory" schema in Project Settings → API.

-- Transaction is owned by inventory-migrate.

SET search_path TO inventory, public, auth;

REVOKE ALL ON SCHEMA inventory FROM PUBLIC, anon;
REVOKE ALL ON ALL TABLES IN SCHEMA inventory FROM PUBLIC, anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA inventory FROM PUBLIC, anon;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA inventory FROM PUBLIC, anon;

-- Who may use the inventory API (PostgREST / supabase-js as authenticated).
CREATE TABLE IF NOT EXISTS operators (
                                         user_id     uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email       text,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
    );

COMMENT ON TABLE operators IS
    'Allow-list of Supabase Auth users who may access inventory via RLS. '
    'Populate by hand (SQL Editor). Not used by postgres/service_role/psycopg2.';

CREATE OR REPLACE FUNCTION is_operator()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO inventory
AS $$
SELECT EXISTS (
    SELECT 1
    FROM inventory.operators o
    WHERE o.user_id = auth.uid()
);
$$;

REVOKE ALL ON FUNCTION is_operator() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION is_operator() TO authenticated, service_role;

-- Underlying-table RLS is ignored for security-definer views (the default).
ALTER VIEW image_scan_urgent SET (security_invoker = true);
ALTER VIEW image_scan_value_backlog SET (security_invoker = true);
ALTER VIEW image_scan_worklist SET (security_invoker = true);

GRANT SELECT ON image_scan_urgent, image_scan_value_backlog, image_scan_worklist
    TO authenticated, service_role;

DO $$
DECLARE
t text;
    tables text[] := ARRAY[
        'operators',
        'publishers',
        'series',
        'issues',
        'creators',
        'issue_creators',
        'variants',
        'variant_guide_values',
        'inventory_items',
        'inventory_images',
        'sales_channels',
        'channel_listings',
        'orders',
        'order_items',
        'stock_movements',
        'market_sources',
        'market_items',
        'inventory_fmv',
        'listing_view_events',
        'listing_view_daily',
        'image_scan_queue',
        'staging_legacy_comics'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE inventory.%I ENABLE ROW LEVEL SECURITY', t);
EXECUTE format('DROP POLICY IF EXISTS operators_all ON inventory.%I', t);
EXECUTE format(
        'CREATE POLICY operators_all ON inventory.%I
         FOR ALL
         TO authenticated
         USING (inventory.is_operator())
         WITH CHECK (inventory.is_operator())',
        t
        );
END LOOP;
END $$;

-- Operators rows: an authenticated user may read their own row
-- (so the client can confirm access). Writes stay postgres/service_role only.
DROP POLICY IF EXISTS operators_select_self ON operators;
CREATE POLICY operators_select_self ON operators
    FOR SELECT
                        TO authenticated
                        USING (user_id = auth.uid());

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA inventory
    TO authenticated, service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA inventory
    TO authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA inventory
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA inventory
    GRANT USAGE, SELECT ON SEQUENCES TO authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA inventory
    GRANT EXECUTE ON FUNCTIONS TO authenticated, service_role;