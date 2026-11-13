/** Small formatting helpers shared by the Life pages. */

export function formatTs(seconds: number | null | undefined): string {
  if (!seconds) return '-';
  const date = new Date(seconds * 1000);
  return date.toLocaleString();
}

export function truncate(text: string | null | undefined, limit = 80): string {
  const value = String(text ?? '');
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

export function downloadJson(filename: string, payload: unknown): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: 'application/json;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
