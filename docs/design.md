# Dashboard design

The dashboard uses neutral surfaces, platform typography, blue actions, and explicit status labels. It observes agent activity; payment approval stays on the Watch. The implementation lives in [styles.css](../frontend/src/styles.css) and [landing.css](../frontend/src/landing.css).

## Visual language

- Use the platform system font stack and tabular numerals for money and time.
- Separate white panels with borders and spacing. Avoid decorative gradients, display fonts, and nested floating cards.
- Pair every status color with a written label. Preserve the distinction between sample data, live data, unavailable resources, and retained snapshots.
- Reserve strong blue emphasis for actions, selected navigation, and important approval context.

## Color roles

| Role | Value / behavior |
| --- | --- |
| Primary action | `#0866e6`; hover `#0056c7` |
| Canvas | `#f6f7f9` |
| Sidebar | `#f1f3f6` |
| Surface | `#ffffff` |
| Main text | `#20242d` |
| Secondary text | `#59677a` |
| Borders | `#e9ebef`; stronger controls use `#dce1e8` |
| Positive | Green text with a pale green surface and a written state |
| Waiting | Amber text with a pale amber surface and a written state |
| Approval | Pale blue callout with a link to order details |

Use the CSS tokens as the source of truth when changing a component.

## Typography and layout

Body text uses a 1.5 line height; headings use tighter spacing. Money and time use tabular numerals. Preserve readable labels, visible keyboard focus, and wrapping for long activity metadata.

The desktop shell has a 240px sidebar, a 76px toolbar, and a centered workspace capped at 1510px. The overview combines a flexible main column with a 310px secondary column and 24px gaps. Density tightens at 1190px; the overview stacks at 1000px. At 760px, the sidebar gives way to five-item bottom navigation with safe-area padding. Secondary panels stack at 430px.

Dialogs stay within the viewport and scroll internally. Approval callouts open details; they do not authorize payments. Search, status filtering, and empty states should remain usable at narrow widths.

## Controls and motion

Buttons use bordered secondary or solid blue primary styles. Keep visible focus outlines, disabled states, and phone touch targets of at least 44px. Panels are flat; use elevation for dialogs and overlays.

Button feedback is brief, with subtle press scaling. Dialog entry uses a small movement and fade. Respect reduced-motion, reduced-transparency, and increased-contrast preferences. Color must never be the only signal for state.

## Verification

Run the [frontend browser checks](../frontend/README.md#verify) after layout changes. They cover five screens at five widths, viewport overflow, order dialogs, filtering, demo replays, and authentication boundaries. Generated captures belong in `frontend/test-results/ui/`; curated README screenshots belong in `docs/media/`.
