import { describe, expect, it } from 'vitest'
import { websocketUrl } from './api'

describe('websocketUrl', () => {
  it('keeps the configured origin and upgrades https', () => {
    const query = new URLSearchParams({ sample_rate: '16000' })
    expect(websocketUrl({
      endpoint: 'https://stt.lan/',
      apiKey: '',
      outputScript: 'simplified',
      trimLeadingSilence: true,
    }, query)).toBe(
      'wss://stt.lan/v1/audio/transcriptions/stream?sample_rate=16000',
    )
  })
})
