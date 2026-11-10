-- 当前质检摘要列：列表筛选与导出使用，完整历史仍在 ticket_messages。
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS quality_status VARCHAR(24) NOT NULL DEFAULT 'unreviewed';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS quality_issue_codes JSON NOT NULL DEFAULT '[]';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS quality_reviewed_at TIMESTAMP;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS quality_reviewer_id VARCHAR(36);
CREATE INDEX IF NOT EXISTS ix_tickets_quality_status ON tickets (quality_status);

-- 存量 PostgreSQL 数据回填最新一条客户不可见质检记录。
WITH latest AS (
    SELECT DISTINCT ON (ticket_id)
        ticket_id,
        meta ->> 'quality_result' AS quality_status,
        meta -> 'quality_issue_codes' AS quality_issue_codes,
        created_at AS quality_reviewed_at,
        actor_id AS quality_reviewer_id
    FROM ticket_messages
    WHERE visible_to_customer = FALSE
      AND meta ->> 'message_type' = 'quality_review'
    ORDER BY ticket_id, created_at DESC
)
UPDATE tickets AS t
SET quality_status = COALESCE(latest.quality_status, 'unreviewed'),
    quality_issue_codes = COALESCE(latest.quality_issue_codes, '[]'::json),
    quality_reviewed_at = latest.quality_reviewed_at,
    quality_reviewer_id = latest.quality_reviewer_id
FROM latest
WHERE t.id = latest.ticket_id;
