import { requireSession, forwardGet } from "../../_forward";

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  // Job status — gated by NextAuth session but no bearer token
  // needed (read-only on console-api).
  const session = await requireSession();
  if (!session.ok) return session.response;
  const { jobId } = await ctx.params;
  return forwardGet(
    `/api/operations/data-pipeline/jobs/${encodeURIComponent(jobId)}`,
  );
}
