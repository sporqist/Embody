---
description: "Skill loading requirements before MCP tool calls -- must load the relevant skill BEFORE acting"
---

# Skill Prerequisites

Skills are prerequisites, not optional reference. **Load the relevant skill BEFORE acting:**

The default Envoy surface is codemode (`code_mode` / `describe` / `view`); these skills load before those tools, keyed to the ACTION you are about to take. Verb-tool names (from full mode, `SetToolSurface('full')`) stay in parens as the full-mode trigger for the same action.

| Before this action | Load skill |
|---|---|
| Building operators (`tk.make` in `code_mode`; `create_op`) | `/create-operator` |
| Creating or querying annotations (native `parent.create(annotateCOMP)` in `code_mode`; `create_annotation` / `set_annotation`) | `/manage-annotations` |
| Creating an extension (native in `code_mode`; `create_extension`) | `/create-extension` |
| Externalizing an operator (`tk.externalize` in `code_mode`; `externalize_op` / `save_externalization`) | `/externalize-operator` |
| Writing TD Python (`code_mode` code; `execute_python`, `set_dat_content`, `edit_dat_content` in full mode) | `/td-api-reference` |
| Fetching data over HTTP, or any background / long-running / blocking task | `/td-api-reference` (Background and Long-Running Work) |
| Recording, exporting, or batch-encoding any movie or image sequence | `/movie-export` |
| Creating or designing custom parameters on any COMP | `/parameter-design` |
| Connectivity broken beyond ~15s of self-heal waiting | `/td-recovery` |
| The moment a `_peers` advisory or a second session appears | `/multi-session-etiquette` |
| Diagnosing operator errors (`tk.errors` / `code_mode` auto-settle diagnostics; `get_op_errors`) | `/debug-operator` |
| Building or refining any visual / rendered output (generative art, VJ visuals, shaders, scenes, renders, anything shown on screen) -- including `view`-ing a TOP (`capture_top`) | `/visual-aesthetics` |
| Creating or editing POP operators, particle systems, GPU point/geometry work, glslPOP compute, or converting SOP chains to POPs | `/pop-networks` |
| Building or styling a TD panel UI (dialog, wizard, HUD, control panel, buttons/text) | `/build-ui` (design system) + `rules/td-ui.md` (mechanics) |
| Multi-instance workflows (`switch_instance`) | `/multi-instance` |
| Building or persisting a Specimen (gallery TDN networks) | `/specimen-authoring` |
| First MCP call in a new session | `/mcp-tools-reference` |
