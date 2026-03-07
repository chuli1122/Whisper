import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

// Telegram menu button replaces the URL hash with #tgWebAppData=...
// This breaks HashRouter. Restore proper route before React mounts.
if (window.location.hash && !window.location.hash.startsWith("#/")) {
  history.replaceState(null, "", window.location.pathname + "#/cot");
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
