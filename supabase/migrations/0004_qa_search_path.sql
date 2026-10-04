-- Plan_Travel migration 0004_qa_search_path · version 3.0.0-alpha.1 · F01 QA
-- CHANGE 2026-10-04 F01: pin search_path on the qa harness functions (security advisor 0011). All references are schema-qualified.
alter function qa.f01_as(uuid, text)                 set search_path = '';
alter function qa.f01_count(uuid, text, text)        set search_path = '';
alter function qa.f01_dml(uuid, text, text)          set search_path = '';
alter function qa.f01_dml_rb(uuid, text, text)       set search_path = '';
alter function qa.f01_val(uuid, text, text)          set search_path = '';
alter function qa.f01_match(text, text)              set search_path = '';
alter function qa.f01_rec(jsonb, int, text, text, text) set search_path = '';
alter function qa.f01_run()                          set search_path = '';
