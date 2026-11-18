import { defineConfig, loadEnv, searchForWorkspaceRoot } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import yaml from '@modyfi/vite-plugin-yaml'

export default defineConfig(({ mode }) => {
  // In development the app calls /api/ and Vite forwards it to the deployed API,
  // since the API's CORS settings only allow the deployed sites
  const apiEndpoint = loadEnv(mode, process.cwd(), '').VITE_API_ENDPOINT
  const api = apiEndpoint ? new URL(apiEndpoint) : null

  return {
    plugins: [react(), tailwindcss(), yaml()],
    // 5173 is the chat frontend; this port is registered as a Cognito callback in dev mode
    server: {
      port: 5174,
      strictPort: true,
      // The URL listing is shared with the backend and lives outside this app
      fs: { allow: [searchForWorkspaceRoot(process.cwd()), '../shared/source_urls.yaml'] },
      proxy: api
        ? {
            '/api/': {
              target: api.origin,
              changeOrigin: true,
              secure: true,
              rewrite: (path) => path.replace(/^\/api\//, api.pathname.replace(/\/?$/, '/')),
            },
          }
        : undefined,
    },
  }
})
