---
name: Pyrometer
description: A dark, warm-soot experiment dashboard where every run has a temperature: live runs glow on the blackbody scale and cool to steel when finished.
colors:
  soot-sunken: "#110e0b"
  soot-ground: "#17130f"
  soot-panel: "#1e1814"
  soot-raised: "#261f19"
  hairline-soft: "#30271f"
  hairline-strong: "#3f3329"
  ash-faint: "#6b5b4d"
  ash-muted: "#a8998a"
  ash-soft: "#d6c8b8"
  bone-text: "#f3e9dc"
  heat-0: "#3b0f0a"
  heat-1: "#7a2414"
  heat-2: "#b8361a"
  heat-3: "#e0521d"
  heat-4: "#f6b23c"
  heat-5: "#fbd36b"
  heat-6: "#fff1cf"
  steel-finished: "#8a97a6"
  failure-red: "#e5484d"
  improving-green: "#5fb87a"
typography:
  title:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.45
  heading:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.45
  body:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  small:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "0.08em"
  numeric:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  chip: "1px"
  control: "2px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.ash-soft}"
    textColor: "{colors.soot-ground}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: "28px"
    padding: "0 12px"
  button-primary-hover:
    backgroundColor: "{colors.bone-text}"
  button:
    backgroundColor: "{colors.soot-panel}"
    textColor: "{colors.ash-soft}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: "28px"
    padding: "0 12px"
  button-hover:
    backgroundColor: "{colors.soot-raised}"
    textColor: "{colors.bone-text}"
  input:
    backgroundColor: "{colors.soot-raised}"
    textColor: "{colors.bone-text}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 8px"
  status-badge:
    backgroundColor: "{colors.soot-panel}"
    textColor: "{colors.ash-soft}"
    typography: "{typography.small}"
    rounded: "{rounded.control}"
    padding: "2px 6px"
  panel:
    backgroundColor: "{colors.soot-panel}"
    textColor: "{colors.bone-text}"
    rounded: "{rounded.control}"
    padding: "16px"
  sidebar:
    backgroundColor: "{colors.soot-sunken}"
    width: "288px"
---

# Design System: Pyrometer

## Overview

**Creative North Star: "The Firing Log"**

A kiln operator's log book read in a dim room: warm soot surfaces, one stepped ramp of neutrals, and heat colour reserved for the data itself. Every run has a temperature. A live run glows on the blackbody scale; when it finishes it cools to steel; a failure goes red. The dashboard is dark-first, dense and quiet, built for a tab left open beside a terminal for hours.

Structure is drawn with one-pixel hairlines and corners of 2px or less. There are no shadows. Depth comes from stepping up the neutral ramp. Controls are plain and standard (tabs, buttons, selects, inputs, tables) so the only expressive things on screen are the heat colours and the firing curve.

**Key Characteristics:**
- Warm soot ground, never grey; every neutral is a step of one ramp.
- Blackbody ramp carries heat and data only, never chrome.
- Steel means finished, red means failed, a green trend means improving.
- Hairline borders, 2px radius, zero shadows.
- Archivo for UI, condensed uppercase for form-style labels, JetBrains Mono for numbers only.
- Dark only; a light theme is not built.

## Colors

A single warm neutral ramp with a hot-metal accent ramp used for data. Tokens are mirrored in `theme.ts`, `index.css` (`--n0`..`--n9`, `--heat-0`..`--heat-6`) and Tailwind (`py-*`).

### Primary (heat, data only)
- **Amber Heat** (#f6b23c, heat-4): the running state, the default single-series chart colour, focus outlines, caret and form accents. The one accent that appears in chrome, as a focus ring only.
- **Blackbody ramp** (#3b0f0a, #7a2414, #b8361a, #e0521d, #f6b23c, #fbd36b, #fff1cf): dull red to white-hot. The firing curve colours each slice by its own GPU value; running sidebar chips take their colour from the run's GPU heat; selection highlight uses heat-1 on heat-6.

### Secondary (state)
- **Cooled Steel** (#8a97a6): completed runs.
- **Failure Red** (#e5484d): failed or crashed runs, anomalies, anomaly cones, error counts.
- **Improving Green** (#5fb87a): a downward loss trend in the key-numbers row.

### Neutral
- **Sunken Soot** (#110e0b): sidebar and sunken areas.
- **Soot Ground** (#17130f): page ground.
- **Panel Soot** (#1e1814): panels, cards, badges, notes.
- **Raised Soot** (#261f19): hover fill, inputs, skeletons.
- **Soft Hairline** (#30271f): default borders and chart grid.
- **Strong Hairline** (#3f3329): control borders, chart axes.
- **Faint Ash** (#6b5b4d): disabled text, axis ticks, placeholders.
- **Muted Ash** (#a8998a): secondary text, killed status.
- **Soft Ash** (#d6c8b8): body-adjacent text, primary button fill, active tab underline.
- **Bone Text** (#f3e9dc): primary text.

Compare and kernel views use separate categorical sets (`runColors.ts`, `kernelColors.ts`) tuned to stay distinct from the heat ramp and from red.

### Named Rules
**The Heat Is Data Rule.** The blackbody ramp encodes values and state only. Buttons, borders, headings and backgrounds never take a heat colour.
**The One Ramp Rule.** Every neutral is one of the ten ramp steps. No ad-hoc greys.
**The Cooled Rule.** Finished is steel, failed is red; neither is ever shown in heat colours.

## Typography

**UI Font:** Archivo Variable (with Archivo, system-ui, sans-serif)
**Numeric Font:** JetBrains Mono (with ui-monospace, monospace)

**Character:** A grotesque UI face with a condensed width axis for small log-form labels, and a monospace reserved for values so columns of numbers align. Tabular numerals are on globally.

### Hierarchy
- **Title** (600, 22px, 1.45): page titles on Compare, Settings and the empty state.
- **Heading** (600, 15px, 1.45): section headings inside tabs.
- **Body** (400, 13px, 1.45): buttons, tabs, inputs, sidebar run names, notes. The working size.
- **Small** (400, 12px): metadata, status badges, secondary readouts.
- **Label** (600, 11px, condensed 80% width, 0.08em tracking, uppercase): field labels for controls and chart options (`.log-label`), muted ash.
- **Numeric** (JetBrains Mono, 10 to 12px): timestamps, steps, metric values, ids, chart ticks and tooltips.

### Named Rules
**The Numbers Are Mono Rule.** Values, steps, ages and ids use the `num` class; prose and labels never do.
**The Form Label Rule.** Condensed uppercase is for short control labels only, never for decorative eyebrows above headings.

## Layout

A fixed 288px run sidebar on the left; content to its right in a centred column (`max-w-6xl`, 24px padding). Below the 768px breakpoint the sidebar yields to a sticky top bar with a menu button. Charts sit in a single column that becomes two columns at the `lg` breakpoint with 16px gaps. The key-numbers readout is a row of cells divided by 1px gaps on the hairline colour (a 2-column grid on phones). Spacing is Tailwind's 4px scale in practice: 4, 8, 12, 16, 24. Density is high: controls are 28px tall, sidebar rows at least 44px.

The run page order is fixed: header, tabs, then the Overview tab's firing curve (110px tall), key numbers, and pinned charts. Visibility of charts and sections is configurable per project.

## Elevation & Depth

Flat by construction. There are no box shadows anywhere. Depth is tonal: sunken (#110e0b) for the sidebar, ground for the page, panel for containers, raised for hover and inputs, each separated by a hairline. Chart tooltips follow the same rule: panel fill, strong hairline, 2px radius.

### Named Rules
**The No Shadow Rule.** Never add a shadow. Step up the neutral ramp and add a hairline instead.

## Shapes

Near-square. Controls, panels, badges and inputs use a 2px radius; small chips and status squares use 1px. Tailwind's radius scale is overridden so every `rounded-*` resolves to 2px or less. The status marker is a small square, not a circle. Borders are always 1px; the only thicker line is the 2px active-tab underline. Scrollbars are 6px with a 2px thumb.

## Components

### Buttons
- **Shape:** 2px radius, 28px tall, 12px horizontal padding, 13px text, icon plus label with 6px gap.
- **Primary:** Soft Ash fill (#d6c8b8) with soot text, medium weight; one per view (Compare in the run header). Hover lifts to Bone Text.
- **Secondary:** Panel fill, strong hairline border, soft ash text; hover raises to Raised Soot with bone text.
- **Icon button:** 24px square, same secondary styling, 13px SVG icons (a custom `Icon` component).
- **Disabled:** 50% opacity, not-allowed cursor.

### Status Badge
A 8px square chip (1px radius) plus a sentence-case label in a hairline-bordered panel. Running uses amber heat and breathes (opacity 1 to 0.55 over 2.4s, off under reduced motion); completed is steel; failed and crashed are red; killed is muted ash. In the sidebar the running chip is tinted by the run's live GPU heat.

### Tabs
Standard tablist with arrow-key navigation. 36px tall, 13px text, 2px bottom border: Soft Ash and bone text when active, transparent and muted otherwise. Count badges are mono, red.

### Inputs / Fields
32px tall, Raised Soot fill, strong hairline border, 2px radius, bone text, faint ash placeholder. Focus is a 1px amber outline offset 2px (global `:focus-visible`); checkboxes, ranges and radios take amber as accent colour.

### Sidebar Run Row
Signature navigation item. Minimum 44px, 2px radius, transparent border at rest; the active row gets the strong hairline and raised fill. A checkbox, a 8x14px heat chip, the run name (bone when running, soft ash when not) and a mono age line with tags. Selected runs fill the checkbox with their compare colour.

### Firing Curve
The memorable component: full-width, 110px, GPU utilisation drawn as 300 thin vertical slices each coloured by its own value on the blackbody ramp. Bent cone marks above it show events; anomalies are red. Hover shows a hairline with step, GPU % and nearest event; drag or click selects a time window and jumps the Metrics tab to it.

### Key Numbers
A bordered row of cells separated by 1px hairlines: a condensed label, a 20px mono value and a sub line. Loss turns green when falling; anomalies turn red when above zero.

### Charts
Recharts with shared styling from `theme.ts`: soft hairline grid, strong hairline axes, 10px mono muted ticks, panel-coloured tooltips. A single series is amber; compare views use the categorical run colours.

## Do's and Don'ts

### Do:
- **Do** take every neutral from the ten-step ramp and reference it through the `py-*` tokens or CSS variables.
- **Do** keep heat colours for values, run liveness and the firing curve.
- **Do** separate surfaces with 1px hairlines and a step on the neutral ramp.
- **Do** keep radius at 2px or less.
- **Do** set numbers in JetBrains Mono via `num`, and keep UI text at 13px.
- **Do** use 150 to 200ms transitions for state changes, and honour `prefers-reduced-motion`.
- **Do** keep theme values in sync across `theme.ts`, `index.css` and `tailwind.config.js`.

### Don't:
- **Don't** use a shadow, glow or blur for depth.
- **Don't** use heat colours for buttons, borders, tabs or headings.
- **Don't** introduce a grey that is not a ramp step, or a blue accent for chrome.
- **Don't** use steel or red for anything but finished and failed.
- **Don't** add page-load choreography.
- **Don't** use a rounded pill or circle for status; status is a square chip.
