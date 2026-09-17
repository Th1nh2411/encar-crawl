-- Stock status: rent_product joined to the Encar crawl by plate.
--
-- The spec is "a car_number with no Encar data is 판매완료". That holds only for
-- cars the crawl actually covers. The live stock is Korean-market models --
-- 현대 그랜저, 쌍용 티볼리, 기아 카니발, 제네시스 G80 -- while models.json crawls
-- BMW 3-Series G20, so today 0 stock plates appear in any crawl. Applying the
-- rule blindly would mark the entire inventory sold.
--
-- Hence three states rather than two:
--
--   ON_SALE   판매중    the plate is live on Encar right now
--   SOLD_OUT  판매완료  the plate WAS on Encar and every listing for it is gone
--   UNKNOWN   미확인    the plate has never appeared in any crawl
--
-- UNKNOWN is the honest verdict for a car outside the crawl's model coverage:
-- absent from a search that never looked for it is not evidence of a sale. Add
-- a car's model to models.json and it resolves to ON_SALE or SOLD_OUT by itself.
-- The dashboard can still fold UNKNOWN into 판매완료 to match the spec exactly,
-- but as a deliberate choice rather than a silent default.

CREATE OR REPLACE VIEW stock_status AS
SELECT
    rp.id,
    rp.product_id,
    rp.car_number,
    rp.model_name,
    -- Encar knows the real trim; fall back to the product_group path.
    COALESCE(enc.model_info, rp.model_name)   AS model_info,
    YEAR(rp.car_manufacture_date)             AS model_year,
    rp.car_manufacture_date,
    rp.mileage,
    rp.purchase_price                         AS price,
    rp.car_fuel,
    rp.ex_color,
    rp.car_region,
    rp.car_origin,
    rp.car_status,
    -- 21 live rows carry QA placeholders ('test 0101', 'For Test 02', '1')
    -- instead of a plate. They can never match Encar, so the dashboard hides
    -- them by default rather than parading them as 미확인.
    (rp.car_number REGEXP '^[0-9]{2,3}[가-힣][0-9]{4}$') AS plate_valid,
    enc.listing_count,
    enc.active_listings,
    enc.last_seen_at                          AS encar_last_seen_at,
    CASE
        WHEN enc.car_number IS NULL  THEN 'UNKNOWN'
        WHEN enc.active_listings > 0 THEN 'ON_SALE'
        ELSE                              'SOLD_OUT'
    END                                       AS status,
    CASE
        WHEN enc.car_number IS NULL  THEN '미확인'
        WHEN enc.active_listings > 0 THEN '판매중'
        ELSE                              '판매완료'
    END                                       AS status_ko
FROM rent_product rp
LEFT JOIN (
    SELECT
        v.vehicle_no             AS car_number,
        COUNT(*)                 AS listing_count,
        SUM(v.status = 'ACTIVE') AS active_listings,
        MAX(v.model_info)        AS model_info,
        MAX(v.last_seen_at)      AS last_seen_at
    FROM vehicles v
    WHERE v.vehicle_no IS NOT NULL AND v.vehicle_no <> ''
    GROUP BY v.vehicle_no
) enc ON enc.car_number = rp.car_number;
