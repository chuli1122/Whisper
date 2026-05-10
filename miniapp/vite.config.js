import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const API_TARGET = process.env.VITE_API_TARGET || "http://localhost:8002";
const WS_TARGET = process.env.VITE_WS_TARGET || API_TARGET.replace(/^http/, "ws");

// Move module scripts from <head> to end of <body>
// Fixes white screen in Telegram WebView
function moveScriptsToBody() {
  return {
    name: "move-scripts-to-body",
    enforce: "post",
    transformIndexHtml(html) {
      const scripts = [];
      html = html.replace(
        /<script\s+type="module"[^>]*><\/script>/gi,
        (match) => { scripts.push(match); return ""; }
      );
      return html.replace("</body>", scripts.join("\n    ") + "\n  </body>");
    },
  };
}

export default defineConfig({
  plugins: [react(), moveScriptsToBody()],
  base: "/miniapp/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2018",
  },
  server: {
    proxy: {
      "/api": API_TARGET,
      "/ws": {
        target: WS_TARGET,
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
