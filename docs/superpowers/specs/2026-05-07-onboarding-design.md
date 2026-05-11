# Slice 6: Onboarding + Landing Page — Design Spec

**Date:** 2026-05-07
**Status:** Approved
**Depends on:** Slice 5 (Settings)

---

## Architecture

### First-run detection
- SQLite `settings` table key: `onboarding_completed`
- Default: absent (treated as first run)
- Set to `"true"` when onboarding completes
- Checked in Rust `setup()` on app initialization

### Onboarding window
- Separate Tauri webview window via `WebviewWindowBuilder`
- Label: `onboarding`, centered on primary monitor, ~480×400px
- Frameless, dark warm background (bg.page token)
- Loads React route `/onboarding`
- Closed on completion — bubble window then visible

### Landing page
- Static HTML/CSS at `landing/index.html` and `landing/alternatives.html`
- Tailwind CSS via CDN
- No build toolchain, no framework

---

## Flow

```
App start → check settings.onboarding_completed
  ├─ Present → skip, show bubble as normal
  └─ Absent → create onboarding window → load /onboarding route

OnboardingPage (state machine):
  Step 1 (Welcome) → Step 2 (Hardware) → Step 3 (Ready) → complete
    ↓ skip               ↓ skip                ↓ click
    └────────────────────┴─────────────────────┘
```

All steps: forward-only, every step is skippable, window close = defer (re-shows next launch).

---

## Components

### `src/components/onboarding/onboarding-page.tsx`
State machine container.
- **State:** `step` (1|2|3), `stepState` (loading|ready|error), `hardware`, `downloadProgress` (0-100)
- Calls `invoke("detect_hardware")` on mount for step 2
- Calls `invoke("complete_onboarding")` on step 3 dismiss
- Window close handler: does NOT set completed flag (re-shows next launch)

### `src/components/onboarding/welcome-step.tsx`
Step 1. No loading state (instant render).
- Brand mark (bubble preview, animated pulse)
- `<h1>` "Your assistant is ready." (DM Serif Display, size.xl)
- `<p>` "Click the bubble to ask questions, run tasks, or get things done. No tabs, no terminals." (body-lg, text.secondary)
- `<Button variant="primary">` "Get started" → advances to step 2
- Reduced motion: fade in (duration.slow, 400ms)

### `src/components/onboarding/hardware-step.tsx`
Step 2. Hardware detection + optional model download.

**Loading state:**
- Radar/pulse animation
- "Checking what your computer can run..."

**Good result state:**
- GPU icon + name + VRAM display
- "Your [GPU] with [N]GB can run [model]. Fast, private, and offline."
- Button A: "Download [model] ([N]GB)" → starts simulated download
- Button B: "Skip — use cloud models" → advances to step 3

**Low spec state:**
- Warning icon
- "Your computer may struggle with local models. You can still use Ouroboros with cloud providers."
- Button: "Continue with cloud" → advances to step 3

**Downloading state:**
- Progress bar (0-100%), cancel button
- "Downloading model..."
- Simulated: +10% every 300ms, total ~3 seconds
- Cancel → returns to result state

**Download done:**
- Green check + "Ready to use" → auto-advances to step 3 after 1 second

**Error state (download fail):**
- "Download didn't complete."
- Retry button + Skip button

### `src/components/onboarding/ready-step.tsx`
Step 3. Bubble interaction CTA.
- `<h2>` "You're all set." (DM Serif Display)
- Animated bubble preview (pulse animation)
- `<p>` "Click the bubble to start."
- Click anywhere on overlay → emit `complete_onboarding` → close window
- No loading state (instant render)

---

## Rust Commands

### `complete_onboarding`
```rust
#[tauri::command]
async fn complete_onboarding(state: State<'_, AppState>) -> Result<(), String>
```
- Calls `set_setting("onboarding_completed", "true")`
- Closes the `onboarding` webview window
- Returns Ok

**Startup check (in `main.rs` build function):**

In the `tauri::Builder::default().setup()` closure, before creating the bubble window:
```rust
fn main() {
    tauri::Builder::default()
        .setup(|app| {
            // Check onboarding state, create onboarding window if first run
            // ...existing setup...
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error running app");
}
```

No new `start_onboarding` command needed — handled in app startup.

---

## Landing Page

### `landing/index.html`
Single static page, Tailwind CDN, matching design tokens.

**Sections:**
1. **Hero**
   - Brand mark + "Ouroboros"
   - `<h1>` "Your desktop AI assistant"
   - `<p>` "A self-evolving AI that lives on your desktop. Ask questions, run tasks, get things done — no terminal required."
   - Download CTA button with OS detection (placeholder — links to GitHub releases when available, currently disabled/deferred)

### Delivery
Static files in `landing/`. Served via GitHub Pages, a simple HTTP server (`python -m http.server` / `npx serve`), or opened directly in browser. Not bundled with the Tauri app. OS detection pre-selects the right download when release binaries exist.
2. **Features (3 cards)**
   - "Runs on your hardware" — local models, GPU detection, private
   - "Self-evolving" — writes its own tools, adapts to you
   - "Bring your own keys" — no subscription lock-in, BYOK
3. **Social proof** — placeholder section
4. **Footer** — links, copyright

**OS detection:** Simple JS:
```js
const platform = navigator.platform || navigator.userAgent;
if (platform.includes('Win')) showWindowsDownload();
else if (platform.includes('Mac')) showMacDownload();
else showLinuxDownload();
```

### `landing/alternatives.html`
Comparison page for `/alternatives/openclaw`.
- Side-by-side: Ouroboros vs OpenClaw
- Key differentiators: no terminal required, native Windows app, GUI bubble
- Link back to main page + download CTA

---

## States Coverage

| Step | Loading | Success | Error | Edge/Empty |
|------|---------|---------|-------|------------|
| Welcome | N/A (instant) | Renders + button | N/A | User closes window (defer) |
| Hardware | Radar pulse animation | GPU details + CTAs | Low spec notice / download fail | No GPU detected |
| Ready | N/A (instant) | Bubble CTA + dismiss | N/A | User closes window (defer) |

All copy from `docs/copy-v1.md` keys. All visual values from `docs/design-system.md` tokens.

---

## Testing Strategy

- **Vitest:** Unit tests for OnboardingPage state machine (step transitions, download simulation)
- **Playwright:** E2E onboarding flow (window open → step through → completion → flag set)
- **Cargo test:** `complete_onboarding` sets the flag correctly

---

## Files Summary

**Create:**
- `src/components/onboarding/onboarding-page.tsx`
- `src/components/onboarding/welcome-step.tsx`
- `src/components/onboarding/hardware-step.tsx`
- `src/components/onboarding/ready-step.tsx`
- `src/routes/onboarding.tsx`
- `src-tauri/src/commands/onboarding.rs`
- `landing/index.html`
- `landing/alternatives.html`
- `src/components/onboarding/__tests__/onboarding-page.test.tsx`

**Modify:**
- `src-tauri/src/main.rs` — add onboarding window creation in setup
- `src-tauri/src/commands/mod.rs` — register onboarding commands
- `src-tauri/src/lib.rs` — register commands, add setup logic
- `docs/PROGRESS.md` — mark slice 6 complete