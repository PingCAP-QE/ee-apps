import ArrowBackRounded from "@mui/icons-material/ArrowBackRounded";
import { Box, Breadcrumbs, Button, Typography } from "@mui/material";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export function PageFrame({ title, description, eyebrow, back, action, children }: {
  title: string;
  description?: string;
  eyebrow?: string;
  back?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Box sx={{ maxWidth: 1440, mx: "auto", px: { xs: 2, sm: 3, lg: 5 }, py: { xs: 2.5, md: 4 } }}>
      {back && <Button component={Link} to={back} color="inherit" startIcon={<ArrowBackRounded />} sx={{ mb: 1.5 }}>Back</Button>}
      {eyebrow && <Breadcrumbs sx={{ mb: 1 }}><Typography variant="caption" color="text.secondary">{eyebrow}</Typography></Breadcrumbs>}
      <Box sx={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 2, mb: 3 }}>
        <Box>
          <Typography variant="h4" component="h1">{title}</Typography>
          {description && <Typography color="text.secondary" sx={{ mt: 0.75, maxWidth: 720 }}>{description}</Typography>}
        </Box>
        {action}
      </Box>
      {children}
    </Box>
  );
}
