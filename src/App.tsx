import React, { useState, useCallback, useEffect } from 'react';
import { listen } from '@tauri-apps/api/event';
import { setViewSize } from './lib/tauri';
import { Bubble } from './components/bubble';
import { InputBar } from './components/chat/input-bar';
import { ChatContainer } from './components/chat/chat-container';
import { SettingsPanel } from './components/settings/settings-panel';
import { OnboardingPage } from './components/onboarding/onboarding-page';
import { useBubble } from './hooks/useBubble';
import { useChat } from './hooks/useChat';

type View = 'bubble' | 'input' | 'chat';
const VIEW_ORDER: View[] = ['bubble', 'input', 'chat'];
const DEFAULT_MODELS = [{id:'local/default',name:'Local',provider:'local'},{id:'cloud/openai/gpt-4o-mini',name:'GPT-4o Mini',provider:'openai'},{id:'cloud/anthropic/claude-sonnet-4',name:'Claude Sonnet 4',provider:'anthropic'},{id:'cloud/google/gemini-2.0-flash',name:'Gemini 2.0 Flash',provider:'google'}];

function App() {
  const isOnboarding = new URLSearchParams(window.location.search).get('onboarding') === 'true';
  if (isOnboarding) {
    return React.createElement(OnboardingPage, null);
  }

  const [view, setView] = useState<View>('bubble');
  const [draftMessage, setDraftMessage] = useState('');
  const [showSettings, setShowSettings] = useState(false);
  const { dragOffset, onMouseDown, onMouseMove, onMouseUp } = useBubble();
  const { messages, isLoading, sendMessage, conversationId, modelId, setModelId, switchConversation, newConversation } = useChat();

  const cycleView = useCallback(() => {
    setView(v => {
      const idx = VIEW_ORDER.indexOf(v);
      return VIEW_ORDER[(idx + 1) % VIEW_ORDER.length];
    });
  }, []);

  useEffect(() => {
    listen('cycle-view', cycleView).then(unlisten => unlisten);
  }, [cycleView]);

  useEffect(() => {
    setViewSize(view !== 'bubble').catch(() => {});
  }, [view]);

  const handleBubbleClick = useCallback(() => { setView('input'); }, []);
  const handleSendFromInput = useCallback(async (text?: string) => {
    const msg = text ?? draftMessage;
    if (!msg.trim() || isLoading) return;
    setDraftMessage('');
    setView('chat');
    await sendMessage(msg);
  }, [draftMessage, isLoading, sendMessage]);
  const handleClose = useCallback(() => { setView('bubble'); }, []);
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape' && (view === 'input' || view === 'chat')) {
      setView('bubble');
    }
  }, [view]);

  return React.createElement('div', { className: 'w-screen h-screen bg-transparent overflow-hidden', onMouseMove: onMouseMove, onMouseUp: onMouseUp, onKeyDown: handleKeyDown },
    React.createElement(Bubble, { dragOffset: dragOffset, onClick: handleBubbleClick, onMouseDown: onMouseDown, onSettings: () => setShowSettings(true) }),
    view === 'input' && React.createElement('div', { className: 'fixed bottom-0 left-0 right-0 z-50 p-6' },
      React.createElement('div', { className: 'max-w-[480px] mx-auto bg-neutral-900/95 backdrop-blur-md rounded-xl shadow-level-3 border border-white/6' },
        React.createElement(InputBar, { value: draftMessage, onChange: setDraftMessage, onSubmit: () => handleSendFromInput(), isLoading: isLoading }))),
    view === 'chat' && React.createElement(ChatContainer, { messages: messages, isLoading: isLoading, conversationId: conversationId, modelId: modelId, models: DEFAULT_MODELS, onSendMessage: sendMessage, onSwitchConversation: switchConversation, onNewConversation: newConversation, onModelChange: setModelId, onClose: handleClose, onOpenSettings: () => setShowSettings(true) }),
    showSettings && React.createElement(SettingsPanel, { onClose: () => setShowSettings(false), onModelChange: setModelId }));
}
export default App;