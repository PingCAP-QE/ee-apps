export function SegmentedControl({ ariaLabel, options, value, onChange }) {
  return (
    <div className="segmented-control" aria-label={ariaLabel}>
      {options.map((option) => (
        <button
          key={option.key}
          type="button"
          className={buildSegmentedButtonClassName(value === option.key)}
          onClick={() => onChange(option.key)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function buildSegmentedButtonClassName(active) {
  return [
    "segmented-control__button",
    active ? "segmented-control__button--active" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

export function buildDimensionChipClassName(active) {
  return [
    "dimension-chip",
    active ? "dimension-chip--active" : "",
  ]
    .filter(Boolean)
    .join(" ");
}
