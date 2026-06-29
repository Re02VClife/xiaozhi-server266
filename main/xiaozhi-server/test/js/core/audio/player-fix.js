// 音频播放器简化版 — 绕过原播放器的复杂调度
import { log } from '../../utils/logger.js?v=0205';

export class AudioPlayerFix {
    constructor() {
        this.ctx = null;
        this.destination = null;
        this.playing = false;
    }

    async start() {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)();
        if (this.ctx.state === 'suspended') await this.ctx.resume();
        this.destination = this.ctx.createGain();
        this.destination.gain.value = 1.0;
        this.destination.connect(this.ctx.destination);
        log('AudioPlayerFix: 就绪, rate=' + this.ctx.sampleRate, 'success');
    }

    // 直接播放已解码的 PCM Float32 数据
    playPCM(samples, sampleRate) {
        if (!this.ctx || !samples || samples.length === 0) return;
        const buf = this.ctx.createBuffer(1, samples.length, sampleRate);
        buf.copyToChannel(new Float32Array(samples), 0);
        const src = this.ctx.createBufferSource();
        src.buffer = buf;
        src.connect(this.destination);
        src.start();
    }

    enqueueAudioData(opusData) {
        // 使用原始播放器解码 OPUS
        // this will be overridden after player init
    }

    clearAllAudio() {
        this.playing = false;
    }
}

let instance = null;
export function getAudioPlayerFix() {
    if (!instance) instance = new AudioPlayerFix();
    return instance;
}
