import React, { type FC } from 'react';
interface Props { code: string; language: string; onAllow: () => void; onDeny: () => void; }
export const PermissionDialog: FC<Props> = ({ code, language, onAllow, onDeny }) => {
  const preview = code.length > 400 ? code.slice(0, 400) + '\n...' : code;
  return React.createElement('div', { className: 'fixed inset-0 z-[100] flex items-center justify-center' },
    React.createElement('div', { className: 'fixed inset-0 bg-black/60', onClick: onDeny }),
    React.createElement('div', { className: 'relative w-full max-w-md bg-neutral-900 border border-white/10 rounded-xl shadow-level-3 p-5', role: 'dialog', 'aria-modal': 'true' },
      React.createElement('h3', { className: 'text-h2 font-heading text-neutral-100 mb-2' }, 'Run this code?'),
      React.createElement('p', { className: 'text-body-sm text-neutral-400 mb-3' },
        'Ouroboros wants to run ', React.createElement('span', { className: 'text-brand-400 font-medium' }, language), '. Review it first.'),
      React.createElement('pre', { className: 'bg-neutral-950 rounded-md p-3 mb-4 font-mono text-xs text-neutral-300 max-h-48 overflow-y-auto whitespace-pre-wrap' }, preview),
      React.createElement('div', { className: 'flex gap-2' },
        React.createElement('button', { onClick: onAllow, className: 'flex-1 rounded-sm px-4 py-2 text-label bg-brand-500 text-neutral-950 hover:bg-brand-600 transition-colors font-medium focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none' }, 'Run'),
        React.createElement('button', { onClick: onDeny, className: 'rounded-sm px-4 py-2 text-label bg-neutral-800 text-neutral-300 hover:bg-neutral-700 transition-colors focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none' }, 'Cancel'))));
};