import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./styles.css";

function normalizeBasename(baseUrl) {
  if (!baseUrl || baseUrl === "/") {
    return undefined;
  }
  return baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter basename={normalizeBasename(import.meta.env.BASE_URL)}>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
