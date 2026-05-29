import { requireSession, forwardPost } from "../../_forward";

export async function POST(
  _req: Request,
  ctx: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  const session = await requireSession();
  if (!session.ok) return session.response;
  const { jobId } = await ctx.params;
  return forwardPost(
    `/api/operations/data-pipeline/abort/${encodeURIComponent(jobId)}`,
    session.actor,
  );
}
