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

  // 重启服务
  document.getElementById('restart-btn').onclick = async () => {
    if (!confirm('确定要重启 Elysia 服务吗？')) return;
    try {
      const res = await fetch('/restart', { method: 'POST' });
      if (res.ok) {
        addStatusMsg('服务正在重启…');
      }
    } catch (_) {
      // fetch 失败说明服务已断开，静默等待恢复
    }
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-size:18px;">服务重启中…</div>';
    // 轮询 /health 等待服务恢复后自动刷新
    for (let i = 0; i < 60; i++) {
      await new Promise(r => setTimeout(r, 1000));
      try {
        const hr = await fetch('/health');
        if (hr.ok) { location.reload(); return; }
      } catch (_) {}
    }
    location.reload();  // 60 秒超时，强制刷新
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
