import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

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
      "/api": "https://chat.chuli.win",
      "/ws": {
        target: "wss://chat.chuli.win",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
