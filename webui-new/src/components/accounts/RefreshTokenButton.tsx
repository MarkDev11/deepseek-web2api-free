import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { post, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import type { RefreshResponse } from '@/lib/types'

interface Props {
  accountId: string
  onDone?: () => void
}

export function RefreshTokenButton({ accountId, onDone }: Props) {
  const [busy, setBusy] = useState(false)
  const handleClick = async () => {
    setBusy(true)
    try {
      const res = await post<RefreshResponse>(`/admin/api/accounts/${encodeURIComponent(accountId)}/refresh`)
      if (res.ok) {
        toast({ title: 'Token refreshed', description: res.message, variant: 'success' })
      } else {
        toast({ title: 'Refresh failed', description: res.message, variant: 'destructive' })
      }
      onDone?.()
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: 'Refresh failed', description: msg, variant: 'destructive' })
    } finally {
      setBusy(false)
    }
  }
  return (
    <Button variant="outline" size="sm" onClick={handleClick} disabled={busy}>
      <RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} />
      Refresh
    </Button>
  )
}
