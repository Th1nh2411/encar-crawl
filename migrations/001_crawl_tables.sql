-- Encar crawl <-> stock sold-detection tables (MySQL)
--
-- crawl_batches    : one row per crawl run, holds the safeguard metrics
-- crawled_vehicles : snapshot of the listings seen in one run
--
-- The matching key is the Encar listing id (`car_id`), because the Encar
-- listing/search API does NOT expose the vehicle number (verified against
-- both /search/car/list/premium and /search/car/list/general). `vehicle_no`
-- is kept nullable so an optional detail-API enrichment pass can fill it in.

CREATE TABLE IF NOT EXISTS crawl_batches (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    model_key            VARCHAR(64)  NOT NULL COMMENT 'Key from models.json, e.g. bmw_3series_g20',
    started_at           DATETIME     NOT NULL,
    finished_at          DATETIME     NULL,
    total_expected_count INT          NULL COMMENT 'Count reported by the first API response',
    total_expected_pages INT          NULL COMMENT 'ceil(total_expected_count / page_size)',
    fetched_pages        INT          NOT NULL DEFAULT 0,
    completion_rate      DECIMAL(5,4) NULL COMMENT 'fetched_pages / total_expected_pages',
    crawled_count        INT          NOT NULL DEFAULT 0 COMMENT 'Distinct car_id rows written',
    status               ENUM('RUNNING','OK','PARTIAL','FAILED') NOT NULL DEFAULT 'RUNNING',
    comparison_ran       TINYINT(1)   NOT NULL DEFAULT 0,
    sold_marked_count    INT          NOT NULL DEFAULT 0,
    note                 VARCHAR(500) NULL COMMENT 'Reason a batch was skipped/failed',
    PRIMARY KEY (id),
    KEY idx_batches_model_status (model_key, status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS crawled_vehicles (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    crawl_batch_id BIGINT UNSIGNED NOT NULL,
    car_id         VARCHAR(32)  NOT NULL COMMENT 'Encar listing id (SearchResults[].Id) - matching key',
    vehicle_no     VARCHAR(32)  NULL     COMMENT 'Not available from the listing API; optional enrichment',
    model_info     VARCHAR(255) NULL     COMMENT 'Manufacturer + Model (+ Badge)',
    is_duplicate   TINYINT(1)   NOT NULL DEFAULT 0 COMMENT 'ServiceCopyCar = DUPLICATION',
    crawled_at     DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_batch_car (crawl_batch_id, car_id),
    KEY idx_crawled_car_id (car_id),
    KEY idx_crawled_vehicle_no (vehicle_no),
    CONSTRAINT fk_crawled_batch FOREIGN KEY (crawl_batch_id)
        REFERENCES crawl_batches (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
