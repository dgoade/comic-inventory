-- Migration 0001_inventory_schema
-- Apply with: poetry run inventory-migrate up
-- Requires a session-mode or direct connection (not transaction pooler :6543).

-- =============================================================================
-- NEW MULTI-CHANNEL COMIC BOOK INVENTORY MANAGEMENT SCHEMA
-- Designed from legacy comics collection Postgres (Supabase dump)
-- Target: eBay + Shopify (+ future channels) inventory, listings, sales, market data
-- PostgreSQL 16+ recommended (uses modern features: GENERATED, jsonb, enums, etc.)
--
-- ALL OBJECTS LIVE IN THE "inventory" SCHEMA (legacy remains in public).
-- =============================================================================
-- Design principles:
-- 1. Proper normalization of the comic domain (Series → Issue → Variant → Physical Copy)
-- 2. Clear separation of catalog vs physical inventory vs channel listings
-- 3. Physical status on the copy; "listed" is derived from active channel_listings
-- 4. Channel-agnostic core + channel-specific extensions
-- 5. Soft deletes + full audit trail ready
-- 6. Keep GoCollect / eBay sold comps integration cleanly
-- 7. No binary blobs in core tables (images go to storage / S3 / Supabase Storage)
-- 8. Ready for webhooks, sync jobs, and multi-warehouse later
-- 9. View / impression analytics from all channels
-- 10. Dual image-scan queues (urgent_listed vs value_backlog) + persistent worklist
-- 11. Isolated in schema "inventory" so legacy public tables are untouched
-- =============================================================================
-- SUPABASE NOTES
-- 1. Run this script in the Supabase SQL Editor (or via a session-mode / direct
--    connection). Avoid the transaction-mode pooler (port 6543) for DDL.
-- 2. Do NOT add "inventory" to Exposed schemas until RLS is in place.
--    Backend (psycopg2 / service_role) only for now.
-- 3. App connections that use the new tables (psycopg2) should set:
--       SET search_path TO inventory, public;
--    or use qualified names (inventory.inventory_items, etc.).
-- 4. Legacy tables stay in public and are not modified by this script.
--    Legacy code does not need a search_path change.
-- =============================================================================

-- Transaction is owned by migrate.py (BEGIN/COMMIT removed).

-- ---------------------------------------------------------------------------
-- SCHEMA
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS inventory;

-- Everything below is created inside inventory
SET search_path TO inventory, public;

-- ---------------------------------------------------------------------------
-- ENUMS
-- ---------------------------------------------------------------------------
CREATE TYPE grade_scale AS ENUM (
    'raw', 'cgc', 'pgx', 'cbcs', 'csg', 'other'
);

-- Overstreet-style labels for RAW copies only.
-- Slabbed copies use grade_numeric (e.g. 9.8) + grade_scale instead.
CREATE TYPE condition_grade AS ENUM (
    'gem_mint', 'mint', 'near_mint_mint', 'near_mint', 'very_fine_near_mint',
    'very_fine', 'fine_very_fine', 'fine', 'very_good_fine', 'very_good',
    'good_very_good', 'good', 'fair', 'poor', 'incomplete', 'restored', 'unknown'
);

-- Physical state of the copy only. "Listed" is NOT stored here —
-- derive it from channel_listings.status = 'active'.
CREATE TYPE inventory_status AS ENUM (
    'in_stock', 'reserved', 'sold', 'damaged', 'lost', 'returned', 'archived'
);

CREATE TYPE channel_type AS ENUM (
    'ebay', 'shopify', 'website', 'facebook', 'whatnot', 'mercari', 'other'
);

CREATE TYPE listing_status AS ENUM (
    'draft', 'active', 'ended', 'sold', 'cancelled', 'error', 'archived'
);

CREATE TYPE order_status AS ENUM (
    'pending', 'paid', 'shipped', 'delivered', 'cancelled', 'refunded', 'partially_refunded'
);

CREATE TYPE stock_movement_type AS ENUM (
    'purchase', 'sale', 'return', 'adjustment', 'transfer', 'damage', 'found', 'reserve', 'unreserve'
);

-- ---------------------------------------------------------------------------
-- SHARED HELPER FUNCTIONS (must be before any triggers that use them)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- CORE CATALOG (normalized from legacy comics.title / series / volume / number)
-- ---------------------------------------------------------------------------

CREATE TABLE publishers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    short_name      text,
    website         text,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

CREATE TABLE series (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    publisher_id    uuid REFERENCES publishers(id),
    name            text NOT NULL,                     -- e.g. "Amazing Spider-Man"
    volume          text,                             -- "Vol. 1", "2022", etc.
    start_year      integer,
    end_year        integer,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    UNIQUE (publisher_id, name, volume)
);

CREATE TABLE issues (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id       uuid NOT NULL REFERENCES series(id),
    number          text NOT NULL,                    -- ETL must normalize "1" / "#1" / "01"
    title           text,
    cover_date      date,
    publish_year    integer,
    publish_month   smallint CHECK (publish_month BETWEEN 1 AND 12),
    cover_price     numeric(10,2),
    page_count      integer,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    UNIQUE (series_id, number)
);

CREATE TABLE creators (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE issue_creators (
    issue_id        uuid NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    creator_id      uuid NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    role            text NOT NULL,                    -- 'writer', 'penciller', 'inker', 'colorist', 'cover', etc.
    PRIMARY KEY (issue_id, creator_id, role)
);

CREATE TABLE variants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_id        uuid NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    name            text NOT NULL,                    -- "Cover A", "1:25 Incentive", "Newsstand", etc.
    is_default      boolean NOT NULL DEFAULT false,
    description     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    UNIQUE (issue_id, name)
);

-- Guide / Overstreet-style prices belong on the variant (the book), not the copy.
-- Legacy comics.near_mint_value / very_fine_value / etc. land here during ETL.
CREATE TABLE variant_guide_values (
    variant_id      uuid PRIMARY KEY REFERENCES variants(id) ON DELETE CASCADE,
    near_mint       numeric(12,2),
    very_fine       numeric(12,2),
    fine            numeric(12,2),
    very_good       numeric(12,2),
    good            numeric(12,2),
    fair            numeric(12,2),
    as_of           date,
    source          text,                             -- 'legacy_import', 'overstreet', etc.
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- PHYSICAL INVENTORY (the actual copies you own)
-- Each row is one physical copy. quantity defaults to 1.
-- ---------------------------------------------------------------------------

CREATE TABLE inventory_items (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    variant_id          uuid NOT NULL REFERENCES variants(id),
    legacy_id           integer UNIQUE,               -- public.comics.id for ETL / dual-run

    copy_label          text,                         -- leftover legacy "copy" if useful

    -- Grading: do NOT require both styles.
    --   raw    → use grade (Overstreet label)
    --   slabbed → use grade_scale + grade_numeric + certification_number
    grade_scale         grade_scale NOT NULL DEFAULT 'raw',
    grade               condition_grade,
    grade_numeric       numeric(4,1) CHECK (grade_numeric IS NULL OR (grade_numeric >= 0.5 AND grade_numeric <= 10.0)),
    grader              text,
    certification_number text,
    grading_comments    text,

    is_restored         boolean NOT NULL DEFAULT false,
    is_signed           boolean NOT NULL DEFAULT false,
    signature_notes     text,
    notes               text,

    location_code       text,
    location_notes      text,

    -- Cost of THIS copy. Current market value lives in inventory_fmv.
    purchase_price      numeric(12,2),
    purchase_date       date,
    purchase_source     text,
    cover_price_paid    numeric(10,2),

    -- Physical state only. Listed-on-a-channel is derived from channel_listings.
    status              inventory_status NOT NULL DEFAULT 'in_stock',
    quantity            integer NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    reserved_quantity   integer NOT NULL DEFAULT 0 CHECK (reserved_quantity >= 0),

    -- Cache only. Source of truth is inventory_images.
    primary_image_url   text,

    entry_date          timestamptz NOT NULL DEFAULT now(),
    as_of_date          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,

    available_quantity  integer GENERATED ALWAYS AS (quantity - reserved_quantity) STORED
);

CREATE INDEX idx_inventory_variant ON inventory_items(variant_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_inventory_status ON inventory_items(status) WHERE deleted_at IS NULL;
CREATE INDEX idx_inventory_location ON inventory_items(location_code) WHERE deleted_at IS NULL;
CREATE INDEX idx_inventory_cert ON inventory_items(certification_number) WHERE certification_number IS NOT NULL;
CREATE INDEX idx_inventory_grade ON inventory_items(grade_scale, grade_numeric);
CREATE INDEX idx_inventory_legacy ON inventory_items(legacy_id) WHERE legacy_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- INVENTORY IMAGES (source of truth for photos)
-- Store files in object storage (Supabase Storage, R2, S3, etc.).
-- Only URLs / storage keys live in the database — never bytea.
-- ---------------------------------------------------------------------------
CREATE TABLE inventory_images (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_item_id   uuid NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,

    storage_path        text NOT NULL,                -- e.g. comic-images/{item_id}/01.webp
    url                 text NOT NULL,
    thumbnail_url       text,

    sort_order          integer NOT NULL DEFAULT 0,
    is_primary          boolean NOT NULL DEFAULT false,

    alt_text            text,
    content_type        text,
    width_px            integer,
    height_px           integer,
    file_size_bytes     bigint,
    content_hash        text,

    uploaded_at         timestamptz NOT NULL DEFAULT now(),
    uploaded_by         text,
    source              text,                         -- 'scan', 'phone', 'vendor', 'import', etc.
    notes               text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

CREATE INDEX idx_inventory_images_item ON inventory_images(inventory_item_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_inventory_images_primary ON inventory_images(inventory_item_id) WHERE is_primary AND deleted_at IS NULL;
CREATE UNIQUE INDEX idx_inventory_images_one_primary
    ON inventory_images(inventory_item_id)
    WHERE is_primary AND deleted_at IS NULL;

CREATE TRIGGER trg_inventory_images_updated BEFORE UPDATE ON inventory_images
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Keep inventory_items.primary_image_url in sync with the current primary image.
-- pg_trigger_depth() prevents recursion when we clear other is_primary flags.
CREATE OR REPLACE FUNCTION sync_inventory_primary_image()
RETURNS TRIGGER AS $$
DECLARE
    target_item uuid;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    target_item := COALESCE(NEW.inventory_item_id, OLD.inventory_item_id);

    IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
        IF NEW.is_primary AND NEW.deleted_at IS NULL THEN
            UPDATE inventory_images
            SET is_primary = false, updated_at = now()
            WHERE inventory_item_id = NEW.inventory_item_id
              AND id <> NEW.id
              AND is_primary
              AND deleted_at IS NULL;
        END IF;
    END IF;

    UPDATE inventory_items
    SET primary_image_url = (
            SELECT url FROM inventory_images
            WHERE inventory_item_id = target_item
              AND is_primary AND deleted_at IS NULL
            ORDER BY sort_order
            LIMIT 1
        ),
        updated_at = now()
    WHERE id = target_item;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sync_primary_image
    AFTER INSERT OR UPDATE OR DELETE ON inventory_images
    FOR EACH ROW EXECUTE FUNCTION sync_inventory_primary_image();

-- ---------------------------------------------------------------------------
-- MULTI-CHANNEL SUPPORT
-- ---------------------------------------------------------------------------

CREATE TABLE sales_channels (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type            channel_type NOT NULL,
    name            text NOT NULL,                    -- "eBay Main Store", "Shopify US", etc.
    external_id     text,
    is_active       boolean NOT NULL DEFAULT true,
    -- Operational settings only. NEVER store API keys / secrets here.
    config          jsonb DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (type, name)
);

CREATE TABLE channel_listings (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_item_id   uuid NOT NULL REFERENCES inventory_items(id),
    channel_id          uuid NOT NULL REFERENCES sales_channels(id),

    external_listing_id text,                         -- eBay ItemID, Shopify product/variant ID
    external_sku        text,
    title               text NOT NULL,
    description         text,

    price               numeric(12,2) NOT NULL,
    currency            char(3) NOT NULL DEFAULT 'USD',
    quantity_listed     integer NOT NULL DEFAULT 1 CHECK (quantity_listed >= 0),

    status              listing_status NOT NULL DEFAULT 'draft',
    listed_at           timestamptz,
    ended_at            timestamptz,

    channel_data        jsonb DEFAULT '{}'::jsonb,
    url                 text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,

    UNIQUE (channel_id, external_listing_id)
);

CREATE INDEX idx_listings_inventory ON channel_listings(inventory_item_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_listings_channel_status ON channel_listings(channel_id, status) WHERE deleted_at IS NULL;
CREATE INDEX idx_listings_external ON channel_listings(channel_id, external_listing_id);
CREATE INDEX idx_listings_active_item ON channel_listings(inventory_item_id)
    WHERE status = 'active' AND deleted_at IS NULL;

CREATE TABLE orders (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id          uuid NOT NULL REFERENCES sales_channels(id),
    external_order_id   text NOT NULL,
    status              order_status NOT NULL DEFAULT 'pending',

    buyer_name          text,
    buyer_email         text,
    buyer_username      text,

    subtotal            numeric(12,2),
    shipping_amount     numeric(12,2),
    tax_amount          numeric(12,2),
    fees_amount         numeric(12,2),
    total_amount        numeric(12,2),
    currency            char(3) NOT NULL DEFAULT 'USD',

    ordered_at          timestamptz,
    paid_at             timestamptz,
    shipped_at          timestamptz,

    shipping_address    jsonb,
    tracking_number     text,
    carrier             text,

    notes               text,
    channel_data        jsonb DEFAULT '{}'::jsonb,

    utm_source          text,
    utm_medium          text,
    utm_campaign        text,
    utm_content         text,
    utm_term            text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (channel_id, external_order_id)
);

CREATE TABLE order_items (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    inventory_item_id   uuid REFERENCES inventory_items(id),
    listing_id          uuid REFERENCES channel_listings(id),

    quantity            integer NOT NULL DEFAULT 1,
    unit_price          numeric(12,2) NOT NULL,
    total_price         numeric(12,2) NOT NULL,

    title_snapshot      text,
    grade_snapshot      text,
    sku_snapshot        text,

    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_order_items_inventory ON order_items(inventory_item_id);
CREATE INDEX idx_orders_channel ON orders(channel_id, ordered_at DESC);

-- ---------------------------------------------------------------------------
-- STOCK MOVEMENTS / AUDIT TRAIL
-- App (or a later trigger) must write a row whenever quantity/reserved changes.
-- ---------------------------------------------------------------------------

CREATE TABLE stock_movements (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_item_id   uuid NOT NULL REFERENCES inventory_items(id),
    movement_type       stock_movement_type NOT NULL,
    quantity_change     integer NOT NULL,
    quantity_before     integer NOT NULL,
    quantity_after      integer NOT NULL,

    order_item_id       uuid REFERENCES order_items(id),
    listing_id          uuid REFERENCES channel_listings(id),

    reason              text,
    performed_by        text DEFAULT current_user,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_stock_movements_item ON stock_movements(inventory_item_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- MARKET DATA & COMPS
-- ---------------------------------------------------------------------------

CREATE TABLE market_sources (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    base_url        text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE market_items (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           uuid NOT NULL REFERENCES market_sources(id),
    variant_id          uuid REFERENCES variants(id),
    inventory_item_id   uuid REFERENCES inventory_items(id),

    external_id         text,
    external_url        text,
    title               text,
    grade               text,
    price               numeric(12,2),
    currency            char(3) DEFAULT 'USD',
    sold_at             date,
    raw_data            jsonb,

    fetched_at          timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_market_items_variant ON market_items(variant_id);
CREATE INDEX idx_market_items_source_external ON market_items(source_id, external_id);

-- Current market value for THIS copy at its actual grade.
CREATE TABLE inventory_fmv (
    inventory_item_id   uuid PRIMARY KEY REFERENCES inventory_items(id) ON DELETE CASCADE,
    source              text NOT NULL DEFAULT 'gocollect',
    closest_grade       text,
    fmv                 numeric(12,2),
    fmv_as_of           date,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- VIEW / IMPRESSION ANALYTICS + DUAL IMAGE-SCAN QUEUES
-- ---------------------------------------------------------------------------

CREATE TYPE view_event_type AS ENUM (
    'impression',
    'view',
    'watch',
    'click_out',
    'other'
);

CREATE TYPE scan_queue_type AS ENUM (
    'urgent_listed',
    'value_backlog'
);

CREATE TYPE scan_queue_status AS ENUM (
    'pending',
    'in_progress',
    'scanned',
    'skipped',
    'needs_revisit',
    'blocked'
);

CREATE TABLE listing_view_events (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    listing_id          uuid REFERENCES channel_listings(id) ON DELETE SET NULL,
    inventory_item_id   uuid REFERENCES inventory_items(id) ON DELETE SET NULL,
    channel_id          uuid NOT NULL REFERENCES sales_channels(id),

    event_type          view_event_type NOT NULL DEFAULT 'view',
    viewed_at           timestamptz NOT NULL DEFAULT now(),

    external_event_id   text,
    session_id          text,
    viewer_country      char(2),
    referrer            text,
    device_type         text,
    raw_data            jsonb DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT view_event_has_target CHECK (listing_id IS NOT NULL OR inventory_item_id IS NOT NULL)
);

CREATE INDEX idx_view_events_inventory_time ON listing_view_events(inventory_item_id, viewed_at DESC)
    WHERE inventory_item_id IS NOT NULL;
CREATE INDEX idx_view_events_listing_time ON listing_view_events(listing_id, viewed_at DESC)
    WHERE listing_id IS NOT NULL;
CREATE INDEX idx_view_events_channel_time ON listing_view_events(channel_id, viewed_at DESC);
CREATE INDEX idx_view_events_type_time ON listing_view_events(event_type, viewed_at DESC);

CREATE TABLE listing_view_daily (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_item_id   uuid NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
    channel_id          uuid NOT NULL REFERENCES sales_channels(id),
    day                 date NOT NULL,

    impressions         integer NOT NULL DEFAULT 0,
    views               integer NOT NULL DEFAULT 0,
    watches             integer NOT NULL DEFAULT 0,
    other_events        integer NOT NULL DEFAULT 0,
    unique_sessions     integer,

    updated_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (inventory_item_id, channel_id, day)
);

CREATE INDEX idx_view_daily_item_day ON listing_view_daily(inventory_item_id, day DESC);
CREATE INDEX idx_view_daily_day ON listing_view_daily(day DESC);

-- Helper: a copy is "missing photos" if it has no live image rows.
-- Do not use primary_image_url IS NULL (cache can lag / miss is_primary).

-- QUEUE 1: currently live on a channel, no photos yet.
-- App queries MUST repeat ORDER BY; view ORDER BY is not guaranteed.
CREATE OR REPLACE VIEW image_scan_urgent AS
SELECT
    i.id AS inventory_item_id,
    v.name AS variant_name,
    iss.number AS issue_number,
    s.name AS series_name,
    p.name AS publisher_name,
    i.grade_scale,
    i.grade,
    i.grade_numeric,
    i.location_code,
    i.status AS inventory_status,
    i.purchase_price,
    f.fmv AS current_fmv,
    COALESCE(f.fmv, i.purchase_price, 0) AS estimated_value,

    COALESCE(SUM(d.views) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '7 days'), 0)  AS views_7d,
    COALESCE(SUM(d.views) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) AS views_30d,
    COALESCE(SUM(d.watches) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) AS watches_30d,
    COALESCE(SUM(d.impressions) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) AS impressions_30d,

    (SELECT count(*) FROM channel_listings cl
     WHERE cl.inventory_item_id = i.id
       AND cl.status = 'active'
       AND cl.deleted_at IS NULL) AS active_listing_count,

    (
        COALESCE(SUM(d.views) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '7 days'), 0) * 5.0
      + COALESCE(SUM(d.watches) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) * 12.0
      + COALESCE(SUM(d.views) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) * 1.5
    ) AS demand_score,

    MAX(d.day) AS last_view_day
FROM inventory_items i
JOIN variants v ON v.id = i.variant_id
JOIN issues iss ON iss.id = v.issue_id
JOIN series s ON s.id = iss.series_id
LEFT JOIN publishers p ON p.id = s.publisher_id
LEFT JOIN inventory_fmv f ON f.inventory_item_id = i.id
LEFT JOIN listing_view_daily d ON d.inventory_item_id = i.id
WHERE i.deleted_at IS NULL
  AND i.status NOT IN ('sold', 'archived', 'lost')
  AND NOT EXISTS (
        SELECT 1 FROM inventory_images img
        WHERE img.inventory_item_id = i.id
          AND img.deleted_at IS NULL
      )
  AND EXISTS (
        SELECT 1 FROM channel_listings cl
        WHERE cl.inventory_item_id = i.id
          AND cl.status = 'active'
          AND cl.deleted_at IS NULL
      )
GROUP BY i.id, v.name, iss.number, s.name, p.name, f.fmv;

-- QUEUE 2: no photos, not currently live. Ranked by money + grade.
CREATE OR REPLACE VIEW image_scan_value_backlog AS
SELECT
    i.id AS inventory_item_id,
    v.name AS variant_name,
    iss.number AS issue_number,
    s.name AS series_name,
    p.name AS publisher_name,
    i.grade_scale,
    i.grade,
    i.grade_numeric,
    i.location_code,
    i.status AS inventory_status,
    i.purchase_price,
    f.fmv AS current_fmv,
    COALESCE(f.fmv, i.purchase_price, 0) AS estimated_value,

    COALESCE(SUM(d.views) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) AS views_30d,
    COALESCE(SUM(d.watches) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) AS watches_30d,

    (SELECT count(*) FROM channel_listings cl
     WHERE cl.inventory_item_id = i.id
       AND cl.status = 'active'
       AND cl.deleted_at IS NULL) AS active_listing_count,

    (
        COALESCE(f.fmv, i.purchase_price, 0) * 1.0
      + COALESCE(i.grade_numeric, 0) * 15.0
      + COALESCE(SUM(d.views) FILTER (WHERE d.day >= CURRENT_DATE - INTERVAL '30 days'), 0) * 0.5
    ) AS value_score
FROM inventory_items i
JOIN variants v ON v.id = i.variant_id
JOIN issues iss ON iss.id = v.issue_id
JOIN series s ON s.id = iss.series_id
LEFT JOIN publishers p ON p.id = s.publisher_id
LEFT JOIN inventory_fmv f ON f.inventory_item_id = i.id
LEFT JOIN listing_view_daily d ON d.inventory_item_id = i.id
WHERE i.deleted_at IS NULL
  AND i.status NOT IN ('sold', 'archived', 'lost')
  AND NOT EXISTS (
        SELECT 1 FROM inventory_images img
        WHERE img.inventory_item_id = i.id
          AND img.deleted_at IS NULL
      )
  AND NOT EXISTS (
        SELECT 1 FROM channel_listings cl
        WHERE cl.inventory_item_id = i.id
          AND cl.status = 'active'
          AND cl.deleted_at IS NULL
      )
GROUP BY i.id, v.name, iss.number, s.name, p.name, f.fmv;

-- ---------------------------------------------------------------------------
-- PERSISTENT SCAN QUEUE
-- One row per item. Re-scan = update the same row (do not insert a second).
-- If both enqueue scripts hit the same item, urgent_listed wins.
-- ---------------------------------------------------------------------------
CREATE TABLE image_scan_queue (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_item_id   uuid NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,

    queue_type          scan_queue_type NOT NULL,
    status              scan_queue_status NOT NULL DEFAULT 'pending',

    priority_score      numeric(12,2),
    rank_hint           integer,

    assigned_to         text,
    claimed_at          timestamptz,
    started_at          timestamptz,
    completed_at        timestamptz,

    notes               text,
    skip_reason         text,
    images_added        integer DEFAULT 0,
    quality_rating      smallint CHECK (quality_rating BETWEEN 1 AND 5),

    enqueued_from       text DEFAULT 'dual_queue_view',
    enqueued_at         timestamptz NOT NULL DEFAULT now(),

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (inventory_item_id)
);

CREATE INDEX idx_scan_queue_type_status ON image_scan_queue(queue_type, status, priority_score DESC NULLS LAST);
CREATE INDEX idx_scan_queue_status ON image_scan_queue(status, priority_score DESC NULLS LAST);
CREATE INDEX idx_scan_queue_assigned ON image_scan_queue(assigned_to, status) WHERE assigned_to IS NOT NULL;

CREATE TRIGGER trg_scan_queue_updated BEFORE UPDATE ON image_scan_queue
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE VIEW image_scan_worklist AS
SELECT
    q.id AS queue_id,
    q.inventory_item_id,
    q.queue_type,
    q.status,
    q.priority_score,
    q.rank_hint,
    q.assigned_to,
    q.claimed_at,
    q.notes,
    q.enqueued_at,
    p.name AS publisher,
    s.name AS series,
    iss.number AS issue_number,
    v.name AS variant,
    i.grade_scale,
    i.grade,
    i.grade_numeric,
    i.location_code,
    i.purchase_price,
    f.fmv AS current_fmv,
    COALESCE(f.fmv, i.purchase_price, 0) AS estimated_value,
    NOT EXISTS (
        SELECT 1 FROM inventory_images img
        WHERE img.inventory_item_id = i.id
          AND img.deleted_at IS NULL
    ) AS still_missing_image
FROM image_scan_queue q
JOIN inventory_items i ON i.id = q.inventory_item_id
JOIN variants v ON v.id = i.variant_id
JOIN issues iss ON iss.id = v.issue_id
JOIN series s ON s.id = iss.series_id
LEFT JOIN publishers p ON p.id = s.publisher_id
LEFT JOIN inventory_fmv f ON f.inventory_item_id = i.id
WHERE q.status IN ('pending', 'in_progress', 'needs_revisit');

-- ---------------------------------------------------------------------------
-- HELPER / STAGING (mirrors public.comics for ETL)
-- ---------------------------------------------------------------------------
CREATE TABLE staging_legacy_comics (
    legacy_id           integer,
    title               text,
    series              text,
    publishing_company  text,
    volume              text,
    number              text,
    copy                text,
    series_notes        text,
    newbox              text,
    publish_date        text,
    publish_year        integer,
    publish_month       text,
    writer              text,
    art                 text,
    inks                text,
    colors              text,
    grade               text,
    grading_comments    text,
    cover_price         numeric,
    purchase_price      numeric,
    near_mint_value     numeric,
    comments            text,
    box                 text,
    very_fine_value     numeric,
    fine_value          numeric,
    very_good_value     numeric,
    good_value          numeric,
    fair_value          numeric,
    entry_date          timestamptz,
    as_of_date          timestamptz,
    order_number        integer,
    imported_at         timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- TRIGGERS (updated_at + stock safety)
-- ---------------------------------------------------------------------------

CREATE TRIGGER trg_publishers_updated BEFORE UPDATE ON publishers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_series_updated BEFORE UPDATE ON series
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_issues_updated BEFORE UPDATE ON issues
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_variants_updated BEFORE UPDATE ON variants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_guide_values_updated BEFORE UPDATE ON variant_guide_values
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_inventory_updated BEFORE UPDATE ON inventory_items
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_listings_updated BEFORE UPDATE ON channel_listings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_orders_updated BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION check_inventory_quantities()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.reserved_quantity > NEW.quantity THEN
        RAISE EXCEPTION 'reserved_quantity (%) cannot exceed quantity (%)',
            NEW.reserved_quantity, NEW.quantity;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_inventory_qty_check
    BEFORE INSERT OR UPDATE ON inventory_items
    FOR EACH ROW EXECUTE FUNCTION check_inventory_quantities();

-- ---------------------------------------------------------------------------
-- GRANTS
-- Backend / service_role (and authenticated if you add a logged-in admin UI).
-- Do NOT grant to anon. Do NOT expose this schema in PostgREST until RLS exists.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA inventory TO authenticated, service_role, postgres;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA inventory
    TO authenticated, service_role;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA inventory
    TO authenticated, service_role;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA inventory
    TO authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA inventory
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA inventory
    GRANT USAGE, SELECT ON SEQUENCES TO authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA inventory
    GRANT EXECUTE ON FUNCTIONS TO authenticated, service_role;


-- =============================================================================
-- END OF SCHEMA
-- All objects are in: inventory.*
-- Legacy remains untouched in: public.*
-- =============================================================================
