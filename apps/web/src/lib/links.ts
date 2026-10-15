/**
 * The address to link to, only if it is a web address. A saved URL came from the internet, so
 * a link with any other scheme (javascript:, data:) is never rendered.
 */
export function webAddress(url: string | null): string | undefined {
  if (!url) return undefined;
  try {
    const { protocol } = new URL(url);
    return protocol === "http:" || protocol === "https:" ? url : undefined;
  } catch {
    return undefined;
  }
}
