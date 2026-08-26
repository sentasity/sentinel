import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import mermaid from 'astro-mermaid';
import { sidebar } from './sidebar.config.mjs';

export { sidebar };

// https://astro.build/config
export default defineConfig({
  site: 'https://sentasity.github.io',
  base: '/sentinel/',
  integrations: [
    // Must come before starlight: it registers the rehype plugin that turns
    // ```mermaid fences into client-rendered diagrams, and that plugin has to run
    // before Starlight's markdown processing. autoTheme binds the diagram theme to
    // Starlight's data-theme attribute, so diagrams follow the light/dark toggle.
    mermaid({
      theme: 'dark',
      autoTheme: true,
      mermaidConfig: {
        // Set in Mermaid's own config, not in CSS: Mermaid measures label boxes
        // with this font during layout, so a CSS override afterwards makes labels
        // wider than their pre-sized foreignObjects and clips the text.
        fontFamily: '"IBM Plex Mono", monospace',
        themeVariables: {
          fontSize: '14px',
          // Mermaid otherwise paints a light edge-label chip through an ID-scoped
          // !important rule a class selector cannot override. Transparent here so
          // mermaid.css can draw its own token-based chip.
          edgeLabelBackground: 'transparent',
        },
        flowchart: { curve: 'basis' },
      },
    }),
    starlight({
      title: 'Sentinel',
      favicon: '/favicon.ico',
      description:
        'Unattended investigation of Sentry issues, with optional autofix pull requests.',
      logo: {
        src: './src/assets/perch.svg',
        alt: 'Sentinel',
      },
      components: {
        Footer: './src/components/Footer.astro',
      },
      // A docs site with no way to correct it reads as a brochure. Both of these are
      // conventions readers of open-source documentation look for.
      editLink: {
        baseUrl: 'https://github.com/sentasity/sentinel/edit/main/website/',
      },
      lastUpdated: true,
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/sentasity/sentinel',
        },
      ],
      head: [
        {
          tag: 'meta',
          attrs: {
            property: 'og:image',
            content: 'https://sentasity.github.io/sentinel/social-card.png',
          },
        },
        {
          tag: 'meta',
          attrs: {
            name: 'twitter:image',
            content: 'https://sentasity.github.io/sentinel/social-card.png',
          },
        },
        { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
      ],
      customCss: [
        '@fontsource/outfit/500.css',
        '@fontsource/outfit/600.css',
        '@fontsource/outfit/700.css',
        '@fontsource/ibm-plex-sans/400.css',
        '@fontsource/ibm-plex-sans/500.css',
        '@fontsource/ibm-plex-sans/600.css',
        '@fontsource/ibm-plex-mono/400.css',
        '@fontsource/ibm-plex-mono/500.css',
        './src/styles/theme.css',
        './src/styles/mermaid.css',
      ],
      sidebar,
    }),
  ],
});
