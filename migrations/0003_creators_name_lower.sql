-- Migration 0003_creators_name_lower
-- Apply with: poetry run inventory-migrate up
-- Session-mode or direct connection only (not transaction pooler :6543).
--
-- creators.name is UNIQUE and case-sensitive. Tom Defalco / Tom DeFalco
-- would otherwise be two people. Match on lower(name) for ETL identity.

SET search_path TO inventory, public;

CREATE UNIQUE INDEX creators_name_lower
    ON creators (lower(name));
