-- Sonokong `rent_product` — the stock the Encar crawl exists to serve.
--
-- Dropped and rebuilt on purpose: the first version of this table guessed its
-- columns by profiling a data-only dump. rent_product.sch is now available, so
-- the real names are used and two of those guesses turned out wrong:
--   * the FK is `group_id` into product_group, not a flat model id
--   * position 56 is `purchase_price` (the buy price), not the sale price
--
-- The source table has 90 columns; only the ones this dashboard actually reads
-- are kept. Everything about insurance, contracts, exposure flags, purchase
-- paperwork and descriptions is dropped.
--
-- Only live stock is imported (`is_del = 0`): 711 of 3,365 rows. A soft-deleted
-- car is out of inventory, so whether Encar still lists it is not a question
-- worth asking.
--
-- `model_name` is materialised at import time by walking the product_group
-- tree, because the path is what makes the row readable and the taxonomy is
-- effectively static.
--
-- `car_number` is NOT unique — the dump repeats a few plates.

DROP VIEW IF EXISTS stock_status;
DROP TABLE IF EXISTS rent_product;

CREATE TABLE rent_product (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    product_id           BIGINT       NOT NULL COMMENT 'rent_product.id in sonokong',
    car_number           VARCHAR(30)  NOT NULL COMMENT 'Plate — joins to vehicles.vehicle_no',
    vin                  VARCHAR(50)  NULL,
    group_id             INT          NULL COMMENT 'FK to product_group',
    model_name           VARCHAR(255) NULL COMMENT 'Full product_group path, built at import',
    car_manufacture_date DATETIME     NULL COMMENT 'Source of the displayed YEAR',
    mileage              BIGINT       NULL,
    purchase_price       BIGINT       NULL COMMENT 'KRW. sale_amt is 0 in 3364/3365 rows, so unused',
    engine_displacement  INT          NULL,
    car_fuel             VARCHAR(50)  NULL,
    ex_color             VARCHAR(10)  NULL,
    car_region           VARCHAR(10)  NULL,
    car_origin           VARCHAR(16)  NULL COMMENT 'DOMESTIC / IMPORTED',
    car_status           VARCHAR(16)  NULL COMMENT 'Rental state OPERATING/WAITING/SOLD, not the sale status',
    web_visibility       VARCHAR(16)  NULL,
    updated_at           DATETIME     NULL,
    imported_at          DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rent_product_pid (product_id),
    KEY idx_rent_product_car_number (car_number),
    KEY idx_rent_product_group (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
