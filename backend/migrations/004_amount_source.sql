-- 004_amount_source.sql
--
-- 给 recommendations 增加"金额来源"列（amount_source）。
--
-- 为什么需要：规则节点本来就区分金额的来源 —— 退款引用**支付事实的实付金额**，
-- 物流停滞补偿引用**政策条款里的标准**。但这两者以前只写在 rationale 的散文里，
-- 没有落库：审批人在界面上看不到"这 89 元是怎么来的"，事后也无法按来源统计
-- （例如"有多少赔付是引用政策标准、有多少是引用订单实付"）。
--
-- 兼容性：
--   * 全新建库：001_init.sql / 003_tickets.sql 已包含该列，因此必须使用 IF NOT EXISTS；
--   * 已有库升级：执行下面这条 ALTER（SQLite 与 Postgres 语法一致）。
-- 注意：正式迁移只支持 PostgreSQL；旧的存量行该列为 NULL，语义是"未知来源"。

ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS amount_source VARCHAR(64);
