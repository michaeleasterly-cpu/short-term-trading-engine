Design-reference artifacts. Not part of the build.

`southern-illinois-dashboard-scaffold.html` — the static HTML scaffold that
defines the visual system used across the public dashboard pages
(`/southern-illinois`, `/carbondale`, `/murphysboro`, `/market`).

The live design system is in `console/src/components/dashboard-chrome.tsx`
(`DASHBOARD_CSS` + `<DashboardHead>` + `<Topbar>` + `<DashboardFooter>`).
Update the live system when you adjust typography, color palette, or
structural components — the scaffold is kept here for reference and
side-by-side comparison only.

The scaffold's *example content* (hero copy, eyebrow text, weather words)
is illustrative; it does not transfer to the live pages. Live page copy
follows the data-first / no-judgment-headlines rule.
