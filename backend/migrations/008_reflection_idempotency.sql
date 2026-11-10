-- 反思/修订节点可能因 checkpoint 恢复或 worker 重放再次 finalize。
-- 数据库层保证同一分析任务只产生一条建议、同一草稿版本只产生一条消息。
ALTER TABLE ticket_messages
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_message_idempotency
    ON ticket_messages (ticket_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_reco_ticket_analysis_task
    ON recommendations (ticket_id, analysis_task_id)
    WHERE analysis_task_id IS NOT NULL;
