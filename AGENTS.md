# comic-inventory — agent / CLI context

Draft handoff from the Grok project conversation (2026-08).
Read this before changing schema, migrations, or ETL.
Canonical repo: https://github.com/dgoade/comic-inventory
Legacy data lives in the same Supabase project, schema `public`.
New design lives in schema `inventory`.

Copy this file into the repo root as `AGENTS.md` (or merge if you already have one).

## Current state (as of 2026-08-19)

- `0001_inventory_schema` **applied** on Supabase.
- `0002_row_level_security` **applied** on Supabase.
- RLS is implemented for security only. Most migration work will be done using DATABASE_URL / psycopg2 login. 
- `inventory.operators` is empty — insert the owner’s `auth.users` id before using PostgREST / supabase-js.
- `inventory.sales_channels` is empty.
- **No ETL yet.** `inventory` tables are empty (except operators / empty catalogs). Legacy `public.comics` is still the live collection.
- Legacy psycopg2 app still talks to `public`. It does **not** need `search_path` changes.
- Image scanning has not started (zero photos).
- YouTube / content layer was designed then **removed**. Do not add it back unless asked.

## Next step

ETL `public.comics` → `inventory`:

1. Load into `inventory.staging_legacy_comics`.
2. Upsert publishers / series / issues / variants (normalize issue numbers: `"1"`, `"#1"`, `"01"` → one value).
3. Put legacy NM/VF/F/VG/G/Fair guide prices on `variant_guide_values` (not on the copy).
4. One `inventory_items` row per legacy comic. Set **`legacy_id`** = `public.comics.id`.
5. Map GoCollect FMV → `inventory_fmv` if straightforward.

Then: images + dual scan queues. Then listings/orders when selling on two channels.

Do **not** morph `public.comics` in place. Dual-run until the new app is ready.

## Stack

- Python 3.12 + Poetry 2.2. IntelliJ Ultimate (Python plugin). Not PyCharm menus.
- Migrations: numbered SQL + `poetry run inventory-migrate` (psycopg2). **Not Alembic / SQLAlchemy.**
- New files: `migrations/0003_whatever.sql`. Never edit an already-applied file.
- Connection: session-mode pooler or direct (**port 5432**). **Never run DDL through transaction pooler `:6543`.** `migrate.py` refuses `:6543`.
- Mac is IPv4-only; that is why the pooler is used. Session-mode on the pooler host is the right DDL path.
- Secrets: env / `.env`. Never put API keys in `sales_channels.config` jsonb.

## Schema rules (do not regress)

### Physical copy vs listing

- Each `inventory_items` row is **one physical copy**. `quantity` defaults to 1.
- `inventory_items.status` is **physical only**: `in_stock`, `reserved`, `sold`, `damaged`, `lost`, `returned`, `archived`.
- There is **no** `status = 'listed'`. “Listed” = exists an active `channel_listings` row.
- Do not dual-write listing state onto the copy. It drifted in earlier drafts.

### Grades

- Raw: `grade_scale = 'raw'` + `grade` (Overstreet label).
- Slabbed: `grade_scale` + `grade_numeric` + `certification_number`.
- Do **not** require both styles. CGC 9.4 does not map cleanly onto Overstreet enums.

### Money

- Copy stores `purchase_price` (cost of this book).
- Current value for **this copy/grade** → `inventory_fmv`.
- Guide prices for the **book** (NM/VF/…) → `variant_guide_values`.
- Do not put six guide columns back on `inventory_items`.

### Images

- Files in Supabase Storage (later R2 if needed). **Never `bytea`.**
- Source of truth: `inventory_images`. `primary_image_url` is a cache (trigger + `pg_trigger_depth()`).
- “Missing photos” = `NOT EXISTS` on `inventory_images`, **not** `primary_image_url IS NULL`.
- No `image_urls` jsonb (it drifts).

### Dual scan queues (zero images today)

A single demand score starves high-value books that were never listed.

| Queue | Who | Ranked by |
|-------|-----|-----------|
| `urgent_listed` | Active listing + no images | Views/watches, then value |
| `value_backlog` | No images, not live | FMV/purchase + grade |

- `image_scan_queue`: one row per item. Re-scan updates that row.
- If both enqueue scripts collide, **urgent wins**.
- Views do not guarantee order — queries must `ORDER BY`.
- Worklist: urgent first, then backlog; optional `location_code` when pulling boxes.

### RLS

- Enabled on all `inventory` tables. `anon` denied.
- `authenticated` only if `auth.uid()` is in `inventory.operators`.
- `postgres` / `service_role` / migrate.py **bypass RLS**. Connection string does not take an “authorized user”.
- Authorize someone: `INSERT INTO inventory.operators (user_id, email) VALUES (...)`.
- Views use `security_invoker = true`.
- **Do not** expose schema `inventory` in Supabase API settings until the operator row exists.
- Do not use `USING (true)` for authenticated — any signed-up user would see the whole collection.

### Legacy app

- Leave `public` alone. Unqualified `comics` etc. still resolve there.
- New code: `SET search_path TO inventory, public` or qualify `inventory.*`.
- Version table: `public.schema_migrations`.

## Explicitly deferred

- YouTube / content layer
- Normalized `locations` table
- Listing sync fields (`last_synced_at`, `sync_status`)
- Item-level fee / profit columns
- Auto-writing `stock_movements` via trigger
- Alembic
- Multi-user tenancy (operators allow-list is enough)

## Git / this file

IntelliJ + GitHub is canonical. The Grok web project does **not** auto-sync.
If you change a decision, update this file in the same PR as the code.
