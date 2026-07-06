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

-- IMPORTANT CONTEXT FOR SQL GENERATION:
-- ebitda_margin_pct and customer_concentration_top1_pct are stored as
-- PERCENTAGE POINTS on a 0-100 scale, NOT as a 0-1 fraction. For
-- example, a value of 15.78 means an EBITDA margin of 15.78%, not
-- 1578%. When a user says "EBITDA margin above 12%", filter with
-- `ebitda_margin_pct > 12`, NOT `ebitda_margin_pct > 0.12` (the latter
-- is a no-op filter that matches every row, since every margin in
-- this table is well above 0.12).
CREATE TABLE IF NOT EXISTS mna.target_companies (
    company_id                         TEXT PRIMARY KEY,
    legal_name                         TEXT NOT NULL,
    headquarters_region                TEXT,
    revenue_usd                        NUMERIC(14, 2),
    ebitda_margin_pct                  NUMERIC(5, 2), -- 0-100 scale, e.g. 15.78 = 15.78%
    fleet_size                         INTEGER,
    employee_count                     INTEGER,
    customer_concentration_top1_pct    NUMERIC(5, 2), -- 0-100 scale, e.g. 29.2 = 29.2%
    service_lines                      TEXT[],
    last_updated                       TIMESTAMP DEFAULT NOW()
);

-- IMPORTANT CONTEXT FOR SQL GENERATION:
-- All 25 companies in this table are transportation and logistics companies.
-- There is NO need to filter by industry — every row is already in the
-- transportation/logistics sector.
--
-- Valid service_lines values (operational capabilities, not industry labels):
--   'cold-chain', 'drayage', 'freight-brokerage', 'intermodal',
--   'last-mile', 'less-than-truckload', 'ocean-freight', 'rail',
--   'truckload', 'warehousing'
--
-- When a user asks for "transportation companies" or "logistics companies",
-- do NOT filter on service_lines — simply query by numeric criteria
-- (revenue_usd, ebitda_margin_pct, fleet_size, employee_count, etc.).
-- Only filter on service_lines when the user asks for a specific operational
-- capability (e.g. "cold-chain operators" or "companies with rail service").

CREATE INDEX IF NOT EXISTS idx_tc_revenue
    ON mna.target_companies (revenue_usd);

CREATE INDEX IF NOT EXISTS idx_tc_ebitda
    ON mna.target_companies (ebitda_margin_pct);
