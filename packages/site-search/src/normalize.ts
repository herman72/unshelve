import { U_NORM_RUNS } from "./u-norm-table.ts";

// Each character that u_norm() of the server changes, with its result: "" for a removed one.
const CHANGES: ReadonlyMap<string, string> = (() => {
  const changes = new Map<string, string>();
  for (const [first, last, step, shift] of U_NORM_RUNS) {
    for (let codePoint = first; codePoint <= last; codePoint += step) {
      changes.set(
        String.fromCodePoint(codePoint),
        shift === null ? "" : String.fromCodePoint(codePoint + shift),
      );
    }
  }
  return changes;
})();

/**
 * The text as u_norm() of the server makes it, before it searches: small letters, Persian
 * letters for their Arabic forms, ASCII digits, no diacritics, and a space for each zero-width
 * non-joiner. The server changes one character at a time, so this does too, with the server's
 * own table (docs/learn/46).
 *
 * Not toLowerCase(): it gives "i̇stanbul" for "İSTANBUL" and "σας" for "ΣΑΣ", where the server
 * gives "istanbul" and "σασ".
 */
export function uNorm(text: string): string {
  let result = "";
  // for...of goes through code points, so a character outside the first plane stays whole.
  for (const character of text) {
    result += CHANGES.get(character) ?? character;
  }
  return result;
}
