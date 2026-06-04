---
name: nextauth-public-route-pattern
description: New public dashboard routes must be added to console/src/proxy.ts matcher exclude list or they default to auth-gated
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

NextAuth proxy at `console/src/proxy.ts` protects every route by default. (Renamed from `middleware.ts` on 2026-05-29 per the Next.js [middleware-to-proxy deprecation](https://nextjs.org/docs/messages/middleware-to-proxy) — same matcher semantics, only the file name + named export changed.) Public dashboards (the regional + city stakeholder pages) are listed by name in a negative-lookahead matcher:

```ts
matcher: ["/((?!api/auth|login|market|carbondale|murphysboro|southern-illinois|east-central-illinois|charleston|_next/static|_next/image|favicon.ico).*)"]
```

**When adding a new public regional or city dashboard:**
1. Add the route slug (no leading slash, no trailing slash) to the matcher's exclude list — e.g., a new `/west-central-illinois` page would need `|west-central-illinois|` inserted.
2. Verify with `curl -sI https://ste-console.vercel.app/<new-route>` — should return HTTP 200, NOT 307 redirect to `/login`.
3. Wait ~1-2 min for Vercel deploy after pushing the proxy change.

**Incident 2026-05-29:** `/east-central-illinois` and `/charleston` were both built as public stakeholder dashboards but the middleware matcher was never updated. Operator's friend hit a login prompt. Fixed in commit `3aa7f15`.

**Sibling pages already in the exclude list** (all confirmed public 2026-05-29): `market`, `carbondale`, `murphysboro`, `southern-illinois`, `east-central-illinois`, `charleston`. The shared footer (`DEFAULT_FOOTER_COLUMNS` in `src/components/dashboard-chrome.tsx`) links all 6 — any new public route should be added there too so cross-navigation works.
