import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // 5173 is the chat frontend; this port is registered as a Cognito callback in dev mode
  server: { port: 5174, strictPort: true },
})
