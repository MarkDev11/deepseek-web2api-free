import { Badge } from '@/components/ui/badge'
import type { Account } from '@/lib/types'

type ExpiryVariant = 'env' | 'destructive' | 'warning' | 'secondary'

/** Map the remaining token lifetime to a small badge label + variant. */
export function tokenExpiryBadge(expiresIn: Account['token_expires_in']): {
  label: string
  variant: ExpiryVariant
} {
  if (expiresIn == null) return { label: 'Unknown', variant: 'env' }
  if (expiresIn < 0) return { label: 'Expired', variant: 'destructive' }
  if (expiresIn < 3600) return { label: `${Math.max(0, Math.floor(expiresIn / 60))}m left`, variant: 'warning' }
  if (expiresIn < 86400) return { label: `${Math.floor(expiresIn / 3600)}h left`, variant: 'secondary' }
  return { label: `${Math.floor(expiresIn / 86400)}d left`, variant: 'secondary' }
}

export function TokenExpiryBadge({ expiresIn }: { expiresIn: Account['token_expires_in'] }) {
  const { label, variant } = tokenExpiryBadge(expiresIn)
  return <Badge variant={variant}>{label}</Badge>
}
