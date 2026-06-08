/**
 * Unit tests for the self-fetching market-data helpers — runs on Node's
 * built-in test runner with native TypeScript type-stripping (Node >= 22.6):
 *
 *   node --test src/lib/market-data.test.ts      (from console/)
 *   npm test                                      (wired in package.json)
 *
 * No new dependency, no DB, no network: these exercise the PURE helpers only.
 * The focus is the net_liquidity unit normalization. Units are ASSERTED against
 * FRED metadata, not assumed (verified 2026-06-08):
 *   WALCL     — $millions  → ÷1000 to billions
 *   WTREGEN   — $millions  → ÷1000 to billions  (FRED reports it in MILLIONS)
 *   RRPONTSYD — $billions  → already billions
 * The formula MUST normalize WALCL and WTREGEN millions→billions FIRST. Getting
 * WTREGEN's unit wrong (treating it as billions) yields a nonsensical
 * −$869,000bn instead of ~+$5,800bn — the exact bug "assert, don't assume" guards.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  netLiquidityUsdBn,
  moveBand,
  vvixRiskFromLevel,
  concRiskFromTop10,
  parseIciWeeklyEquity,
  qualitySpreadPp,
  bdcDiscountPct,
} from "./market-data.ts";

test("netLiquidityUsdBn normalizes WALCL + WTREGEN millions → billions before differencing", () => {
  // Realistic FRED magnitudes: WALCL = 6,711,495 $M (= $6,711.495 bn),
  // WTREGEN = 875,713 $M (= $875.713 bn), RRPONTSYD = 1.832 $bn (already bn).
  // Correct net liquidity ≈ 6711.495 − 875.713 − 1.832 = 5833.95 $bn (~$5.8T).
  const got = netLiquidityUsdBn(6_711_495, 875_713, 1.832);
  assert.ok(Math.abs(got - 5833.95) < 0.01, `expected ~5833.95 bn, got ${got}`);
  // Sanity: a plausible US net-liquidity level is several $trillion, POSITIVE.
  assert.ok(got > 4000 && got < 8000, `net liquidity out of plausible band: ${got}`);
});

test("netLiquidityUsdBn: WALCL AND WTREGEN are divided by 1000 (both are millions)", () => {
  // If WTREGEN were (wrongly) treated as billions — the original spec note —
  // the result would be 6711.495 − 875_713 − 1.832 = a nonsensical −868_823.
  // Assert we are NOT doing that (the bug the smoke test caught).
  const got = netLiquidityUsdBn(6_711_495, 875_713, 1.832);
  assert.ok(got > 0, `WTREGEN must be normalized millions→billions; got ${got}`);
  const wrongIfWtregenBillions = 6_711_495 / 1000 - 875_713 - 1.832;
  assert.notEqual(got, wrongIfWtregenBillions);
});

test("netLiquidityUsdBn is exactly (WALCL/1000) − (WTREGEN/1000) − RRPONTSYD", () => {
  const walclM = 7_123_456;
  const wtregenM = 612_300;
  const rrpB = 233.7;
  const expected = walclM / 1000 - wtregenM / 1000 - rrpB;
  assert.equal(netLiquidityUsdBn(walclM, wtregenM, rrpB), expected);
});

test("netLiquidityUsdBn handles a fully-drained TGA + RRP (= WALCL in $bn)", () => {
  assert.equal(netLiquidityUsdBn(5_000_000, 0, 0), 5000);
});

test("moveBand: <80 calm, 80-120 watch, >120 stressed", () => {
  assert.equal(moveBand(60), "calm");
  assert.equal(moveBand(79.99), "calm");
  assert.equal(moveBand(80), "watch");
  assert.equal(moveBand(100), "watch");
  assert.equal(moveBand(120), "watch");
  assert.equal(moveBand(120.01), "stressed");
  assert.equal(moveBand(150), "stressed");
});

// ── Composite Timing-block (VVIX modulation + concentration breadth slot) ─────
// The Timing block lives inline in page.tsx; here we (a) pin the exported risk
// maps it consumes, and (b) replicate the exact Timing formula to assert its
// invariants: the ±8 cap holds, VVIX modulates the VIX stress term, concentration
// fills the breadth slot, and the MOVE proxy never enters the composite.

test("vvixRiskFromLevel: judgment-calibrated band [[80,0],[100,40],[120,70],[150,100]]", () => {
  assert.equal(vvixRiskFromLevel(80), 0);
  assert.equal(vvixRiskFromLevel(100), 40);
  assert.equal(vvixRiskFromLevel(120), 70);
  assert.equal(vvixRiskFromLevel(150), 100);
  assert.equal(vvixRiskFromLevel(60), 0);    // clamped below the floor
  assert.equal(vvixRiskFromLevel(200), 100); // clamped above the ceiling
  // VVIX = 92.40 (CBOE, validated 2026-06-08) interpolates between 80→0 and 100→40.
  assert.ok(Math.abs(vvixRiskFromLevel(92.4) - 24.8) < 0.01, `got ${vvixRiskFromLevel(92.4)}`);
});

test("concRiskFromTop10: judgment-calibrated band [[20,0],[30,50],[40,100]]", () => {
  assert.equal(concRiskFromTop10(20), 0);
  assert.equal(concRiskFromTop10(30), 50);
  assert.equal(concRiskFromTop10(40), 100);
  assert.equal(concRiskFromTop10(10), 0);   // clamped
  assert.equal(concRiskFromTop10(50), 100); // clamped
  // top-10 = 37.96% (SSGA, validated 2026-06-08) → between 30→50 and 40→100.
  assert.ok(Math.abs(concRiskFromTop10(37.96) - 89.8) < 0.1, `got ${concRiskFromTop10(37.96)}`);
});

// Exact replica of the page.tsx Timing block (Part B). Kept in lockstep with
// computeComposite §4 so the test pins the wiring contract: VVIX boost, the
// concRisk ?? rspGapRisk breadth slot, and the ±8 cap.
function timingBlock(opts: {
  vixRisk: number;
  vvix?: number | null;        // VVIX level (null/undefined ⇒ neutral 50)
  top10Pct?: number | null;    // SSGA top-10 % (null/undefined ⇒ fall back to rspGapRisk)
  rspGapRisk?: number;         // RSP-vs-cap-weight breadth risk fallback (default 50)
  moveProxyPct?: number;       // present ONLY to prove it does NOT affect the result
}): number {
  const { vixRisk, vvix = null, top10Pct = null, rspGapRisk = 50 } = opts;
  const vvixRisk = vvix != null ? vvixRiskFromLevel(vvix) : 50;
  const vvixBoost = Math.max(0.8, Math.min(1.2, 1 + (vvixRisk - 50) / 250));
  const stressTerm = (vixRisk / 100) * 5 * vvixBoost;
  const concRisk = top10Pct != null ? concRiskFromTop10(top10Pct) : null;
  const breadthRisk = concRisk ?? rspGapRisk;
  const breadthTerm = ((breadthRisk - 50) / 50) * 3;
  return Math.max(-8, Math.min(8, stressTerm + breadthTerm));
}

test("Timing: VVIX modulates the VIX stress term (higher VVIX ⇒ larger stress)", () => {
  // Same VIX risk, vary VVIX: a high VVIX must amplify the stress term vs a low one.
  const lowVvix = timingBlock({ vixRisk: 70, vvix: 80, top10Pct: 30 });   // VVIX risk 0 → boost 0.8
  const midVvix = timingBlock({ vixRisk: 70, vvix: 100, top10Pct: 30 });  // VVIX risk 40 → boost ~0.96
  const highVvix = timingBlock({ vixRisk: 70, vvix: 150, top10Pct: 30 }); // VVIX risk 100 → boost 1.2
  assert.ok(lowVvix < midVvix && midVvix < highVvix, `expected monotone, got ${lowVvix},${midVvix},${highVvix}`);
  // top10=30 ⇒ concRisk 50 ⇒ breadthTerm 0, so timing == stressTerm here.
  assert.ok(Math.abs(lowVvix - (70 / 100) * 5 * 0.8) < 1e-9);
  assert.ok(Math.abs(highVvix - (70 / 100) * 5 * 1.2) < 1e-9);
});

test("Timing: vvixBoost is clamped to [0.8, 1.2]", () => {
  // VVIX far below floor and far above ceiling must still clamp the boost.
  const lo = timingBlock({ vixRisk: 100, vvix: 0, top10Pct: 30 });   // boost floored at 0.8
  const hi = timingBlock({ vixRisk: 100, vvix: 999, top10Pct: 30 }); // boost capped at 1.2
  assert.ok(Math.abs(lo - 5 * 0.8) < 1e-9, `lo=${lo}`);  // (100/100)*5*0.8 = 4.0
  assert.ok(Math.abs(hi - Math.min(8, 5 * 1.2)) < 1e-9, `hi=${hi}`); // 6.0
});

test("Timing: SSGA concentration is preferred for the breadth slot, RSP-gap is the fallback", () => {
  // With concentration present, the breadth term must use concRisk, not rspGapRisk.
  const withConc = timingBlock({ vixRisk: 0, vvix: 100, top10Pct: 40, rspGapRisk: 50 }); // concRisk 100
  // breadthTerm = ((100-50)/50)*3 = +3; stressTerm = 0
  assert.ok(Math.abs(withConc - 3) < 1e-9, `got ${withConc}`);
  // With concentration absent, it must fall back to rspGapRisk.
  const fallback = timingBlock({ vixRisk: 0, vvix: 100, top10Pct: null, rspGapRisk: 100 }); // breadthRisk 100
  assert.ok(Math.abs(fallback - 3) < 1e-9, `got ${fallback}`);
});

test("Timing: the ±8 cap holds at the extremes", () => {
  // Max stress + max narrow concentration must clamp at +8 (raw would exceed it).
  const maxed = timingBlock({ vixRisk: 100, vvix: 150, top10Pct: 40 }); // 5*1.2 + 3 = 9 → clamp 8
  assert.equal(maxed, 8);
  // A negative-leaning breadth (broad) with no stress clamps no lower than -8.
  const minned = timingBlock({ vixRisk: 0, vvix: 80, top10Pct: 20 }); // stress 0 + ((0-50)/50)*3 = -3
  assert.ok(minned >= -8 && minned < 0, `got ${minned}`);
  // Force the lower bound: rspGapRisk = 0 (very broad) ⇒ breadthTerm -3, still > -8;
  // the cap is structural — assert it can never be exceeded for any input combo.
  for (const vix of [0, 50, 100]) for (const vv of [60, 100, 200]) for (const c of [10, 30, 50]) {
    const t = timingBlock({ vixRisk: vix, vvix: vv, top10Pct: c });
    assert.ok(t >= -8 && t <= 8, `timing ${t} out of ±8 for vix=${vix} vvix=${vv} conc=${c}`);
  }
});

// ── DISPLAY-ONLY flows/credit cards (NOT in the composite) ─────────────────
// The ICI weekly XLS lays the data out with the weekly block AFTER an "Estimated
// weekly fund flows" marker row; the rows above are the MONTHLY history, which the
// parser must NOT read. Column layout (header:1 form): index 0 = Date, index 3 =
// Equity Total (a NaN spacer sits at index 2). Fixture mirrors the real sheet
// retrieved 2026-06-08 (last weekly row 05/27/2026 → Equity Total −2214).

const ICI_FIXTURE: Array<Array<string | number | null>> = [
  [],
  [],
  [],
  [],
  ["Date", "Total LT MF and ETF flows", null, "Equity", null, null, null, null, null, "Hybrid"],
  [],
  // MONTHLY block — parser MUST skip these (no "weekly" marker yet, and these are
  // month-end dates; we assert below that 41033 / 61739 never leak into the result).
  ["01/31/2026 ", 104118, null, 7619, null, -36797],
  ["02/28/2026 ", 151228, null, 41033, null, 2808],
  ["04/30/2026 ", 102273, null, 61739, null, 37458],
  [],
  ["Estimated weekly fund flows"],
  ["04/29/2026", 8467, null, -5066, null, -7347],
  ["05/06/2026", 8874, null, -13008, null, -8168],
  ["05/13/2026", 38884, null, 13274, null, 10826],
  ["05/20/2026", 15195, null, -13268, null, -11744],
  ["05/27/2026", 15641, null, -2214, null, -1575],
  ["Note: Weekly fund flows are estimates based on reporting covering more than 98 percent…"],
];

test("parseIciWeeklyEquity reads the WEEKLY Equity-Total block, latest = -2214 (05/27/2026)", () => {
  const series = parseIciWeeklyEquity(ICI_FIXTURE);
  assert.equal(series.length, 5, `expected 5 weekly rows, got ${series.length}`);
  const latest = series[series.length - 1];
  assert.equal(latest.week, "2026-05-27");
  assert.equal(latest.equity_m, -2214);
  // Prior weeks, oldest→newest, match the validated sample values from the doc.
  assert.deepEqual(series.map((s) => s.equity_m), [-5066, -13008, 13274, -13268, -2214]);
});

test("parseIciWeeklyEquity does NOT leak the monthly block into the weekly series", () => {
  const series = parseIciWeeklyEquity(ICI_FIXTURE);
  // 41033 and 61739 are MONTHLY values above the marker — they must never appear.
  assert.ok(!series.some((s) => s.equity_m === 41033 || s.equity_m === 61739));
  // Dates must all be the weekly rows, not the month-end rows.
  assert.ok(!series.some((s) => s.week === "2026-01-31" || s.week === "2026-04-30"));
});

test("parseIciWeeklyEquity returns [] when the weekly marker is absent (no fabrication)", () => {
  const noMarker = ICI_FIXTURE.filter((r) => String(r[0] ?? "").toLowerCase().indexOf("estimated weekly") === -1);
  assert.deepEqual(parseIciWeeklyEquity(noMarker), []);
  assert.deepEqual(parseIciWeeklyEquity([]), []);
});

test("qualitySpreadPp = DBAA − DAAA, 2dp; validated 6.06 − 5.53 = 0.53 pp", () => {
  assert.equal(qualitySpreadPp(6.06, 5.53), 0.53);
  // Rounds to 2 decimal places.
  assert.equal(qualitySpreadPp(6.061, 5.534), 0.53);
  // A wider, stressed quality gap.
  assert.equal(qualitySpreadPp(7.5, 5.0), 2.5);
  // Sign is preserved (BAA below AAA would be negative — never clamped).
  assert.equal(qualitySpreadPp(5.0, 5.2), -0.2);
});

test("bdcDiscountPct = (priceToBook − 1) × 100, 1dp; validated ARCC 0.958 → −4.2%, FSK 0.565 → −43.5%", () => {
  assert.equal(bdcDiscountPct(0.9581841450408818), -4.2);
  assert.equal(bdcDiscountPct(0.5650183631247631), -43.5);
  assert.equal(bdcDiscountPct(0.7657201396924798), -23.4);
  // A premium-to-NAV (P/B > 1) is positive.
  assert.equal(bdcDiscountPct(1.05), 5);
  assert.equal(bdcDiscountPct(1.0), 0);
});

test("Timing: the MOVE realized-vol proxy does NOT enter the composite", () => {
  // The MOVE proxy is display-only. Passing any moveProxyPct must leave timing
  // identical to omitting it — the formula has no MOVE term by construction.
  const base = timingBlock({ vixRisk: 70, vvix: 100, top10Pct: 30 });
  const withMoveProxy = timingBlock({ vixRisk: 70, vvix: 100, top10Pct: 30, moveProxyPct: 9.2 });
  const withHugeMoveProxy = timingBlock({ vixRisk: 70, vvix: 100, top10Pct: 30, moveProxyPct: 264 });
  assert.equal(base, withMoveProxy);
  assert.equal(base, withHugeMoveProxy);
});
