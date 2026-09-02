import ArrowForwardRounded from "@mui/icons-material/ArrowForwardRounded";
import SearchRounded from "@mui/icons-material/SearchRounded";
import { Box, Card, CardActionArea, Chip, InputAdornment, Stack, TextField, Typography } from "@mui/material";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { ModuleManifest } from "../types";
import { PageFrame } from "./PageFrame";

export function CatalogPage({ modules }: { modules: ModuleManifest[] }) {
  const [search, setSearch] = useState("");
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return modules;
    return modules.filter((module) => [module.metadata.title, module.metadata.description, ...(module.metadata.tags || [])]
      .filter(Boolean).join(" ").toLowerCase().includes(needle));
  }, [modules, search]);

  return (
    <PageFrame title="Engineering tools" description="Start and track engineering workflows from one place.">
      <TextField
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="Search tools"
        sx={{ width: { xs: "100%", sm: 380 }, mb: 3 }}
        slotProps={{
          htmlInput: { "aria-label": "Search tools" },
          input: { startAdornment: <InputAdornment position="start"><SearchRounded /></InputAdornment> },
        }}
      />
      <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "repeat(2, minmax(0, 1fr))", xl: "repeat(3, minmax(0, 1fr))" }, gap: 2 }}>
        {filtered.map((module) => {
          const firstPage = module.spec.pages.find((page) => page.metadata.name === module.spec.navigation[0]?.page);
          return (
            <Card key={module.metadata.name} variant="outlined" className="tool-card">
              <CardActionArea component={Link} to={firstPage?.spec.route || "/"} sx={{ p: 2.5, height: "100%" }}>
                <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 2 }}>
                  <Box className="tool-icon">{module.metadata.icon || module.metadata.title.slice(0, 2).toUpperCase()}</Box>
                  <ArrowForwardRounded color="action" />
                </Box>
                <Typography variant="h6" sx={{ mt: 2 }}>{module.metadata.title}</Typography>
                <Typography color="text.secondary" sx={{ mt: 0.75, minHeight: 48 }}>{module.metadata.description}</Typography>
                <Stack direction="row" sx={{ mt: 2, flexWrap: "wrap", gap: 0.75 }}>
                  {(module.metadata.tags || []).map((tag) => <Chip size="small" key={tag} label={tag} />)}
                </Stack>
              </CardActionArea>
            </Card>
          );
        })}
      </Box>
      {!filtered.length && <Typography color="text.secondary">No tools match “{search}”.</Typography>}
    </PageFrame>
  );
}
