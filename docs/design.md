# Design System Strategy: The Living Canvas

## 1. Overview & Creative North Star
The Creative North Star for this design system is **"The Living Canvas."**

Moving away from the sterile, modular grid of traditional e-commerce, this system treats the interface as a high-end editorial piece. It is designed to feel "grown, not built." We achieve this through a "Soft Editorial" approach: utilizing intentional asymmetry, expansive white space that acts as a breathing element, and a sophisticated hierarchy of "organic depth." By eschewing traditional borders and harsh dividers, we allow content to flow naturally, mimicking the premium, curated experience of a luxury market.

## 2. Colors & Chromatic Texture
Our palette is rooted in the deep, life-giving tones of nature. We lead with a primary vibrant green, supported by a spectrum of "living neutrals" that provide warmth and tactile quality.

### The "No-Line" Rule
To maintain a premium, seamless aesthetic, **the use of 1px solid borders for sectioning is strictly prohibited.** Boundaries must be defined through background color shifts. For example, a promotional section should use `surface-container-low` (#f6f3f2) to distinguish itself from the main `surface` (#fbf9f8). This creates "implied zones" that feel integrated rather than boxed in.

### Surface Hierarchy & Nesting
Treat the UI as a series of physical layers. We use tonal layering to define importance:
*   **Base:** `surface` (#fbf9f8) for the main canvas.
*   **Layer 1:** `surface-container-low` (#f6f3f2) for large content blocks.
*   **Layer 2 (The "Lift"):** `surface-container-lowest` (#ffffff) for high-priority interactive cards.
*   **Layer 3 (Floating):** Semi-transparent `surface` with a 20px backdrop blur to create a "glass" effect for navigation bars or floating action menus.

### The "Glass & Gradient" Rule
Flatness is the enemy of premium. To add "visual soul," apply subtle radial gradients to hero sections transitioning from `primary` (#004d37) to `primary_container` (#00674b). For floating elements, utilize **Glassmorphism**: use surface colors at 80% opacity with a `backdrop-filter: blur(12px)` to allow the vibrant greens of the background to bleed through softly.

## 3. Typography
The typography is an intentional dialogue between authoritative structure and approachable warmth.

*   **Display & Headlines (Plus Jakarta Sans):** These are our "Editorial Voices." Use `display-lg` (3.5rem) with tight tracking to create a bold, premium impact. The rounded terminals of Plus Jakarta Sans provide the "friendly" organic feel required.
*   **Body & Titles (Work Sans):** Chosen for its exceptional legibility and neutral character. It acts as the "Curator," delivering information clearly without competing with the headlines.
*   **Labels (Inter):** Reserved for technical data and micro-copy. Its utilitarian nature provides a "Functional Contrast" to the more expressive headers.

**Intentional Asymmetry:** When laying out typography, avoid centering everything. Use "The Editorial Offset"—align headlines to a 10% left-margin offset while body text remains on a standard grid to create visual tension and interest.

## 4. Elevation & Depth
Depth is achieved through light and layering, never through heavy-handed shadows.

*   **Tonal Layering:** Instead of a shadow, place a `surface-container-lowest` card atop a `surface-container-low` background. This creates a natural "paper-on-table" lift.
*   **Ambient Shadows:** If a floating state is required (e.g., a modal), use an ultra-diffused shadow: `box-shadow: 0 20px 40px rgba(27, 28, 28, 0.05)`. The color is a tint of our `on-surface` charcoal, making the shadow feel like ambient light rather than digital "dirt."
*   **The "Ghost Border" Fallback:** If accessibility requires a container boundary, use the `outline-variant` token (#bec9c2) at **15% opacity**. It should be felt, not seen.

## 5. Components

### Buttons: The Tactile Interaction
*   **Primary:** Solid `primary` (#004d37) with `on-primary` (#ffffff) text. Use `rounded-full` (9999px) for a soft, pebble-like feel.
*   **Secondary:** `surface-container-high` (#eae8e7) background with `primary` text. No border.
*   **States:** On hover, apply a subtle shift to `primary_container` (#00674b) and a light ambient shadow.

### Input Fields: Soft Utility
*   **Canvas:** Background set to `surface-container-low` (#f6f3f2).
*   **Focus:** Transition the background to `surface-container-lowest` (#ffffff) and apply a 1px "Ghost Border" using `primary`.
*   **Typography:** Labels must use `label-md` in `on-surface-variant` (#3f4944) for a soft, charcoal legibility.

### Cards & Lists: The No-Divider Rule
*   **Rule:** Forbid the use of horizontal divider lines.
*   **Separation:** Use the Spacing Scale `6` (2rem) to create clear "islands" of content.
*   **Lists:** Separate items using a subtle background color toggle (Zebra striping using `surface` and `surface-container-low`) rather than lines.

### Signature Component: The "Organic Chip"
Use `tertiary_fixed` (#b8f649) for labels like "Organic," "Local," or "Fresh." These chips should use `rounded-md` (0.75rem) and be placed with slight overlaps on images to create a layered, "scrapbook editorial" feel.

## 6. Surface Hierarchy

**Page** → `#F5F7FA`
**Card/Form** → `#FFFFFF`
**Input** → `#FFFFFF`
**Selected State** → Primary Light + Primary Border

### Rules

1. Every background color must represent a clear hierarchy level or state.
2. Do not mix gray and white backgrounds without a functional reason.
3. Use different backgrounds only for special states (Selected, Disabled, Error, Warning).
4. Keep a maximum of 3 layers: **Page → Card → Component**.


## 7. Do's and Don'ts

### Do:
*   **Embrace Negative Space:** If a section feels "empty," it’s likely working. Let the `surface` color breathe.
*   **Use Intentional Overlaps:** Allow images to slightly overlap container edges or typography to create depth and a custom, non-templated look.
*   **Prioritize Hierarchy:** Use the `headline-lg` and `display-sm` scales aggressively to guide the user's eye.

### Don't:
*   **Never use pure black:** Always use `on-surface` (#1b1c1c) for text to maintain the "soft charcoal" premium feel.
*   **Avoid "Boxiness":** Never use `rounded-none`. Everything in nature has a radius; our UI should too (minimum `DEFAULT`: 0.5rem).
*   **No High-Contrast Borders:** If you find yourself reaching for a 1px solid border, stop and use a background color shift instead.
