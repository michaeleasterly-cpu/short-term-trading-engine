export { auth as middleware } from "@/auth";

export const config = {
  // Skip static assets + the login page + the API routes that auth itself uses
  // + the public /market and /carbondale pages.
  matcher: ["/((?!api/auth|login|market|carbondale|murphysboro|mantracon|_next/static|_next/image|favicon.ico).*)"],
};
