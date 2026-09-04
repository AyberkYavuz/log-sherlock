/**
 * Tailwind theme for the LogSherlock UI — the "Obsidian & Purple" palette.
 *
 * The three obsidian steps are a depth scale rather than a colour ramp: 950 is
 * the canvas, 900 is anything raised off it (panels, cards, table rows), and
 * 800 is the line between two surfaces. Keeping them as one named scale is what
 * lets `bg-obsidian-900` and `border-obsidian-800` read as the same system.
 *
 * The severity names match the vocabulary the backend already speaks — the
 * graph reports ERROR / WARN / INFO levels and info / warning / critical
 * anomaly tiers — so a component can map a payload value onto a class without
 * inventing a second vocabulary in between.
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        obsidian: {
          950: '#0B0F17', // canvas background
          900: '#111827', // panel / card surface
          800: '#1F2937', // borders and dividers
        },
        brand: {
          purple: '#8B5CF6', // accent highlights
          violet: '#7C3AED', // buttons, pressed / hover states
        },
        severity: {
          error: '#F87171',
          warn: '#FBBF24',
          success: '#34D399',
          info: '#38BDF8',
          muted: '#6B7280',
        },
      },
    },
  },
  plugins: [],
}
