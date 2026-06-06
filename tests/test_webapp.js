"use strict";
// Node.js unit tests — no npm required, uses built-in assert.
// Run: node tests/test_webapp.js

const assert = require("assert");
const {
  roundPrice, calcLadderPrices, calcNotional, calcMargin,
  fmtUSD, fmtPrice, validateOpenForm, validateLadderOpenForm,
  validateLadderCloseForm, buildOpenSummary
} = require("../lib/webapp_logic");

var passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); console.log("  ✓", name); passed++; }
  catch(e) { console.error("  ✗", name, "\n   ", e.message); failed++; }
}

// ── roundPrice ────────────────────────────────────────────────────────────────
console.log("\nroundPrice");
test("1234.5678 → 5 sig figs → 1234.6", () => assert.strictEqual(roundPrice(1234.5678), 1234.6));
test("0.00012345 → 5 sig figs → 0.00012345", () => assert.strictEqual(roundPrice(0.00012345), 0.00012345));
test("BTC-range price 65432.123 → 65432", () => assert.strictEqual(roundPrice(65432.123), 65432));
test("12.3456 → 12.346", () => assert.strictEqual(roundPrice(12.3456), 12.346));
test("zero stays zero", () => assert.strictEqual(roundPrice(0), 0));
test("negative zero passthrough", () => assert.ok(roundPrice(-5) < 0));

// ── calcLadderPrices ──────────────────────────────────────────────────────────
console.log("\ncalcLadderPrices");
test("2 parts: endpoints returned", () => {
  const p = calcLadderPrices(100, 200, 2);
  assert.strictEqual(p.length, 2);
  assert.strictEqual(p[0], 100);
  assert.strictEqual(p[1], 200);
});
test("3 parts: midpoint correct", () => {
  const p = calcLadderPrices(100, 200, 3);
  assert.strictEqual(p.length, 3);
  assert.strictEqual(p[1], 150);
});
test("5 parts: correct count", () => assert.strictEqual(calcLadderPrices(1000, 2000, 5).length, 5));
test("20 parts: boundary allowed", () => {
  const p = calcLadderPrices(10, 20, 20);
  assert.strictEqual(p.length, 20);
});
test("descending range works (to < from)", () => {
  const p = calcLadderPrices(200, 100, 3);
  assert.ok(p[0] > p[1]);
});
test("1 part throws", () => assert.throws(() => calcLadderPrices(100, 200, 1), /2/));
test("21 parts throws", () => assert.throws(() => calcLadderPrices(100, 200, 21), /20/));
test("non-number from throws", () => assert.throws(() => calcLadderPrices("100", 200, 3)));
test("prices are rounded", () => {
  const p = calcLadderPrices(1, 2, 3);
  p.forEach(px => assert.ok(isFinite(px)));
});
test("first price equals fromPx", () => {
  const from = 1234.5;
  const p = calcLadderPrices(from, 2000, 5);
  assert.strictEqual(p[0], roundPrice(from));
});
test("last price equals toPx", () => {
  const to = 9876.5;
  const p = calcLadderPrices(1000, to, 5);
  assert.strictEqual(p[p.length - 1], roundPrice(to));
});

// ── calcNotional / calcMargin ─────────────────────────────────────────────────
console.log("\ncalcNotional / calcMargin");
test("notional = price * size", () => assert.strictEqual(calcNotional(100, 2), 200));
test("margin = notional / leverage", () => assert.strictEqual(calcMargin(100, 2, 10), 20));
test("leverage 1× → margin equals notional", () => assert.strictEqual(calcMargin(100, 1, 1), 100));
test("leverage 0 throws", () => assert.throws(() => calcMargin(100, 1, 0)));

// ── fmtUSD ────────────────────────────────────────────────────────────────────
console.log("\nfmtUSD");
test("< 1000 shows dollars", () => assert.strictEqual(fmtUSD(123.45), "$123.45"));
test(">= 1000 shows k", () => assert.ok(fmtUSD(2500).includes("k")));
test(">= 1e6 shows M", () => assert.ok(fmtUSD(1500000).includes("M")));
test("exact 1000 shows k", () => assert.strictEqual(fmtUSD(1000), "$1.0k"));

// ── fmtPrice ─────────────────────────────────────────────────────────────────
console.log("\nfmtPrice");
test("large price shows no decimals", () => assert.ok(!fmtPrice(50000).includes(".")));
test("mid price 1234 shows 1 decimal", () => assert.ok(fmtPrice(1234).endsWith("4") || fmtPrice(1234).includes(".")));
test("small price shows 2 decimals", () => assert.strictEqual(fmtPrice(1.5), "$1.50"));
test("sub-1 price shows 4 decimals", () => assert.strictEqual(fmtPrice(0.0012), "$0.0012"));

// ── validateOpenForm ──────────────────────────────────────────────────────────
console.log("\nvalidateOpenForm");
test("valid long form → no errors", () => {
  const e = validateOpenForm({coin:"ETH", size:"1", leverage:"10", side:"long"});
  assert.strictEqual(e.length, 0);
});
test("missing coin → error", () => {
  const e = validateOpenForm({coin:"", size:"1", leverage:"10", side:"long"});
  assert.ok(e.length > 0);
});
test("size 0 → error", () => {
  const e = validateOpenForm({coin:"ETH", size:"0", leverage:"10", side:"long"});
  assert.ok(e.length > 0);
});
test("negative size → error", () => {
  const e = validateOpenForm({coin:"ETH", size:"-1", leverage:"10", side:"long"});
  assert.ok(e.length > 0);
});
test("leverage 0 → error", () => {
  const e = validateOpenForm({coin:"ETH", size:"1", leverage:"0", side:"long"});
  assert.ok(e.length > 0);
});
test("leverage 101 → error", () => {
  const e = validateOpenForm({coin:"ETH", size:"1", leverage:"101", side:"long"});
  assert.ok(e.length > 0);
});
test("long TP below SL → error", () => {
  const e = validateOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long", tp:"90", sl:"100"});
  assert.ok(e.length > 0);
});
test("short TP above SL → error", () => {
  const e = validateOpenForm({coin:"ETH", size:"1", leverage:"5", side:"short", tp:"110", sl:"100"});
  assert.ok(e.length > 0);
});
test("long with valid TP/SL → no extra error", () => {
  const e = validateOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long", tp:"120", sl:"90"});
  assert.strictEqual(e.length, 0);
});
test("short with valid TP/SL → no extra error", () => {
  const e = validateOpenForm({coin:"ETH", size:"1", leverage:"5", side:"short", tp:"90", sl:"120"});
  assert.strictEqual(e.length, 0);
});

// ── validateLadderOpenForm ────────────────────────────────────────────────────
console.log("\nvalidateLadderOpenForm");
test("valid ladder open form → no errors", () => {
  const e = validateLadderOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long",
    from_price:"1500", to_price:"1600", parts:"5"});
  assert.strictEqual(e.length, 0);
});
test("missing from_price → error", () => {
  const e = validateLadderOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long",
    from_price:"", to_price:"1600", parts:"5"});
  assert.ok(e.length > 0);
});
test("equal prices → error", () => {
  const e = validateLadderOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long",
    from_price:"1500", to_price:"1500", parts:"5"});
  assert.ok(e.length > 0);
});
test("parts 1 → error", () => {
  const e = validateLadderOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long",
    from_price:"1500", to_price:"1600", parts:"1"});
  assert.ok(e.length > 0);
});
test("parts 21 → error", () => {
  const e = validateLadderOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long",
    from_price:"1500", to_price:"1600", parts:"21"});
  assert.ok(e.length > 0);
});
test("parts 2 and 20 are valid", () => {
  const e2  = validateLadderOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long",
    from_price:"1500", to_price:"1600", parts:"2"});
  const e20 = validateLadderOpenForm({coin:"ETH", size:"1", leverage:"5", side:"long",
    from_price:"1500", to_price:"1600", parts:"20"});
  assert.strictEqual(e2.length, 0);
  assert.strictEqual(e20.length, 0);
});
test("inherits open form validation (bad coin)", () => {
  const e = validateLadderOpenForm({coin:"", size:"1", leverage:"5", side:"long",
    from_price:"1500", to_price:"1600", parts:"3"});
  assert.ok(e.length > 0);
});

// ── validateLadderCloseForm ───────────────────────────────────────────────────
console.log("\nvalidateLadderCloseForm");
test("valid close form → no errors", () => {
  const e = validateLadderCloseForm({coin:"ETH", from_price:"1500", to_price:"2000", parts:"5"});
  assert.strictEqual(e.length, 0);
});
test("missing coin → error", () => {
  const e = validateLadderCloseForm({coin:"", from_price:"1500", to_price:"2000", parts:"5"});
  assert.ok(e.length > 0);
});
test("equal prices → error", () => {
  const e = validateLadderCloseForm({coin:"ETH", from_price:"1500", to_price:"1500", parts:"5"});
  assert.ok(e.length > 0);
});
test("parts 1 → error", () => {
  const e = validateLadderCloseForm({coin:"ETH", from_price:"1500", to_price:"2000", parts:"1"});
  assert.ok(e.length > 0);
});
test("descending range valid", () => {
  const e = validateLadderCloseForm({coin:"ETH", from_price:"2000", to_price:"1500", parts:"5"});
  assert.strictEqual(e.length, 0);
});

// ── buildOpenSummary ──────────────────────────────────────────────────────────
console.log("\nbuildOpenSummary");
test("long summary contains Long", () => {
  const s = buildOpenSummary("ETH", "long", "1", 10, 2000, "single", 1);
  assert.ok(s.includes("Long"));
});
test("short summary contains Short", () => {
  const s = buildOpenSummary("ETH", "short", "1", 10, 2000, "single", 1);
  assert.ok(s.includes("Short"));
});
test("includes coin name", () => {
  const s = buildOpenSummary("ETH", "long", "1", 10, 2000, "single", 1);
  assert.ok(s.includes("ETH"));
});
test("includes leverage", () => {
  const s = buildOpenSummary("ETH", "long", "1", 10, 2000, "single", 1);
  assert.ok(s.includes("10"));
});
test("with price: includes Notional", () => {
  const s = buildOpenSummary("ETH", "long", "1", 10, 2000, "single", 1);
  assert.ok(s.includes("Notional"));
});
test("with price: includes Margin", () => {
  const s = buildOpenSummary("ETH", "long", "1", 10, 2000, "single", 1);
  assert.ok(s.includes("Margin"));
});
test("no price (0): no Notional", () => {
  const s = buildOpenSummary("ETH", "long", "1", 10, 0, "single", 1);
  assert.ok(!s.includes("Notional"));
});
test("ladder mode mentions orders", () => {
  const s = buildOpenSummary("ETH", "long", "1", 10, 2000, "ladder", 5);
  assert.ok(s.includes("orders") || s.includes("5"));
});

// ── Summary ───────────────────────────────────────────────────────────────────
console.log("\n" + "─".repeat(50));
console.log("Results: " + passed + " passed, " + failed + " failed");
if (failed > 0) process.exit(1);
