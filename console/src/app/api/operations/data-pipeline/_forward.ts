/**
 * Server-side forwarding helper for operator-trigger endpoints.
 *
 * Browser → Next.js (this layer) → console-api
 *
 * Two-layer auth (per the 2026-05-29 architect decision):
 *   1. Browser → Next.js: NextAuth Credentials JWT (cookie).
 *   2. Next.js → console-api: shared-secret bearer token from
 *      ``process.env.CONSOLE_OPS_TOKEN``.
 *
 * The bearer token MUST NOT be exposed to the browser — that's why
 * it lives in a server-only env var (no ``NEXT_PUBLIC_`` prefix) and
 * is read inside this module only. Browser requests carry only the
 * NextAuth cookie.
 *
 * Audit trail: every operator-trigger request records the
 * authenticated user from the NextAuth session into the forwarded
 * payload (server-side cannot be spoofed by the browser). console-api
 * then writes an ``OPERATOR_RUN_REQUESTED`` row to application_log
 * with that actor.
 */
import { NextResponse } from "next/server";
import { auth } from "@/auth";

const CONSOLE_API_BASE =
  process.env.NEXT_PUBLIC_API_BASE
  || "https://console-api-production-4576.up.railway.app";

export interface ForwardResult {
  status: number;
  body: unknown;
}

export async function requireSession(): Promise<
  | { ok: true; actor: string }
  | { ok: false; response: NextResponse }
> {
  const session = await auth();
  if (!session?.user) {
    return {
      ok: false,
      response: NextResponse.json(
        { error: "unauthenticated" },
        { status: 401 },
      ),
    };
  }
  const actor =
    (session.user as { email?: string; name?: string }).email
    || (session.user as { name?: string }).name
    || "operator";
  return { ok: true, actor };
}

export async function forwardPost(
  path: string,
  actor: string,
  bodyOverride?: Record<string, unknown>,
): Promise<NextResponse> {
  const token = process.env.CONSOLE_OPS_TOKEN;
  if (!token) {
    return NextResponse.json(
      {
        error:
          "CONSOLE_OPS_TOKEN not configured on the Next.js console deploy. "
          + "Operator actions are blocked until the token is set. "
          + "See docs/runbooks/console-operator-actions.md.",
      },
      { status: 503 },
    );
  }
  const url = `${CONSOLE_API_BASE}${path}`;
  // Body shape: always carries actor. If the caller supplied a
  // bodyOverride (e.g. {tickers:[...]} from a scoped repair), merge
  // it in so the downstream endpoint sees both the actor and the
  // scope.
  const body: Record<string, unknown> = { actor, ...(bodyOverride ?? {}) };
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        "X-Console-Actor": actor,
      },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return NextResponse.json(
      { error: `forward failed: ${String(e)}` },
      { status: 502 },
    );
  }
  let respBody: unknown = null;
  try {
    respBody = await res.json();
  } catch {
    respBody = { error: res.statusText };
  }
  return NextResponse.json(respBody, { status: res.status });
}

export async function forwardGet(path: string): Promise<NextResponse> {
  // GET endpoints used here (job status) don't require the bearer
  // token on console-api (read-only). The Next.js route still
  // forwards through this helper so the browser doesn't need to know
  // the console-api URL.
  const url = `${CONSOLE_API_BASE}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: "GET",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch (e) {
    return NextResponse.json(
      { error: `forward failed: ${String(e)}` },
      { status: 502 },
    );
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = { error: res.statusText };
  }
  return NextResponse.json(body, { status: res.status });
}
