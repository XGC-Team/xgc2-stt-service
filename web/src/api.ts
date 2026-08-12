export interface EngineStatus {
  state: 'idle' | 'loading' | 'ready' | 'error' | string
  error: string | null
  backend: string
  variant: 'voxtral' | 'qwen' | string
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
  }
  engine: EngineStatus
}

export interface ConnectionSettings {
  endpoint: string
  apiKey: string
  outputScript: 'simplified' | 'original'
  trimLeadingSilence: boolean
}

export function normalizeEndpoint(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, '')
  return trimmed || window.location.origin
}

export async function getStatus(settings: ConnectionSettings): Promise<ServiceStatus> {
  const response = await fetch(`${normalizeEndpoint(settings.endpoint)}/api/status`, {
    headers: settings.apiKey ? { Authorization: `Bearer ${settings.apiKey}` } : {},
  })
  if (!response.ok) {
    throw new Error(response.status === 401 ? 'API key 无效' : `状态请求失败 (${response.status})`)
  }
  return response.json() as Promise<ServiceStatus>
}

export function websocketUrl(settings: ConnectionSettings, query: URLSearchParams): string {
  const endpoint = new URL(normalizeEndpoint(settings.endpoint))
  endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:'
  endpoint.pathname = '/v1/audio/transcriptions/stream'
  endpoint.search = query.toString()
  return endpoint.toString()
}
