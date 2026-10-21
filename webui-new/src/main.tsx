import React from 'react'
import ReactDOM from 'react-dom/client'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/toaster'
import { App } from './App'
import './styles/globals.css'

/**
 * Last-resort overlay: if the bundle throws before React mounts (bad chunk,
 * syntax error in a cached asset, extension interference) the document would
 * otherwise stay blank. Plain DOM + textContent, so no CSP/inline-script
 * dependency.
 */
function showFatal(reason: unknown) {
  const root = document.getElementById('root')
  if (!root || root.dataset.mounted === '1') return
  const message =
    reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason)
  const box = document.createElement('div')
  box.setAttribute(
    'style',
    'max-width:640px;margin:12vh auto;padding:24px;border:1px solid rgba(127,127,127,.35);border-radius:14px;font:14px/1.5 system-ui,sans-serif',
  )
  const title = document.createElement('div')
  title.textContent = 'The admin panel failed to start'
  title.setAttribute('style', 'font-weight:600;margin-bottom:8px')
  const detail = document.createElement('pre')
  detail.textContent = message
  detail.setAttribute('style', 'white-space:pre-wrap;font-size:12px;opacity:.8;margin:0 0 12px')
  const hint = document.createElement('div')
  hint.textContent =
    'A stale cached build is the usual cause: reload with Ctrl+Shift+R (hard refresh).'
  hint.setAttribute('style', 'font-size:12px;opacity:.6;margin-bottom:12px')
  const link = document.createElement('a')
  link.href = '/webui/'
  link.textContent = 'Reload'
  box.append(title, detail, hint, link)
  root.replaceChildren(box)
}

window.addEventListener('error', (e) => showFatal(e.error ?? e.message))
window.addEventListener('unhandledrejection', (e) => showFatal(e.reason))

// Apply the persisted theme as early as possible so we don't flash a
// light UI for users who prefer dark.
import { useThemeStore, applyTheme } from './stores/theme'
applyTheme(useThemeStore.getState().theme)

const rootEl = document.getElementById('root')!
ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <TooltipProvider delayDuration={200}>
      <App />
      <Toaster />
    </TooltipProvider>
  </React.StrictMode>,
)

// Mark the mount only once React has actually committed, so showFatal()
// keeps its hands off the DOM after a successful boot.
requestAnimationFrame(() => {
  rootEl.dataset.mounted = '1'
})
