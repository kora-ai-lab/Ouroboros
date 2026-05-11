import React, { useState, useEffect, useRef, type FC } from 'react';
import { InputBar } from './input-bar';
import { MessageList } from './message-list';
import { Sidebar } from './sidebar';
import type { ChatMessage } from '../../hooks/useChat';
import type { Conversation } from '../../types';
interface Props { messages: ChatMessage[]; isLoading: boolean; conversationId: string | null; modelId: string; models: Array<{id:string;name:string;provider:string}>; onSendMessage: (text: string) => void; onSwitchConversation: (conv: Conversation) => void; onNewConversation: () => void; onModelChange: (modelId: string) => void; onClose: () => void; onOpenSettings: () => void; }
export const ChatContainer: FC<Props> = ({ messages, isLoading, conversationId, modelId, models, onSendMessage, onSwitchConversation, onNewConversation, onModelChange, onClose, onOpenSettings }) => {
  const [draftMessage, setDraftMessage] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [isScrolledUp, setIsScrolledUp] = useState(false);
  useEffect(() => { if (!isScrolledUp) messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, isScrolledUp]);
  const handleScroll = () => { const el = listRef.current; if (!el) return; setIsScrolledUp(el.scrollHeight - el.scrollTop - el.clientHeight > 80); };
  const handleSubmit = (text?: string) => { const msg = text ?? draftMessage; if (!msg.trim() || isLoading) return; setDraftMessage(''); onSendMessage(msg); };
  const handleNew = () => { onNewConversation(); setRefreshKey(k => k + 1); };
  const handleSwitch = (conv: Conversation) => { onSwitchConversation(conv); setRefreshKey(k => k + 1); };
  return React.createElement('div', { className: 'fixed inset-0 z-50 flex items-end justify-center', onKeyDown: (e: any) => { if (e.ctrlKey && e.key === 'n') { e.preventDefault(); onNewConversation(); } } },
    React.createElement('div', { className: 'fixed inset-0 bg-black/50', onClick: onClose }),
    React.createElement('div', { className: 'relative w-full max-w-[720px] h-[65vh] bg-neutral-900/95 backdrop-blur-md rounded-t-xl shadow-level-3 border border-white/6 flex flex-col overflow-hidden' },
      React.createElement('div', { className: 'flex items-center justify-between px-4 py-2.5 border-b border-white/5 flex-shrink-0' },
        React.createElement('div', { className: 'flex items-center gap-2' },
          React.createElement('button', { onClick: () => setSidebarOpen(o => !o), className: 'text-neutral-500 hover:text-neutral-300 text-label-sm px-2 py-1 rounded-sm hover:bg-neutral-800 transition-colors focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none', 'aria-expanded': sidebarOpen }, '[=]'),
          React.createElement('h2', { className: 'text-h3 font-heading text-neutral-200' }, 'Ouroboros')),
        React.createElement('div', { className: 'flex items-center gap-2' },
          React.createElement('select', { value: modelId, onChange: (e: any) => onModelChange(e.target.value), className: 'bg-neutral-800 border border-white/10 rounded-sm text-label-sm text-neutral-300 px-2 py-1 outline-none cursor-pointer focus-visible:ring-2 focus-visible:ring-brand-400/50', 'aria-label': 'Select model' },
            models.map((m: any) => React.createElement('option', { key: m.id, value: m.id }, m.name))),
          React.createElement('button', { onClick: onOpenSettings, className: 'text-neutral-500 hover:text-neutral-300 text-label-sm px-2 py-1 rounded-sm hover:bg-neutral-800 transition-colors focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none', 'aria-label': 'Open settings' }, 'Settings'),
          React.createElement('button', { onClick: onClose, className: 'text-neutral-500 hover:text-neutral-300 text-label px-2 py-1 rounded-sm hover:bg-neutral-800 transition-colors focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none' }, 'Close'))),
      React.createElement('div', { className: 'flex flex-1 overflow-hidden' },
        React.createElement(Sidebar, { isOpen: sidebarOpen, onSelectConversation: handleSwitch, onNewConversation: handleNew, activeConversationId: conversationId, refreshKey }),
        React.createElement('div', { className: 'flex-1 flex flex-col overflow-hidden' },
          React.createElement('div', { ref: listRef, onScroll: handleScroll, className: 'flex-1 overflow-y-auto' },
            React.createElement(MessageList, { messages: messages, isLoading: isLoading }),
            React.createElement('div', { ref: messagesEndRef })),
          isScrolledUp && React.createElement('button', { onClick: () => { messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }); setIsScrolledUp(false); }, className: 'absolute bottom-20 right-6 w-9 h-9 rounded-full bg-brand-500 text-neutral-950 shadow-level-2 flex items-center justify-center hover:bg-brand-600 transition-colors text-sm z-10 focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none' }, 'v'),
          React.createElement('div', { className: 'p-3 border-t border-white/5 flex-shrink-0' },
            React.createElement(InputBar, { value: draftMessage, onChange: setDraftMessage, onSubmit: () => handleSubmit(), isLoading: isLoading }))))));
};