import { state } from './state.js';
import { MIC_BUFFER_SIZE, MIC_SAMPLE_RATE } from './config.js';
import { send } from './ws.js';
import { addStatusMsg } from './chat.js';

export async function toggleMic() {
  if (state.micStream) { stopMic(); return; }
  try {
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: MIC_SAMPLE_RATE,
        channelCount: 1,
        echoCancellation: true,
      },
    });
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: MIC_SAMPLE_RATE });
    const source = state.audioCtx.createMediaStreamSource(state.micStream);
    state.scriptNode = state.audioCtx.createScriptProcessor(MIC_BUFFER_SIZE, 1, 1);
    state.scriptNode.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      const int16 = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        int16[i] = Math.max(-32768, Math.min(32767, input[i] * 32768));
      }
      send(int16.buffer);
    };
    source.connect(state.scriptNode);
    state.scriptNode.connect(state.audioCtx.destination);
    document.getElementById('mic-btn').classList.add('active');
    addStatusMsg('麦克风已开启');
  } catch (err) {
    let hint = '无法访问麦克风。';
    if (err.name === 'NotAllowedError') hint = '请允许浏览器使用麦克风权限。';
    else if (err.name === 'NotFoundError') hint = '未检测到麦克风设备。';
    addStatusMsg(hint);
    stopMic();
  }
}

export function stopMic() {
  if (state.scriptNode) { state.scriptNode.disconnect(); state.scriptNode = null; }
  if (state.audioCtx) { state.audioCtx.close(); state.audioCtx = null; }
  if (state.micStream) { state.micStream.getTracks().forEach((t) => t.stop()); state.micStream = null; }
  document.getElementById('mic-btn').classList.remove('active');
}

export function flashWakeIndicator() {
  const btn = document.getElementById('mic-btn');
  if (!btn) return;
  btn.style.boxShadow = '0 0 12px 3px rgba(99, 200, 255, 0.9)';
  btn.style.transform = 'scale(1.15)';
  setTimeout(() => {
    btn.style.boxShadow = '';
    btn.style.transform = '';
  }, 800);
}
