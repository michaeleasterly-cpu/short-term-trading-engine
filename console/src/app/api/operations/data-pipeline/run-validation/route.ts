import { requireSession, forwardPost } from "../_forward";

export async function POST(): Promise<Response> {
  const session = await requireSession();
  if (!session.ok) return session.response;
  return forwardPost(
    "/api/operations/data-pipeline/run-validation", session.actor,
  );
}
