/** A time as the operator reads it in a table: UTC, to the minute, the same in every browser. */
export function formatTime(iso: string | null): string {
  if (iso === null) return "never";
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
