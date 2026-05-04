-- SYNTHETIC DATA - NOT REAL
--
-- Schema for the M&A Due Diligence sample's structured target-company
-- data. Applied by the Aurora schema bootstrap Custom Resource
-- (``lambda/aurora_bootstrap/handler.py``) via the RDS Data API.
--
-- Design reference:
--   .kiro/specs/ma-due-diligence-agentcore/design.md
--     section "Data Model - Aurora PostgreSQL Schema"
--
-- Statements are separated by semicolons and intended to be executed
-- one at a time. Every statement must be safely idempotent so the
-- bootstrap CR's Update path (which re-applies the whole file) never
-- breaks a previously deployed schema.

CREATE SCHEMA IF NOT EXISTS mna;

CREATE TABLE IF NOT EXISTS mna.target_companies (
    company_id                         TEXT PRIMARY KEY,
    legal_name                         TEXT NOT NULL,
    headquarters_region                TEXT,
    revenue_usd                        NUMERIC(14, 2),
    ebitda_margin_pct                  NUMERIC(5, 2),
    fleet_size                         INTEGER,
    employee_count                     INTEGER,
    customer_concentration_top1_pct    NUMERIC(5, 2),
    service_lines                      TEXT[],
    last_updated                       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tc_revenue
    ON mna.target_companies (revenue_usd);

CREATE INDEX IF NOT EXISTS idx_tc_ebitda
    ON mna.target_companies (ebitda_margin_pct);
