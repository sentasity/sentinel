// Sidebar configuration: single source of truth.
// Imported by both astro.config.mjs (Starlight integration) and
// scripts/check-sidebar.mjs (consistency checker). Kept in a separate file so the
// checker can import the sidebar without loading Astro/Starlight, which exposes
// TypeScript entry points Node cannot evaluate directly.
//
// Every entry is an explicit link. No autogenerate: a new page should be invisible
// to the sidebar until somebody adds it on purpose, and check-sidebar.mjs turns
// that into a build failure in both directions.
export const sidebar = [
  {
    label: 'Understand',
    items: [
      { label: 'What Sentinel does', link: '/what-sentinel-does/' },
      { label: 'How it works', link: '/how-it-works/' },
      { label: 'Security model', link: '/security-model/' },
    ],
  },
  {
    label: 'See it work',
    items: [{ label: 'See it work', link: '/evidence/' }],
  },
  {
    label: 'Deploy',
    items: [
      { label: 'What you need first', link: '/deploy/prerequisites/' },
      { label: 'Microsoft setup', link: '/deploy/microsoft/' },
      { label: 'Sentry setup', link: '/deploy/sentry/' },
      { label: 'Configure and deploy', link: '/deploy/configure/' },
    ],
  },
  {
    label: 'Operate',
    items: [
      { label: 'Runbooks', link: '/operate/runbooks/' },
      { label: 'Architecture reference', link: '/operate/architecture/' },
      { label: 'Contributing', link: '/operate/contributing/' },
    ],
  },
];
