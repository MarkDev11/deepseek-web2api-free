import { useState } from 'react'
import { LogIn, Plus, RefreshCw, Users } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { useApi } from '@/hooks/useApi'
import { del, post, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import { AccountForm } from '@/components/accounts/AccountForm'
import { AccountTable } from '@/components/accounts/AccountTable'
import { AccountDetailDrawer } from '@/components/accounts/AccountDetailDrawer'
import type { Account, AccountsResponse } from '@/lib/types'

export default function AccountsPage() {
  const { data, error, loading, refresh } = useApi<AccountsResponse>('/admin/api/accounts', {
    pollMs: 10000,
  })
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<Account | null>(null)
  const [viewing, setViewing] = useState<Account | null>(null)
  const [deleting, setDeleting] = useState<Account | null>(null)
  const [browserBusy, setBrowserBusy] = useState(false)

  const accounts = data?.accounts ?? []

  const handleDelete = async () => {
    if (!deleting) return
    try {
      await del(`/admin/api/accounts/${encodeURIComponent(deleting.id)}`)
      toast({ title: 'Account deleted', variant: 'success' })
      setDeleting(null)
      refresh()
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: 'Delete failed', description: msg, variant: 'destructive' })
    }
  }

  // OAuth-like capture: the server opens a real browser window on
  // chat.deepseek.com (same popup flow as the dsh-plugin) and grabs the
  // token + cookies from the live session. Local server runs only - on
  // Vercel the endpoint answers 400 with a Quick-import hint.
  const handleBrowserLogin = async () => {
    setBrowserBusy(true)
    toast({
      title: 'Browser login',
      description: 'Sign in in the small DeepSeek window that opens on this machine - the credentials are captured automatically. Keep this page open until the window closes.',
    })
    try {
      const res = await post<{ ok: boolean; account: Account }>(
        '/admin/api/accounts/browser-login',
        {},
        { signal: AbortSignal.timeout(310_000) },
      )
      toast({
        title: 'Account added',
        description: res.account.email || res.account.id,
        variant: 'success',
      })
      refresh()
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      // Hosted (Vercel) has no browser on the server. A popup tab IS still
      // possible here - it just cannot be read automatically (same-origin:
      // chat.deepseek.com localStorage/cookies are invisible to this app,
      // and ds_session_id is HttpOnly). So guide the one-paste flow: open
      // the tab, pre-copy the console command, open Quick import.
      if (e instanceof ApiCallError && e.status === 400) {
        // Zero-paste path: the silent local helper (hidden wscript task, autostarts
        // at logon, no window) opens the OAuth popup HERE and captures token+cookies
        // via CDP, then we add the account to the hosted pool ourselves.
        try {
          const res = await fetch('http://127.0.0.1:55343/login', { method: 'POST', signal: AbortSignal.timeout(610_000) })
          if (res.status === 409) {
            toast({ title: 'Login already in progress', description: 'A small DeepSeek window is already open - finish the login there.' })
            return
          }
          const cap = (await res.json()) as { ok?: boolean; account?: { token?: string; cookies?: string; email?: string; id?: string } }
          if (cap?.ok && cap.account?.token) {
            await post<{ account: Account }>('/admin/api/accounts', {
              raw: cap.account.token + '||' + (cap.account.cookies || ''),
              email: cap.account.email || undefined,
            })
            toast({ title: 'Account added', description: cap.account.email || cap.account.id || 'Captured from the login window', variant: 'success' })
            refresh()
            return
          }
          console.warn('dsweb login helper capture failed:', cap)
        } catch (he) {
          console.warn('dsweb login helper unavailable:', he)
        }
        // Fallback (no helper on this machine): small OAuth-style popup window + one paste.
        window.open(
          'https://chat.deepseek.com/',
          'dsweb_login',
          'width=480,height=760,left=120,top=90',
        )
        void navigator.clipboard
          ?.writeText("copy(JSON.parse(localStorage.userToken).value + '||' + document.cookie)")
          .catch(() => undefined)
        setAddOpen(true)
        toast({
          title: 'Opened chat.deepseek.com',
          description: 'Log in there, run the copied console command (DevTools > Console, Ctrl+V + Enter), then paste the blob into the Quick import box - the console command is already on your clipboard.',
        })
        return
      }
      toast({ title: 'Browser login failed', description: msg, variant: 'destructive' })
    } finally {
      setBrowserBusy(false)
    }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Account pool"
        description={`Manage DeepSeek accounts · ${data?.total ?? '—'} total`}
        actions={
          <>
            <Button variant="outline" onClick={refresh} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </Button>
            <Button
              variant="outline"
              onClick={handleBrowserLogin}
              disabled={browserBusy}
              title="Opens chat.deepseek.com in a browser window and captures token + cookies automatically - same flow as the DSH plugin. Local server runs only."
            >
              {browserBusy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
              {browserBusy ? 'Waiting for login…' : 'Browser login'}
            </Button>
            <Button onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" />
              Add account
            </Button>
          </>
        }
      />

      <Card>
        <CardContent className="p-0">
          {loading && !data ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : error ? (
            <div className="p-8 text-center text-sm text-destructive">
              Load failed: {error.message}
            </div>
          ) : accounts.length === 0 ? (
            <div className="p-12 text-center">
              <Users className="mx-auto h-10 w-10 text-muted-foreground/40" />
              <h3 className="mt-3 text-sm font-medium">No accounts</h3>
              <p className="mt-1 text-xs text-muted-foreground">
                Click "Browser login" to sign in once (the token is captured automatically), "Add account" to paste credentials, or write accounts to data/accounts.json.
              </p>
            </div>
          ) : (
            <AccountTable
              accounts={accounts}
              onEdit={(a) => setEditing(a)}
              onDelete={(a) => setDeleting(a)}
              onSelect={(a) => setViewing(a)}
              onRefreshed={refresh}
            />
          )}
        </CardContent>
      </Card>

      {/* Add dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add account</DialogTitle>
            <DialogDescription>Enter the DeepSeek account token and cookies to use it</DialogDescription>
          </DialogHeader>
          <AccountForm
            onSaved={() => {
              setAddOpen(false)
              refresh()
            }}
            onCancel={() => setAddOpen(false)}
          />
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit account</DialogTitle>
            <DialogDescription>
              Leave token / cookies blank to keep the current value
            </DialogDescription>
          </DialogHeader>
          {editing && (
            <AccountForm
              account={editing}
              onSaved={() => {
                setEditing(null)
                refresh()
              }}
              onCancel={() => setEditing(null)}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <AlertDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete account?</AlertDialogTitle>
            <AlertDialogDescription>
              This will delete account <code className="font-mono text-xs">{deleting?.id}</code>.
              This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
              Confirm delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Detail drawer */}
      <AccountDetailDrawer
        account={viewing}
        onClose={() => setViewing(null)}
        onRelogin={refresh}
      />
    </div>
  )
}
