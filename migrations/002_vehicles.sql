-- Master list of vehicles seen on Encar, seeded by encar_list_init.py.
--
-- One row per Encar listing id. `vehicle_no` is NOT unique on purpose: the same
-- physical car is often advertised by several dealers at once (Encar flags
-- those with ServiceCopyCar = DUPLICATION), so one plate maps to many listings.

CREATE TABLE IF NOT EXISTS vehicles (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    car_id        VARCHAR(32)  NOT NULL COMMENT 'Encar listing id (SearchResults[].Id)',
    vehicle_no    VARCHAR(32)  NULL     COMMENT 'Car number / plate, from the detail API',
    model_key     VARCHAR(64)  NOT NULL COMMENT 'Key from models.json, e.g. bmw_3series_g20',
    model_info    VARCHAR(255) NULL     COMMENT 'Manufacturer + model + grade',
    manufacturer  VARCHAR(64)  NULL,
    model_group   VARCHAR(64)  NULL,
    model_name    VARCHAR(128) NULL,
    grade_name    VARCHAR(128) NULL,
    form_year     VARCHAR(8)   NULL,
    `year_month`  VARCHAR(8)   NULL,
    mileage       INT          NULL,
    fuel_name     VARCHAR(32)  NULL,
    transmission  VARCHAR(32)  NULL,
    color_name    VARCHAR(32)  NULL,
    price         INT          NULL     COMMENT 'Listing price in 만원',
    is_duplicate  TINYINT(1)   NOT NULL DEFAULT 0 COMMENT 'ServiceCopyCar = DUPLICATION',
    first_seen_at DATETIME     NOT NULL,
    last_seen_at  DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_vehicles_car_id (car_id),
    KEY idx_vehicles_vehicle_no (vehicle_no),
    KEY idx_vehicles_model_key (model_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
