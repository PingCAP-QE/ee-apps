# Roster Schema Design

## Goal

Sync company employee information from Lark into DB tables that can be joined by
email or GitHub account for cost attribution, CI ownership, and issue tracking.

The schema keeps the first version small:

- no snapshots
- no identity table
- no closure table
- no multi-group relation table for now

Hierarchy queries use materialized path columns.

## Tables

### `roster_employees`

Stores one row per employee.

```sql
CREATE TABLE roster_employees (
  id BIGINT NOT NULL AUTO_INCREMENT,
  lark_id VARCHAR(128) NOT NULL,
  name VARCHAR(255) NOT NULL,
  employee_no VARCHAR(64) NULL,
  email VARCHAR(255) NULL,
  github_id VARCHAR(255) NULL,
  join_time DATETIME NULL,
  manager_id BIGINT NULL,
  manager_path VARCHAR(1024) NULL,
  group_id BIGINT NULL,
  group_path VARCHAR(1024) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  last_seen_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_roster_employees_lark_id (lark_id),
  UNIQUE KEY uk_roster_employees_email (email),
  UNIQUE KEY uk_roster_employees_github_id (github_id),
  KEY idx_roster_employees_employee_no (employee_no),
  KEY idx_roster_employees_manager (manager_id),
  KEY idx_roster_employees_group (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

Field notes:

- `id`: internal numeric primary key for joins.
- `lark_id`: Lark `union_id`, used for sync upsert.
- `employee_no`: employee number from Lark, used for HR and cost data reconciliation.
- `email`: company email. Empty values from Lark must be normalized to `NULL`.
- `github_id`: GitHub account value from Lark. This is expected to be the GitHub login/name if Lark stores that as the custom field.
- `join_time`: employee join time from Lark `join_time`, stored as UTC `DATETIME`.
- `manager_id`: direct manager, references `roster_employees.id`.
- `manager_path`: ancestor manager chain, stored as `/ceo_id/cto_id/direct_manager_id/`. It does not include the employee's own `id`.
- `group_id`: employee's primary/direct group, usually the leaf group from Lark.
- `group_path`: full group path for the primary group, copied from `roster_groups.path`.
- `is_active`: active employee flag.
- `last_seen_at`: last successful sync time when this employee was returned by Lark.

### `roster_groups`

Stores one row per Lark group/department.

```sql
CREATE TABLE roster_groups (
  id BIGINT NOT NULL AUTO_INCREMENT,
  lark_group_id VARCHAR(128) NOT NULL,
  parent_id BIGINT NULL,
  name VARCHAR(255) NOT NULL,
  manager_id BIGINT NULL,
  path VARCHAR(1024) NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  last_seen_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_roster_groups_lark_group_id (lark_group_id),
  KEY idx_roster_groups_parent (parent_id),
  KEY idx_roster_groups_manager (manager_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

Field notes:

- `id`: internal numeric primary key.
- `lark_group_id`: Lark group/department ID used for sync upsert.
- `parent_id`: parent group, references `roster_groups.id`.
- `manager_id`: group manager, references `roster_employees.id`.
- `path`: materialized group path including itself, for example `/root_id/dept_id/team_id/`.
- `is_active`: active group flag.
- `last_seen_at`: last successful sync time when this group was returned by Lark.

## Hierarchy Queries

Find all direct and indirect reports of a manager:

```sql
SELECT *
FROM roster_employees
WHERE manager_path LIKE CONCAT('%/', :manager_id, '/%')
  AND is_active = 1;
```

Find a manager and all direct or indirect reports:

```sql
SELECT *
FROM roster_employees
WHERE (id = :manager_id OR manager_path LIKE CONCAT('%/', :manager_id, '/%'))
  AND is_active = 1;
```

Find employees under a group, including nested child groups:

```sql
SELECT *
FROM roster_employees
WHERE group_path LIKE CONCAT('%/', :group_id, '/%')
  AND is_active = 1;
```

Find child groups under a group:

```sql
SELECT *
FROM roster_groups
WHERE path LIKE CONCAT('%/', :group_id, '/%')
  AND is_active = 1;
```

## Sync Rules

The sync job should run as a full refresh:

Implementation should run Pass 1 and Pass 2 in a single DB transaction:

```python
with engine.begin() as conn:
    ...
```

Pass 1:

1. Fetch all Lark groups and employees.
2. Upsert groups by `lark_group_id`, with reference columns set to `NULL` for now.
3. Upsert employees by `lark_id`, with reference columns set to `NULL` for now.
4. Update `last_seen_at` for every group and employee returned by Lark.

Pass 2:

1. Build in-memory maps from Lark IDs to internal numeric IDs.
2. Resolve group `parent_id`.
3. Resolve group `manager_id`.
4. Resolve employee `manager_id`.
5. Resolve employee `group_id`.
6. Build `roster_groups.path` from group parent chains.
7. Build `roster_employees.manager_path` from employee manager chains.
8. Copy the employee primary group's `path` into `roster_employees.group_path`.

Inactive handling:

- A failed sync must not update `is_active`.
- Employees and groups should only be marked inactive after they have not been seen for
  a grace period, for example `last_seen_at < NOW() - INTERVAL 2 DAY`.
- Empty `email` and `github_id` values from Lark must be stored as `NULL`, not empty strings,
  so nullable unique keys do not conflict.
- Duplicate `email` or `github_id` values in one Lark fetch must be stored as `NULL` for all
  employees with that duplicated value. These columns are join keys; keeping ambiguous values is
  worse than leaving the join key empty.
- TODO: During implementation, verify whether Lark `employee_no` is globally unique and whether
  empty values are returned as empty strings. If it is stable and unique, change
  `idx_roster_employees_employee_no` to a unique key.
- Because `group_path` is denormalized onto employees, every full sync must refresh it after
  group paths are rebuilt.

## Current Tradeoffs

The first version stores only one primary group per employee. This is enough for the
initial cost and ownership joins when we choose the leaf/default group from Lark.

If cost attribution later needs multiple direct groups per employee, add a relation
table then. Do not add it before that need is real.
