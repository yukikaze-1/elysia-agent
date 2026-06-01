import { state } from './state.js';
import { SETTINGS_STORAGE_KEYS } from './config.js';
import { clearChat } from './persistence.js';
import { addStatusMsg } from './chat.js';

/** 初始化设置面板事件 */
export function setupSettings() {
  document.getElementById('vol-slider').value = state.volume;
  if (!state.ttsEnabled) document.getElementById('tts-toggle').classList.remove('on');

  // 设置按钮
  document.getElementById('settings-btn').onclick = () => {
    document.getElementById('settings-panel').classList.toggle('open');
  };

  // 面板外点击关闭
  document.addEventListener('click', (e) => {
    const panel = document.getElementById('settings-panel');
    const btn = document.getElementById('settings-btn');
    if (!panel.classList.contains('open')) return;
    if (!panel.contains(e.target) && e.target !== btn) {
      panel.classList.remove('open');
    }
  });

  // TTS 开关
  document.getElementById('tts-toggle').onclick = function () {
    this.classList.toggle('on');
    state.ttsEnabled = this.classList.contains('on');
    localStorage.setItem(SETTINGS_STORAGE_KEYS.TTS, state.ttsEnabled ? 'on' : 'off');
    if (!state.ttsEnabled) {
      state.audioChunks = {};
      state.audioQueue.length = 0;
    }
  };

  // 音量
  document.getElementById('vol-slider').oninput = function () {
    state.volume = parseInt(this.value, 10);
    localStorage.setItem(SETTINGS_STORAGE_KEYS.VOL, state.volume);
  };

  // 关闭服务
  document.getElementById('shutdown-btn').onclick = async () => {
    if (!confirm('确定要关闭 Elysia 服务吗？')) return;
    try {
      const res = await fetch('/shutdown', { method: 'POST' });
      if (res.ok) {
        addStatusMsg('服务已关闭');
        setTimeout(() => {
          document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-size:18px;">Elysia 已关闭 🫧</div>';
        }, 500);
      } else {
        addStatusMsg('关闭失败');
      }
    } catch (e) {
      addStatusMsg('服务已关闭');
      setTimeout(() => {
        document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-size:18px;">Elysia 已关闭 🫧</div>';
      }, 500);
    }
  };

  // 清空对话 (加确认)
  document.getElementById('clear-btn').onclick = () => {
    if (!confirm('确定要清空所有对话记录吗？此操作不可撤销。')) return;
    clearChat();
  };
}
