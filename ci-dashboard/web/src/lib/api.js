import { useEffect, useState } from "react";

function normalizePrefix(value) {
  if (!value || value === "/") {
    return "";
  }
  const trimmed = value.trim().replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}` : "";
}

const API_BASE = normalizePrefix(import.meta.env.VITE_API_BASE_URL || import.meta.env.BASE_URL);

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

export function toDateInputValue(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function buildQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
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

export function useApiData(path, params = {}) {
  const [state, setState] = useState({
    data: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
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
  }, [path, JSON.stringify(params)]);

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

export function formatNumber(value) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value || 0);
}

export function formatCompact(value) {
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value || 0);
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

export function buildScopeLabel(filters) {
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
