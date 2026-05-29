import { requireSession, forwardPost } from "../../_forward";

export async function POST(
  req: Request,
  ctx: { params: Promise<{ stage: string }> },
): Promise<Response> {
  const session = await requireSession();
  if (!session.ok) return session.response;
  const { stage } = await ctx.params;
  // Forward the body verbatim — tickers list etc.
  let body: Record<string, unknown> | undefined;
  try {
    body = await req.json();
  } catch {
    body = undefined;
  }
  return forwardPost(
    `/api/operations/data-pipeline/run-fallback/${encodeURIComponent(stage)}`,
    session.actor,
    body,
  );
}
