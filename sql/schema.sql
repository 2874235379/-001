-- =============================================================================
-- FacePass 人脸考勤打卡系统 · 建库建表脚本
-- 目标库：MySQL 5.7.26（小皮面板 / phpStudy）  字符集：utf8mb4
-- 使用方式：
--   1) 小皮面板 -> 数据库 -> SQL 执行（整份粘贴执行）
--   2) 或命令行：mysql -uroot -p < sql/schema.sql
--   3) 或运行：python scripts/init_db.py（会自动建库建表）
-- =============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE DATABASE IF NOT EXISTS `face_attendance`
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `face_attendance`;

-- -----------------------------------------------------------------------------
-- 员工表
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `employees` (
  `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `emp_no`      VARCHAR(32)  NOT NULL                COMMENT '工号（唯一）',
  `name`        VARCHAR(64)  NOT NULL                COMMENT '姓名',
  `department`  VARCHAR(64)  NOT NULL DEFAULT ''     COMMENT '部门',
  `active`      TINYINT(1)   NOT NULL DEFAULT 1      COMMENT '是否在职：1 是 0 否',
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_emp_no` (`emp_no`),
  KEY `idx_department` (`department`),
  KEY `idx_active` (`active`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
  COMMENT = '员工基础信息';

-- -----------------------------------------------------------------------------
-- 人脸特征表：一条记录 = 该员工的一张人脸样本（推荐每人 3 张）
-- encoding 存 128 个 float32（小端序），固定 512 字节
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `face_encodings` (
  `id`           INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `employee_id`  INT UNSIGNED NOT NULL                COMMENT '员工 ID',
  `encoding`     BLOB         NOT NULL                COMMENT '128 维 float32 特征向量（512 字节）',
  `dims`         SMALLINT UNSIGNED NOT NULL DEFAULT 128 COMMENT '维度',
  `model`        VARCHAR(64)  NOT NULL DEFAULT 'dlib_resnet_v1' COMMENT '特征模型标识',
  `sample_index` TINYINT UNSIGNED NOT NULL DEFAULT 0  COMMENT '样本序号（同一员工可多张）',
  `created_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_employee` (`employee_id`),
  KEY `idx_employee_sample` (`employee_id`, `sample_index`),
  CONSTRAINT `fk_face_employee` FOREIGN KEY (`employee_id`) REFERENCES `employees` (`id`)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
  COMMENT = '人脸特征库（dlib ResNet 128 维）';

-- -----------------------------------------------------------------------------
-- 打卡记录表：冗余存工号 / 姓名 / 部门，便于后台导出与历史追溯
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `attendance_records` (
  `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `employee_id`    INT UNSIGNED    NOT NULL                COMMENT '员工 ID',
  `emp_no`         VARCHAR(32)     NOT NULL                COMMENT '工号（冗余）',
  `employee_name`  VARCHAR(64)     NOT NULL                COMMENT '姓名（冗余）',
  `department`     VARCHAR(64)     NOT NULL DEFAULT ''     COMMENT '部门（冗余）',
  `work_date`      DATE            NOT NULL                COMMENT '考勤日期',
  `punch_type`     VARCHAR(8)      NOT NULL                COMMENT '上班 / 下班',
  `punch_time`     DATETIME        NOT NULL                COMMENT '打卡时间',
  `status`         VARCHAR(8)      NOT NULL                COMMENT '正常 / 迟到 / 早退',
  `liveness_score` DECIMAL(5,4)    NOT NULL DEFAULT 0.0000 COMMENT '活体分数 0~1',
  `match_distance` DECIMAL(6,4)    DEFAULT NULL            COMMENT '欧氏距离',
  `device`         VARCHAR(64)     NOT NULL DEFAULT 'web'  COMMENT '来源设备',
  `created_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_employee_date` (`employee_id`, `work_date`),
  KEY `idx_date_time` (`work_date`, `punch_time`),
  KEY `idx_punch_time` (`punch_time`),
  KEY `idx_status` (`status`),
  CONSTRAINT `fk_attendance_employee` FOREIGN KEY (`employee_id`) REFERENCES `employees` (`id`)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci
  COMMENT = '考勤打卡记录';

SET FOREIGN_KEY_CHECKS = 1;

-- 常用查询示例（不需要可删除）：
-- 今日全部打卡明细
-- SELECT emp_no, employee_name, department, punch_type, punch_time, status
-- FROM attendance_records WHERE work_date = CURDATE() ORDER BY punch_time DESC;
-- 本月迟到次数排行
-- SELECT employee_name, COUNT(*) AS late_times
-- FROM attendance_records
-- WHERE work_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') AND status = '迟到'
-- GROUP BY employee_id, employee_name ORDER BY late_times DESC;
