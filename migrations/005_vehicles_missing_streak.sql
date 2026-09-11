-- Consecutive-miss counter behind the SOLD flag.
--
-- One missed crawl is not a sale. A listing can vanish for a run because the
-- dealer paused it, Encar hid it, or a page came back short while still
-- answering HTTP 200. Marking SOLD on a single miss makes those permanent
-- errors, and a daily job accumulates them.
--
-- So a miss increments `missing_streak` and only a run that pushes the streak
-- to SOLD_MISSING_STREAK (default 2) flips the status. Seeing the listing again
-- resets the counter to 0.

SET @col := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
               AND COLUMN_NAME = 'missing_streak');
SET @sql := IF(@col = 0,
    "ALTER TABLE vehicles ADD COLUMN missing_streak INT NOT NULL DEFAULT 0
        COMMENT 'Consecutive healthy crawls that did not contain this listing'
        AFTER status",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
               AND COLUMN_NAME = 'last_missing_at');
SET @sql := IF(@col = 0,
    "ALTER TABLE vehicles ADD COLUMN last_missing_at DATETIME NULL
        COMMENT 'Most recent healthy crawl that did not contain this listing'
        AFTER missing_streak",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Rows already flagged SOLD by the pre-streak logic: give them a streak that
-- matches their status so nothing re-evaluates them at a lower threshold.
UPDATE vehicles
   SET missing_streak = GREATEST(missing_streak, 1)
 WHERE status = 'SOLD' AND missing_streak = 0;
