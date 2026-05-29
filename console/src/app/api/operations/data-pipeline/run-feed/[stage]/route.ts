import { requireSession, forwardPost } from "../../_forward";

export async function POST(
  _req: Request,
  ctx: { params: Promise<{ stage: string }> },
): Promise<Response> {
  const session = await requireSession();
  if (!session.ok) return session.response;
  const { stage } = await ctx.params;
  return forwardPost(
    `/api/operations/data-pipeline/run-feed/${encodeURIComponent(stage)}`,
    session.actor,
  );
}
