-- 015_drop_local_ledger.sql
-- GL-only refactor, Stage 7: the double-entry ledger now lives in the shared
-- onadapt-gl service. The app posts journal entries to /v1/journal-entries and
-- reads the trial balance, journals, analytics, and audit from it. The local
-- cost ledger is no longer read or written, so drop it.
--
-- reset_activity() no longer touches these tables (see schema.sql); the app
-- clears the GL on a dev-mode reset via gl_backed.reset_gl -> POST /v1/reset.
-- forbid_ledger_update() is kept — audit_certifications still uses it.

DROP TABLE IF EXISTS tag_distributions CASCADE;
DROP TABLE IF EXISTS cost_lines CASCADE;
DROP TABLE IF EXISTS cost_entries CASCADE;
