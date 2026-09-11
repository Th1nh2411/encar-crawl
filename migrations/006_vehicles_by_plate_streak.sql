-- Vehicle-level view, rebuilt on top of migration 005's missing_streak.
--
-- Supersedes 004: same grouping, plus `max_missing_streak` so an operator can
-- see which cars are mid-streak -- absent from recent crawls but not yet at the
-- SOLD threshold. 004 is left untouched so a fresh database still migrates in
-- order (the column does not exist yet when 004 runs).

CREATE OR REPLACE VIEW vehicles_by_plate AS
SELECT
    v.vehicle_no,
    v.model_key,
    MAX(v.model_info)                                        AS model_info,
    MAX(v.manufacturer)                                      AS manufacturer,
    MAX(v.model_name)                                        AS model_name,
    MAX(v.grade_name)                                        AS grade_name,
    MAX(v.form_year)                                         AS form_year,
    MAX(v.color_name)                                        AS color_name,
    MAX(v.mileage)                                           AS mileage,
    COUNT(*)                                                 AS listing_count,
    SUM(v.status = 'ACTIVE')                                 AS active_listings,
    MAX(v.missing_streak)                                    AS max_missing_streak,
    SUM(v.status = 'SOLD')                                   AS sold_listings,
    CASE WHEN SUM(v.status = 'ACTIVE') = 0 THEN 'SOLD' ELSE 'ACTIVE' END AS status,
    -- Only meaningful once every ad is gone; NULL while the car is still listed.
    CASE WHEN SUM(v.status = 'ACTIVE') = 0 THEN MAX(v.sold_detected_at) END AS sold_detected_at,
    MIN(CASE WHEN v.status = 'ACTIVE' THEN v.price END)      AS min_active_price,
    MAX(CASE WHEN v.status = 'ACTIVE' THEN v.price END)      AS max_active_price,
    MIN(v.first_seen_at)                                     AS first_seen_at,
    MAX(v.last_seen_at)                                      AS last_seen_at,
    GROUP_CONCAT(v.car_id ORDER BY v.car_id)                 AS car_ids
FROM vehicles v
WHERE v.vehicle_no IS NOT NULL AND v.vehicle_no <> ''
GROUP BY v.vehicle_no, v.model_key;
