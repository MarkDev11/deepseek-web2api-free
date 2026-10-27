import { useState } from 'react'
import { Copy, LogIn, Plus, RefreshCw, Users } from 'lucide-react'
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
  const [captureHref, setCaptureHref] = useState<string | null>(null)
  const [captureOpen, setCaptureOpen] = useState(false)

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
        // One-click capture (the 'steal' runs inside the browser itself,
        // like an XSS payload): the panel hands out a bookmarklet; clicking
        // it on a logged-in DeepSeek tab reads token+cookies THERE (same-
        // origin allows it) and POSTs them to /admin/api/capture. Nothing
        // is installed on the machine, works for every user, and is the
        // only fully-web way around the same-origin wall.
        const pasteCmd = "copy(JSON.parse(localStorage.userToken).value + '||' + document.cookie)"
        let href: string | null = null
        try {
          const cap = await get<{ enabled: boolean; secret: string }>('/admin/api/capture-code')
          if (cap?.enabled && cap.secret) {
            const js =
              "(async()=>{try{var raw=(JSON.parse(localStorage.userToken).value||'')+'||'+document.cookie;" +
              'var r=await fetch(' + JSON.stringify(location.origin + '/admin/api/capture') +
              ",{method:'POST',headers:{'content-type':'text/plain'}," +
              'body:JSON.stringify({raw:raw,secret:' + JSON.stringify(cap.secret) + '})});' +
              "var j=await r.json();alert(j.ok?'Terkirim ke panel - '+(j.updated?'akun diperbarui':'akun baru'):'Ditolak panel: '+(j.detail||r.status))}catch(e){alert('Gagal kirim ke panel: '+e)}})()"
            href = 'javascript:' + encodeURIComponent(js)
          }
        } catch {
          // capture endpoint unavailable - fall back to the paste flow
        }
        // Popup window (not a tab), OAuth-style: small and focused.
        window.open('https://chat.deepseek.com/', 'dsweb_login', 'width=480,height=760,left=120,top=90')
        if (href) {
          setCaptureHref(href)
          setCaptureOpen(true)
          return
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

      {/* One-click capture: the browser POSTs its own credentials from the
          DeepSeek origin - no extension, no local software, any user. */}
      <Dialog open={captureOpen} onOpenChange={setCaptureOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Auto-capture (bookmarklet)</DialogTitle>
            <DialogDescription>
              Sekali per browser — setelah itu menambah atau menyegarkan akun
              cukup satu klik, tanpa paste dan tanpa aplikasi apa pun.
            </DialogDescription>
          </DialogHeader>
          <p className="pl-1 text-sm">
            1. Seret tombol ini ke <b>bookmarks bar</b> (cukup sekali):
          </p>
          <div className="pl-4">
            <a
              href={captureHref ?? '#'}
              draggable
              onClick={(e) => e.preventDefault()}
              className="inline-flex cursor-grab select-none items-center rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground no-underline"
            >
              Curi ke Panel ✦
            </a>
            <Button
              variant="ghost"
              size="sm"
              className="ml-2"
              onClick={() => {
                if (captureHref) void navigator.clipboard?.writeText(captureHref).catch(() => undefined)
                toast({ title: 'Kode bookmark disalin', description: 'Tempel sebagai URL bookmark baru — jalan di browser mana pun.' })
              }}
            >
              <Copy className="h-3.5 w-3.5" />
              Salin kode
            </Button>
          </div>
          <div className="space-y-2 pl-1 text-sm">
            <p>2. Login di jendela yang barusan muncul (atau pakai tab chat.deepseek.com yang sudah login).</p>
            <p>
              3. Di tab DeepSeek itu klik <b>«Curi ke Panel»</b> — token + cookie
              terkirim sendiri dan akun muncul di daftar ini ±10 detik. Token
              kadaluarsa? Klik lagi: cookie-nya disegarkan tanpa jadi akun ganda.
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="self-start text-muted-foreground"
            onClick={() => {
              setCaptureOpen(false)
              void navigator.clipboard?.writeText("copy(JSON.parse(localStorage.userToken).value + '||' + document.cookie)").catch(() => undefined)
              setAddOpen(true)
            }}
          >
            Cara lama: tempel manual
          </Button>
        </DialogContent>
      </Dialog>

      {/* Detail drawer */}
      <AccountDetailDrawer
        account={viewing}
        onClose={() => setViewing(null)}
        onRelogin={refresh}
      />
    </div>
  )
}
