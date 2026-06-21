/** West Africa Time (UTC+1) formatting helpers. */

const WAT_TZ = 'Africa/Lagos';

export function formatDateWAT(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-NG', { timeZone: WAT_TZ });
}

export function formatDateTimeWAT(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-NG', {
    timeZone: WAT_TZ,
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatTimeWAT(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString('en-NG', {
    timeZone: WAT_TZ,
    hour: '2-digit',
    minute: '2-digit',
  });
}
