import { invoke } from "@tauri-apps/api/core";
import type { Conversation, Message } from "../types";

export async function sendMessage(message: string, conversationId?: string, modelId?: string): Promise<Conversation> {
  return invoke("send_message", { message, conversationId, modelId });
}

export async function getConversations(): Promise<Conversation[]> {
  return invoke("get_conversations");
}

export async function getMessages(conversationId: string): Promise<Message[]> {
  return invoke("get_messages", { conversationId });
}

export async function deleteConversation(id: string): Promise<void> {
  return invoke("delete_conversation", { conversationId: id });
}

export async function updateConversationTitle(id: string, title: string): Promise<void> {
  return invoke("update_conversation_title", { conversationId: id, title });
}

export interface CodeResult { stdout: string; stderr: string; exit_code: number; timed_out: boolean; }
export interface ToolEntry { name: string; description: string; language: string; }
export interface ProviderConfig { id: string; provider: string; modelId: string; label: string | null; connected: boolean; }
export interface HardwareInfo { os: string; cpu_cores: number; total_ram_mb: number; gpu_name: string; gpu_vram_mb: number; recommended_model: string; }
export interface ModelInfo { id: string; name: string; size_mb: number; downloaded: boolean; provider: string; }

export async function executeCode(code: string, language?: string, timeLimit?: number): Promise<CodeResult> {
  return invoke("execute_code", { code, language, timeLimit });
}

export async function listTools(): Promise<ToolEntry[]> { return invoke("list_tools"); }
export async function saveTool(name: string, code: string, language: string, description: string): Promise<ToolEntry> {
  return invoke("save_tool", { name, code, language, description });
}
export async function deleteTool(toolName: string): Promise<void> { return invoke("delete_tool", { toolName }); }

export async function addApiKey(provider: string, key: string, label?: string): Promise<ProviderConfig> {
  return invoke("add_api_key", { provider, key, label });
}
export async function removeApiKey(id: string): Promise<void> { return invoke("remove_api_key", { id }); }
export async function listProviders(): Promise<ProviderConfig[]> { return invoke("list_providers"); }
export async function getHardwareInfo(): Promise<HardwareInfo> { return invoke("get_hardware_info"); }
export async function listModels(): Promise<ModelInfo[]> { return invoke("list_models"); }

export async function getSetting(key: string): Promise<string | null> { return invoke("get_setting", { key }); }
export async function setSetting(key: string, value: unknown): Promise<void> { return invoke("set_setting", { key, value }); }
export async function getAllSettings(): Promise<Array<[string, string]>> { return invoke("get_all_settings"); }

export async function completeOnboarding(): Promise<void> {
  return invoke("complete_onboarding");
}

export async function setViewSize(expanded: boolean): Promise<void> {
  return invoke("set_view_size", { expanded });
}