# comic-inventory — agent / CLI context

Draft handoff from the Grok project conversation (2026-08).
Read this before changing schema, migrations, or ETL.
Canonical repo: https://github.com/dgoade/comic-inventory
Legacy data lives in the same Supabase project, schema `public`.
New design lives in schema `inventory`.

## Current state (as of 2026-08-19)

- `0001_inventory_schema` **applied** on Supabase.
- `0002_row_level_security` **applied** on Supabase.
- `0003_creators_name_lower` **applied** on Supabase.
- `0004_issue_list_view` — `inventory.issue_list` (one row per issue; comma-separated `legacy_id` / `grade` when an issue has multiple copies).
- RLS is implemented for security only. Most migration work will be done using DATABASE_URL / psycopg2 login.
- `inventory.operators` is empty — insert the owner’s `auth.users` id before using PostgREST / supabase-js.
- `inventory.sales_channels` is empty.
- **ETL applied.** `poetry run inventory-etl run` loads copies (`legacy_id` = `public.comics.id`), FMV, catalog, and issue credits. Staging is a snapshot, not a live view.
- Legacy `public.comics` is still the live collection. Dual-run: re-run `inventory-etl run` to refresh. It does **not** overwrite `status` / `quantity` / `reserved_quantity`.
- Legacy psycopg2 app still talks to `public`. It does **not** need `search_path` changes.
- Image scanning has not started (zero photos).
- YouTube / content layer was designed then **removed**. Do not add it back unless asked.

## Next step

Images + dual scan queues. Then listings/orders when selling on two channels.

Do **not** morph `public.comics` in place. Dual-run until the new app is ready.

### Legacy column meanings (verified against live `public.comics`)

Do not use the original design-doc mapping for these four columns:

| Legacy column | Actual meaning | Inventory target |
|---------------|----------------|------------------|
| `title` | Series name (`Batman`, `Amazing Spider-Man`) | `series.name` |
| `series` | Run / volume (`1`/`2`/`3`) | `series.volume` (`'-'`/null/blank → `''` so UNIQUE cannot double-insert NULLs) |
| `volume` | Noisy extra numbering (DC 33–46, `-` on most rows) | staging only — **not** the catalog key |
| `copy` | Copy number (`1`/`2`/`3`), not Cover A | `inventory_items.copy_label` |

Catalog identity is `(publishing_company, title, series-field, number)`. Every imported variant is named `Standard`. GoCollect `variant_description` is not safe (33k rows, one comic has 28k matches).

Credits hang on the **issue**: `writer` → writer, `art` → penciller, `inks` → inker, `colors` → colorist. Empty `inks` means the penciller also inked. Cover artists are parsed from `comments` (not the whole comments field as names). Split commas / `&` / `/`, but keep `Jr.`/`Sr.` as one person. `creators.name` is unique on `lower(name)` (`0003`).

Refresh: `poetry run inventory-etl status` / `run`. Mappers live in `legacy_map.py` (unit-tested); SQL in `etl.py` must stay in sync. `F` = Fair, `FN` = Fine. `location_code` comes from `comics.box` only — do not use `newbox`.

## Stack

- Python 3.12 + Poetry 2.2. IntelliJ Ultimate (Python plugin). Not PyCharm menus.
- Migrations: numbered SQL + `poetry run inventory-migrate` (psycopg2). **Not Alembic / SQLAlchemy.**
- ETL: `poetry run inventory-etl` (psycopg2). Not a numbered migration. Re-runnable.
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
- GoCollect variant names (unsafe join on `gocollect_items`)

## Git / this file

IntelliJ + GitHub is canonical. The Grok web project does **not** auto-sync.
If you change a decision, update this file in the same PR as the code.
