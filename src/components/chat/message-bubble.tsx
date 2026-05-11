import React, { useState, type FC } from 'react';
import { StreamingText } from './streaming-text';
import { ExecutionResult } from '../tools/execution-result';
import { PermissionDialog } from '../tools/permission-dialog';
import { executeCode, saveTool } from '../../lib/tauri';
import type { ChatMessage } from '../../hooks/useChat';

type CodeBlock = { code: string; language: string; raw: string };

function parseCodeBlock(content: string): CodeBlock | null {
  const m = content.match(/```(\w+)\n([\s\S]*?)```/);
  if (!m) return null;
  const lang = m[1];
  const valid = ['exec','shell','sh','bash','python','py','node','js','javascript','powershell','ps1'];
  if (!valid.includes(lang)) return null;
  const normalized = lang === 'sh' || lang === 'bash' || lang === 'exec' ? 'shell' : lang === 'py' ? 'python' : lang === 'js' ? 'node' : lang === 'ps1' ? 'powershell' : lang;
  return { code: m[2].trim(), language: normalized, raw: m[0] };
}

export const MessageBubble: FC<{ message: ChatMessage }> = ({ message }) => {
  const isUser = message.role === 'user';
  const align = isUser ? 'justify-end' : 'justify-start';
  const bg = isUser ? 'bg-brand-700/30 border-brand-600/20' : 'bg-neutral-800 border-white/5';
  const label = isUser ? 'You' : 'Ouroboros';
  const [showPerm, setShowPerm] = useState(false);
  const [pending, setPending] = useState<CodeBlock | null>(null);
  const [r, setR] = useState<{stdout:string;stderr:string;exitCode:number;timedOut:boolean} | null>(null);
  const [running, setRunning] = useState(false);
  const [saved, setSaved] = useState(false);
  const block = parseCodeBlock(message.content);

  async function run(code: string, language: string) {
    setShowPerm(false); setRunning(true); setR(null); setSaved(false);
    try {
      const out = await executeCode(code, language);
      setR({stdout:out.stdout,stderr:out.stderr,exitCode:out.exit_code,timedOut:out.timed_out});
    } catch(e) { setR({stdout:'',stderr:String(e),exitCode:-1,timedOut:false}); }
    setRunning(false);
  }

  async function handleSave() {
    if (!block || !r || r.exitCode !== 0) return;
    try {
      const toolName = 'tool_' + Date.now().toString(36);
      await saveTool(toolName, block.code, block.language, 'Created by Ouroboros');
      setSaved(true);
    } catch(e) { /* ignore */ }
  }

  const cleanContent = block ? message.content.replace(block.raw, '') : message.content;
  return React.createElement(React.Fragment, null,
    React.createElement('div', { className: 'flex w-full ' + align },
      React.createElement('div', { className: 'max-w-[85%] rounded-lg px-4 py-2.5 border ' + bg },
        React.createElement('div', { className: 'text-label-sm text-neutral-500 mb-1' }, label),
        message.isStreaming
          ? React.createElement(StreamingText, { text: message.content.replace(/```\w+\n[\s\S]*?```/g, ''), isStreaming: true })
          : React.createElement('p', { className: 'text-body text-neutral-100 whitespace-pre-wrap' }, cleanContent),
        running && React.createElement('div', { className: 'mt-2 rounded-md bg-neutral-800/80 border border-white/5 px-3 py-2 text-label-sm text-brand-400 animate-pulse' }, 'Building...'),
        r && React.createElement(ExecutionResult, { stdout: r.stdout, stderr: r.stderr, exitCode: r.exitCode, timedOut: r.timedOut }),
        r && r.exitCode === 0 && !saved &&
          React.createElement('button', { onClick: handleSave, className: 'mt-1 text-label-sm text-brand-400 hover:text-brand-300 transition-colors' }, 'Save as tool'),
        saved && React.createElement('div', { className: 'mt-1 text-label-sm text-success-500' }, 'Tool saved'),
        block && !message.isStreaming && !r && !running &&
          React.createElement('div', { className: 'mt-2 rounded-md bg-neutral-900 border border-brand-500/20 px-3 py-2' },
            React.createElement('div', { className: 'text-label-sm text-brand-400 mb-1' }, 'New tool - ' + block.language),
            React.createElement('pre', { className: 'text-xs font-mono text-neutral-300 max-h-24 overflow-y-auto mb-2 whitespace-pre-wrap' }, block.code),
            React.createElement('button', {
              onClick: () => { setPending(block); setShowPerm(true); },
              className: 'rounded-sm px-3 py-1 text-label-sm bg-brand-500 text-neutral-950 hover:bg-brand-600 transition-colors font-medium focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none'
            }, 'Build')
          ),
        showPerm && pending &&
          React.createElement(PermissionDialog, {
            code: pending.code,
            language: pending.language,
            onAllow: () => run(pending.code, pending.language),
            onDeny: () => setShowPerm(false)
          })
      )
    )
  );
};