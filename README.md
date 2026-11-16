# comic-inventory

Postgres inventory schema and psycopg2 tools for multi-channel comic sales.

## Open in IntelliJ

1. **File → Open** this folder (`comic-inventory/`, the one with `pyproject.toml`).
2. When prompted, trust the project. Let IntelliJ detect **Poetry**.
3. Settings → Python → Interpreter → **Add Interpreter → Poetry Environment** → create a new virtualenv for this project (Poetry 2.2, Python 3.12.9).
4. IntelliJ will mark `src/` as the sources root. Do not add a second interpreter or a `.idea` from elsewhere.

```bash
poetry env use 3.12
poetry install
```

## Migrations

```bash
cp .env.example .env   # then edit DATABASE_URL
poetry run inventory-migrate status
poetry run inventory-migrate up
```

The Poetry commands load `.env` from the project root. A variable already set in the shell is left as it is. `pg_restore` below still reads `$DATABASE_URL` from the shell, so source the file for that command: `set -a && source .env && set +a`.

Use a session-mode or direct connection, not the transaction pooler (`:6543`).

## ETL (legacy `public.comics` → `inventory`)

Does not write `public.comics`. Re-runnable. Does not overwrite CGC/slab grades or their FMV.

```bash
poetry run inventory-etl status
poetry run inventory-etl run              # load staging + upsert catalog/items/FMV
poetry run inventory-etl run --skip-load  # transform existing staging only
poetry run inventory-etl load             # staging snapshot only
```

```bash
poetry run pytest
```

## CGC slab on eBay

Uses two local photos you downloaded and resized. The commands do not call CGC. `show` does not write. `prepare` uploads the photos and saves a draft. `publish` is the step that lists it and reserves the copy. Price and grade are whatever you pass. Price is not taken from FMV.

```bash
poetry run inventory-cgc show 4719983018 --legacy-id 123 --grade 9.8 \
    --front front.jpg --back back.jpg
poetry run inventory-cgc prepare 4719983018 --legacy-id 123 --price 25.00 --grade 9.8 \
    --front front.jpg --back back.jpg
poetry run inventory-cgc publish 4719983018
```

Apply `0009_certification_number_unique` first (`poetry run inventory-migrate up`). Storage and eBay keys live in `.env`. The command loads that file. See `.env.example`.

## Backup (`inventory` data only)

Does not dump `public` (legacy comics). Needs `pg_dump` on `PATH`.

```bash
poetry run inventory-backup              # backups/inventory-YYYYMMDD-HHMMSS.dump
poetry run inventory-backup --sql        # plain INSERT SQL
poetry run inventory-backup --out path.dump
```

Restore onto a schema created by `inventory-migrate up`:

```bash
pg_restore --data-only --disable-triggers --no-owner --schema=inventory \
  -d "$DATABASE_URL" backups/inventory-YYYYMMDD-HHMMSS.dump
```
