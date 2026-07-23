-- Connectivity check (the "connectivity" report in reports.toml).
--
-- Works on any PostgreSQL database and confirms the pipeline end to end
-- (login -> query -> file download). Point a report at your real query by
-- adding a [[report]] entry with its own sql_file.
--
-- MySQL note: use DATABASE() instead of current_database().

SELECT
    now()               AS generated_at,
    current_user        AS db_user,
    current_database()  AS db_name;
