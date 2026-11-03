# Design System: Artemis

## Overview

**Creative North Star: "Practical workspace, warm companion"**

Artemis is a practical native Windows workspace with warm Turkish companion language. Slate and blue establish clear surfaces, compact controls and readable task areas. The selected existing pet sits beside the Artemis brand; the configurable wake name never replaces the app identity.

The system uses PySide6 widgets and semantic Qt styles. Four selectable palettes recolor the same structure. The current default is Açık ağırlıklı (light-mixed).

**Key Characteristics:**
- Native Qt controls and Segoe UI
- Four semantic palettes with independent navigation roles
- Flat bordered surfaces and restrained blue emphasis
- Existing pet imagery as the companion signature

Source authority: [PRODUCT.md](PRODUCT.md), [surface contract](.impeccable/surfaces/artemis-channels-desktop-app-py.md), and shipped native sources below. This scan records implementation, not planned features. Machine-readable normative tokens are in the JSON block below instead of YAML frontmatter to preserve vault parser compatibility. Component token references show the default palette; substitute the active theme prefix as one set.

## Colors

Warm neutrals (cream/bone, warm ink) with a turquoise accent provide calm structure; `accent_soft` is the gentle accent tint for user bubbles and soft selected fills. Primary uses `accent` and `accent_text` for actions and selection. Neutral roles are `bg`, `surface`, `soft`, `text`, `muted`, and `border`. Navigation has its own `nav`, `nav_text`, `nav_muted`, and `nav_selected` roles. `warning`, `error`, and `success` carry status meaning. All four complete source palettes follow; no synthetic tonal ramps are shipped.

- **Açık / light:** pale workspace and a slightly deeper cream navigation (≥1.15:1 against the workspace).
- **Koyu / dark:** dark workspace and a slightly lighter warm navigation (≥1.15:1 against the workspace).
- **Açık ağırlıklı / light-mixed:** light workspace and slate navigation; current default.
- **Koyu ağırlıklı / dark-mixed:** dark workspace and pale navigation.

**The Semantic Pair Rule.** Switch foregrounds and backgrounds together; navigation uses its own paired roles.

```json
{
  "name": "Artemis",
  "description": "Native Windows workspace with warm companion language",
  "colors": {
    "light-bg": "#f6f1e9",
    "light-surface": "#fffdf9",
    "light-soft": "#efe8dc",
    "light-text": "#2a2420",
    "light-muted": "#6b5f55",
    "light-border": "#e2d8c8",
    "light-accent": "#0f7f7a",
    "light-accent-text": "#ffffff",
    "light-accent-soft": "#d9efec",
    "light-nav": "#e8dfcf",
    "light-nav-text": "#2a2420",
    "light-nav-muted": "#5f544a",
    "light-nav-selected": "#dacdb7",
    "light-warning": "#8a5a0b",
    "light-error": "#b3392f",
    "light-success": "#1f6d4b",
    "dark-bg": "#16120f",
    "dark-surface": "#201b17",
    "dark-soft": "#2a2420",
    "dark-text": "#f0e9df",
    "dark-muted": "#b3a698",
    "dark-border": "#3b332c",
    "dark-accent": "#5fd0c5",
    "dark-accent-text": "#0d2926",
    "dark-accent-soft": "#1f3a37",
    "dark-nav": "#292119",
    "dark-nav-text": "#f0e9df",
    "dark-nav-muted": "#b3a698",
    "dark-nav-selected": "#3d332a",
    "dark-warning": "#f2c46d",
    "dark-error": "#ff9d8f",
    "dark-success": "#8fd7ae",
    "light-mixed-bg": "#f6f1e9",
    "light-mixed-surface": "#fffdf9",
    "light-mixed-soft": "#efe8dc",
    "light-mixed-text": "#2a2420",
    "light-mixed-muted": "#6b5f55",
    "light-mixed-border": "#e2d8c8",
    "light-mixed-accent": "#0f7f7a",
    "light-mixed-accent-text": "#ffffff",
    "light-mixed-accent-soft": "#d9efec",
    "light-mixed-nav": "#2b231d",
    "light-mixed-nav-text": "#f4ece1",
    "light-mixed-nav-muted": "#cdbfae",
    "light-mixed-nav-selected": "#43382f",
    "light-mixed-warning": "#8a5a0b",
    "light-mixed-error": "#b3392f",
    "light-mixed-success": "#1f6d4b",
    "dark-mixed-bg": "#16120f",
    "dark-mixed-surface": "#201b17",
    "dark-mixed-soft": "#2a2420",
    "dark-mixed-text": "#f0e9df",
    "dark-mixed-muted": "#b3a698",
    "dark-mixed-border": "#3b332c",
    "dark-mixed-accent": "#5fd0c5",
    "dark-mixed-accent-text": "#0d2926",
    "dark-mixed-accent-soft": "#1f3a37",
    "dark-mixed-nav": "#e8dfcf",
    "dark-mixed-nav-text": "#2a2420",
    "dark-mixed-nav-muted": "#5f544a",
    "dark-mixed-nav-selected": "#dacdb7",
    "dark-mixed-warning": "#f2c46d",
    "dark-mixed-error": "#ff9d8f",
    "dark-mixed-success": "#8fd7ae"
  },
  "typography": {
    "greeting": {
      "fontFamily": "Segoe UI Variable Text, Segoe UI",
      "fontSize": "28px",
      "fontWeight": 650
    },
    "headline": {
      "fontFamily": "Segoe UI Variable Text, Segoe UI",
      "fontSize": "22px",
      "fontWeight": 650
    },
    "body": {
      "fontFamily": "Segoe UI Variable Text, Segoe UI",
      "fontSize": "14px"
    },
    "label": {
      "fontFamily": "Segoe UI Variable Text, Segoe UI",
      "fontSize": "13px",
      "fontWeight": 600
    }
  },
  "rounded": {
    "control": "8px",
    "surface": "12px",
    "pet-card": "14px",
    "chip": "8px",
    "user-bubble": "14px",
    "progress": "5px"
  },
  "spacing": {
    "field": "8px",
    "component-gap": "10px",
    "content-gap": "12px",
    "shell-gap": "16px",
    "workspace-gap": "18px",
    "outer": "24px"
  },
  "components": {
    "button-primary": {
      "backgroundColor": "{colors.light-mixed-accent}",
      "textColor": "{colors.light-mixed-accent-text}",
      "rounded": "{rounded.control}",
      "padding": "8px 13px"
    },
    "button-secondary": {
      "backgroundColor": "{colors.light-mixed-soft}",
      "textColor": "{colors.light-mixed-text}",
      "rounded": "{rounded.control}",
      "padding": "8px 13px"
    },
    "input": {
      "backgroundColor": "{colors.light-mixed-surface}",
      "textColor": "{colors.light-mixed-text}",
      "rounded": "{rounded.control}",
      "padding": "8px"
    },
    "navigation": {
      "backgroundColor": "{colors.light-mixed-nav}",
      "textColor": "{colors.light-mixed-nav-text}",
      "rounded": "{rounded.control}",
      "padding": "10px 12px",
      "width": "166px"
    },
    "surface": {
      "backgroundColor": "{colors.light-mixed-surface}",
      "textColor": "{colors.light-mixed-text}",
      "rounded": "{rounded.surface}"
    }
  }
}
```

## Typography

Segoe UI Variable Text (fallback Segoe UI) is the shared family. Body is 14px, labels 13px, captions/meta/hints 12px (nothing smaller), greeting 28px and page titles 22px at weight 650, section titles 16px at 600, and status labels are weight 600. Primary buttons use weight 650. Small tool/status details include 12px overrides; these are local supporting text, not a second display scale. Line height and letter spacing are not explicitly tokenized in Qt source.

## Layout

The shell has outer margins (24px left/right, 20px top, 22px bottom), a 16px vertical gap, a compact header, and a fixed 166px left navigation separated from the workspace by 18px. Navigation margins are 12px horizontal and 18px vertical, with 6px gaps. The current minimum main window is 720 by 560px; no web or mobile breakpoints are defined.

Header padding is 18px horizontal and 15px vertical, with 14px gaps. The selected pet occupies 42 by 44px. Conversation padding is 18px horizontal and 16px vertical with 12px gaps; the optional steps splitter starts at 740:390 with 65:35 stretch. The bottom composer has 12px horizontal and 10px vertical padding and an 8px gap; its editor spans 38–56px height.

Settings has a 24px inset and 10px gaps. Each section owns its scrolling region; theme choice is separate and applies immediately. Pet gallery cells are 156 by 92px with 40 by 44px icons, 4px spacing and a 120px list height cap. The workshop places description and chat beside a 240 by 260px preview at a 2:1 stretch, separated by 24px.

## Elevation & Depth

Shared surfaces are flat: tonal separation and 1px semantic borders establish hierarchy. The shared stylesheet defines no shadows, gradients or glows. No new animation timing is established here. Existing pet playback remains controlled by pet_sprite.FPS; this document does not redefine it.

## Shapes

Controls and fields have 8px corners. Cards, sidebar and composer surfaces use 12px; pet gallery cards 14px; chips 8px; user bubbles 14px; tabs/tool buttons 8px; progress bars 5px. These are distinct implemented roles, not a universal pill shape.

## Components

### Buttons

Secondary buttons use soft background, semantic text, a 1px border, 8px by 13px padding and a 22px minimum content height. Primary buttons use accent/accent_text and weight 650. Hover changes the border to accent; keyboard focus uses a 2px accent border. Danger buttons use the error role for text and border. Disabled button text becomes muted. No extra hover movement is defined.

### Inputs / Fields

Native line edits, text edits, lists, combos and spin boxes use surface/text, a 1px border, 8px corners and 8px padding. Text selection pairs accent with accent_text. Explicit 2px accent focus is defined for line edits, plain text edits and combos. Disabled combos use muted text. Do not infer unimplemented validation states.

### Cards / Containers

Cards, raised containers and blocks share surface, a 1px border and 12px corners. Raised is a semantic name, not a shadow. Conversation and activity containers use the measured Layout spacing above.

### Navigation

Left aligned native checkable buttons use navigation foreground/background and 10px by 12px padding. Checked and hover states share nav_selected. Settings sits below a stretch. Routes are Sohbet, Projeler, Petler, Geçmiş and Ayarlar.

### Header state and companion

Model name and microphone state stay visible next to the brand, voice toggle, automatic approval toggle and execution status. State text accompanies colored dots. Preserve the existing glyph indicators as implementation evidence, not a new reusable icon system.

### Pet gallery and workshop

The icon gallery shows installed assets, their source and selection. Selection, import and delete remain explicit actions. Workshop method/model selection, request chat, preview state, animation generation/cancel and save remain native controls. Theme switching retains the existing draft; no new imagery is shipped by the visual system.

## Do's and Don'ts

### Do:
- **Do** apply semantic workspace and navigation colors together when switching themes.
- **Do** keep Artemis as the app brand and show the selected pet beside it.
- **Do** use 8px control corners, 12px surface corners and the extracted typography.
- **Do** preserve existing pet assets and their established interactions.

### Don't:
- **Don't** introduce gradients or glows into this workspace.
- **Don't** infer a web migration or replace native widgets with HTML previews.
- **Don't** invent extra motion, new pet artwork or responsive breakpoints from this document.

Evidence: [themes.py](artemis/channels/desktop/themes.py), [app.py](artemis/channels/desktop/app.py), [settings_panel.py](artemis/channels/desktop/settings_panel.py), [pet_creator_panel.py](artemis/channels/desktop/pet_creator_panel.py), [pet_section.py](artemis/channels/desktop/pet_section.py). Screenshots are in reports/native-redesign/.

Not canonized: the uppercase selected-model eyebrow, glyph status dots and isolated hardcoded colors/legacy overlay styling are implementation drift, not rules for future surfaces. This documentation pass does not repair them. High-DPI/screen-reader behavior and live wake-name recognition remain manual acceptance.

## Redesign contract (2026-10-27, shipped)

Umut approved: keep structure (palettes, four themes, Segoe UI, left navigation) and raise craft everywhere. This section is implemented in the desktop sources and overrides the older measured values above where they conflict (those describe the pre-pass build). Evidence: `reports/native-redesign/before/` vs `final/` (synthetic demo captures, 4 themes × 1280×850 and 900×700). Shared helpers: `projects_panel.make_page_header`, `EmptyState`, `AutoHideLabel`; theme-colored SVG arrows and radio indicator next to `themes.py`. Open: pet gallery still uses list tiles rather than `galleryCard` frames; live Windows/High-DPI check pending.

### Scale
- **Spacing:** 4, 8, 12, 16, 24, 32. No other margins/gaps (no 6, 10, 14, 15, 18, 22).
- **Type:** 12 caption/meta, 13 secondary/label, 14 body, 16 section heading (600), 22 page title (650). Never uppercase eyebrows; sentence case everywhere.
- **Control height:** 32px standard (buttons, line edits, combos, spin boxes), 36px primary call-to-action and composer send. Multi-line editors start at 72px.
- **Radius:** 8px controls, 12px surfaces/cards, 14px pet gallery cards (updated 2026-10-28). Nothing pill-shaped except the status dot.
- **Shell:** outer margin 16px, shell gaps 16px, sidebar 176px with 12px inner padding, page content inset 24px, page title row 48px tall.

### Layout rules
- Every page: title row (22px title left, page actions right) then content. One scroll region per page, never nested.
- Labels sit above their field (13px, `muted`, 4px gap). No right-aligned `QFormLayout` label column. Checkbox rows use the label as checkbox text plus an optional 12px muted hint below.
- Group related controls in a `card` (surface, 1px border, 12px radius, 16px padding, 12px internal gap) with a 16px section heading. Cards stack with 16px gap.
- Buttons size to content (min width 88px). Full-width buttons only inside narrow side columns. Button rows are right-aligned, primary last.
- Max readable width for forms: 720px; extra width stays as margin, not stretched fields.
- At 900px window width nothing clips: header items elide or collapse, side panels hide.

### States
- Hover: border `accent` for secondary controls; nav items `nav_selected` at reduced emphasis.
- Focus: 2px `accent` border (Qt cannot draw outline offset; keep padding compensated by 1px so focus does not shift layout).
- Selected list rows: `nav_selected`-like tint in workspace = `soft` background + 2px `accent` left edge + `text` color. Never solid accent fill on list rows.
- Disabled: `muted` text, unchanged background, no hover.
- **Empty:** `emptyState` frame, centered 16px heading + 13px muted sentence + at most one action button. Distinguish "not loaded yet" (action: Yükle/Yenile) from "loaded, empty" (no action, plain sentence).
- **Error:** `error` text 13px under the relevant control with a recovery hint.

### Components
- **Top bar (`topbar`):** 56px tall card. Left: pet 32px + "Artemis" 16px/650. Center-left: model chip (muted 12 "Model" + 13 name). Right: one voice control (button showing dot + "Ses kapalı/Dinliyor", toggles voice), auto-approve toggle, status pill (`statusPill`: dot + text). DEMO badge = small `warning` chip. No duplicated voice text.
- **Nav item:** 36px tall, 12px horizontal padding, 13-14px text, checked = `nav_selected` + 600 weight. Bottom: Ayarlar then a compact theme toggle (light/dark, 32px).
- **Chat:** messages max 680px wide. User message right-aligned `soft` bubble; assistant left, plain on surface with 12px muted "Artemis" meta. Step blocks compact rows (status glyph + text, 13px), never overlapping. Composer card at bottom: editor + right-aligned İptal (secondary, only enabled while busy) and Gönder (primary 36px).
- **Step panel (`stepPanel`):** right column 280px on windows ≥1100px wide: 16px heading "Son işlem", rows with 12px time + 13px text, status color only on glyph. Hidden below 1100px.
- **Section list (`sectionList`):** settings left column 200px, 36px rows, same selected treatment as list rows.
- **Gallery card (`galleryCard`):** 140×120, pet image 64px centered, 13px name, 12px muted source; selected = 2px `accent` border + small "Seçili" chip.
- **Stage bar (`stageBar`):** horizontal steps Tarif → Görsel → Animasyon → Kaydet; current step `accent` text + 2px underline, done steps `success` check, future `muted`.
- **Dialogs:** 24px padding, 22px title, plain-language summary first, technical detail in a collapsible area, buttons right-aligned, safe default focused.

## Warm companion direction (2026-10-28, supersedes conflicting rules above)

Umut: the UI "looks too robotic". No voice orb / waveform / audio presence (voice is rarely used in the UI; the pet already is the companion). The selected pet is the face of the app and reuses its existing state texts ("Seni dinliyor", "Konuşuyor"); no new voice widget.

- **Palette:** warm neutrals replace corporate slate-blue. Light: cream/bone workspace, warm white surfaces. Dark: warm ink, never blue-black. Accent = the existing turquoise status colour; secondary = amber (approval, warnings). Still flat: tonal surfaces, no gradients, no glows. This replaces "restrained blue emphasis" and "Don't introduce gradients" only in the sense of colour: the no-gradient/no-glow rule stays.
- **Type:** Segoe UI Variable where available, falling back to Segoe UI. Page greeting 28/650, page title 22/650, section 16/600, body 14, label 13, caption 12 minimum (no 11px, help text uses `muted` with AA contrast on its surface).
- **Shape:** 12px surfaces, 8px controls, 14px pet cards. Fewer borders: separate by tone first, 1px border only where tone is not enough. No box inside a box inside a box: Settings sections are flat blocks with a heading, not nested cards.
- **Presence:** empty chat shows the selected pet large (96px) + time-based greeting + three real example commands; assistant messages carry the small pet avatar; header pet stays.
- **Nav:** themed SVG icons + label, soft selected pill. **Header:** pet + "Artemis", model chip, one status dot, voice and auto-approve as tooltip icon toggles.
- **Motion:** hover/focus transitions only. Pet frame rate is 6 fps (Umut, 2026-10-28; was 5).
- **Contract that stays:** spacing scale, labels above fields, one scroll region per page, empty/error state patterns, safe-default dialogs.
