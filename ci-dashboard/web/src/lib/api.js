import { useEffect, useState } from "react";

import { ALL_COST_SOURCES, COST_PATH } from "./filterUrl.js";

function normalizePrefix(value) {
  if (!value || value === "/") {
    return "";
  }
  const trimmed = value.trim().replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}` : "";
}

const viteEnv = import.meta.env || {};
const API_BASE = normalizePrefix(viteEnv.VITE_API_BASE_URL || viteEnv.BASE_URL);
export const COST_DATA_LAG_DAYS = 4;

export function getDefaultDateRange() {
  const end = new Date();
  const start = startOfMondayWeek(offsetMonth(end, -1));
  return {
    start_date: toDateInputValue(start),
    end_date: toDateInputValue(end),
  };
}

function offsetMonth(value, deltaMonths) {
  const next = new Date(value);
  next.setMonth(next.getMonth() + deltaMonths);
  return next;
}

function startOfMondayWeek(value) {
  const start = new Date(value);
  const day = start.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  start.setDate(start.getDate() + diff);
  return start;
}

export function getPreviousDateRange(startDate, endDate) {
  if (!startDate || !endDate) {
    return getDefaultDateRange();
  }
  const start = new Date(`${startDate}T00:00:00`);
  const end = new Date(`${endDate}T00:00:00`);
  const spanDays = Math.max(
    Math.round((end.getTime() - start.getTime()) / 86400000) + 1,
    1,
  );
  const previousEnd = new Date(start);
  previousEnd.setDate(previousEnd.getDate() - 1);
  const previousStart = new Date(previousEnd);
  previousStart.setDate(previousStart.getDate() - spanDays + 1);
  return {
    start_date: toDateInputValue(previousStart),
    end_date: toDateInputValue(previousEnd),
  };
}

export function getPreviousCompleteMondayWeek(referenceDate = new Date()) {
  const end = new Date(referenceDate);
  end.setHours(0, 0, 0, 0);
  const daysSincePreviousSunday = end.getDay() || 7;
  end.setDate(end.getDate() - daysSincePreviousSunday);
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  return {
    start_date: toDateInputValue(start),
    end_date: toDateInputValue(end),
  };
}

export function getStableCostSummaryWeek(referenceDate = new Date()) {
  const previousWeek = getPreviousCompleteMondayWeek(referenceDate);
  const day = referenceDate.getDay();
  const mondayBasedDay = day === 0 ? 7 : day;
  if (mondayBasedDay >= 4) {
    return previousWeek;
  }
  return getPreviousDateRange(previousWeek.start_date, previousWeek.end_date);
}

export function getPreviousCompleteSaturdayWeek(referenceDate = new Date()) {
  const end = new Date(referenceDate);
  end.setHours(0, 0, 0, 0);
  const daysSinceFriday = (end.getDay() - 5 + 7) % 7 || 7;
  end.setDate(end.getDate() - daysSinceFriday);
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  return {
    start_date: toDateInputValue(start),
    end_date: toDateInputValue(end),
  };
}

export function getLaggedTrailingDateRange(
  referenceDate = new Date(),
  windowDays = 7,
  lagDays = COST_DATA_LAG_DAYS,
) {
  const end = new Date(referenceDate);
  end.setHours(0, 0, 0, 0);
  end.setDate(end.getDate() - lagDays);
  const start = new Date(end);
  start.setDate(start.getDate() - windowDays + 1);
  return {
    start_date: toDateInputValue(start),
    end_date: toDateInputValue(end),
  };
}

export function toDateInputValue(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function buildQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (Array.isArray(value)) {
      if (!value.length) {
        return;
      }
      search.set(key, value.join(","));
      return;
    }
    if (value === undefined || value === null || value === "") {
      return;
    }
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : "";
}

export async function fetchJson(path, params = {}, signal) {
  const response = await fetch(`${API_BASE}${path}${buildQuery(params)}`, {
    headers: {
      Accept: "application/json",
    },
    signal,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

export function useApiData(path, params = {}, enabled = true) {
  const [state, setState] = useState({
    data: null,
    loading: enabled,
    error: null,
  });

  useEffect(() => {
    if (!enabled) {
      setState({
        data: null,
        loading: false,
        error: null,
      });
      return undefined;
    }

    const controller = new AbortController();
    setState((current) => ({
      data: current.data,
      loading: true,
      error: null,
    }));

    fetchJson(path, params, controller.signal)
      .then((data) => {
        setState({
          data,
          loading: false,
          error: null,
        });
      })
      .catch((error) => {
        if (controller.signal.aborted) {
          return;
        }
        setState({
          data: null,
          loading: false,
          error: error instanceof Error ? error.message : "Unknown error",
        });
      });

    return () => controller.abort();
  }, [enabled, path, JSON.stringify(params)]);

  return state;
}

export function sumSeriesPoints(series, key) {
  const target = series?.find((item) => item.key === key);
  if (!target) {
    return 0;
  }
  return target.points.reduce((total, point) => total + Number(point[1] || 0), 0);
}

export function averageSeriesPoints(series, key) {
  const target = series?.find((item) => item.key === key);
  if (!target || !target.points.length) {
    return 0;
  }
  const total = target.points.reduce((sum, point) => sum + Number(point[1] || 0), 0);
  return total / target.points.length;
}

export function findGroup(groups, name) {
  return groups?.find((group) => group.name === name)?.values || null;
}

export function getLatestBranchValue(rows, branch) {
  const row = (rows || []).find((item) => item.branch === branch);
  return Number((row?.values || []).at(-1) || 0);
}

export function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value || 0);
}

export function formatCompact(value) {
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value || 0);
}

export function formatCurrency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: Math.abs(Number(value || 0)) >= 100000 ? "compact" : "standard",
    maximumFractionDigits: Math.abs(Number(value || 0)) >= 1000 ? 0 : 2,
  }).format(value || 0);
}

export function formatCompactCurrency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 0,
  }).format(value || 0);
}

export function formatRoundedThousands(value) {
  const numeric = Number(value || 0);
  if (numeric === 0) {
    return "0";
  }
  if (Math.abs(numeric) >= 1000) {
    return `${Math.round(numeric / 1000)}K`;
  }
  return formatNumber(numeric);
}

export function formatPercent(value) {
  return `${Number(value || 0).toFixed(1)}%`;
}

export function formatDateRangeLabel(start, end) {
  if (!start || !end) {
    return "Date range unavailable";
  }

  const startDate = parseIsoDate(start);
  const endDate = parseIsoDate(end);
  if (!startDate || !endDate) {
    return `${start} - ${end}`;
  }

  const shortFormatter = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  const longFormatter = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });

  if (startDate.getUTCFullYear() === endDate.getUTCFullYear()) {
    return `${shortFormatter.format(startDate)} - ${longFormatter.format(endDate)}`;
  }
  return `${longFormatter.format(startDate)} - ${longFormatter.format(endDate)}`;
}

export function formatSeconds(value) {
  const seconds = Number(value || 0);
  if (seconds >= 3600) {
    return `${(seconds / 3600).toFixed(1)}h`;
  }
  if (seconds >= 60) {
    return `${Math.round(seconds / 60)}m`;
  }
  return `${Math.round(seconds)}s`;
}

export function formatBrowserDateTime(value) {
  if (!value) {
    return "";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(parsed);
}

export function formatCostSourceLabel(value) {
  if (!value || value === ALL_COST_SOURCES) {
    return "All sources";
  }
  const [vendor, accountId] = String(value || "").split(":");
  if (!vendor || !accountId) {
    return "Selected source";
  }
  return `${vendor} / ${accountId}`;
}

export function buildCostSourceOptions(items, selectedCostSource) {
  const options = [
    { value: ALL_COST_SOURCES, label: "All sources" },
    ...(items || []),
  ];
  if (
    selectedCostSource
    && selectedCostSource !== ALL_COST_SOURCES
    && !options.some((item) => item.value === selectedCostSource)
  ) {
    options.splice(1, 0, {
      value: selectedCostSource,
      label: formatCostSourceLabel(selectedCostSource),
    });
  }
  return options;
}

export function formatDelta(current, previous, suffix = "") {
  const delta = Number(current || 0) - Number(previous || 0);
  const sign = delta > 0 ? "+" : "";
  const value = Math.abs(delta) >= 100 ? Math.round(delta) : delta.toFixed(1);
  return `${sign}${value}${suffix}`;
}

function parseIsoDate(value) {
  if (!value) {
    return null;
  }
  const [year, month, day] = String(value).split("-").map(Number);
  if (!year || !month || !day) {
    return null;
  }
  return new Date(Date.UTC(year, month - 1, day, 12, 0, 0));
}

export function buildScopeLabel(filters, pathname, costSourceLabel = "") {
  if (pathname === COST_PATH) {
    return [
      costSourceLabel || formatCostSourceLabel(filters.cost_source),
      `${filters.start_date} to ${filters.end_date}`,
    ].join(" · ");
  }

  const parts = [];
  if (filters.repo) {
    parts.push(filters.repo);
  }
  if (filters.branch) {
    parts.push(filters.branch);
  }
  if (filters.job_name) {
    parts.push(filters.job_name);
  }
  if (filters.cloud_phase) {
    parts.push(filters.cloud_phase);
  }
  if (filters.issue_status) {
    parts.push(`${filters.issue_status} issues`);
  }
  if (!parts.length) {
    parts.push("All repos");
  }
  parts.push(`${filters.start_date} to ${filters.end_date}`);
  return parts.join(" · ");
}
