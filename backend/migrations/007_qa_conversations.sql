-- 知识库问答会话归档：会话列表、消息恢复与连续追问。
CREATE TABLE IF NOT EXISTS qa_conversations (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    title VARCHAR(120) NOT NULL,
    last_message_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_qa_conversations_tenant_id ON qa_conversations (tenant_id);
CREATE INDEX IF NOT EXISTS ix_qa_conversations_user_id ON qa_conversations (user_id);
CREATE INDEX IF NOT EXISTS ix_qa_conversations_last_message_at ON qa_conversations (last_message_at);
CREATE INDEX IF NOT EXISTS ix_qa_conversation_owner_updated
    ON qa_conversations (tenant_id, user_id, last_message_at);

CREATE TABLE IF NOT EXISTS qa_messages (
    id VARCHAR(36) PRIMARY KEY,
    conversation_id VARCHAR(36) NOT NULL,
    tenant_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    sequence INTEGER NOT NULL,
    query TEXT NOT NULL,
    answer JSON NOT NULL,
    request_options JSON NOT NULL,
    source_audit_id VARCHAR(36),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_qa_message_conversation_sequence UNIQUE (conversation_id, sequence),
    CONSTRAINT uq_qa_messages_source_audit_id UNIQUE (source_audit_id)
);

CREATE INDEX IF NOT EXISTS ix_qa_messages_conversation_id ON qa_messages (conversation_id);
CREATE INDEX IF NOT EXISTS ix_qa_messages_tenant_id ON qa_messages (tenant_id);
CREATE INDEX IF NOT EXISTS ix_qa_messages_user_id ON qa_messages (user_id);
CREATE INDEX IF NOT EXISTS ix_qa_messages_source_audit_id ON qa_messages (source_audit_id);
CREATE INDEX IF NOT EXISTS ix_qa_message_conversation_created
    ON qa_messages (conversation_id, created_at);
