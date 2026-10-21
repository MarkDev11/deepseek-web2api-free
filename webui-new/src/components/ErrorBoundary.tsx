import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'

interface Props {
  children: ReactNode
  /** Route path, only used for the hint line. */
  context?: string
}

interface State {
  error: Error | null
}

/**
 * Catches render-time errors in a single page so a throw in one screen
 * cannot blank the whole panel (React 18 unmounts the tree on an
 * uncaught render error, which shows as a pure white page).
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Kept in the browser console for the operator to copy out.
    console.error('panel_render_error', this.props.context ?? '', error, info.componentStack)
  }

  private retry = () => this.setState({ error: null })

  private reload = () => window.location.reload()

  private signOut = () => {
    try {
      localStorage.removeItem('ds2api-auth')
    } catch {
      /* private mode — nothing to clear */
    }
    window.location.assign('/webui/login')
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="flex min-h-[60vh] items-center justify-center p-4">
        <div className="w-full max-w-2xl space-y-4 rounded-xl border bg-card p-6 shadow-sm">
          <div className="space-y-1">
            <h2 className="text-base font-semibold">This screen failed to render</h2>
            <p className="text-sm text-muted-foreground">
              The rest of the panel is still fine — this is a bug on
              {this.props.context ? ` ${this.props.context} ` : ' this page '}
              only, not a broken login. Reloading usually clears it.
            </p>
          </div>
          <pre className="max-h-48 overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap">
{error.name}: {error.message}
            {error.stack ? `\n\n${error.stack}` : ''}
          </pre>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={this.retry}>
              Try again
            </Button>
            <Button size="sm" variant="outline" onClick={this.reload}>
              Reload page
            </Button>
            <Button size="sm" variant="ghost" onClick={this.signOut}>
              Sign out &amp; clear session
            </Button>
          </div>
        </div>
      </div>
    )
  }
}
