export interface Conversation {
  id: string;
  title: string;
  model_id: string;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  tool_calls_json: string | null;
  token_count: number;
  created_at: string;
}

export interface ProviderConfig {
  id: string;
  provider: string;
  model_id: string;
  label: string | null;
  connected: boolean;
}

export interface ModelInfo {
  id: string;
  name: string;
  size_mb: number;
  downloaded: boolean;
  provider: string;
}

export interface HardwareInfo {
  os: string;
  cpu_cores: number;
  total_ram_mb: number;
  gpu_name: string;
  gpu_vram_mb: number;
  recommended_model: string;
}

export interface Tool {
  id: string;
  name: string;
  description: string;
  schema_json: string;
  enabled: boolean;
  is_builtin: boolean;
  created_at: string;
}

export type PermissionDefault = "ask" | "allow" | "deny";