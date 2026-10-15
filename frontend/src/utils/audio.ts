/**
 * 浏览器端音频处理工具
 *
 * 主要用于「语音转文字」：MediaRecorder 录出来的是 webm/opus、mp4/aac 等容器格式，
 * 各家 ASR 接口对这些格式的支持不一致，统一在浏览器里解码重编码成
 * 16bit / 单声道 / 16kHz 的 WAV，兼容性最好，也避免服务端依赖 ffmpeg。
 */

/** 把 Float32 采样编码成 16bit PCM WAV 文件 */
function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const bytesPerSample = 2
  const buffer = new ArrayBuffer(44 + samples.length * bytesPerSample)
  const view = new DataView(buffer)

  const writeString = (offset: number, str: string) => {
    for (let i = 0; i < str.length; i += 1) {
      view.setUint8(offset + i, str.charCodeAt(i))
    }
  }

  writeString(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * bytesPerSample, true)
  writeString(8, 'WAVE')
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true) // fmt chunk 长度
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // 单声道
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * bytesPerSample, true) // byteRate
  view.setUint16(32, bytesPerSample, true) // blockAlign
  view.setUint16(34, 8 * bytesPerSample, true) // bitsPerSample
  writeString(36, 'data')
  view.setUint32(40, samples.length * bytesPerSample, true)

  let offset = 44
  for (let i = 0; i < samples.length; i += 1) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true)
    offset += bytesPerSample
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

/**
 * 把任意浏览器可解码的音频 blob 转成 16kHz/16bit/单声道 WAV
 * 转换失败时返回原始 blob（交给后端直接上传，由服务端兜底）
 */
export async function blobToWav(blob: Blob, sampleRate = 16000): Promise<Blob> {
  try {
    const Ctor: typeof AudioContext =
      window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    if (!Ctor) return blob

    const ctx = new Ctor({ sampleRate })
    try {
      const audioBuffer = await ctx.decodeAudioData(await blob.arrayBuffer())
      const mono = new Float32Array(audioBuffer.length)
      const channels = Math.max(1, audioBuffer.numberOfChannels)
      for (let ch = 0; ch < channels; ch += 1) {
        const data = audioBuffer.getChannelData(ch)
        for (let i = 0; i < mono.length; i += 1) {
          mono[i] += data[i] / channels
        }
      }
      return encodeWav(mono, audioBuffer.sampleRate)
    } finally {
      void ctx.close()
    }
  } catch {
    return blob
  }
}
