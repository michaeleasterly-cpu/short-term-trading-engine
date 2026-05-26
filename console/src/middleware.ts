export { auth as middleware } from "@/auth";

export const config = {
  // Skip static assets + the login page + the API routes that auth itself uses
  // + the public /market page (publicly viewable market-health snapshot).
  matcher: ["/((?!api/auth|login|market|_next/static|_next/image|favicon.ico).*)"],
};
