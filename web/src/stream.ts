import { websocketUrl, type ConnectionSettings } from './api'

export interface StreamEvent {
  type: string
  session_id?: string
  sequence?: number
  text?: string
  message?: string
  state?: string
  audio_seconds?: number
  inference_seconds?: number
}

export type AudioLevelListener = (levels: readonly number[]) => void

export class MicrophoneStream {
  private socket: WebSocket | null = null
  private media: MediaStream | null = null
  private context: AudioContext | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private capture: AudioWorkletNode | null = null
  private sink: GainNode | null = null
  private onLevels: AudioLevelListener | null = null

  async start(
    connection: ConnectionSettings,
    onEvent: (event: StreamEvent) => void,
    onClose: (reason: string) => void,
    onLevels?: AudioLevelListener,
  ): Promise<void> {
    if (!window.isSecureContext) {
      throw new Error('麦克风需要 HTTPS；仅 localhost 可使用 HTTP')
    }
    const query = new URLSearchParams({
      sample_rate: '16000',
      output_script: connection.outputScript,
      trim_leading_silence: connection.trimLeadingSilence ? '1' : '0',
    })
    if (connection.apiKey) query.set('access_token', connection.apiKey)
    const socket = new WebSocket(websocketUrl(connection, query))
    socket.binaryType = 'arraybuffer'
    this.socket = socket
    this.onLevels = onLevels ?? null
    try {
      await new Promise<void>((resolve, reject) => {
        const timeout = window.setTimeout(() => reject(new Error('WebSocket 连接超时')), 10_000)
        socket.addEventListener('open', () => {
          window.clearTimeout(timeout)
          resolve()
        }, { once: true })
        socket.addEventListener('error', () => {
          window.clearTimeout(timeout)
          reject(new Error('WebSocket 连接失败'))
        }, { once: true })
      })
    } catch (error) {
      this.socket = null
      this.onLevels?.([])
      this.onLevels = null
      throw error
    }
    socket.addEventListener('message', (message) => {
      if (typeof message.data !== 'string') return
      try {
        onEvent(JSON.parse(message.data) as StreamEvent)
      } catch {
        onEvent({ type: 'error', message: '服务返回了无效事件' })
      }
    })
    socket.addEventListener('close', (event) => {
      void this.releaseAudio()
      onClose(event.reason || `连接已关闭 (${event.code})`)
    })

    try {
      this.media = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      this.context = new AudioContext()
      await this.context.audioWorklet.addModule('/pcm-worklet.js')
      this.source = this.context.createMediaStreamSource(this.media)
      this.capture = new AudioWorkletNode(this.context, 'xgc2-pcm-capture', {
        processorOptions: { targetSampleRate: 16000 },
      })
      this.sink = this.context.createGain()
      this.sink.gain.value = 0
      this.capture.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
        this.onLevels?.(pcmWaveformLevels(event.data))
        if (socket.readyState === WebSocket.OPEN) socket.send(event.data)
      }
      this.source.connect(this.capture)
      this.capture.connect(this.sink)
      this.sink.connect(this.context.destination)
    } catch (error) {
      // Browsers only allow clients to close with 1000 or an application code
      // in the 3000-4999 range. 1011 is reserved for server-side failures.
      socket.close(4001, 'microphone unavailable')
      await this.releaseAudio()
      throw error
    }
  }

  async stop(): Promise<void> {
    await this.releaseAudio()
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: 'commit' }))
  }

  close(): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: 'close' }))
    }
    this.socket = null
  }

  async cancel(): Promise<void> {
    const socket = this.socket
    this.socket = null
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'reset' }))
      socket.close(1000, 'cancelled')
    }
    await this.releaseAudio()
  }

  private async releaseAudio(): Promise<void> {
    this.capture?.disconnect()
    this.source?.disconnect()
    this.sink?.disconnect()
    this.media?.getTracks().forEach((track) => track.stop())
    if (this.context && this.context.state !== 'closed') await this.context.close()
    this.capture = null
    this.source = null
    this.sink = null
    this.media = null
    this.context = null
    this.onLevels?.([])
    this.onLevels = null
  }
}

/** Convert the exact PCM frame sent to STT into normalized peak levels for UI instrumentation. */
export function pcmWaveformLevels(buffer: ArrayBuffer, barCount = 24): number[] {
  const pcm = new Int16Array(buffer)
  const count = Math.max(3, Math.min(64, Math.round(barCount)))
  if (!pcm.length) return Array.from({ length: count }, () => 0)
  return Array.from({ length: count }, (_, index) => {
    const start = Math.floor(index * pcm.length / count)
    const end = Math.max(start + 1, Math.ceil((index + 1) * pcm.length / count))
    let peak = 0
    for (let cursor = start; cursor < Math.min(end, pcm.length); cursor += 1) {
      peak = Math.max(peak, Math.abs(pcm[cursor] ?? 0) / 0x8000)
    }
    return Math.min(1, peak)
  })
}
