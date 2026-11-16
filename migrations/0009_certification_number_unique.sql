-- Migration 0009_certification_number_unique
-- Apply with: poetry run inventory-migrate up
-- Session-mode or direct connection only (not transaction pooler :6543).
--
-- One live physical copy per certification number.

SET search_path TO inventory, public;

CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_cert_unique
    ON inventory.inventory_items (certification_number)
    WHERE certification_number IS NOT NULL
      AND btrim(certification_number) <> ''
      AND deleted_at IS NULL;
