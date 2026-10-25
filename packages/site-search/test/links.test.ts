import assert from "node:assert/strict";
import { test } from "node:test";
import { safeLink } from "../src/links.ts";

test("a web link stays a link", () => {
  assert.equal(safeLink("https://example.com/leases"), "https://example.com/leases");
  assert.equal(safeLink("http://example.com/a?b=c"), "http://example.com/a?b=c");
});

test("an address that runs code or holds data is no link", () => {
  assert.equal(safeLink("javascript:alert(1)"), null);
  assert.equal(safeLink(" JavaScript:alert(1)"), null);
  assert.equal(safeLink("data:text/html,<script>alert(1)</script>"), null);
});

test("an address without its scheme, and no address, are no link", () => {
  assert.equal(safeLink("example.com/leases"), null);
  assert.equal(safeLink("/leases"), null);
  assert.equal(safeLink(null), null);
});
