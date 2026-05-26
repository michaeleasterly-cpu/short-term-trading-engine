export { auth as middleware } from "@/auth";

export const config = {
  // Skip static assets + the login page + the API routes that auth itself uses
  matcher: ["/((?!api/auth|login|_next/static|_next/image|favicon.ico).*)"],
};
