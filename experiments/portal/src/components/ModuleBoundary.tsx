import { Alert, Box } from "@mui/material";
import { Component, type ErrorInfo, type ReactNode } from "react";

export class ModuleBoundary extends Component<{ module: string; children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {};
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error(`Module ${this.props.module} failed`, error, info); }
  render() {
    if (this.state.error) return <Box sx={{ p: 4 }}><Alert severity="error">This module could not be rendered: {this.state.error.message}</Alert></Box>;
    return this.props.children;
  }
}
