-- Migration 0008_issue_list_issue_id
-- Apply with: poetry run inventory-migrate up
-- Session-mode or direct connection only (not transaction pooler :6543).
--
-- Adds issues.id as issue_id (catalog issue PK, not public.comics.id).

SET search_path TO inventory, public;

DROP VIEW IF EXISTS issue_list;

CREATE VIEW issue_list AS
SELECT
    iss.id AS issue_id,
    string_agg(DISTINCT it.legacy_id::text, ', ' ORDER BY it.legacy_id::text)
        FILTER (WHERE it.deleted_at IS NULL) AS legacy_id,
    p.name AS publishing_company,
    s.name AS series_name,
    s.volume AS series_volume,
    iss.number AS issue_number,
    iss.cover_date,
    string_agg(
        DISTINCT
            CASE
                WHEN it.grade_scale = 'cgc' AND it.grade_numeric IS NOT NULL
                    THEN 'cgc ' || it.grade_numeric::text
                ELSE it.grade::text
            END,
        ', '
        ORDER BY
            CASE
                WHEN it.grade_scale = 'cgc' AND it.grade_numeric IS NOT NULL
                    THEN 'cgc ' || it.grade_numeric::text
                ELSE it.grade::text
            END
    ) FILTER (WHERE it.deleted_at IS NULL) AS grade,
    string_agg(DISTINCT it.location_code, ', ' ORDER BY it.location_code)
        FILTER (WHERE it.deleted_at IS NULL) AS location_code,
    string_agg(DISTINCT f.fmv::text, ', ' ORDER BY f.fmv::text)
        FILTER (WHERE it.deleted_at IS NULL) AS fmv,
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
LEFT JOIN inventory_fmv f ON f.inventory_item_id = it.id
LEFT JOIN issue_creators ic ON ic.issue_id = iss.id
LEFT JOIN creators cr ON cr.id = ic.creator_id
WHERE iss.deleted_at IS NULL
  AND s.deleted_at IS NULL
GROUP BY p.name, s.name, s.volume, iss.id, iss.number, iss.cover_date;

ALTER VIEW issue_list SET (security_invoker = true);

GRANT SELECT ON issue_list TO authenticated, service_role;
