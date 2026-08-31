"use strict";

class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    // 60 ms at 16 kHz; four frames form one 240 ms Paraformer window.
    this.outputChunk = 960;
    this.pending = new Float32Array(0);
    this.position = 0;
    this.output = new Int16Array(this.outputChunk);
    this.outputOffset = 0;
    this.port.onmessage = (event) => {
      if (event.data?.type === "reset") {
        this.pending = new Float32Array(0);
        this.position = 0;
        this.outputOffset = 0;
      }
    };
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input?.length) return true;
    const combined = new Float32Array(this.pending.length + input.length);
    combined.set(this.pending);
    combined.set(input, this.pending.length);
    this.pending = combined;
    const ratio = sampleRate / this.targetRate;
    while (Math.floor(this.position) + 1 < this.pending.length) {
      const left = Math.floor(this.position);
      const fraction = this.position - left;
      const value = this.pending[left] * (1 - fraction) + this.pending[left + 1] * fraction;
      const clipped = Math.max(-1, Math.min(1, value));
      this.output[this.outputOffset++] = clipped < 0 ? clipped * 32768 : clipped * 32767;
      this.position += ratio;
      if (this.outputOffset === this.outputChunk) {
        const chunk = this.output.buffer;
        this.port.postMessage(chunk, [chunk]);
        this.output = new Int16Array(this.outputChunk);
        this.outputOffset = 0;
      }
    }
    const consumed = Math.floor(this.position);
    if (consumed) {
      this.pending = this.pending.slice(consumed);
      this.position -= consumed;
    }
    return true;
  }
}

registerProcessor("pcm16-downsampler", Pcm16Downsampler);
