-- Predefined report query.
--
-- This default works on any PostgreSQL database and lets you verify the whole
-- pipeline end to end (login -> SSH tunnel -> query -> file download) BEFORE you
-- write the real report. Replace it with your actual query when ready.
--
-- MySQL note: use DATABASE() instead of current_database().

SELECT
    now()               AS generated_at,
    current_user        AS db_user,
    current_database()  AS db_name;
