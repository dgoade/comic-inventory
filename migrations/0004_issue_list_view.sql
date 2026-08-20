-- Migration 0004_issue_list_view
-- Apply with: poetry run inventory-migrate up
-- Session-mode or direct connection only (not transaction pooler :6543).
--
-- One row per issue for reviewing catalog + credits.
-- legacy_id and grade are copy-level: a single copy is one value,
-- multiple copies of the same issue are comma-separated.

SET search_path TO inventory, public;

CREATE OR REPLACE VIEW issue_list AS
SELECT
    string_agg(DISTINCT it.legacy_id::text, ', ' ORDER BY it.legacy_id::text)
        FILTER (WHERE it.deleted_at IS NULL) AS legacy_id,
    p.name AS publishing_company,
    s.name AS series_name,
    s.volume AS series_volume,
    iss.number AS issue_number,
    iss.cover_date,
    string_agg(DISTINCT it.grade::text, ', ' ORDER BY it.grade::text)
        FILTER (WHERE it.deleted_at IS NULL) AS grade,
    string_agg(DISTINCT cr.name, ', ' ORDER BY cr.name)
        FILTER (WHERE ic.role = 'writer') AS writer,
    string_agg(DISTINCT cr.name, ', ' ORDER BY cr.name)
        FILTER (WHERE ic.role = 'penciller') AS penciller,
    string_agg(DISTINCT cr.name, ', ' ORDER BY cr.name)
        FILTER (WHERE ic.role = 'inker') AS inker,
    string_agg(DISTINCT cr.name, ', ' ORDER BY cr.name)
        FILTER (WHERE ic.role = 'colorist') AS colorist
FROM issues iss
JOIN series s ON s.id = iss.series_id
LEFT JOIN publishers p ON p.id = s.publisher_id
LEFT JOIN variants v
       ON v.issue_id = iss.id AND v.deleted_at IS NULL
LEFT JOIN inventory_items it
       ON it.variant_id = v.id AND it.deleted_at IS NULL
LEFT JOIN issue_creators ic ON ic.issue_id = iss.id
LEFT JOIN creators cr ON cr.id = ic.creator_id
WHERE iss.deleted_at IS NULL
  AND s.deleted_at IS NULL
GROUP BY p.name, s.name, s.volume, iss.id, iss.number, iss.cover_date;

ALTER VIEW issue_list SET (security_invoker = true);

GRANT SELECT ON issue_list TO authenticated, service_role;
