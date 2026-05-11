# Slice 6: Onboarding + Landing Page — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build first-run onboarding (3-step overlay window) + static landing page.

**Architecture:** Rust detects first run via `onboarding_completed` setting → hides main window → creates frameless onboarding window loading `index.html?onboarding=true`. React detects query param, renders `OnboardingPage` state machine. Landing page is two static HTML files.

---

## Task 1: Rust — onboarding command + lib.rs + mod.rs

**Create:** `src-tauri/src/commands/onboarding.rs`
**Modify:** `src-tauri/src/commands/mod.rs`, `src-tauri/src/lib.rs`

- [ ] **1A.** Write `src-tauri/src/commands/onboarding.rs`:
  - `#[tauri::command] pub async fn complete_onboarding(app: AppHandle) -> Result<(), String>`
  - Gets `app.state::<AppState>()`, calls `state.db.set_setting("onboarding_completed", &Value::String("true".into()))`
  - Shows main window (`app.get_webview_window("main").unwrap().show()`)
  - Closes onboarding window (`app.get_webview_window("onboarding").unwrap().close()`)

- [ ] **1B.** In `mod.rs`, add `pub mod onboarding;` after settings line.

- [ ] **1C.** In `lib.rs`:
  - Import: add `onboarding` to `commands::{...}` block
  - In `generate_handler!`, add `onboarding::complete_onboarding` after auth handlers
  - In `setup()` closure, after `let database = ...`:
    - Check `database.get_setting("onboarding_completed")`
    - If `None`: hide main window, create `WebviewWindowBuilder` for `"onboarding"` at URL `App("index.html?onboarding=true".into())`, 500x420, center, no decorations, always_on_top

- [ ] **1D.** Run `cargo check 2>&1` — expect clean.

---

## Task 2: Tauri API wrapper

**Modify:** `src/lib/tauri.ts`

- [ ] Add at end:
```ts
export async function completeOnboarding(): Promise<void> {
  return invoke("complete_onboarding");
}
```

---

## Task 3: Onboarding components

**Create:** `src/components/onboarding/index.ts`, `onboarding-page.tsx`, `welcome-step.tsx`, `hardware-step.tsx`, `ready-step.tsx`

- [ ] **3A.** `index.ts` barrel: `export { OnboardingPage } from "./onboarding-page";`

- [ ] **3B.** `onboarding-page.tsx` — state machine:
  - State: `step` (1|2|3), `hardware` (HardwareInfo|null)
  - `useEffect` on mount: calls `getHardwareInfo()`, sets result or null on error
  - `handleNext`: increments step capped at 3
  - `handleDismiss`: calls `completeOnboarding().catch(console.error)`
  - Renders `<div class="w-full h-screen bg-neutral-950...">` wrapping WelcomeStep(step1) / HardwareStep(step2) / ReadyStep(step3)
  - All copy from copy-v1.md keys. All visual values from design tokens.

- [ ] **3C.** `welcome-step.tsx` — props: `onNext: () => void`
  - Brand mark (amber circle 44px, pulse animation)
  - h1: "Your assistant is ready." (DM Serif Display)
  - p: "Click the bubble to ask questions, run tasks, or get things done. No tabs, no terminals."
  - Button "Get started" → `onNext()`
  - States: render only (no loading), reduced motion: fade not slide

- [ ] **3D.** `hardware-step.tsx` — props: `hardware: HardwareInfo|null`, `onNext: () => void`
  - States: "loading" (spin + "Checking what your computer can run..."), "result" (good/low), "downloading" (progress bar 0-100), "done" (checkmark → auto-next), "error" (retry/skip)
  - `useEffect`: if hardware loaded → "result", else setTimeout 2.5s → "result"
  - Download: simulated `setInterval` (+10%/300ms), cancel via ref, done → auto-next after 1s
  - Good result: GPU name + VRAM, model recommendation, Download + Skip buttons
  - Low result: warning "Your computer may struggle with local models.", Skip only
  - Copy from copy-v1.md keys

- [ ] **3E.** `ready-step.tsx` — props: `onDismiss: () => void`
  - Full clickable div → `onDismiss()`
  - Bubble preview (amber circle, pulse animation)
  - h2: "You're all set." (DM Serif Display)
  - p: "Click the bubble to start."
  - cursor-pointer

---

## Task 4: App.tsx routing

**Modify:** `src/App.tsx`

- [ ] Add import: `import { OnboardingPage } from './components/onboarding/onboarding-page';`
- [ ] At top of `App()` body, before any hooks:
```tsx
const isOnboarding = new URLSearchParams(window.location.search).get('onboarding') === 'true';
if (isOnboarding) {
  return React.createElement(OnboardingPage, null);
}
```

---

## Task 5: Vitest test

**Create:** `src/components/onboarding/__tests__/onboarding-page.test.tsx`

- [ ] 3 tests:
  1. "renders welcome step initially" — mock empty hardware, renders, assert welcome heading visible
  2. "advances to hardware step on click" — mock good hardware, click "Get started", waitFor GPU text visible
  3. "calls completeOnboarding on ready step dismiss" — mock good hardware + resolve completeOnboarding, click through all steps, assert completeOnboarding() called

---

## Task 6: Landing page

**Create:** `landing/index.html`, `landing/alternatives.html`

- [ ] **6A.** `landing/index.html` — Tailwind CDN, DM Sans + DM Serif Display via Google Fonts.
  Sections: Hero (brand mark + h1 + tagline + download button with OS detection), Features (3 cards: hardware, self-evolving, BYOK), Social Proof (2 placeholder testimonials), CTA, Footer.
  OS detection JS: navigator.platform sniffing, sets button text to "Download for Windows/Mac/Linux".

- [ ] **6B.** `landing/alternatives.html` — Ouroboros vs OpenClaw comparison table.
  Rows: native desktop app, Windows support, terminal required, config files, local models, BYOK.
  Ouroboros column: checkmarks. OpenClaw column: X marks / qualifiers.
  Bottom CTA: "Same spirit, different audience" + link back to homepage.

---

## Task 7: Verify everything

- [ ] `pnpm build` — passes
- [ ] `pnpm test` — all tests pass (5+ total)
- [ ] `cargo check` — no new errors
- [ ] Update `docs/PROGRESS.md`: mark Slice 6 ✅ with completion note