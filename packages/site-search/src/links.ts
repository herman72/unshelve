/**
 * The link of a record, when a page may use it: an absolute http or https address. A feed that
 * holds another kind of address, such as javascript: or data:, gets no link. The server writes
 * only web links, so this is a second guard, in the place where the link becomes an <a>.
 */
export function safeLink(url: string | null): string | null {
  if (url === null) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    // Not an absolute address.
    return null;
  }
}
