/**
 * Shared design system for the public dashboard pages
 * (/southern-illinois, /carbondale, /murphysboro, /market).
 *
 * Drop <DashboardHead /> in the page <head>, wrap body content in
 * <DashboardShell>, and use the named structural components (<Topbar>,
 * <Hero>, <Freshness>, <StickyNav>, <Section>) to get the unified
 * newspaper-style design specified by the Southern Illinois Dashboard
 * scaffold (May 2026).
 *
 * Typography: IBM Plex Serif (display), Plex Sans (body), Plex Mono (data).
 * Color palette: paper tones + slate-blue accent + restrained semantic
 * colors (pos / warn / neg / hi).
 */

export const DASHBOARD_CSS = `
:root {
  --paper:    #f3f0e8;
  --paper-2:  #ece8dd;
  --card:     #fbfaf6;
  --card-2:   #f7f4ec;
  --ink:      #1b1a17;
  --ink-2:    #3d3a33;
  --ink-3:    #6b6759;
  --ink-4:    #8e8a7c;
  --rule:     #d3ccb8;
  --rule-2:   #bdb59f;
  --rule-3:   #ece6d4;
  --accent:   #1f5f8f;
  --accent-2: #14456a;
  --pos:      #2e6f4f;
  --warn:     #a06a14;
  --neg:      #9b2c2c;
  --hi:       #c3a55a;
  --pos-bg:   #e6efe5;
  --warn-bg:  #f5ecd6;
  --neg-bg:   #f1ddd9;
  --hi-bg:    #f4ead2;
}

* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--paper);
  color: var(--ink);
  font: 14px/1.55 "IBM Plex Sans", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "cv11", "ss01";
}
a { color: var(--accent); text-decoration: none; border-bottom: 1px solid color-mix(in oklab, var(--accent) 30%, transparent); }
a:hover { color: var(--accent-2); border-bottom-color: var(--accent-2); }
.mono { font-family: "IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; }
.serif { font-family: "IBM Plex Serif", Georgia, serif; }
.tab { font-variant-numeric: tabular-nums; }
.eyebrow { font-family: "IBM Plex Mono", monospace; font-size: 10.5px; font-weight: 500; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-3); }
.muted { color: var(--ink-3); }
.shell { max-width: 1240px; margin: 0 auto; padding: 28px 28px 96px; }

.topbar { display: flex; align-items: center; gap: 14px; padding: 10px 0 18px; border-bottom: 1px solid var(--rule); flex-wrap: wrap; }
.brand { display: flex; align-items: center; gap: 10px; font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink-2); }
.brand img { width: 22px; height: 22px; display: block; }
.topbar .sep { flex: 1; }
.topbar .meta { display: flex; gap: 18px; font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--ink-3); flex-wrap: wrap; }
.topbar .meta b { color: var(--ink); font-weight: 500; }
.topbar .dot { width: 7px; height: 7px; background: var(--pos); border-radius: 50%; display: inline-block; margin-right: 6px; vertical-align: 1px; box-shadow: 0 0 0 3px color-mix(in oklab, var(--pos) 20%, transparent); }

.hero { padding: 40px 0 28px; display: grid; grid-template-columns: 1.45fr 1fr; gap: 56px; align-items: end; }
.hero-tag { display: inline-flex; align-items: center; gap: 10px; padding: 5px 10px; background: var(--neg-bg); color: var(--neg); font-family: "IBM Plex Mono", monospace; font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase; border: 1px solid color-mix(in oklab, var(--neg) 25%, transparent); border-radius: 2px; }
.hero-tag.pos { background: var(--pos-bg); color: var(--pos); border-color: color-mix(in oklab, var(--pos) 25%, transparent); }
.hero-tag.warn { background: var(--warn-bg); color: var(--warn); border-color: color-mix(in oklab, var(--warn) 30%, transparent); }
.hero h1 { font-family: "IBM Plex Serif", Georgia, serif; font-weight: 500; font-size: 64px; line-height: 1.02; margin: 18px 0 18px; letter-spacing: -0.02em; color: var(--ink); text-wrap: balance; }
.hero h1 em { font-style: normal; color: var(--neg); }
.hero h1 em.pos { color: var(--pos); }
.hero h1 em.warn { color: var(--warn); }
.hero .lead { font-size: 17.5px; line-height: 1.5; color: var(--ink-2); max-width: 58ch; }
.hero .lead b { color: var(--ink); font-weight: 600; }

.hero-side { border-left: 1px solid var(--rule); padding: 0 0 6px 28px; display: flex; flex-direction: column; gap: 18px; }
.hero-stat { display: flex; align-items: baseline; gap: 14px; }
.hero-stat .n { font-family: "IBM Plex Mono", monospace; font-size: 34px; font-weight: 500; color: var(--ink); letter-spacing: -0.01em; }
.hero-stat .n.warn { color: var(--warn); }
.hero-stat .n.neg { color: var(--neg); }
.hero-stat .n.pos { color: var(--pos); }
.hero-stat .label { font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-3); line-height: 1.35; }

.freshness { margin-top: 24px; background: var(--card-2); border: 1px solid var(--rule); border-radius: 4px; display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); font-family: "IBM Plex Mono", monospace; font-size: 11.5px; }
.fresh-cell { padding: 14px 18px; border-right: 1px solid var(--rule); }
.fresh-cell:last-child { border-right: none; }
.fresh-cell .k { font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3); margin-bottom: 6px; }
.fresh-cell .v { color: var(--ink); font-weight: 500; }
.fresh-cell .sub { color: var(--ink-3); font-size: 10.5px; margin-top: 2px; }

.nav { position: sticky; top: 0; z-index: 20; margin: 28px -28px 0; padding: 11px 28px; background: color-mix(in oklab, var(--paper) 92%, transparent); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); display: flex; align-items: center; gap: 20px; flex-wrap: wrap; font-family: "IBM Plex Mono", monospace; font-size: 11px; }
.nav .nav-label { color: var(--ink-3); letter-spacing: 0.14em; text-transform: uppercase; font-weight: 500; }
.nav a { color: var(--ink-2); border-bottom: none; letter-spacing: 0.02em; }
.nav a:hover { color: var(--accent); }
.nav .num { color: var(--ink-4); margin-right: 5px; }

section { margin-top: 14px; scroll-margin-top: 64px; }
.section-head { margin-bottom: 14px; padding-top: 14px; border-top: 2px solid var(--ink); }
.section-num { font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: 0.14em; color: var(--ink-4); margin-bottom: 10px; }
.section-head h2 { font-family: "IBM Plex Serif", Georgia, serif; font-weight: 500; font-size: 32px; line-height: 1.1; margin: 0 0 10px 0; letter-spacing: -0.015em; color: var(--ink); max-width: 28ch; }
.section-head .desc { font-size: 14.5px; color: var(--ink-2); max-width: 78ch; margin-top: 4px; line-height: 1.55; }

.card { background: var(--card); border: 1px solid var(--rule); border-radius: 4px; }
.callout { background: var(--hi-bg); border: 1px solid color-mix(in oklab, var(--hi) 45%, transparent); border-radius: 4px; padding: 18px 22px; font-size: 13.5px; color: var(--ink-2); line-height: 1.5; }
.callout b { color: var(--ink); }
.callout.pos { background: var(--pos-bg); border-color: color-mix(in oklab, var(--pos) 35%, transparent); }
.callout.warn { background: var(--warn-bg); border-color: color-mix(in oklab, var(--warn) 35%, transparent); }
.callout.neg { background: var(--neg-bg); border-color: color-mix(in oklab, var(--neg) 35%, transparent); }

.pill { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; font-family: "IBM Plex Mono", monospace; font-size: 10.5px; font-weight: 500; letter-spacing: 0.06em; text-transform: uppercase; border-radius: 2px; border: 1px solid; white-space: nowrap; }
.pill.pos  { color: var(--pos);  background: var(--pos-bg);  border-color: color-mix(in oklab, var(--pos) 25%, transparent); }
.pill.warn { color: var(--warn); background: var(--warn-bg); border-color: color-mix(in oklab, var(--warn) 30%, transparent); }
.pill.neg  { color: var(--neg);  background: var(--neg-bg);  border-color: color-mix(in oklab, var(--neg) 25%, transparent); }
.pill.hi   { color: #6e5410; background: var(--hi-bg); border-color: color-mix(in oklab, var(--hi) 50%, transparent); }
.pill.muted{ color: var(--ink-3); background: var(--paper-2); border-color: var(--rule); }

.diff { font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: 0.04em; }
.diff.neg { color: var(--neg); } .diff.pos { color: var(--pos); } .diff.warn { color: var(--warn); }

.sources { margin-top: 14px; font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--ink-3); line-height: 1.5; }
.sources::before { content: "Sources ·"; letter-spacing: 0.12em; text-transform: uppercase; font-weight: 500; color: var(--ink-4); margin-right: 6px; }

footer.dashboard-footer { margin-top: 80px; padding: 32px 24px 24px; border-top: 3px solid var(--ink); background: var(--card-2); display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 36px; font-family: "IBM Plex Mono", monospace; font-size: 13px; color: var(--ink); }
footer.dashboard-footer h5 { font-size: 11.5px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink); margin: 0 0 14px; font-weight: 700; border-bottom: 2px solid var(--ink); padding-bottom: 6px; }
footer.dashboard-footer a { color: var(--ink); font-weight: 500; text-decoration: underline; text-decoration-color: var(--ink-4); text-underline-offset: 3px; line-height: 1.9; }
footer.dashboard-footer a:hover { color: oklch(45% 0.18 240); text-decoration-color: oklch(45% 0.18 240); }
footer.dashboard-footer > div > div { line-height: 1.9; }

@media (max-width: 1100px) {
  .hero { grid-template-columns: 1fr; gap: 28px; }
  .hero-side { border-left: none; border-top: 1px solid var(--rule); padding: 28px 0 0; }
  .section-head { padding-top: 12px; }
  .section-head h2 { font-size: 26px; }
  .hero h1 { font-size: 44px; }
}
@media (max-width: 700px) {
  .shell { padding: 18px 16px 60px; }
  .nav { margin: 18px -16px 0; padding: 9px 16px; }
  .hero h1 { font-size: 34px; }
}
`;

/** Drop into <head> of each page for fonts + viewport + favicon hooks. */
export function DashboardHead({ title }: { title: string }) {
  return (
    <>
      <title>{title}</title>
      <meta name="viewport" content="width=device-width,initial-scale=1" />
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link
        href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Serif:wght@400;500;600&display=swap"
        rel="stylesheet"
      />
      <style dangerouslySetInnerHTML={{ __html: DASHBOARD_CSS }} />
    </>
  );
}

/** Newspaper-style topbar with logo + page-specific brand line + meta strip.
 * `brand` is the per-page identity — name the page for what it actually is,
 * not the internal company name. `logoSrc` defaults to /logo-icon.svg. */
export function Topbar({
  brand,
  region,
  renderedAt,
  build,
  logoSrc = "/logo-icon.svg",
}: {
  brand: string;
  region: string;
  renderedAt: string;
  build?: string;
  logoSrc?: string;
}) {
  return (
    <div className="topbar">
      <div className="brand">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={logoSrc} alt="" width={22} height={22} style={{ display: "block" }} />
        <div>{brand}</div>
      </div>
      <div className="sep" />
      <div className="meta">
        <span><span className="dot" />LIVE FEED</span>
        <span>RENDERED <b>{renderedAt}</b></span>
        <span>REGION <b>{region}</b></span>
        {build && <span>BUILD <b>{build}</b></span>}
      </div>
    </div>
  );
}

/** Shared dashboard footer with link columns. */
export function DashboardFooter({
  columns = [],
}: {
  columns?: Array<{ title: string; items: Array<{ label: string; href: string; external?: boolean }> }>;
}) {
  return (
    <footer className="dashboard-footer">
      {columns.map((col, i) => (
        <div key={i}>
          <h5>{col.title}</h5>
          {col.items.map((item, j) => (
            <div key={j}>
              <a href={item.href} target={item.external ? "_blank" : undefined} rel={item.external ? "noopener noreferrer" : undefined}>
                {item.label}
              </a>
            </div>
          ))}
        </div>
      ))}
    </footer>
  );
}

/** Standard cross-link footer used on all 4 public pages. */
export const DEFAULT_FOOTER_COLUMNS = [
  {
    title: "Public dashboards",
    items: [
      { label: "Southern Illinois Region (LWA-25)", href: "/southern-illinois" },
      { label: "East Central Illinois (LWA-23)", href: "/east-central-illinois" },
      { label: "Carbondale, IL", href: "/carbondale" },
      { label: "Murphysboro, IL", href: "/murphysboro" },
      { label: "Charleston, IL", href: "/charleston" },
      { label: "US Market Health", href: "/market" },
    ],
  },
  {
    title: "Authoritative sources",
    items: [
      { label: "BLS — Carbondale-Marion MSA", href: "https://www.bls.gov/regions/midwest/news-release/occupationalemploymentandwages_carbondale.htm", external: true },
      { label: "Census ACS data explorer", href: "https://data.census.gov/", external: true },
      { label: "USAspending.gov", href: "https://www.usaspending.gov/", external: true },
      { label: "Illinois workNet WIOA", href: "https://www.illinoisworknet.com/WIOA/Pages/PerformanceTransparency.aspx", external: true },
    ],
  },
  {
    title: "Build",
    items: [
      { label: "Operator console source (GitHub)", href: "https://github.com/michaeleasterly-cpu/short-term-trading-engine", external: true },
    ],
  },
];
