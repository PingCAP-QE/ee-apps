import ContentCopyRounded from "@mui/icons-material/ContentCopyRounded";
import OpenInNewRounded from "@mui/icons-material/OpenInNewRounded";
import { Box, Chip, IconButton, Link, Tooltip, Typography } from "@mui/material";
import type { DisplayField } from "../types";
import { formatValue } from "../lib/runtime";
import { getPointer } from "../lib/pointer";

const statusColors = {
  neutral: { color: "#48515a", background: "#edf0f2" },
  info: { color: "#175cd3", background: "#eaf2ff" },
  success: { color: "#067647", background: "#e8f7ef" },
  warning: { color: "#9a6700", background: "#fff4d6" },
  danger: { color: "#b42318", background: "#feeceb" },
};

export function FieldValue({ field, data, compact = false }: { field: DisplayField; data: unknown; compact?: boolean }) {
  const value = getPointer(data, field.pointer);
  if (field.visibleWhen && getPointer(data, field.visibleWhen.pointer) !== field.visibleWhen.equals) return null;
  if (field.hideWhenEmpty && (value === undefined || value === null || value === "")) return null;
  const text = formatValue(value, field.format);

  if (field.format === "status") {
    const tone = field.statusMap?.[String(value)] || "neutral";
    return <Chip size="small" label={text} sx={{ fontWeight: 700, ...statusColors[tone] }} />;
  }
  if (field.format === "link" && typeof value === "string" && /^https?:\/\//.test(value)) {
    return (
      <Link href={value} target="_blank" rel="noreferrer" aria-label={`${field.label}: ${value}`} sx={{ display: "inline-flex", gap: 0.5, alignItems: "center" }}>
        Open <OpenInNewRounded sx={{ fontSize: 15 }} />
      </Link>
    );
  }
  if (field.format === "copy") {
    return (
      <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, minWidth: 0 }}>
        <Typography component="span" variant="body2" className="truncate" sx={{ fontFamily: "var(--font-mono)" }}>
          {text}
        </Typography>
        {text !== "—" && (
          <Tooltip title={`Copy ${field.label}`}>
            <IconButton size="small" aria-label={`Copy ${field.label}`} onClick={() => navigator.clipboard.writeText(text)}>
              <ContentCopyRounded sx={{ fontSize: 15 }} />
            </IconButton>
          </Tooltip>
        )}
      </Box>
    );
  }
  return (
    <Typography
      component="span"
      variant={compact ? "body2" : "body1"}
      className={field.format === "code" ? "code-value" : undefined}
      sx={{ overflowWrap: "anywhere" }}
    >
      {text}
    </Typography>
  );
}
