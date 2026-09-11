-- Sold-out status for the `vehicles` master table.
--
-- A listing that stops appearing in a healthy crawl of its model is considered
-- sold. `last_batch_id` is what makes that decision cheap and safe: every row
-- refreshed by a crawl carries that run's batch id, so "missing from the latest
-- batch" is just `last_batch_id <> <batch>` — no anti-join against a temp set,
-- and no way for a half-finished batch to look like a full one.
--
-- MySQL has no ADD COLUMN IF NOT EXISTS, so each change is guarded against
-- information_schema to keep this file re-runnable on an already-migrated DB.

SET @col := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
               AND COLUMN_NAME = 'status');
SET @sql := IF(@col = 0,
    "ALTER TABLE vehicles ADD COLUMN status ENUM('ACTIVE','SOLD') NOT NULL DEFAULT 'ACTIVE'
        COMMENT 'ACTIVE = seen in the latest healthy crawl; SOLD = gone from it'
        AFTER is_duplicate",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
               AND COLUMN_NAME = 'sold_detected_at');
SET @sql := IF(@col = 0,
    "ALTER TABLE vehicles ADD COLUMN sold_detected_at DATETIME NULL
        COMMENT 'When the listing was first found missing (not the real sale date)'
        AFTER status",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
               AND COLUMN_NAME = 'last_batch_id');
SET @sql := IF(@col = 0,
    "ALTER TABLE vehicles ADD COLUMN last_batch_id BIGINT UNSIGNED NULL
        COMMENT 'crawl_batches.id of the run that last saw this listing'
        AFTER sold_detected_at",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx := (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
               AND INDEX_NAME = 'idx_vehicles_model_status');
SET @sql := IF(@idx = 0,
    'ALTER TABLE vehicles ADD KEY idx_vehicles_model_status (model_key, status)',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx := (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
               AND INDEX_NAME = 'idx_vehicles_last_batch');
SET @sql := IF(@idx = 0,
    'ALTER TABLE vehicles ADD KEY idx_vehicles_last_batch (last_batch_id)',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
