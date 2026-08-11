class Xgc2PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.targetSampleRate = options.processorOptions?.targetSampleRate || 16000
    this.ratio = sampleRate / this.targetSampleRate
    this.pending = []
    this.offset = 0
    this.output = []
  }

  process(inputs) {
    const channel = inputs[0]?.[0]
    if (!channel) return true
    for (let index = 0; index < channel.length; index += 1) this.pending.push(channel[index])
    while (this.offset + 1 < this.pending.length) {
      const left = Math.floor(this.offset)
      const fraction = this.offset - left
      const sample = this.pending[left] * (1 - fraction) + this.pending[left + 1] * fraction
      this.output.push(Math.max(-1, Math.min(1, sample)))
      this.offset += this.ratio
    }
    const consumed = Math.floor(this.offset)
    if (consumed > 0) {
      this.pending.splice(0, consumed)
      this.offset -= consumed
    }
    if (this.output.length >= 320) {
      const pcm = new Int16Array(this.output.length)
      for (let index = 0; index < this.output.length; index += 1) {
        const sample = this.output[index]
        pcm[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff
      }
      this.output = []
      this.port.postMessage(pcm.buffer, [pcm.buffer])
    }
    return true
  }
}

registerProcessor('xgc2-pcm-capture', Xgc2PcmCapture)
