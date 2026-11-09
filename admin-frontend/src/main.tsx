import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { authConfig } from './auth/config'

// Without Cognito config, useAuth would redirect to a relative URL in an endless loop
const configured = authConfig.domain && authConfig.clientId && authConfig.redirectUri

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {configured ? (
      <App />
    ) : (
      <p className="p-8 text-sm">
        Missing VITE_COGNITO_* settings. Deploy with scripts/deploy-admin-frontend.sh, or set them in
        admin-frontend/.env for local development.
      </p>
    )}
  </StrictMode>,
)
