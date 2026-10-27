import { useEffect, useRef, useState } from 'react'
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
import { del, get, post, ApiCallError } from '@/lib/api'
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
  const autoTimer = useRef<number | null>(null)
  useEffect(() => () => {
    if (autoTimer.current !== null) window.clearInterval(autoTimer.current)
  }, [])

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
        // Auto-capture: the DeepSeek login window opens first, then this
        // panel keeps ATTACHING a tiny script to it - assigning
        // popup.location.href = 'javascript:...' runs the script inside the
        // popup's own origin (chat.deepseek.com), where userToken and the
        // visible cookies are readable. The user only logs in; nothing else
        // to click. The script posts credentials once (with a one-time tag),
        // the popup closes itself, and /capture-events?tag=... tells the
        // panel the delivery happened. Zero installs, zero local software.
        const pasteCmd = "copy(JSON.parse(localStorage.userToken).value + '||' + document.cookie)"
        // Popup window (not a tab), OAuth-style: small and focused.
        const w = window.open('https://chat.deepseek.com/', 'dsweb_login', 'width=480,height=760,left=120,top=90')
        try {
          const cap = await get<{ enabled: boolean; secret: string }>('/admin/api/capture-code')
          if (w && cap?.enabled && cap.secret) {
            const tag = Math.random().toString(36).slice(2, 10)
            const js =
              'if(window.__dsw||window.__dswe){void 0}else{' +
              'var t=localStorage.getItem("userToken");if(!t){void 0}else{' +
              'window.__dsw=1;fetch(' + JSON.stringify(location.origin + '/admin/api/capture') +
              ',{method:"POST",headers:{"content-type":"text/plain"},' +
              'body:JSON.stringify({raw:t+"||"+document.cookie,secret:' + JSON.stringify(cap.secret) +
              ',tag:' + JSON.stringify(tag) + '})})' +
              '.then(function(r){return r.json()}).then(function(j){' +
              'if(j.ok){setTimeout(function(){window.close()},600)}' +
              'else{window.__dswe=1;alert("Ditolak panel: "+(j.detail||"?"))}})' +
              '.catch(function(e){window.__dswe=1;alert("Gagal kirim ke panel: "+e)})}}void 0'
            toast({
              title: 'Auto-capture aktif',
              description: 'Login saja di jendela DeepSeek itu. Begitu berhasil, token + cookie terkirim sendiri ke panel dan jendela menutup otomatis - tanpa klik apa pun.',
            })
            const stop = () => {
              if (autoTimer.current !== null) window.clearInterval(autoTimer.current)
              autoTimer.current = null
            }
            if (autoTimer.current !== null) window.clearInterval(autoTimer.current)
            let ticks = 0
            autoTimer.current = window.setInterval(async () => {
              ticks += 1
              try {
                if (!w.closed) w.location.href = 'javascript:' + encodeURIComponent(js)
              } catch {
                // browser guarded the cross-origin navigation; the backend
                // poll below is the delivery signal anyway
              }
              try {
                const ev = await get<{ seen: boolean }>('/admin/api/capture-events?tag=' + tag)
                if (ev.seen) {
                  stop()
                  toast({ title: 'Akun tertangkap ke panel', description: 'Daftar akun akan termutakhirkan otomatis.' })
                  refresh()
                  return
                }
              } catch {
                // transient network blip; keep polling
              }
              if (ticks >= 30) {
                stop()
                void navigator.clipboard?.writeText(pasteCmd).catch(() => undefined)
                setAddOpen(true)
                toast({
                  title: 'Auto-capture tidak terdeteksi',
                  description: 'Situs DeepSeek mungkin memblokir script otomatis. Cara lama: paste blob login (sudah tersalin di clipboard) ke Quick import.',
                })
              }
            }, 2000)
            return
          }
        } catch {
          // capture endpoint unavailable - fall back to the paste flow
        }
        void navigator.clipboard?.writeText(pasteCmd).catch(() => undefined)
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
