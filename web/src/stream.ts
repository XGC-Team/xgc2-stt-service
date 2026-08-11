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

export class MicrophoneStream {
  private socket: WebSocket | null = null
  private media: MediaStream | null = null
  private context: AudioContext | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private capture: AudioWorkletNode | null = null
  private sink: GainNode | null = null

  async start(
    connection: ConnectionSettings,
    onEvent: (event: StreamEvent) => void,
    onClose: (reason: string) => void,
  ): Promise<void> {
    if (!window.isSecureContext) {
      throw new Error('麦克风需要 HTTPS；仅 localhost 可使用 HTTP')
    }
    const query = new URLSearchParams({ sample_rate: '16000' })
    if (connection.apiKey) query.set('access_token', connection.apiKey)
    const socket = new WebSocket(websocketUrl(connection, query))
    socket.binaryType = 'arraybuffer'
    this.socket = socket
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
    socket.addEventListener('message', (message) => {
      if (typeof message.data !== 'string') return
      try {
        onEvent(JSON.parse(message.data) as StreamEvent)
      } catch {
        onEvent({ type: 'error', message: '服务返回了无效事件' })
      }
    })
    socket.addEventListener('close', (event) => onClose(event.reason || `连接已关闭 (${event.code})`))

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
    await this.releaseAudio()
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: 'reset' }))
      this.socket.close(1000, 'cancelled')
    }
    this.socket = null
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
  }
}
