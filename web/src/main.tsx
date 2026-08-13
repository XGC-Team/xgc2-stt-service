import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { initializeSkin } from '@xgc2/ui-react'
import '@xgc2/ui-react/styles.css'
import App from './App'
import './styles.css'

initializeSkin({ defaultSkin: 'light', storageKey: 'xgc2-stt.skin' })

createRoot(document.getElementById('app')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
