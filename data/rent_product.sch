/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19-11.4.5-MariaDB, for Linux (x86_64)
--
-- Host: localhost    Database: sonokong
-- ------------------------------------------------------
-- Server version	11.4.5-MariaDB-log

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*M!100616 SET @OLD_NOTE_VERBOSITY=@@NOTE_VERBOSITY, NOTE_VERBOSITY=0 */;

--
-- Table structure for table `rent_product`
--

DROP TABLE IF EXISTS `rent_product`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `rent_product` (
  `id` int(20) NOT NULL AUTO_INCREMENT,
  `car_number` varchar(30) DEFAULT NULL,
  `vin` varchar(50) DEFAULT NULL,
  `car_registration_certificate` int(11) DEFAULT NULL,
  `group_id` int(20) DEFAULT NULL,
  `is_del` int(2) DEFAULT 0,
  `car_option_note` mediumtext DEFAULT NULL,
  `car_db_regist_date` datetime DEFAULT current_timestamp(),
  `car_current_location` varchar(150) DEFAULT NULL,
  `car_owner` varchar(30) DEFAULT NULL,
  `purchase_location` varchar(60) DEFAULT NULL,
  `new_car_price` bigint(20) DEFAULT NULL,
  `discount_amount` bigint(20) DEFAULT NULL,
  `purchase_amount` bigint(20) DEFAULT NULL,
  `purchase_supply_price` bigint(20) DEFAULT NULL,
  `purchase_vat` bigint(20) DEFAULT NULL,
  `insurance_person1` varchar(60) DEFAULT NULL,
  `insurance_person2` varchar(60) DEFAULT NULL,
  `property_compensation_limit` varchar(60) DEFAULT NULL,
  `child_compensation_limit` varchar(60) DEFAULT NULL,
  `car_insurance` varchar(60) DEFAULT NULL,
  `uninsured_car_injury` varchar(60) DEFAULT NULL,
  `insurance_company` varchar(60) DEFAULT NULL,
  `insurance_annual_fee` bigint(20) DEFAULT NULL,
  `insurance_condition` varchar(100) DEFAULT NULL,
  `insurance_age` int(11) DEFAULT NULL,
  `warranty_info` varchar(50) DEFAULT NULL,
  `warranty_status` varchar(50) DEFAULT NULL,
  `car_source_url` varchar(200) DEFAULT NULL,
  `insurance_regist_date` datetime DEFAULT NULL,
  `insurance_sale_date` datetime DEFAULT NULL,
  `insurance_end_date` datetime DEFAULT NULL,
  `submenu` varchar(20) DEFAULT 'new',
  `use_autobegin` tinyint(2) DEFAULT NULL,
  `car_fuel` varchar(50) DEFAULT NULL,
  `resource` varchar(20) DEFAULT 'new',
  `purchaser_brand` varchar(20) DEFAULT NULL,
  `purchasing_officer` varchar(20) DEFAULT NULL,
  `purchasing_officer_phone_num` varchar(20) DEFAULT NULL,
  `operation_status` varchar(20) DEFAULT NULL,
  `car_status` enum('OPERATING','WAITING','SOLD') DEFAULT NULL,
  `contract_category` enum('NORMAL','LOW_CREDIT','SPECIAL') DEFAULT NULL,
  `contract_type` enum('RENT_BUY','RENT_RETURN','SUBSCRIBE_BUY','SUBSCRIBE_RETURN','KB_CAPITAL','HANA_CAPITAL') DEFAULT NULL,
  `rental_period_start` datetime DEFAULT NULL,
  `contracted_mileage` enum('10000','15000','20000','25000','30000') DEFAULT NULL,
  `updated_at` datetime DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `web_visibility` enum('VISIBLE','HIDDEN') DEFAULT 'VISIBLE',
  `ex_color` varchar(10) DEFAULT NULL,
  `in_color` varchar(10) DEFAULT NULL,
  `car_reg_date` timestamp NULL DEFAULT NULL,
  `reg_car_mn` varchar(255) DEFAULT NULL,
  `engine_displacement` int(11) DEFAULT NULL,
  `car_origin` enum('DOMESTIC','IMPORTED') DEFAULT NULL,
  `purchase_vendor` varchar(255) DEFAULT NULL,
  `purchase_manager` varchar(255) DEFAULT NULL,
  `manager_contract` varchar(255) DEFAULT NULL,
  `purchase_price` bigint(20) DEFAULT NULL,
  `sale_amt` bigint(20) NOT NULL,
  `sales_channel` varchar(250) DEFAULT NULL,
  `purchase_branch` varchar(255) DEFAULT NULL,
  `car_manufacture_date` datetime DEFAULT NULL,
  `bodily_injury_liability` varchar(20) DEFAULT NULL,
  `bodily_injury_deductible` bigint(20) DEFAULT NULL,
  `property_damage_deductible` bigint(20) DEFAULT NULL,
  `own_damage_deductible` varchar(100) DEFAULT NULL,
  `roadside_assist_per_year` varchar(20) DEFAULT NULL,
  `car_source` enum('SON_NO_KONG','KB_CAPITAL_EXTERNAL','HANA_CAPITAL_EXTERNAL','BNK_CAPITAL_EXTERNAL','WOORI_CAPITAL_EXTERNAL','TCAR_EXTERNAL','KB_CHACHACHA_EXTERNAL') DEFAULT NULL,
  `car_location` enum('KB_CHACHACHA','GIMPO_KOOKMIN','SEOUL_MOTORIUM','TCAR') DEFAULT NULL,
  `mileage` bigint(20) DEFAULT NULL,
  `contract_mileage` int(11) DEFAULT NULL,
  `rental_period` int(11) DEFAULT NULL,
  `car_class` varchar(20) DEFAULT NULL,
  `quotation_issue_count` int(11) DEFAULT 0,
  `user_no` int(11) DEFAULT NULL,
  `contract_available` enum('Y','N') NOT NULL DEFAULT 'N',
  `car_description` mediumtext DEFAULT NULL,
  `exposed_info_rent_return` tinyint(1) NOT NULL DEFAULT 0,
  `exposed_info_subscribe_take` tinyint(1) NOT NULL DEFAULT 0,
  `exposed_info_kb` tinyint(1) NOT NULL DEFAULT 0,
  `exposed_info_hana` tinyint(1) NOT NULL DEFAULT 0,
  `car_description_writer` varchar(30) DEFAULT NULL,
  `car_description_date` datetime DEFAULT NULL,
  `purchase_manager_staff_no` int(11) DEFAULT NULL,
  `is_unnormal_basic` tinyint(4) GENERATED ALWAYS AS (`group_id` is null or `car_manufacture_date` is null or `purchase_price` is null or `purchase_price` = 0 or `engine_displacement` is null or `car_fuel` is null) STORED,
  `inventory_check_status` enum('NEED_TO_CHECK','IN_STOCK','OUT_OF_STOCK') NOT NULL DEFAULT 'NEED_TO_CHECK',
  `exposed_info_subscribe_return` tinyint(1) NOT NULL DEFAULT 0,
  `save_count` int(11) DEFAULT 0,
  `car_region` varchar(10) DEFAULT NULL,
  `installment_available` enum('Y','N') DEFAULT 'N',
  `is_crawled` tinyint(1) DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_rent_product_isdel_group` (`is_del`,`group_id`),
  KEY `idx_rent_product_is_unnormal_basic` (`is_unnormal_basic`),
  KEY `idx_product_active_group` (`is_del`,`group_id`,`car_source`,`car_fuel`) USING BTREE,
  KEY `idx_product_updated` (`updated_at` DESC) USING BTREE
) ENGINE=InnoDB AUTO_INCREMENT=8870 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_general_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'IGNORE_SPACE,STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_AUTO_CREATE_USER,NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`127.0.0.1`*/ /*!50003 TRIGGER trg_rent_product_update
    BEFORE UPDATE ON rent_product
    FOR EACH ROW
BEGIN
    IF NEW.car_status <> OLD.car_status AND NEW.car_status <> 'WAITING' THEN
        SET NEW.web_visibility = 'HIDDEN';
    END IF;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*M!100616 SET NOTE_VERBOSITY=@OLD_NOTE_VERBOSITY */;

-- Dump completed on 2026-09-10 22:42:01
