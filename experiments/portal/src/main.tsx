import { CssBaseline, ThemeProvider, createTheme } from "@mui/material";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

const theme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#cf2e2e", dark: "#a32121", contrastText: "#fff" },
    background: { default: "#f7f7f5", paper: "#ffffff" },
    text: { primary: "#1f2529", secondary: "#687077" },
    divider: "#e2e4e5",
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: 'Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    h4: { fontWeight: 760, letterSpacing: "-0.035em" },
    h6: { fontWeight: 700 },
    button: { fontWeight: 680, textTransform: "none" },
  },
  components: {
    MuiButton: { defaultProps: { disableElevation: true } },
    MuiCard: { styleOverrides: { root: { backgroundImage: "none" } } },
    MuiTableCell: { styleOverrides: { head: { background: "#fafafa", color: "#687077", fontSize: 12, fontWeight: 750, letterSpacing: ".03em", textTransform: "uppercase" } } },
    MuiListItemButton: { styleOverrides: { root: { borderRadius: 8, marginBottom: 3, "&.Mui-selected": { backgroundColor: "#f9eaea", color: "#a32121" } } } },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><ThemeProvider theme={theme}><CssBaseline /><App /></ThemeProvider></React.StrictMode>,
);
