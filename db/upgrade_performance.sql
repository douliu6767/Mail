-- 数据库性能优化升级脚本
-- 此脚本用于为现有数据库添加性能优化索引
-- 可以安全地在已有数据的数据库上运行

-- 为 card_logs 添加组合索引以优化 last_used_at 查询
CREATE INDEX IF NOT EXISTS idx_card_logs_card_created ON card_logs(card_id, created_at DESC);

-- 为邮箱搜索优化的额外索引
CREATE INDEX IF NOT EXISTS idx_mail_accounts_id_email ON mail_accounts(id, email);
CREATE INDEX IF NOT EXISTS idx_mail_accounts_server_status ON mail_accounts(server, status);

-- 为卡密搜索优化的额外索引  
CREATE INDEX IF NOT EXISTS idx_cards_status_id ON cards(status, id);
CREATE INDEX IF NOT EXISTS idx_cards_key_status ON cards(card_key, status);

-- 为代理搜索优化的额外索引
CREATE INDEX IF NOT EXISTS idx_http_proxies_status_id ON http_proxies(status, id);
CREATE INDEX IF NOT EXISTS idx_socks5_proxies_status_id ON socks5_proxies(status, id);
CREATE INDEX IF NOT EXISTS idx_http_proxies_name_host ON http_proxies(name, host);
CREATE INDEX IF NOT EXISTS idx_socks5_proxies_name_host ON socks5_proxies(name, host);

-- SQLite 特定优化: 启用 WAL 模式以提高并发性能
-- 注意: 这个命令不能在事务中执行，需要单独运行
-- PRAGMA journal_mode=WAL;
-- PRAGMA synchronous=NORMAL;

-- 为邮箱与分组关联表添加覆盖索引
CREATE INDEX IF NOT EXISTS idx_mailbox_group_mappings_group_mailbox ON mailbox_group_mappings(group_id, mailbox_id);
