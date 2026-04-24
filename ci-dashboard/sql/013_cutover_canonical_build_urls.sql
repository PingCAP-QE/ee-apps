ALTER TABLE ci_l1_builds
  CHANGE COLUMN normalized_build_key normalized_build_url VARCHAR(1024) NULL;

ALTER TABLE ci_l1_builds
  DROP INDEX IF EXISTS idx_ci_l1_builds_normalized_build_key;

UPDATE ci_l1_builds
SET normalized_build_url = COALESCE(NULLIF(TRIM(normalized_build_url), ''), NULLIF(TRIM(url), ''));

UPDATE ci_l1_builds
SET normalized_build_url = REPLACE(normalized_build_url, '/display/redirect', '')
WHERE normalized_build_url IS NOT NULL;

UPDATE ci_l1_builds
SET normalized_build_url = REGEXP_REPLACE(normalized_build_url, '^https?://prow\\.tidb\\.net', '')
WHERE normalized_build_url REGEXP '^https?://prow\\.tidb\\.net/';

UPDATE ci_l1_builds
SET normalized_build_url = REGEXP_REPLACE(normalized_build_url, '^https?://do\\.pingcap\\.net', '')
WHERE normalized_build_url REGEXP '^https?://do\\.pingcap\\.net/';

UPDATE ci_l1_builds
SET normalized_build_url = REGEXP_REPLACE(
  normalized_build_url,
  '^https?://jenkins\\.jenkins\\.svc\\.cluster\\.local(:[0-9]+)?',
  ''
)
WHERE normalized_build_url REGEXP '^https?://jenkins\\.jenkins\\.svc\\.cluster\\.local(:[0-9]+)?/';

UPDATE ci_l1_builds
SET normalized_build_url = CONCAT('/', TRIM(LEADING '/' FROM normalized_build_url))
WHERE normalized_build_url IS NOT NULL
  AND normalized_build_url <> ''
  AND LEFT(normalized_build_url, 1) <> '/';

UPDATE ci_l1_builds
SET normalized_build_url = CONCAT('/jenkins', normalized_build_url)
WHERE normalized_build_url LIKE '/job/%';

UPDATE ci_l1_builds
SET normalized_build_url = CONCAT(
  'https://prow.tidb.net',
  REGEXP_REPLACE(normalized_build_url, '/+$', ''),
  '/'
)
WHERE normalized_build_url LIKE '/jenkins/job/%'
   OR normalized_build_url LIKE '/view/gs/%';

UPDATE ci_l1_builds
SET normalized_build_url = NULL
WHERE normalized_build_url IS NOT NULL
  AND normalized_build_url <> ''
  AND normalized_build_url NOT LIKE 'https://prow.tidb.net/jenkins/job/%'
  AND normalized_build_url NOT LIKE 'https://prow.tidb.net/view/gs/%';

CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_normalized_build_url
  ON ci_l1_builds (normalized_build_url(768));

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS normalized_build_url VARCHAR(1024) NULL AFTER source_prow_job_id;

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = COALESCE(
  NULLIF(TRIM(normalized_build_url), ''),
  NULLIF(TRIM(normalized_build_key), ''),
  NULLIF(TRIM(jenkins_build_url_key), '')
);

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = REPLACE(normalized_build_url, '/display/redirect', '')
WHERE normalized_build_url IS NOT NULL;

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = REGEXP_REPLACE(normalized_build_url, '^https?://prow\\.tidb\\.net', '')
WHERE normalized_build_url REGEXP '^https?://prow\\.tidb\\.net/';

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = REGEXP_REPLACE(normalized_build_url, '^https?://do\\.pingcap\\.net', '')
WHERE normalized_build_url REGEXP '^https?://do\\.pingcap\\.net/';

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = REGEXP_REPLACE(
  normalized_build_url,
  '^https?://jenkins\\.jenkins\\.svc\\.cluster\\.local(:[0-9]+)?',
  ''
)
WHERE normalized_build_url REGEXP '^https?://jenkins\\.jenkins\\.svc\\.cluster\\.local(:[0-9]+)?/';

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = CONCAT('/', TRIM(LEADING '/' FROM normalized_build_url))
WHERE normalized_build_url IS NOT NULL
  AND normalized_build_url <> ''
  AND LEFT(normalized_build_url, 1) <> '/';

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = CONCAT('/jenkins', normalized_build_url)
WHERE normalized_build_url LIKE '/job/%';

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = CONCAT(
  'https://prow.tidb.net',
  REGEXP_REPLACE(normalized_build_url, '/+$', ''),
  '/'
)
WHERE normalized_build_url LIKE '/jenkins/job/%'
   OR normalized_build_url LIKE '/view/gs/%';

UPDATE ci_l1_pod_lifecycle
SET normalized_build_url = NULL
WHERE normalized_build_url IS NOT NULL
  AND normalized_build_url <> ''
  AND normalized_build_url NOT LIKE 'https://prow.tidb.net/jenkins/job/%'
  AND normalized_build_url NOT LIKE 'https://prow.tidb.net/view/gs/%';

ALTER TABLE ci_l1_pod_lifecycle
  DROP INDEX IF EXISTS idx_ci_l1_pod_lifecycle_jenkins_build_key;

ALTER TABLE ci_l1_pod_lifecycle
  DROP COLUMN IF EXISTS normalized_build_key;

ALTER TABLE ci_l1_pod_lifecycle
  DROP COLUMN IF EXISTS jenkins_build_url_key;

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_lifecycle_normalized_build_url
  ON ci_l1_pod_lifecycle (normalized_build_url(768));
