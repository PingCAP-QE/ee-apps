import { useEffect, useMemo, useRef, useState } from "react";

import { ALL_COST_SOURCES } from "../lib/filterUrl";

export const COST_BREAKDOWN_GROUPS = [
  { key: "account", label: "Account", description: "accounts" },
  { key: "owner", label: "Owner", description: "owners" },
  { key: "team", label: "Team", description: "teams" },
  { key: "sku", label: "SKU", description: "SKUs" },
  { key: "cost_driver", label: "SKU class", description: "SKU classes" },
  { key: "project", label: "Project", description: "projects" },
  { key: "region", label: "Region", description: "regions" },
];

export default function CostFilterControls({
  filters,
  onFilterChange,
  costSources = [],
  filterValues = {},
  costBreakdownGroupBy = "owner",
  onCostBreakdownGroupByChange,
}) {
  const [openPicker, setOpenPicker] = useState("");
  const accounts = useMemo(
    () => buildAccounts(costSources, filters.cost_source),
    [costSources, filters.cost_source],
  );
  const selectedAccounts = selectedValues(filters.cost_source);

  return (
    <section className="filter-bar cost-filter-controls">
      <div className="cost-filter-controls__dates">
        <DateField
          label="Start"
          value={filters.start_date}
          onChange={(value) => onFilterChange("start_date", value)}
        />
        <DateField
          label="End"
          value={filters.end_date}
          onChange={(value) => onFilterChange("end_date", value)}
        />
        <label className="filter-field">
          <span>Bucket</span>
          <select
            value={filters.granularity}
            onChange={(event) => onFilterChange("granularity", event.target.value)}
          >
            <option value="week">Week</option>
            <option value="month">Month</option>
          </select>
        </label>
        <label className="filter-field">
          <span>Group by</span>
          <select
            value={costBreakdownGroupBy}
            onChange={(event) => onCostBreakdownGroupByChange?.(event.target.value)}
          >
            {COST_BREAKDOWN_GROUPS.map((option) => (
              <option key={option.key} value={option.key}>{option.label}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="cost-filter-controls__picker-grid">
        <AccountPicker
          accounts={accounts}
          values={selectedAccounts}
          onChange={(values) => onFilterChange(
            "cost_source",
            values.length ? values.join(",") : ALL_COST_SOURCES,
          )}
          open={openPicker === "account"}
          onToggle={() => setOpenPicker((current) => (current === "account" ? "" : "account"))}
          onDismiss={() => setOpenPicker("")}
        />
        {[
          ["owner", "Owner"],
          ["team", "Team"],
          ["project", "Project"],
        ].map(([key, label]) => (
          <DimensionPicker
            key={key}
            label={label}
            options={filterValues[key] || []}
            selection={dimensionSelection(filters, key)}
            onChange={(selection) => onFilterChange({
              [`${key}_include`]: selection.mode === "include" ? selection.values.join(",") : "",
              [`${key}_exclude`]: selection.mode === "exclude" ? selection.values.join(",") : "",
            })}
            open={openPicker === key}
            onToggle={() => setOpenPicker((current) => (current === key ? "" : key))}
            onDismiss={() => setOpenPicker("")}
          />
        ))}
      </div>
    </section>
  );
}

function DateField({ label, value, onChange }) {
  return (
    <label className="filter-field">
      <span>{label}</span>
      <input type="date" value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function AccountPicker({ accounts, values, onChange, open, onToggle, onDismiss }) {
  const [query, setQuery] = useState("");
  const [draftValues, setDraftValues] = useState(values);
  const visibleAccounts = accounts.filter((item) => (
    item.label.toLowerCase().includes(query.trim().toLowerCase())
  ));
  const groups = groupAccounts(visibleAccounts);

  function handleToggle() {
    if (!open) {
      setQuery("");
      setDraftValues(values);
    }
    onToggle();
  }

  return (
    <PickerShell
      label="Account"
      summary={accountSelectionLabel(accounts, values)}
      open={open}
      onToggle={handleToggle}
      onDismiss={onDismiss}
      filtered={values.length > 0}
    >
      <PickerSearch placeholder="Filter accounts" value={query} onChange={setQuery} />
      {groups.map(([category, items]) => (
        <AccountGroup
          key={category}
          label={`${category} accounts`}
          items={items}
          values={draftValues}
          onChange={setDraftValues}
        />
      ))}
      <PickerFooter
        onClear={() => {
          setDraftValues([]);
          onChange([]);
        }}
        onApply={() => {
          onChange(draftValues);
          onToggle();
        }}
      />
    </PickerShell>
  );
}

function AccountGroup({ label, items, values, onChange }) {
  const itemValues = items.map((item) => item.value);
  const allSelected = itemValues.length > 0 && itemValues.every((value) => values.includes(value));

  return (
    <div className="cost-filter-controls__account-group">
      <div className="cost-filter-controls__account-group-header">
        <strong>{label}</strong>
        <button
          type="button"
          onClick={() => onChange(
            allSelected
              ? values.filter((value) => !itemValues.includes(value))
              : Array.from(new Set([...values, ...itemValues])),
          )}
        >
          {allSelected ? "Clear" : "Select all"}
        </button>
      </div>
      {items.map((item) => (
        <label key={item.value} className="cost-filter-controls__option">
          <input
            type="checkbox"
            checked={values.includes(item.value)}
            onChange={() => onChange(toggleValue(values, item.value))}
          />
          <span>{item.label}</span>
        </label>
      ))}
    </div>
  );
}

function DimensionPicker({ label, options, selection, onChange, open, onToggle, onDismiss }) {
  const [query, setQuery] = useState("");
  const [draftSelection, setDraftSelection] = useState(selection);
  const visibleOptions = options.filter((option) => (
    option.label.toLowerCase().includes(query.trim().toLowerCase())
  ));

  function handleToggle() {
    if (!open) {
      setQuery("");
      setDraftSelection(selection);
    }
    onToggle();
  }

  return (
    <PickerShell
      label={label}
      summary={dimensionSelectionLabel(label, selection)}
      open={open}
      onToggle={handleToggle}
      onDismiss={onDismiss}
      filtered={selection.values.length > 0}
    >
      <PickerSearch
        placeholder={`Filter ${label.toLowerCase()} values`}
        value={query}
        onChange={setQuery}
      />
      <div className="cost-filter-controls__mode" aria-label={`${label} filter mode`}>
        {["include", "exclude"].map((nextMode) => (
          <button
            key={nextMode}
            type="button"
            className={draftSelection.mode === nextMode ? "cost-filter-controls__mode-button cost-filter-controls__mode-button--active" : "cost-filter-controls__mode-button"}
            onClick={() => setDraftSelection((current) => ({ ...current, mode: nextMode }))}
          >
            {nextMode === "include" ? "Includes" : "Excludes"}
          </button>
        ))}
      </div>
      <div className="cost-filter-controls__option-list">
        {visibleOptions.map((option) => (
          <label key={option.value} className="cost-filter-controls__option">
            <input
              type="checkbox"
              checked={draftSelection.values.includes(option.value)}
              onChange={() => setDraftSelection((current) => ({
                ...current,
                values: toggleValue(current.values, option.value),
              }))}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
      <PickerFooter
        onClear={() => {
          const cleared = { mode: "include", values: [] };
          setDraftSelection(cleared);
          onChange(cleared);
        }}
        onApply={() => {
          onChange(draftSelection);
          onToggle();
        }}
      />
    </PickerShell>
  );
}

function PickerShell({ label, summary, open, filtered, onToggle, onDismiss, children }) {
  const pickerRef = useRef(null);

  useEffect(() => {
    if (!open || typeof document === "undefined") return undefined;
    const handlePointerDown = (event) => {
      if (!pickerRef.current?.contains(event.target)) onDismiss();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open, onDismiss]);

  return (
    <div ref={pickerRef} className="cost-filter-controls__picker">
      <span className="cost-filter-controls__picker-label">{label}</span>
      <button
        type="button"
        className={[
          "cost-filter-controls__picker-trigger",
          open ? "cost-filter-controls__picker-trigger--open" : "",
          filtered ? "cost-filter-controls__picker-trigger--filtered" : "",
        ].filter(Boolean).join(" ")}
        aria-expanded={open}
        onClick={onToggle}
      >
        <span>{summary}</span>
        <span aria-hidden="true">⌄</span>
      </button>
      {open ? <div className="cost-filter-controls__menu">{children}</div> : null}
    </div>
  );
}

function PickerSearch({ placeholder, value, onChange }) {
  return (
    <label className="cost-filter-controls__search">
      <span aria-hidden="true">⌕</span>
      <input
        type="search"
        placeholder={placeholder}
        aria-label={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function PickerFooter({ onClear, onApply }) {
  return (
    <div className="cost-filter-controls__menu-footer">
      <button type="button" onClick={onClear}>Clear</button>
      <button type="button" className="cost-filter-controls__apply" onClick={onApply}>Apply</button>
    </div>
  );
}

function selectedValues(value) {
  return value && value !== ALL_COST_SOURCES
    ? value.split(",").map((item) => item.trim()).filter(Boolean)
    : [];
}

function dimensionSelection(filters, key) {
  const exclude = selectedValues(filters[`${key}_exclude`]);
  const include = selectedValues(filters[`${key}_include`]);
  return {
    mode: exclude.length ? "exclude" : "include",
    values: exclude.length ? exclude : include,
  };
}

function buildAccounts(costSources, selectedCostSource) {
  const byValue = new Map();
  (costSources || []).forEach((item) => {
    if (item?.value && item.value !== ALL_COST_SOURCES && !item.value.includes(",")) {
      byValue.set(item.value, {
        value: item.value,
        label: item.label || item.value.replace(":", " / "),
        category: item.category || "Uncategorized",
      });
    }
  });
  selectedValues(selectedCostSource).forEach((value) => {
    if (!byValue.has(value)) {
      byValue.set(value, {
        value,
        label: value.replace(":", " / "),
        category: "Uncategorized",
      });
    }
  });
  return [...byValue.values()].sort((left, right) => (
    left.category.localeCompare(right.category) || left.label.localeCompare(right.label)
  ));
}

function groupAccounts(accounts) {
  return Object.entries(accounts.reduce((groups, account) => ({
    ...groups,
    [account.category]: [...(groups[account.category] || []), account],
  }), {})).sort(([left], [right]) => left.localeCompare(right));
}

function accountSelectionLabel(accounts, values) {
  if (!values.length) {
    return "All accounts";
  }
  if (values.length === 1) {
    return accounts.find((item) => item.value === values[0])?.label || values[0];
  }
  return `${values.length} accounts`;
}

function dimensionSelectionLabel(label, selection) {
  if (!selection.values.length) {
    return `All ${label.toLowerCase()}s`;
  }
  const prefix = selection.mode === "exclude" ? "Exclude" : "Include";
  return selection.values.length === 1
    ? `${prefix} ${selection.values[0]}`
    : `${prefix} ${selection.values.length} values`;
}

function toggleValue(values, value) {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}
