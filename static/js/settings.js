import { state } from './state.js';
import { SETTINGS_STORAGE_KEYS } from './config.js';
import { clearChat } from './persistence.js';
import { addStatusMsg } from './chat.js';
import { send } from './ws.js';
import { esc } from './esc.js';

/** 初始化设置面板事件 */
export function setupSettings() {
  document.getElementById('vol-slider').value = state.volume;
  if (!state.ttsEnabled) document.getElementById('tts-toggle').classList.remove('on');

  // 设置按钮
  document.getElementById('settings-btn').onclick = () => {
    document.getElementById('tools-panel').classList.remove('open');
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

  // 唤醒词开关
  if (state.wakeEnabled) document.getElementById('wake-toggle').classList.add('on');
  document.getElementById('wake-toggle').onclick = function () {
    this.classList.toggle('on');
    state.wakeEnabled = this.classList.contains('on');
    localStorage.setItem(SETTINGS_STORAGE_KEYS.WAKE, state.wakeEnabled ? 'on' : 'off');
    send({ type: 'wake_toggle', enabled: state.wakeEnabled });
    addStatusMsg(state.wakeEnabled ? '唤醒词已开启' : '唤醒词已关闭');
  };

  // Live2D 初始状态同步
  if (!state.live2dEnabled) {
    document.getElementById('live2d-toggle').classList.remove('on');
    toggleLive2D();
  }

  // Live2D 开关
  document.getElementById('live2d-toggle').onclick = function () {
    this.classList.toggle('on');
    state.live2dEnabled = this.classList.contains('on');
    localStorage.setItem(SETTINGS_STORAGE_KEYS.LIVE2D, state.live2dEnabled ? 'on' : 'off');
    toggleLive2D();
    addStatusMsg(state.live2dEnabled ? 'Live2D 已开启' : 'Live2D 已关闭');
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

  // 模型配置
  loadProviders();
  setupCustomModal();
}

// ---- 模型配置 ----

/** 从 /api/providers 加载可用 provider 列表并填充下拉框 */
async function loadProviders() {
  try {
    const res = await fetch('/api/providers');
    const data = await res.json();

    fillSelect('llm-model-select', data.llm, data.custom?.llm || [], 'llm');
    fillSelect('stt-model-select', data.stt, data.custom?.stt || [], 'stt');
    fillSelect('tts-model-select', data.tts, data.custom?.tts || [], 'tts');

    renderCustomProviders(data.custom);
  } catch (e) {
    console.warn('加载 provider 列表失败', e);
  }
}

function fillSelect(selectId, registry, customEntries, service) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  sel.innerHTML = '';

  for (const [provider, info] of Object.entries(registry)) {
    if (!info.models || info.models.length === 0) continue;
    const models = provider === 'custom'
      ? customEntries.map(e => e.name)
      : info.models;
    for (const m of models) {
      const value = `${provider}/${m}`;
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = `${provider}/${m}`;
      sel.appendChild(opt);
    }
  }

  // 读取保存的当前选择
  const saved = localStorage.getItem(`model-${service}`);
  if (saved) sel.value = saved;

  sel.onchange = () => {
    const modelId = sel.value;
    if (!modelId) return;
    localStorage.setItem(`model-${service}`, modelId);
    send({ type: 'switch_model', service, model: modelId });
    addStatusMsg(`${service.toUpperCase()} 已切换至 ${modelId}`);
  };
}

function renderCustomProviders(customData) {
  const container = document.getElementById('custom-providers-list');
  if (!container) return;
  container.innerHTML = '';

  const all = [
    ...(customData?.llm || []).map(e => ({ ...e, service: 'llm' })),
    ...(customData?.stt || []).map(e => ({ ...e, service: 'stt' })),
    ...(customData?.tts || []).map(e => ({ ...e, service: 'tts' })),
  ];

  for (const entry of all) {
    const div = document.createElement('div');
    div.style.cssText = 'border:1px solid var(--border);border-radius:8px;padding:8px;margin-bottom:6px;font-size:12px;';
    div.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:center;">' +
        '<span style="font-weight:600;">' + esc(entry.name) + '</span>' +
        '<span style="color:var(--muted);font-size:10px;">' + entry.service + '</span>' +
      '</div>' +
      '<div style="color:var(--muted);margin:4px 0;">' + esc(entry.base_url) + '</div>' +
      '<div style="color:var(--muted);">models: ' + (entry.models || []).join(', ') + '</div>' +
      '<button data-delete-svc="' + esc(entry.service) + '" data-delete-name="' + esc(entry.name) + '" style="margin-top:4px;padding:2px 8px;font-size:11px;background:transparent;border:1px solid #a33;color:#f88;border-radius:4px;cursor:pointer;">删除</button>';
    container.appendChild(div);
  }

  // 删除按钮事件
  container.querySelectorAll('button[data-delete-svc]').forEach(btn => {
    btn.onclick = async () => {
      const service = btn.dataset.deleteSvc;
      const name = btn.dataset.deleteName;
      if (!confirm('确定删除 ' + name + '？')) return;
      try {
        const res = await fetch('/api/providers/' + service + '/' + encodeURIComponent(name), { method: 'DELETE' });
        if (res.ok) {
          addStatusMsg(name + ' 已删除');
          loadProviders();
        }
      } catch (e) {
        addStatusMsg('删除失败');
      }
    };
  });
}

// ---- 自定义端点模态框 ----

function setupCustomModal() {
  const modal = document.getElementById('custom-modal');
  const addBtn = document.getElementById('add-custom-btn');
  const cancelBtn = document.getElementById('custom-cancel-btn');
  const saveBtn = document.getElementById('custom-save-btn');
  const testBtn = document.getElementById('custom-test-btn');

  if (!modal || !addBtn) return;

  addBtn.onclick = () => { modal.style.display = 'flex'; };
  cancelBtn.onclick = () => { modal.style.display = 'none'; };

  saveBtn.onclick = async () => {
    const service = document.getElementById('custom-service').value;
    const name = document.getElementById('custom-name').value.trim();
    const base_url = document.getElementById('custom-url').value.trim();
    const api_key_env = document.getElementById('custom-key-env').value.trim();
    const modelsStr = document.getElementById('custom-models').value.trim();

    if (!name || !base_url) {
      alert('名称和 Base URL 不能为空');
      return;
    }

    const entry = {
      name, base_url, api_key_env,
      models: modelsStr ? modelsStr.split(',').map(s => s.trim()).filter(Boolean) : [],
    };

    try {
      const res = await fetch('/api/providers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service, entry }),
      });
      const data = await res.json();
      if (data.ok) {
        addStatusMsg(name + ' 已保存');
        modal.style.display = 'none';
        loadProviders();
      } else {
        alert('保存失败: ' + (data.error || '未知错误'));
      }
    } catch (e) {
      alert('保存失败: ' + e.message);
    }
  };

  testBtn.onclick = async () => {
    const service = document.getElementById('custom-service').value;
    const base_url = document.getElementById('custom-url').value.trim();
    const modelsStr = document.getElementById('custom-models').value.trim();
    const model = modelsStr ? modelsStr.split(',')[0].trim() : 'default';
    const resultEl = document.getElementById('custom-test-result');

    if (!base_url) {
      resultEl.innerHTML = '<span style="color:#f88;">请输入 Base URL</span>';
      return;
    }

    resultEl.innerHTML = '<span style="color:var(--muted);">测试中…</span>';
    try {
      const res = await fetch('/api/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service, base_url, api_key: '', model }),
      });
      const data = await res.json();
      if (data.ok) {
        resultEl.innerHTML = '<span style="color:#4caf50;">连接成功 (' + data.latency_ms + 'ms)</span>';
      } else {
        resultEl.innerHTML = '<span style="color:#f88;">' + esc(data.error || '连接失败') + '</span>';
      }
    } catch (e) {
      resultEl.innerHTML = '<span style="color:#f88;">' + esc(e.message) + '</span>';
    }
  };
}

/** 根据 WS capabilities 消息更新 UI 状态指示 */
export function updateCapabilityIndicators() {
  const micBtn = document.getElementById('mic-btn');
  if (micBtn) {
    if (!state.sttAvailable) {
      micBtn.title = 'STT 服务不可用，语音识别已禁用';
      micBtn.style.opacity = '0.4';
      micBtn.style.cursor = 'not-allowed';
    } else {
      micBtn.title = '';
      micBtn.style.opacity = '';
      micBtn.style.cursor = '';
    }
  }
  // TTS 状态指示 — 服务端不可用时强制关闭 toggle
  const ttsToggle = document.getElementById('tts-toggle');
  if (ttsToggle) {
    if (!state.ttsAvailable) {
      ttsToggle.classList.remove('on');
      ttsToggle.title = 'TTS 服务不可用';
      ttsToggle.style.opacity = '0.4';
      ttsToggle.style.cursor = 'not-allowed';
    } else {
      ttsToggle.title = '';
      ttsToggle.style.opacity = '';
      ttsToggle.style.cursor = '';
    }
  }
  // 更新模型标签
  setModelTag(state.llmModel || '');
}

/** 在角色名下方显示当前 LLM 模型 */
export function setModelTag(modelId) {
  const tag = document.getElementById('llm-model-tag');
  if (tag) {
    tag.textContent = modelId || '';
    tag.style.display = modelId ? '' : 'none';
  }
}

/** 切换 Live2D iframe / 静态图片 */
export function toggleLive2D() {
  const wrap = document.getElementById('avatar-wrap');
  if (!wrap) return;
  const iframe = document.getElementById('live2d-frame');
  const staticImg = document.getElementById('live2d-static-img');

  if (state.live2dEnabled) {
    // 开启 Live2D — 移除静态图，重建 iframe
    if (staticImg) staticImg.remove();
    if (!iframe) {
      const newIframe = document.createElement('iframe');
      newIframe.id = 'live2d-frame';
      newIframe.src = '/static/live2d-demo/index.html';
      newIframe.sandbox = 'allow-scripts allow-same-origin';
      wrap.appendChild(newIframe);
    }
  } else {
    // 关闭 Live2D — 移除 iframe 释放资源，显示静态图
    if (iframe) iframe.remove();
    if (!staticImg) {
      const img = document.createElement('img');
      img.id = 'live2d-static-img';
      img.src = state.live2dStaticImage || '';
      img.alt = '';
      if (!state.live2dStaticImage) img.style.display = 'none';
      wrap.appendChild(img);
    }
  }
}
