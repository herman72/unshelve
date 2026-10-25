import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { uNorm } from "../src/normalize.ts";
import { U_NORM_RUNS } from "../src/u-norm-table.ts";

// The same cases as the test of u_norm() in the database:
// apps/api/tests/publish/test_site_search.py reads this file too.
interface Case {
  why: string;
  input: string;
  expected: string;
}
const CASES = JSON.parse(
  readFileSync(new URL("./u-norm-cases.json", import.meta.url), "utf8"),
) as Case[];

for (const { why, input, expected } of CASES) {
  test(`uNorm, as the database: ${why}`, () => {
    assert.equal(uNorm(input), expected);
  });
}

test("uNorm changes each character of the table as its run says, and no other character", () => {
  const expected = new Map<number, string>();
  for (const [first, last, step, shift] of U_NORM_RUNS) {
    for (let codePoint = first; codePoint <= last; codePoint += step) {
      expected.set(codePoint, shift === null ? "" : String.fromCodePoint(codePoint + shift));
    }
  }

  const wrong: string[] = [];
  for (let codePoint = 1; codePoint <= 0x10ffff; codePoint++) {
    if (codePoint >= 0xd800 && codePoint <= 0xdfff) continue; // halves of a character
    const character = String.fromCodePoint(codePoint);
    if (uNorm(character) !== (expected.get(codePoint) ?? character)) {
      wrong.push(`U+${codePoint.toString(16).toUpperCase()}`);
    }
  }
  assert.deepEqual(wrong, []);
});
