-- normalized_build_url is a correlation key. Legacy non-GCP URLs may have been
-- canonicalized to prow.tidb.net before the source URL column was removed.
UPDATE ci_l1_builds
SET cloud_phase = 'TENCENT'
WHERE UPPER(COALESCE(cloud_phase, '')) <> 'GCP';

UPDATE problem_case_runs
SET cloud_phase = CASE
  WHEN COALESCE(build_url, '') LIKE 'https://prow.tidb.net/%' THEN 'GCP'
  ELSE 'TENCENT'
END
WHERE
  (COALESCE(build_url, '') LIKE 'https://prow.tidb.net/%'
   AND UPPER(COALESCE(cloud_phase, '')) <> 'GCP')
  OR
  (COALESCE(build_url, '') NOT LIKE 'https://prow.tidb.net/%'
   AND UPPER(COALESCE(cloud_phase, '')) <> 'TENCENT');
