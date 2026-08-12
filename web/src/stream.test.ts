// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { MicrophoneStream, pcmWaveformLevels } from './stream'

class FakeWebSocket extends EventTarget {
  static readonly OPEN = 1
  static instances: FakeWebSocket[] = []
  readonly close = vi.fn()
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
})
