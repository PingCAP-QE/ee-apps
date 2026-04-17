import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";

export function DashboardLayout({
  filters,
  onFilterChange,
  onResetFilters,
  filterOptions,
  children,
}) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-mark">
          <span className="brand-mark__eyebrow">CI Metrics</span>
          <h1>Dashboard Studio</h1>
          <p>Build health, flaky patterns, and drift across repos and clouds.</p>
        </div>

        <nav className="sidebar-nav" aria-label="Primary">
          <NavItem to="/" label="Overview" caption="Signal at a glance" />
          <NavItem to="/build-trend" label="CI Status" caption="Volume and duration" />
          <NavItem to="/flaky" label="Flaky" caption="Noisy failures and blind-retry-loop patterns" />
        </nav>

        <div className="sidebar-note">
          <span className="sidebar-note__label">V1 scope</span>
          <p>
            Build ingestion stays all-repo. PR metadata is best effort and can lag a little
            behind source activity.
          </p>
        </div>
      </aside>

      <div className="main-column">
        <FilterBar
          filters={filters}
          onFilterChange={onFilterChange}
          onResetFilters={onResetFilters}
          filterOptions={filterOptions}
        />
        <main className="page-content">{children}</main>
      </div>
    </div>
  );
}

function NavItem({ to, label, caption }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        isActive ? "sidebar-link sidebar-link--active" : "sidebar-link"
      }
    >
      <span className="sidebar-link__label">{label}</span>
      <span className="sidebar-link__caption">{caption}</span>
    </NavLink>
  );
}

function FilterBar({ filters, onFilterChange, onResetFilters, filterOptions }) {
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(false);
  const [isCompact, setIsCompact] = useState(false);
  const scopePills = [
    { label: "Repo", value: filters.repo || "All repos" },
    { label: "Branch", value: filters.branch || "All branches" },
    { label: "Job", value: filters.job_name || "All jobs" },
    { label: "Cloud", value: filters.cloud_phase || "All clouds" },
    { label: "Issues", value: filters.issue_status || "All issues" },
    { label: "Bucket", value: filters.granularity },
  ];

  useEffect(() => {
    let frameId = 0;

    function syncCompactState() {
      frameId = 0;
      setIsCompact(window.scrollY > 8);
    }

    function handleScroll() {
      if (frameId !== 0) {
        return;
      }
      frameId = window.requestAnimationFrame(syncCompactState);
    }

    syncCompactState();
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => {
      if (frameId !== 0) {
        window.cancelAnimationFrame(frameId);
      }
      window.removeEventListener("scroll", handleScroll);
    };
  }, []);

  return (
    <section className={isCompact ? "filter-bar filter-bar--compact" : "filter-bar"}>
      <div className="filter-bar__header">
        <div className="filter-bar__header-copy">
          <span className="filter-bar__eyebrow">Global filters</span>
          <h2>{filterOptions.scopeLabel}</h2>
        </div>
        <div className="filter-bar__actions">
          <button
            className="ghost-button ghost-button--secondary"
            type="button"
            onClick={() => setShowAdvancedFilters((current) => !current)}
          >
            {showAdvancedFilters ? "Less filters" : "More filters"}
          </button>
          <button className="ghost-button" type="button" onClick={onResetFilters}>
            Reset
          </button>
        </div>
      </div>

      <div className="filter-bar__scope">
        {scopePills.map((item) => (
          <span key={item.label} className="scope-pill">
            <strong>{item.label}</strong>
            <span>{item.value}</span>
          </span>
        ))}
      </div>

      <p className="filter-bar__note">
        Ingestion stays all-repo and all-branch. These filters only narrow the dashboard view.
      </p>

      <div className="filter-grid">
        <FilterField
          label="Start"
          control={
            <input
              type="date"
              value={filters.start_date}
              onChange={(event) => onFilterChange("start_date", event.target.value)}
            />
          }
        />
        <FilterField
          label="End"
          control={
            <input
              type="date"
              value={filters.end_date}
              onChange={(event) => onFilterChange("end_date", event.target.value)}
            />
          }
        />
        <FilterField
          label="Repo"
          control={
            <select
              value={filters.repo}
              onChange={(event) => onFilterChange("repo", event.target.value)}
            >
              <option value="">All repos</option>
              {filterOptions.repos.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          }
        />
        <FilterField
          label="Branch"
          control={
            <select
              value={filters.branch}
              onChange={(event) => onFilterChange("branch", event.target.value)}
            >
              <option value="">All branches</option>
              {filterOptions.branches.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          }
        />
      </div>

      {showAdvancedFilters ? (
        <div className="filter-grid filter-grid--advanced">
          <FilterField
            label="Job"
            className="filter-field--job"
            control={
              <select
                value={filters.job_name}
                onChange={(event) => onFilterChange("job_name", event.target.value)}
                disabled={!filterOptions.jobs.length}
              >
                <option value="">All jobs</option>
                {filterOptions.jobs.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            }
          />
          <FilterField
            label="Cloud"
            control={
              <select
                value={filters.cloud_phase}
                onChange={(event) => onFilterChange("cloud_phase", event.target.value)}
              >
                <option value="">All clouds</option>
                {filterOptions.cloudPhases.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            }
          />
          <FilterField
            label="Issue status"
            control={
              <select
                value={filters.issue_status}
                onChange={(event) => onFilterChange("issue_status", event.target.value)}
              >
                <option value="">All issues</option>
                <option value="open">Open</option>
                <option value="closed">Closed</option>
              </select>
            }
          />
          <FilterField
            label="Bucket"
            control={
              <select
                value={filters.granularity}
                onChange={(event) => onFilterChange("granularity", event.target.value)}
              >
                <option value="day">Day</option>
                <option value="week">Week</option>
              </select>
            }
          />
        </div>
      ) : null}
    </section>
  );
}

function FilterField({ label, control, className = "" }) {
  return (
    <label className={className ? `filter-field ${className}` : "filter-field"}>
      <span>{label}</span>
      {control}
    </label>
  );
}
