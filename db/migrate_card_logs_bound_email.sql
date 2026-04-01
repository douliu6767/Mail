-- Migration: Add bound_email column to card_logs table
-- This ensures that the bound mailbox email is preserved in logs even after card deletion

-- For SQLite
-- Add bound_email column if it doesn't exist
-- Note: SQLite doesn't support IF NOT EXISTS for ALTER TABLE ADD COLUMN
-- The application code will handle checking if the column exists before attempting migration

ALTER TABLE card_logs ADD COLUMN bound_email TEXT DEFAULT '';

-- For MySQL/PostgreSQL, use this syntax instead:
-- ALTER TABLE card_logs ADD COLUMN IF NOT EXISTS bound_email VARCHAR(255) DEFAULT '';
