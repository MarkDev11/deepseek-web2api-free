import { useState } from 'react'
import { ClipboardCopy, Loader2, Plus, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { post, put, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import type { Account } from '@/lib/types'

interface Props {
  /** When set, the form is in "edit" mode. */
  account?: Account | null
  onSaved: (saved: Account) => void
  onCancel?: () => void
}

/**
 * Runs in the DevTools console on chat.deepseek.com. It copies the bearer token
 * plus every non-HttpOnly cookie as one blob; the server merges it into the
 * stored account so the HttpOnly ds_session_id survives.
 */
const CONSOLE_SNIPPET =
  "copy(JSON.parse(localStorage.userToken).value + '||' + document.cookie)"

export function AccountForm({ account, onSaved, onCancel }: Props) {
  const isEdit = !!account
  const [email, setEmail] = useState(account?.email ?? '')
  const [token, setToken] = useState('')
  const [cookies, setCookies] = useState('')
  const [raw, setRaw] = useState('')
  const [busy, setBusy] = useState(false)

  const handleCopySnippet = async () => {
    try {
      await navigator.clipboard.writeText(CONSOLE_SNIPPET)
      toast({ title: 'Command copied', description: 'Paste it in the chat.deepseek.com console, then paste the result here.' })
    } catch {
      toast({ title: 'Copy failed', description: 'Copy the command manually from the box above.', variant: 'destructive' })
    }
  }

  const handleSave = async () => {
    if (busy) return
    if (!raw.trim() && !token && !cookies && !isEdit) {
      toast({ title: 'Paste the browser blob, or fill Token and Cookies', variant: 'destructive' })
      return
    }
    setBusy(true)
    try {
      if (isEdit && account) {
        const body: Record<string, string> = { email }
        if (raw.trim()) body.raw = raw.trim()
        else if (token) body.token = token
        if (!raw.trim() && cookies) body.cookies = cookies
        await put(`/admin/api/accounts/${encodeURIComponent(account.id)}`, body)
        toast({ title: 'Account saved', variant: 'success' })
        // Refetch the account to get the fresh data.
        const saved: Account = { ...account, email: email || account.email }
        setRaw('')
        setToken('')
        setCookies('')
        onSaved(saved)
      } else {
        const payload: Record<string, string> = { email: email || '' }
        if (raw.trim()) payload.raw = raw.trim()
        else {
          payload.token = token
          payload.cookies = cookies
        }
        const res = await post<{ ok: boolean; account: Account }>('/admin/api/accounts', payload)
        toast({ title: 'Account added successfully', variant: 'success' })
        onSaved(res.account)
        setEmail('')
        setToken('')
        setCookies('')
        setRaw('')
      }
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: 'Save failed', description: msg, variant: 'destructive' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-dashed p-3 space-y-2">
        <Label className="text-sm">Quick import (one paste)</Label>
        <p className="text-xs text-muted-foreground">
          On chat.deepseek.com: open DevTools → Console → paste the command below →{' '}
          the credentials are copied → paste them into this box.
        </p>
        <code className="block overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-[11px]">
          {CONSOLE_SNIPPET}
        </code>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={handleCopySnippet} type="button">
            <ClipboardCopy className="h-3.5 w-3.5" />
            Copy console command
          </Button>
          <span className="text-xs text-muted-foreground">Token alone, Cookie header alone, or both joined also work.</span>
        </div>
        <textarea
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          spellCheck={false}
          rows={3}
          placeholder={isEdit ? 'Paste here to replace this account credentials' : 'Paste the copied blob here'}
          className="w-full resize-y rounded-md border border-input bg-background px-2 py-1 font-mono text-xs outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="acct-email">Label (Email / Note)</Label>
        <Input
          id="acct-email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="e.g. user@example.com"
          autoComplete="off"
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="acct-token">Token {raw.trim() ? <span className="text-xs text-muted-foreground">(optional — quick import wins)</span> : null}</Label>
        <Input
          id="acct-token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          disabled={!!raw.trim()}
          placeholder={isEdit ? 'Leave blank to keep Token unchanged' : 'Authorization Bearer token (without Bearer prefix)'}
          autoComplete="off"
          className="font-mono text-xs"
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="acct-cookies">Cookies</Label>
        <Input
          id="acct-cookies"
          value={cookies}
          onChange={(e) => setCookies(e.target.value)}
          disabled={!!raw.trim()}
          placeholder={isEdit ? 'Leave blank to keep Cookies unchanged' : 'cf_clearance=...; session=...'}
          autoComplete="off"
          className="font-mono text-xs"
        />
      </div>
      <div className="flex items-center gap-2 pt-2">
        <Button onClick={handleSave} disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : isEdit ? <Save className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
          {isEdit ? 'Save' : 'Add'}
        </Button>
        {isEdit && onCancel && (
          <Button variant="outline" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
      </div>
    </div>
  )
}
