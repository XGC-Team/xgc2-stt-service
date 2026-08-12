// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { MicrophoneStream, pcmWaveformLevels } from './stream'

class FakeWebSocket extends EventTarget {
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []
  readonly close = vi.fn(() => {
    this.readyState = FakeWebSocket.CLOSED
    queueMicrotask(() => this.dispatchEvent(new Event('close')))
  })
  readonly send = vi.fn()
  readonly url: string
  binaryType = ''
  readyState = FakeWebSocket.OPEN

  constructor(url: string) {
    super()
    this.url = url
    FakeWebSocket.instances.push(this)
    queueMicrotask(() => this.dispatchEvent(new Event('open')))
  }
}

describe('MicrophoneStream', () => {
  afterEach(() => {
    FakeWebSocket.instances = []
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('uses a browser-valid application close code when microphone access fails', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new Error('permission denied')) },
    })

    const stream = new MicrophoneStream()
    await expect(stream.start(
      {
        endpoint: 'http://example.test',
        apiKey: '',
        outputScript: 'simplified',
        trimLeadingSilence: true,
      },
      vi.fn(),
      vi.fn(),
    )).rejects.toThrow('permission denied')

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalledWith(4001, 'microphone unavailable')
    expect(FakeWebSocket.instances[0]?.url).toBe(
      'ws://example.test/v1/audio/transcriptions/stream?sample_rate=16000&output_script=simplified&trim_leading_silence=1',
    )
  })

  it('derives waveform levels from the same PCM samples sent to transcription', () => {
    const pcm = new Int16Array([0, 16384, -32768, 8192])
    expect(pcmWaveformLevels(pcm.buffer, 4)).toEqual([0, 0.5, 1, 0.25])
    expect(pcmWaveformLevels(new ArrayBuffer(0), 3)).toEqual([0, 0, 0])
  })

  it('replaces the recognition socket without stopping microphone capture when cleared', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const connection = {
      endpoint: 'http://example.test',
      apiKey: '',
      outputScript: 'simplified' as const,
      trimLeadingSilence: true,
    }
    const stream = new MicrophoneStream()
    const previous = new FakeWebSocket('ws://old.test')
    Reflect.set(stream, 'connection', connection)
    Reflect.set(stream, 'eventListener', vi.fn())
    Reflect.set(stream, 'closeListener', vi.fn())
    Reflect.set(stream, 'socket', previous)

    await stream.clearSession()

    expect(previous.send).toHaveBeenCalledWith(JSON.stringify({ type: 'reset' }))
    expect(previous.close).toHaveBeenCalledWith(1000, 'cleared')
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(Reflect.get(stream, 'socket')).toBe(FakeWebSocket.instances[1])
  })
})
