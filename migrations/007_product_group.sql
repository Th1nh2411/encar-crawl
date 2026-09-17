-- Sonokong `product_group` — the vehicle taxonomy behind rent_product.group_id.
--
-- A tree: `upper_id` points at the parent, 0 at the root. Four levels are in
-- use, and a car's model name is the path from its root down to its group:
--
--     level 0  brand        현대 / 기아 / BMW            (223 rows)
--     level 1  model group  그랜저 / 쏘렌토 / 3시리즈     (2,082)
--     level 2  model        더 뉴그랜저IG               (7,666)
--     level 3  trim         1.6 HEV_노블레스            (33,755)
--
-- rent_product.group_id lands on level 2 or 3, so the readable name is built by
-- walking up: "기아 쏘렌토 쏘렌토 4세대 하이브리드 1.6 HEV_노블레스".
--
-- Columns mirror the source schema (product_group.sch), minus the ones nothing
-- here reads: desc, vehicle_image, reg_user_no, platform, is_interested,
-- autobegins_no, reg_date.

DROP TABLE IF EXISTS product_group;

CREATE TABLE product_group (
    group_id        INT          NOT NULL,
    upper_id        INT          NULL DEFAULT 0 COMMENT 'Parent group_id; 0 at the root',
    name            VARCHAR(255) NULL COMMENT 'Label for this level only, not the full path',
    group_code      VARCHAR(255) NULL,
    brand_nt        VARCHAR(255) NULL,
    model_nt        VARCHAR(255) NULL,
    model_detail_nt VARCHAR(255) NULL,
    is_del          INT          NOT NULL DEFAULT 0,
    is_enabled      INT          NOT NULL DEFAULT 1,
    PRIMARY KEY (group_id),
    KEY idx_product_group_upper (upper_id, is_del)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
