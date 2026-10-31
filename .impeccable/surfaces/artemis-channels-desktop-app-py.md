---
version: 1
slug: "artemis-channels-desktop-app-py"
primary_target: "artemis/channels/desktop/app.py"
related_targets: ["artemis/channels/desktop/themes.py","artemis/channels/desktop/settings_panel.py","artemis/channels/desktop/pet_creator_panel.py"]
---

## Scope
Native Windows Operate surface: Artemis desktop shell, settings, pet workshop, four themes. Existing voice/tool/safety behavior is preserved.

## Direction contract
THESIS: A restrained working area with warm Artemis language; daily work is organized by task instead of accumulating top-level tabs and nested settings.
OWN-WORLD: Slate/blue semantic palettes, Segoe UI 14px, 22px headings, 7px controls, 10px surfaces, no gradients or glows. Four palettes cover light/dark and opposing navigation surfaces.
STORY: User opens chat, sees the active model and voice state, gives a command and inspects its steps when needed. Pet creation is an independent workspace; technical setup stays in settings.
FIRST VIEWPORT: Selected pet left of immutable Artemis brand; compact header, 166px left navigation, spacious chat, bottom composer. Settings has one scrolling content region and a separate immediate theme choice.
FORM: User-pinned hybrid: practical workspace with warm companion language, chosen after two functional comparison sketches. seed:41adfa01b09e60b7. Brand stays Artemis; configurable voice wake name is independent. Signature interaction: theme switches recolor navigation and workspace while preserving the current chat and pet draft.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Evidence and open scope
Current screenshots: reports/native-redesign/. Native palette contrast and theme/draft continuity tests. No new shipped raster; selected pets are existing user assets, demo captures are synthetic. Live recognition of a newly selected wake name and high-DPI screen reader testing remain manual acceptance, not claimed by unit tests.
