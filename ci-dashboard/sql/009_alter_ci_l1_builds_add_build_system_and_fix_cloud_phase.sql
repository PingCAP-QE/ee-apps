ALTER TABLE ci_l1_builds
  ADD COLUMN build_system VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN' AFTER cloud_phase;

UPDATE ci_l1_builds
SET cloud_phase = CASE
    WHEN url LIKE 'https://prow.tidb.net/%' THEN 'GCP'
    ELSE 'IDC'
  END,
  build_system = CASE
    WHEN url LIKE 'https://prow.tidb.net/view/gs/%' THEN 'PROW_NATIVE'
    WHEN url LIKE 'https://prow.tidb.net/jenkins/%' THEN 'JENKINS'
    WHEN url LIKE 'https://do.pingcap.net/%' THEN 'JENKINS'
    ELSE 'UNKNOWN'
  END;
