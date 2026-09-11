-- Vehicle-level view over the listing-level `vehicles` table.
--
-- One physical car is often advertised as several Encar listings at once (the
-- same plate under different dealers, each with its own car_id and price), so
-- `vehicles` holds duplicates by design. This view collapses them by plate.
--
-- Do NOT use `vehicles.is_duplicate` (Encar's ServiceCopyCar) to dedup: it is
-- set on only some members of a duplicate group — observed 1 of 3 for plate
-- 145나7148. The plate is the reliable key.
--
-- The status rule is the point of the view: a listing disappearing means that
-- ad ended, but the CAR is only sold once *every* ad for that plate is gone.
-- So status = SOLD only when active_listings = 0.
--
-- Rows with a NULL vehicle_no (detail pass skipped or failed) are excluded --
-- they cannot be grouped by plate without merging unrelated cars.

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
