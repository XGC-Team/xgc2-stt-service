// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { MicrophoneStream } from './stream'

class FakeWebSocket extends EventTarget {
  static readonly OPEN = 1
  static instances: FakeWebSocket[] = []
  readonly close = vi.fn()
  readonly send = vi.fn()
  binaryType = ''
  readyState = FakeWebSocket.OPEN

  constructor(_url: string) {
    super()
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
      { endpoint: 'http://example.test', apiKey: '' },
      vi.fn(),
      vi.fn(),
    )).rejects.toThrow('permission denied')

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalledWith(4001, 'microphone unavailable')
  })
})
