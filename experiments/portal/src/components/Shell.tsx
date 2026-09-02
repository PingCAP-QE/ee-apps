import AppsRounded from "@mui/icons-material/AppsRounded";
import ConstructionRounded from "@mui/icons-material/ConstructionRounded";
import LogoutRounded from "@mui/icons-material/LogoutRounded";
import MenuRounded from "@mui/icons-material/MenuRounded";
import {
  AppBar,
  Box,
  Button,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Toolbar,
  Typography,
  useMediaQuery,
  useTheme,
} from "@mui/material";
import { useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import type { ModuleManifest } from "../types";

const drawerWidth = 248;

function Navigation({ modules, close }: { modules: ModuleManifest[]; close?: () => void }) {
  const location = useLocation();
  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <Box sx={{ px: 2.5, pt: 2.75, pb: 2 }}>
        <Box className="brand-mark">EE</Box>
        <Typography variant="h6" sx={{ mt: 1.5, letterSpacing: "-0.02em" }}>EE Portal</Typography>
        <Typography variant="caption" color="text.secondary">Engineering workspace</Typography>
      </Box>
      <Divider />
      <List sx={{ p: 1.25 }}>
        <ListItemButton component={Link} to="/" selected={location.pathname === "/"} onClick={close}>
          <ListItemIcon><AppsRounded fontSize="small" /></ListItemIcon>
          <ListItemText primary="All tools" />
        </ListItemButton>
        {modules.flatMap((module) => module.spec.navigation.map((item) => {
          const page = module.spec.pages.find((candidate) => candidate.metadata.name === item.page);
          if (!page) return null;
          const route = page.spec.route.replace(/:\w+/g, "");
          return (
            <ListItemButton key={`${module.metadata.name}-${item.page}`} component={Link} to={route} selected={location.pathname.startsWith(route)} onClick={close}>
              <ListItemIcon><ConstructionRounded fontSize="small" /></ListItemIcon>
              <ListItemText primary={item.label} />
            </ListItemButton>
          );
        }))}
      </List>
      <Box sx={{ mt: "auto", p: 1.5 }}>
        <Button color="inherit" size="small" startIcon={<LogoutRounded />} href="/ee/logout">Sign out</Button>
      </Box>
    </Box>
  );
}

export function Shell({ modules, children }: { modules: ModuleManifest[]; children: ReactNode }) {
  const theme = useTheme();
  const mobile = useMediaQuery(theme.breakpoints.down("md"));
  const [open, setOpen] = useState(false);
  return (
    <Box sx={{ minHeight: "100vh", bgcolor: "background.default" }}>
      {mobile && (
        <AppBar position="sticky" elevation={0} color="inherit" sx={{ borderBottom: "1px solid", borderColor: "divider" }}>
          <Toolbar>
            <IconButton edge="start" aria-label="Open navigation" onClick={() => setOpen(true)}><MenuRounded /></IconButton>
            <Typography variant="h6" sx={{ ml: 1 }}>EE Portal</Typography>
          </Toolbar>
        </AppBar>
      )}
      <Drawer
        variant={mobile ? "temporary" : "permanent"}
        open={mobile ? open : true}
        onClose={() => setOpen(false)}
        slotProps={{ paper: { sx: { width: drawerWidth, borderRightColor: "divider", backgroundImage: "none" } } }}
      >
        <Navigation modules={modules} close={() => setOpen(false)} />
      </Drawer>
      <Box component="main" sx={{ ml: mobile ? 0 : `${drawerWidth}px`, minHeight: "100vh" }}>
        {children}
      </Box>
    </Box>
  );
}
