# Slice 7: Polish + Edge Cases — Design Spec

**Date:** 2026-05-07
**Status:** Approved
**Design tokens ref:** docs/design-system.md
**Copy ref:** docs/copy-v1.md
**UX ref:** docs/ux.md (accessibility sections per screen)

---

## 1. Global Shortcut (Alt+Space)

- **Plugin:** `tauri-plugin-global-shortcut` (already in Cargo.toml + package.json)
- **Registration:** In `lib.rs` `setup()` closure
- **Behavior:** Toggle main window visibility
  - Window hidden → show + focus
  - Window visible → hide
- **Unregistration:** Automatically cleaned up on app termination
- **Future:** Read configurable shortcut from settings; default = `Alt+Space`

**Rust side (lib.rs setup):**
```rust
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut};

app.plugin(tauri_plugin_global_shortcut::Builder::new().build())?;
let handle = app.handle().clone();
app.handle().plugin(
    tauri_plugin_global_shortcut::Builder::new()
        .with_handler(move |_app, _shortcut, _event| {
            if let Some(w) = handle.get_webview_window("main") {
                if w.is_visible().unwrap_or(false) {
                    w.hide().ok();
                } else {
                    w.show().ok();
                }
            }
        })
        .build(),
)?;
handle.global_shortcut().register(Shortcut::parse("Alt+Space").unwrap())?;
```

**Test:** Manual — global shortcut cannot be tested in Vitest/Playwright headless.

---

## 2. In-app Shortcuts

### Escape behavior
- In `App.tsx`, add keydown listener on the root div:
  - If `view === 'input'` → setView('bubble')
  - If `view === 'chat'` → setView('bubble')
  - Otherwise → no-op

### Ctrl+N (new conversation)
- In `ChatContainer`, add keydown listener:
  - `Ctrl+N` → calls `onNewConversation()`

### Tab focus traps
- **Chat container:** When chat is open, focus cycles within it (Tab through elements, Shift+Tab backwards). On Escape, close to bubble.
- **Permission dialog:** Focus trap already exists via dialog HTML element.
- **Settings panel:** Focus trap within the modal. Escape closes. Implement via `onKeyDown` handler that traps Tab.

### Enter (send) / Shift+Enter (newline)
- Already implemented in InputBar via existing textarea behavior.

---

## 3. Focus States

Add to ALL interactive elements: `focus-visible:ring-2 focus-visible:ring-amber-500/50 focus-visible:ring-offset-0 focus-visible:outline-none`

**Elements to update:**
- `bubble.tsx` — bubble div (when it acts as button)
- `input-bar.tsx` — textarea, submit button
- `sidebar.tsx` — conversation list items, new chat button, toggle button
- `chat-container.tsx` — model selector button, scroll-to-bottom button
- `message-bubble.tsx` — Run button on code blocks
- `settings-panel.tsx` — tab buttons, form inputs (provider dropdown, key input, label input), Save/Remove buttons, Use model button
- `permission-dialog.tsx` — Allow, Deny, Always buttons
- `execution-result.tsx` — Save as tool button
- `welcome-step.tsx` — Get started button
- `hardware-step.tsx` — Download, Skip, Cancel, Try again buttons
- `ready-step.tsx` — not applicable (full div click area)

**Approach:** Add the focus-visible classes to each component's className strings where buttons/inputs exist.

---

## 4. ARIA Labels

**Components and their ARIA attributes:**

| Component | Attribute | Value |
|-----------|-----------|-------|
| Bubble div | role, aria-label | "button", "Open Ouroboros assistant" |
| InputBar textarea | aria-label | "Ask anything" |
| InputBar submit button | aria-label | "Send message" |
| MessageList container | role, aria-live | "log", "polite" |
| StreamingText wrapper | aria-live | "assertive" |
| Sidebar toggle button | aria-expanded | {sidebarOpen} |
| Sidebar container | role, aria-label | "region", "Conversations" |
| PermissionDialog container | role, aria-modal | "dialog", "true" |
| SettingsPanel container | role, aria-modal, aria-label | "dialog", "true", "Settings" |
| ChatContainer input | aria-label | "Message" |
| Model selector button | aria-label | "Select model" |

---

## 5. Reduced Motion

**In `src/index.css`:**
```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

Also add `motion-safe:` prefix to custom animation classes currently in components:
- `.animate-fade-in` → use via `motion-safe:animate-fade-in` in components
- `.animate-pulse-slow` → use via `motion-safe:animate-pulse-slow`

**Components using animations:**
- Bubble: pulse animation on hover → `motion-safe:animate-pulse-slow`
- Message bubble: slide-in on new message → `motion-safe:animate-fade-in`
- Onboarding steps: fade-in → `motion-safe:animate-fade-in`
- Hardware step: spin loader → `motion-safe:animate-spin`
- Download progress bar: width transition → browser default, covered by media query

---

## 6. Dual Monitor

- On app start, center bubble on monitor containing the cursor
- If cursor position unavailable, center on primary monitor
- Bubble already draggable (via `useBubble` hook) — persists position
- Tauri window config: `visibleOnAllWorkspaces: true` already set

**Implementation:** In `useBubble.ts`, on initial position calc, use `window.screenX`/`window.screenY` for monitor awareness. Or in Rust `setup()`, set window position to cursor monitor center.

Actually, simpler approach: in `lib.rs` setup, after creating the window, position it at center of primary monitor with offset for "bubble corner position" (e.g., bottom-right quadrant). The bubble's drag position override will kick in on subsequent launches.

---

## 7. Verification Passes (no code changes)

### Design tokens audit
- Read every component file, verify no raw hex codes or ad-hoc spacing values
- Confirm all colors use Tailwind tokens (neutral-*, amber-*, emerald-*, etc.)
- Confirm spacing uses design tokens (p-6, gap-4, rounded-lg, etc.)

### Copy keys audit
- Read all user-facing strings, confirm they match copy-v1.md keys
- Verify no inline strings in components (all text hardcoded since no i18n yet — acceptable for MVP)

### Anti-algorithm guardrails
- No centered-everything layouts → verify asymmetric/side-aligned content where appropriate
- No gradient text on headlines
- No icon-grid feature sections (check onboarding welcome step)
- No stock-photography vibes

---

## Files to Modify (summary)

- `src-tauri/src/lib.rs` — register global-shortcut plugin, register Alt+Space
- `src/App.tsx` — add Escape keydown handler
- `src/index.css` — add reduced-motion media query
- `src/components/bubble/bubble.tsx` — add focus-visible, ARIA attributes
- `src/components/chat/input-bar.tsx` — add focus-visible, ARIA labels
- `src/components/chat/chat-container.tsx` — add Ctrl+N listener, focus-visible on model selector
- `src/components/chat/message-list.tsx` — add role=log, aria-live
- `src/components/chat/sidebar.tsx` — add aria-expanded, focus-visible
- `src/components/chat/streaming-text.tsx` — add aria-live=assertive
- `src/components/settings/settings-panel.tsx` — add focus-visible, ARIA, focus trap + Escape
- `src/components/tools/permission-dialog.tsx` — add focus-visible, ARIA
- `src/components/tools/execution-result.tsx` — add focus-visible
- `src/components/onboarding/welcome-step.tsx` — add focus-visible
- `src/components/onboarding/hardware-step.tsx` — add focus-visible
- `src/hooks/useBubble.ts` — dual monitor centering
- `docs/PROGRESS.md` — mark Slice 7 complete