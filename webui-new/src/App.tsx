import { Navigate, createBrowserRouter, RouterProvider, Outlet, useLocation } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { useAuthStore } from '@/stores/auth'
import LoginPage from '@/pages/LoginPage'
import DashboardPage from '@/pages/DashboardPage'
import AccountsPage from '@/pages/AccountsPage'
import PlaygroundPage from '@/pages/PlaygroundPage'
import SettingsPage from '@/pages/SettingsPage'
import NotFoundPage from '@/pages/NotFoundPage'

/** Wrapper that redirects to /login if there's no token. */
function RequireAuth() {
  const token = useAuthStore((s) => s.token)
  const location = useLocation()
  if (!token) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}

/**
 * Keeps AppShell (nav + sidebar) mounted when a page throws, and resets the
 * boundary on navigation so a broken screen does not stick.
 */
function PageBoundary() {
  const location = useLocation()
  return (
    <ErrorBoundary key={location.pathname} context={location.pathname}>
      <Outlet />
    </ErrorBoundary>
  )
}

// basename: '/webui' — the SPA is served under /webui/* by the backend,
// so client-side navigation keeps the prefix and deep links refresh cleanly.
const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    element: <RequireAuth />,
    children: [
      {
        element: <AppShell />,
        children: [
          {
            element: <PageBoundary />,
            children: [
              { index: true, element: <DashboardPage /> },
              { path: 'accounts', element: <AccountsPage /> },
              { path: 'accounts/:id', element: <AccountsPage /> },
              { path: 'playground', element: <PlaygroundPage /> },
              { path: 'settings', element: <SettingsPage /> },
              { path: '*', element: <NotFoundPage /> },
            ],
          },
        ],
      },
    ],
  },
], { basename: '/webui' })

export function App() {
  return <RouterProvider router={router} />
}
