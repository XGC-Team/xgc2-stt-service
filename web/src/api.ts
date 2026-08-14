export interface EngineStatus {
  state: 'idle' | 'loading' | 'ready' | 'error' | string
  error: string | null
  backend: string
  variant: 'qwen' | string
  model: string
  device: string
  device_index: number
  compute_type: string
  cuda_devices: number | null
  loaded_at: number | null
  load_seconds: number | null
  transcription_delay_ms: number
  process_id: number | null
}

export interface ServiceStatus {
  service: string
  version: string
  uptime_seconds: number
  authentication: 'api-key' | 'trusted-network'
  stream: {
    protocol: string
    format: string
    sample_rate: number
    channels: number
    transcription_delay_ms: number
    finalization: string
    silence_commit_ms: number
    active: number
    capacity: number
  }
  engine: EngineStatus
  gpu: GpuStatus
}

export interface GpuSample {
  sampled_at: number
  utilization_percent: number | null
  memory_percent: number | null
  temperature_c: number | null
  power_watts: number | null
}

export interface GpuStatus {
  available: boolean
  error?: string
  name?: string
  sampled_at?: number
  utilization_percent?: number
  memory_used_bytes?: number
  memory_total_bytes?: number
  memory_percent?: number
  temperature_c?: number
  power_watts?: number
  power_limit_watts?: number
  history: GpuSample[]
}

export interface ApiKeyRecord {
  id: string
  name: string
  prefix: string
  created_at: string
  rotated_at?: string
  last_used_at: string | null
  request_count: number
  stream_sessions: number
  audio_bytes: number
  audio_seconds: number
  active_sessions: number
  enabled: boolean
}

export interface RuntimeTuning {
  silence_commit_ms: number
  max_active_streams: number
  gpu_memory_utilization: number
  max_model_len: number
  max_num_seqs: number
  vllm_enforce_eager: boolean
  qwen_chunk_size_seconds: number
  qwen_unfixed_chunk_num: number
  qwen_unfixed_token_num: number
}

export interface RuntimeSettingsResponse {
  values: RuntimeTuning
  pending: Partial<RuntimeTuning>
  restart_required: boolean
}

export interface ConnectionSettings {
  endpoint: string
  apiKey: string
  outputScript: 'simplified' | 'original'
  trimLeadingSilence: boolean
}

interface ApiKeyMutation {
  key: ApiKeyRecord
  secret: string
}

export function normalizeEndpoint(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, '')
  return trimmed || window.location.origin
}

export async function getStatus(): Promise<ServiceStatus> {
  const response = await fetch('/api/status')
  if (!response.ok) {
    throw new Error(`状态请求失败 (${response.status})`)
  }
  return response.json() as Promise<ServiceStatus>
}

export async function getApiKeys(): Promise<ApiKeyRecord[]> {
  const response = await fetch('/api/keys')
  if (!response.ok) throw new Error(`API Key 请求失败 (${response.status})`)
  const payload = await response.json() as { keys: ApiKeyRecord[] }
  return payload.keys
}

export async function createApiKey(name: string): Promise<ApiKeyMutation> {
  const response = await fetch('/api/keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!response.ok) throw new Error(`API Key 创建失败 (${response.status})`)
  return response.json() as Promise<ApiKeyMutation>
}

export async function rotateApiKey(id: string): Promise<ApiKeyMutation> {
  const response = await fetch(`/api/keys/${encodeURIComponent(id)}/rotate`, { method: 'POST' })
  if (!response.ok) throw new Error(`API Key 轮换失败 (${response.status})`)
  return response.json() as Promise<ApiKeyMutation>
}

export async function revokeApiKey(id: string): Promise<ApiKeyRecord> {
  const response = await fetch(`/api/keys/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!response.ok) throw new Error(`API Key 停用失败 (${response.status})`)
  const payload = await response.json() as { key: ApiKeyRecord }
  return payload.key
}

export async function getRuntimeSettings(): Promise<RuntimeSettingsResponse> {
  const response = await fetch('/api/settings')
  if (!response.ok) throw new Error(`服务参数请求失败 (${response.status})`)
  return response.json() as Promise<RuntimeSettingsResponse>
}

export async function updateRuntimeSettings(values: RuntimeTuning): Promise<RuntimeSettingsResponse> {
  const response = await fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(values),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(payload.detail || `服务参数保存失败 (${response.status})`)
  }
  return response.json() as Promise<RuntimeSettingsResponse>
}

export async function restartEngine(): Promise<RuntimeSettingsResponse> {
  const response = await fetch('/api/engine/restart', { method: 'POST' })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(payload.detail || `模型重启失败 (${response.status})`)
  }
  return response.json() as Promise<RuntimeSettingsResponse>
}

export function websocketUrl(settings: ConnectionSettings, query: URLSearchParams): string {
  const endpoint = new URL(normalizeEndpoint(settings.endpoint))
  endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:'
  endpoint.pathname = '/v1/audio/transcriptions/stream'
  endpoint.search = query.toString()
  return endpoint.toString()
}
