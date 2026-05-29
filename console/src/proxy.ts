// 2026-05-29 — renamed from middleware.ts per Next.js deprecation
// (https://nextjs.org/docs/messages/middleware-to-proxy). The
// ``middleware`` file convention is deprecated in favor of ``proxy``.
// Same NextAuth gating semantics; only the file name + named export
// change. Same matcher.

export { auth as proxy } from "@/auth";

export const config = {
  // Skip static assets + the login page + the API routes that auth itself uses
  // + the public regional/city dashboards (market, regional reports, city pages).
  matcher: ["/((?!api/auth|login|market|carbondale|murphysboro|southern-illinois|east-central-illinois|charleston|_next/static|_next/image|favicon.ico).*)"],
};
