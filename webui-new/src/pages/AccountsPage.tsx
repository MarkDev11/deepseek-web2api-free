import { useEffect, useRef, useState } from 'react'
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
  const [captureOpen, setCaptureOpen] = useState(false)
  const [captureHref, setCaptureHref] = useState<string | null>(null)
  const pasteCmd = "copy(JSON.parse(localStorage.userToken).value + '||' + document.cookie)"
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
        // The zero-click auto-attach was not a coding bug: measured in
        // Chrome (headless test, 2026-10-27) assigning popup.location.href =
        // 'javascript:...' into ANOTHER site's window throws SecurityError -
        // the spec forbids cross-origin javascript: navigation, so Chrome
        // silently never ran it and the popup just sat there. What still
        // works cross-origin: close(), closed, and the backend poll. So the
        // panel still TRIES the injection every 2s (Firefox-family may
        // honour it = full zero-click), but the guaranteed path is the
        // one-click bookmark this dialog offers: bookmarklets are
        // user-initiated and DO run inside DeepSeek's page, they POST
        // token+cookies to /admin/api/capture with a one-time tag and close
        // their own tab; the panel notices via /capture-events?tag=...,
        // refreshes the list and closes the login popup (allowed).
        // Popup window (not a tab), OAuth-style: small and focused.
        const w = window.open('https://chat.deepseek.com/', 'dsweb_login', 'width=480,height=760,left=120,top=90')
        try {
          const cap = await get<{ enabled: boolean; secret: string }>('/admin/api/capture-code')
          if (w && cap?.enabled && cap.secret) {
            const tag = Math.random().toString(36).slice(2, 10)
            const captureUrl = JSON.stringify(location.origin + '/admin/api/capture')
            const secretAndTag = JSON.stringify(cap.secret) + ',tag:' + JSON.stringify(tag)
            // Silent variant for the automatic cross-origin attempts.
            const autoJs =
              "(function(){try{if(window.__dswSent)return;var t=localStorage.getItem('userToken');if(!t)return;window.__dswSent=1;fetch(" +
              captureUrl +
              ",{method:'POST',headers:{'content-type':'text/plain'},body:JSON.stringify({raw:t+'||'+document.cookie,secret:" +
              secretAndTag +
              "})}).then(function(r){return r.json()}).then(function(j){if(j.ok)setTimeout(function(){window.close()},600)}).catch(function(){window.__dswSent=0})}catch(e){void 0}})()"
            // Loud variant carried by the bookmark (user click = feedback).
            const clickJs =
              "(async()=>{try{var t=localStorage.getItem('userToken');" +
              "if(!t){alert('Login dulu di halaman ini, lalu klik bookmark ini lagi.');return}" +
              "var r=await fetch(" + captureUrl +
              ",{method:'POST',headers:{'content-type':'text/plain'},body:JSON.stringify({raw:t+'||'+document.cookie,secret:" +
              secretAndTag +
              "})});var j=await r.json();" +
              "if(j.ok){alert('Terkirim ke panel - '+(j.updated?'akun diperbarui':'akun baru'));window.close()}" +
              "else{alert('Ditolak panel: '+(j.detail||r.status))}}catch(e){alert('Gagal kirim ke panel: '+e)}})()"
            // Console flow: the clipboard carries the RAW js (no
            // javascript: prefix, no percent-encoding). The address-bar
            // variant proved unreliable in the field: Chrome strips a
            // pasted "javascript:" scheme and the snippet became a Bing
            // search query (user screenshot 2026-10-27). The DevTools
            // Console runs exactly this text with zero scheme games.
            setCaptureHref(clickJs)
            setCaptureOpen(true)
            void navigator.clipboard?.writeText(clickJs).catch(() => undefined)
            const stop = () => {
              if (autoTimer.current !== null) window.clearInterval(autoTimer.current)
              autoTimer.current = null
            }
            if (autoTimer.current !== null) window.clearInterval(autoTimer.current)
            let ticks = 0
            autoTimer.current = window.setInterval(async () => {
              ticks += 1
              try {
                if (!w.closed) w.location.href = 'javascript:' + encodeURIComponent(autoJs)
              } catch {
                // Chrome refuses this (see note above); the bookmark click
                // is the guaranteed delivery path there.
              }
              try {
                const ev = await get<{ seen: boolean }>('/admin/api/capture-events?tag=' + tag)
                if (ev.seen) {
                  stop()
                  setCaptureOpen(false)
                  try { if (!w.closed) w.close() } catch { /* popup already gone */ }
                  toast({ title: 'Akun tertangkap ke panel', description: 'Daftar akun akan termutakhirkan otomatis.' })
                  refresh()
                  return
                }
              } catch {
                // transient network blip; keep polling
              }
              if (ticks >= 150) {
                // 5 min without a delivery: keep the dialog open so the
                // paste fallback stays reachable, but stop polling.
                stop()
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

      {/* One-paste capture: the raw JS is auto-copied; run it in the
          DeepSeek window's DevTools Console - no installs, no bookmarks,
          and unlike the address bar it cannot turn into a search. */}
      <Dialog open={captureOpen} onOpenChange={setCaptureOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Tangkap akun DeepSeek</DialogTitle>
            <DialogDescription>
              Browser melarang panel membaca data jendela DeepSeek, jadi
              jalankan kodenya langsung di halaman itu lewat Console:
            </DialogDescription>
          </DialogHeader>
          <ol className="list-decimal space-y-1.5 pl-5 text-sm">
            <li>Login dulu di jendela DeepSeek yang terbuka tadi.</li>
            <li>
              Di jendela itu tekan <b>F12</b>, buka tab <b>Console</b>. Kalau
              Chrome meminta izin, ketik <b>allow pasting</b> lalu Enter
              (hanya sekali per sesi).
            </li>
            <li>
              Tekan <b>Ctrl+V</b> lalu <b>Enter</b> - kodenya sudah otomatis
              tersalin dari panel saat jendela terbuka.
            </li>
            <li>Jendela menutup sendiri dan akun muncul di daftar ini.</li>
          </ol>
          <p className="text-xs text-muted-foreground">
            Paste di Console, jangan di address bar - Chrome membuang awalan
            javascript: saat paste di sana, jadi kodenya malah dianggap
            kata kunci pencarian (Bing).
          </p>
          <div className="space-y-2">
            <Button
              variant="outline"
              size="sm"
              className="w-full"
              onClick={() => {
                void navigator.clipboard?.writeText(captureHref ?? "").catch(() => undefined)
                toast({ title: "Kode disalin ulang", description: "Tempel di Console jendela DeepSeek (F12) lalu tekan Enter." })
              }}
            >
              <Copy className="mr-1 h-4 w-4" />
              Salin ulang kode
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="w-full justify-start px-0 text-xs text-muted-foreground"
              onClick={() => {
                setCaptureOpen(false)
                void navigator.clipboard?.writeText(pasteCmd).catch(() => undefined)
                setAddOpen(true)
                toast({
                  title: "Cara lama siap",
                  description: "Perintah console tersalin: jalankan di tab DeepSeek (F12 lalu Console, Ctrl+V + Enter), kemudian tempel hasilnya di kotak Quick import.",
                })
              }}
            >
              Cara lama: tempel manual
            </Button>
          </div>
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
