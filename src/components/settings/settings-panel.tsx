import React, { useState, useEffect, type FC } from 'react';
import { listModels, listProviders, addApiKey, removeApiKey, getHardwareInfo, listTools, deleteTool } from '../../lib/tauri';
import type { ModelInfo, ProviderConfig, ToolEntry, HardwareInfo } from '../../lib/tauri';
type Tab = 'general' | 'models' | 'providers' | 'tools';

export const SettingsPanel: FC<{ onClose: () => void; onModelChange?: (id: string) => void }> = ({ onClose, onModelChange }) => {
  const [tab, setTab] = useState<Tab>('general');
  const [hardware, setHardware] = useState<HardwareInfo | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [tools, setTools] = useState<ToolEntry[]>([]);
  const [providerKey, setProviderKey] = useState('');
  const [providerName, setProviderName] = useState('openai');
  const [providerLabel, setProviderLabel] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getHardwareInfo().then(setHardware).catch(()=>{});
    listModels().then(setModels).catch(()=>{});
    listProviders().then(setProviders).catch(()=>{});
    listTools().then(setTools).catch(()=>{});
  }, []);

  async function handleAddProvider() {
    if (!providerKey.trim()) return;
    setSaving(true);
    try {
      await addApiKey(providerName, providerKey, providerLabel || undefined);
      const p = await listProviders();
      setProviders(p);
      setProviderKey('');
      setProviderLabel('');
    } catch (e) { alert(String(e)); }
    setSaving(false);
  }

  async function handleRemoveProvider(id: string) {
    await removeApiKey(id);
    setProviders(await listProviders());
  }

  async function handleRemoveTool(name: string) {
    await deleteTool(name);
    setTools(await listTools());
  }

  function renderGeneral() {
    if (!hardware) return React.createElement('p', { className: 'text-label-sm text-neutral-500' }, 'Loading hardware...');
    return React.createElement('div', { className: 'space-y-4' },
      React.createElement('div', null,
        React.createElement('div', { className: 'text-label-sm text-neutral-400 mb-2' }, 'System'),
        React.createElement('div', { className: 'bg-neutral-800 rounded-md p-3 text-label-sm text-neutral-300 space-y-1' },
          React.createElement('div', null, 'OS: ' + hardware.os + ' | CPU: ' + hardware.cpu_cores + ' cores | RAM: ' + hardware.total_ram_mb + ' MB'),
          React.createElement('div', null, 'GPU: ' + hardware.gpu_name + ' (VRAM: ' + hardware.gpu_vram_mb + ' MB)'),
          React.createElement('div', { className: 'text-brand-400' }, 'Recommended: ' + hardware.recommended_model))),
      React.createElement('div', null,
        React.createElement('div', { className: 'text-label-sm text-neutral-400 mb-2' }, 'Shortcut'),
        React.createElement('input', { readOnly: true, value: 'F11', className: 'bg-neutral-800 border border-white/5 rounded-sm px-3 py-1.5 text-label-sm text-neutral-300 w-full outline-none' })),
      React.createElement('div', null,
        React.createElement('div', { className: 'text-label-sm text-neutral-400 mb-2' }, 'Startup'),
        React.createElement('label', { className: 'flex items-center gap-2 text-label-sm text-neutral-300' },
          React.createElement('input', { type: 'checkbox', disabled: true, className: 'rounded-sm' }),
          'Start with Windows (coming soon)')));
  }

  function renderModels() {
    if (models.length === 0) return React.createElement('p', { className: 'text-label-sm text-neutral-500' }, 'No models found.');
    return React.createElement('div', { className: 'space-y-2' },
      models.map(m => React.createElement('div', { key: m.id, className: 'flex items-center justify-between bg-neutral-800 rounded-md px-3 py-2' },
        React.createElement('div', null,
          React.createElement('div', { className: 'text-label-sm text-neutral-200' }, m.name),
          React.createElement('div', { className: 'text-xs text-neutral-500' }, m.provider + (m.size_mb > 0 ? ' | ' + m.size_mb + ' MB' : ' | cloud'))),
        m.provider === 'local' && onModelChange
          ? React.createElement('button', { onClick: () => { onModelChange(m.id); onClose(); }, className: 'text-brand-400 hover:text-brand-300 text-label-sm' }, 'Use')
          : null)));
  }

  function renderProviders() {
    return React.createElement('div', { className: 'space-y-3' },
      React.createElement('div', { className: 'bg-neutral-800 rounded-md p-3 space-y-2' },
        React.createElement('select', { value: providerName, onChange: (e: any) => setProviderName(e.target.value), className: 'bg-neutral-700 border border-white/5 rounded-sm px-2 py-1 text-label-sm text-neutral-200 w-full outline-none' },
          React.createElement('option', { value: 'openai' }, 'OpenAI'),
          React.createElement('option', { value: 'anthropic' }, 'Anthropic'),
          React.createElement('option', { value: 'google' }, 'Google Gemini')),
        React.createElement('input', { type: 'password', value: providerKey, onChange: (e: any) => setProviderKey(e.target.value), placeholder: 'API key', className: 'bg-neutral-700 border border-white/5 rounded-sm px-3 py-1.5 text-label-sm text-neutral-200 w-full outline-none' }),
        React.createElement('input', { type: 'text', value: providerLabel, onChange: (e: any) => setProviderLabel(e.target.value), placeholder: 'Label (optional)', className: 'bg-neutral-700 border border-white/5 rounded-sm px-3 py-1.5 text-label-sm text-neutral-200 w-full outline-none' }),
        React.createElement('button', { onClick: handleAddProvider, disabled: saving || !providerKey.trim(), className: 'rounded-sm px-4 py-1.5 text-label-sm bg-brand-500 text-neutral-950 hover:bg-brand-600 disabled:opacity-50 transition-colors font-medium focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none' }, saving ? 'Saving...' : 'Save key')),
      providers.length === 0 && React.createElement('p', { className: 'text-label-sm text-neutral-500' }, 'No providers configured.'),
      providers.map(p => React.createElement('div', { key: p.id, className: 'flex items-center justify-between bg-neutral-800 rounded-md px-3 py-2' },
        React.createElement('div', null,
          React.createElement('div', { className: 'text-label-sm text-neutral-200' }, p.provider + (p.label ? ' (' + p.label + ')' : '')),
          React.createElement('div', { className: 'text-xs text-success-500' }, p.connected ? 'Connected' : 'Error')),
        React.createElement('button', { onClick: () => handleRemoveProvider(p.id), className: 'text-error-500 hover:text-error-400 text-label-sm' }, 'Remove'))));
  }

  function renderTools() {
    if (tools.length === 0) return React.createElement('p', { className: 'text-label-sm text-neutral-500' }, 'No tools built yet. Tools appear here after you build and save them.');
    return React.createElement('div', { className: 'space-y-2' },
      tools.map(t => React.createElement('div', { key: t.name, className: 'flex items-center justify-between bg-neutral-800 rounded-md px-3 py-2' },
        React.createElement('div', null,
          React.createElement('div', { className: 'text-label-sm text-neutral-200' }, t.name),
          React.createElement('div', { className: 'text-xs text-neutral-500' }, t.language + (t.description ? ' | ' + t.description : ''))),
        React.createElement('button', { onClick: () => handleRemoveTool(t.name), className: 'text-error-500 hover:text-error-400 text-label-sm' }, 'Remove'))));
  }

  return React.createElement('div', { className: 'fixed inset-0 z-[200] flex items-center justify-center' },
    React.createElement('div', { className: 'fixed inset-0 bg-black/60', onClick: onClose }),
    React.createElement('div', { className: 'relative w-full max-w-2xl h-[70vh] bg-neutral-900 border border-white/10 rounded-xl shadow-level-3 flex overflow-hidden', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Settings' },
      React.createElement('div', { className: 'w-44 bg-neutral-900/80 border-r border-white/5 py-3 flex flex-col flex-shrink-0' },
        React.createElement('h3', { className: 'text-label font-medium text-neutral-300 px-4 mb-3' }, 'Settings'),
        ['general','models','providers','tools'].map(id =>
          React.createElement('button', {
            key: id,
            onClick: () => setTab(id as Tab),
            className: 'text-left text-label-sm px-4 py-2 transition-colors ' + (tab === id ? 'text-brand-400 bg-brand-500/10 border-r-2 border-brand-400' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800/50') + ' focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none'
          }, id.charAt(0).toUpperCase() + id.slice(1)))),
      React.createElement('div', { className: 'flex-1 flex flex-col overflow-hidden' },
        React.createElement('div', { className: 'flex items-center justify-between px-5 py-3 border-b border-white/5 flex-shrink-0' },
          React.createElement('h4', { className: 'text-h3 text-neutral-200 font-medium' }, tab.charAt(0).toUpperCase() + tab.slice(1)),
          React.createElement('button', { onClick: onClose, className: 'text-neutral-500 hover:text-neutral-300 text-label focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none rounded-sm px-1' }, 'Close')),
        React.createElement('div', { className: 'flex-1 overflow-y-auto px-5 py-4' },
          tab === 'general' ? renderGeneral() :
          tab === 'models' ? renderModels() :
          tab === 'providers' ? renderProviders() :
          tab === 'tools' ? renderTools() : null))));
};