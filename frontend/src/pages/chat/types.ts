export interface ToolStep {
  tool: string;
  status?: string;
  actionId?: string;
}

export interface PendingAction {
  id: string;
  title: string;
  risk: string;
  expiresAt: string;
  preview: unknown;
}

export interface ChatReply {
  sessionId: string;
  reply: string;
  steps?: ToolStep[];
  pendingActions?: PendingAction[];
}

export interface ExecutedAction {
  title: string;
  status: string;
  result?: {
    downloadUrl?: string;
    previewUrl?: string;
    fileName?: string;
    [key: string]: unknown;
  };
}

export interface AgentTool {
  name: string;
  risk: string;
  riskLabel: string;
  needsConfirm: boolean;
  description: string;
}

export interface AgentStatus {
  agent: {
    enabled: boolean;
    available: boolean;
    maxToolSteps: number;
    llm: { configured: boolean; model: string; endpoint: string };
    tools: AgentTool[];
  };
  forecast: { ready: boolean };
}

/** 一条对话气泡。待确认动作挂在助手回复上，确认后原地替换成执行结果。 */
export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  pending?: boolean;
  error?: boolean;
  steps?: ToolStep[];
  actions?: PendingAction[];
  executed?: Record<string, ExecutedAction>;
}
