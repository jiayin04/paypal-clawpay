---
name: "ClawPay dashboard"
description: "A calm system-app companion for watch-first shopping activity."
colors: {"action": "#0866e6", "action-hover": "#0056c7", "canvas": "#f6f7f9", "sidebar": "#f1f3f6", "surface": "#ffffff", "ink": "#20242d", "muted": "#59677a", "line": "#e9ebef", "panel-border": "#e3e7ed", "control-border": "#dce1e8", "control-ink": "#414b5b", "control-hover": "#f1f5fa", "nav-ink": "#626d7e", "nav-hover": "#e9edf2", "nav-selected": "#e0ebfc", "nav-selected-ink": "#085ecb", "approval-surface": "#eaf2ff", "approval-heading": "#24558d", "approval-copy": "#40638e", "approval-action": "#175bb0", "positive": "#21734c", "positive-surface": "#eaf6ef", "waiting": "#946016", "waiting-surface": "#fff2dc", "neutral": "#606c7b", "neutral-surface": "#f0f2f5", "reserved": "#9ec0f6", "available": "#edf0f4", "banner-surface": "#edf2f8", "banner-copy": "#52647d"}
typography: {"headline": {"fontFamily": "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif", "fontSize": "32px", "fontWeight": 680, "lineHeight": 1.18, "letterSpacing": "-0.035em"}, "title": {"fontFamily": "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif", "fontSize": "16px", "fontWeight": 650, "lineHeight": 1.35, "letterSpacing": "-0.018em"}, "body": {"fontFamily": "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif", "fontSize": "14px", "lineHeight": 1.5}, "label": {"fontFamily": "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif", "fontSize": "12px", "fontWeight": 580}, "metric": {"fontFamily": "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif", "fontSize": "35px", "fontWeight": 650, "lineHeight": 1.35, "letterSpacing": "-0.035em"}}
rounded: {"status": "5px", "control": "8px", "inset": "10px", "callout": "12px", "panel": "14px", "dialog": "16px"}
spacing: {"4": "4px", "8": "8px", "12": "12px", "16": "16px", "20": "20px", "24": "24px", "32": "32px", "40": "40px"}
components: {"button-primary": {"backgroundColor": "{colors.action}", "textColor": "{colors.surface}", "rounded": "{rounded.control}", "padding": "10px 14px", "typography": "{typography.label}"}, "button-primary-hover": {"backgroundColor": "{colors.action-hover}"}, "button-secondary": {"backgroundColor": "{colors.surface}", "textColor": "{colors.control-ink}", "rounded": "{rounded.control}", "padding": "10px 14px", "typography": "{typography.label}"}, "search": {"textColor": "{colors.ink}", "height": "38px"}, "nav-selected": {"backgroundColor": "{colors.nav-selected}", "textColor": "{colors.nav-selected-ink}", "rounded": "{rounded.control}", "padding": "12px 13px"}, "status-waiting": {"backgroundColor": "{colors.waiting-surface}", "textColor": "{colors.waiting}", "rounded": "{rounded.status}", "padding": "4px 7px"}, "panel": {"backgroundColor": "{colors.surface}", "rounded": "{rounded.panel}"}, "approval": {"backgroundColor": "{colors.approval-surface}", "textColor": "{colors.approval-copy}", "rounded": "{rounded.callout}", "padding": "20px 23px"}}
---

# Design System: ClawPay dashboard

## Overview

**Creative North Star: "System-app work area"**

The user-pinned Apple-inspired direction uses a quiet system-app work area: cool neutral surfaces, platform typography, blue actions and clear status language. Personal agency stays visible: the dashboard observes activity while payment approval stays on the watch.

This is a scan of the implemented dashboard, not a new visual identity. The surface brief records the concept seed retrospectively; no original concept-board workshop or user direction choice is claimed.

**Key Characteristics:**
- Platform system typography and tabular money/time.
- Flat bordered panels, restrained blue emphasis and labeled states.
- Responsive desktop work area and phone bottom navigation.

## Colors

### Primary

Action blue identifies interactive controls, selected phone navigation and spent budget. The deeper hover tone belongs to primary buttons. Approval uses a pale blue surface, separate heading ink and the approval-copy foreground from the final contrast fix.

### Neutral

Canvas and sidebar are cool gray; panels and dialogs are white. Ink carries primary information. Muted is the final shared secondary-text foreground, including timestamps, order metadata, placeholders and inactive phone navigation. Panel borders and structural dividers are distinct tokens.

### Semantic status

Positive, waiting and neutral each pair a foreground with a pale surface. These colors always accompany words. Reserved budget uses a lighter blue allocation beside spent blue and the available track; segment widths express actual values.

**The Labeled State Rule.** Color reinforces a written state; it never carries status alone.

## Typography

Use the platform system stack for headings, content and controls. The body inherits a line height of 1.5; headings use tighter line height and tracking as captured in the frontmatter. Money and time use tabular numerals.

Headline is the page title, title is the section heading, metric is the summary number, and label describes standard buttons. At phone widths the page title is 29px and the document base becomes 16px. Explicit component sizes still apply: content text generally ranges from 12–14px; status labels are 10px on desktop and 9px on phone. Timestamps may reach 9px at the narrowest breakpoint. Do not mistake those metadata sizes for a new body scale.

**The System Type Rule.** Preserve platform typography; do not add a decorative display face.

## Layout

The desktop shell has a fixed 240px sidebar, a sticky 76px toolbar and a centered workspace capped at 1510px. Main padding is 36px 40px at full width. The overview uses a flexible primary column and a 310px secondary column with 24px gaps. Panels contain ordered rows rather than nested decorative cards.

At 1190px density tightens. At 1000px the sidebar is 186px and the overview stacks, with secondary panels in two columns. At 760px the sidebar gives way to a five-item fixed bottom navigation; the toolbar becomes 64px and main padding is 26px 20px 78px. At 430px main horizontal padding becomes 16px and secondary panels stack. Bottom navigation includes safe-area padding.

The observed spacing vocabulary is in the frontmatter; individual component padding can use nearby values where already implemented. Dialogs cap their height to the viewport and scroll internally. Long activity metadata wraps instead of expanding the layout.

## Elevation & Depth

Panels are flat, separated by borders and surface tone. The toolbar and phone navigation use functional white translucency with 18px blur. Dialogs use the offset soft shadow recorded in the sidecar, a dimmed backdrop and 3px backdrop blur. The selected detail segment has a small local shadow; do not generalize it to panel elevation.

Buttons change color over 120ms and press to scale 0.98. Dialog entry moves 8px over 200ms with an easing curve; loading indicators rotate in one second. Reduced motion removes animations and transitions. Reduced transparency removes navigation/backdrop blur. Higher contrast strengthens borders and statuses; several explicit final secondary-text declarations remain the default muted token.

## Shapes

Panels use the panel radius; controls use the control radius; status labels use the status radius. Inset utility surfaces and callouts are gently rounded. Dialog corners are slightly larger. Circular dots, avatars and step markers carry compact status or identity. Avoid turning every content row into a floating card.

## Components

- **Buttons:** white bordered secondary and solid blue primary variants; standard padding and type are in frontmatter. Hover changes surface, focus uses a 3px action-blue outline with 3px offset, press scales, and disabled opacity is 0.5. Standard buttons have a 40px minimum height; small buttons become 44px on phone.
- **Search:** a borderless field inside the list toolbar, muted placeholder, visible focus, 38px minimum desktop height; phone text is 16px with a 42px minimum height.
- **Navigation:** sidebar rows use a pale selected fill and deeper blue foreground; phone items use a blue selected foreground, visible labels and a 44px minimum height.
- **Status labels:** compact text plus dot, with semantic foreground/surface pairs. Status is written explicitly.
- **Panels:** white surface, thin border, clipped rounded corners and a clear heading region. No panel shadow.
- **Approval callout:** pale blue surface, separate watch illustration and stronger copy foreground. Its review action opens order details; it does not authorize payment.
- **Detail dialog:** native modal behavior with a close control; a segmented control switches between timeline and sanitized agent messages. Preserve keyboard focus and viewport scrolling.

The sidecar contains scoped, self-contained previews of implemented primitives. It carries CSS features outside the frontmatter schema; these previews document the appearance and do not replace application behavior.

## Do's and Don'ts

### Do:
- Do pair status color with explicit words.
- Do preserve clear sample, live, unavailable and retained-snapshot labels.
- Do send payment confirmation to the watch; dashboard actions inspect activity.
- Do retain visible keyboard focus and reduced motion/transparency support.

### Don't:
- Don't add decorative gradients or replace the system font stack.
- Don't invent analytics, orders or zero counts for unread resources.
- Don't promote retrospective concept evidence into a claim of original workshop compliance.
