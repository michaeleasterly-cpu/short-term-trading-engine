/**
 * Daily revalidation endpoint for the public /market page. A Vercel Cron hits
 * this once a day (≈ midnight US Eastern) to force a fresh server render, so the
 * self-fetched gauges (FRED/FMP/multpl/AAII) pull new data on the daily cycle
 * even with no organic traffic. Revalidation is idempotent + cheap.
 *
 * Optional protection: if CRON_SECRET is set, require the matching Bearer token
 * (Vercel Cron sends it automatically). Absent the secret, the endpoint still
 * only triggers a cache refresh — no data is exposed or mutated.
 */
import { revalidatePath } from "next/cache";
import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export function GET(req: NextRequest) {
  const secret = process.env.CRON_SECRET;
  if (secret && req.headers.get("authorization") !== `Bearer ${secret}`) {
    return NextResponse.json({ ok: false }, { status: 401 });
  }
  revalidatePath("/market");
  return NextResponse.json({ revalidated: true, at: new Date().toISOString() });
}
