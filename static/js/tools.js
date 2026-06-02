import { esc } from './chat.js';

/** 初始化工具管理面板 */
export function setupTools() {
  const panel = document.getElementById('tools-panel');
  const list = document.getElementById('tools-list');
  const sp = document.getElementById('settings-panel');

  // 工具按钮
  document.getElementById('tools-btn').onclick = () => {
    const wasOpen = panel.classList.contains('open');
    sp.classList.remove('open');
    if (wasOpen) {
      panel.classList.remove('open');
    } else {
      panel.classList.add('open');
      loadPlugins();
    }
  };

  // 面板外点击关闭
  document.addEventListener('click', (e) => {
    if (!panel.classList.contains('open')) return;
    const btn = document.getElementById('tools-btn');
    if (!panel.contains(e.target) && e.target !== btn) {
      panel.classList.remove('open');
    }
  });
}

/** 加载并渲染插件列表 */
async function loadPlugins() {
  const list = document.getElementById('tools-list');
  list.innerHTML = '<div class="tools-loading">加载中…</div>';
  try {
    const res = await fetch('/tools');
    const data = await res.json();
    if (data.plugins && data.plugins.length > 0) {
      list.innerHTML = data.plugins.map(renderCard).join('');
    } else {
      list.innerHTML = '<div class="tools-empty">暂无已加载的插件</div>';
    }
  } catch (_) {
    list.innerHTML = '<div class="tools-empty">加载失败</div>';
  }
}

/** 渲染单个插件卡片 */
function renderCard(p) {
  const caps = (p.capabilities || []).map(c => `<span class="tool-cap-tag">${esc(c)}</span>`).join('');
  const s = p.safety || {};
  return `
    <div class="tool-card">
      <div class="tool-card-name">${esc(p.name)}</div>
      <div class="tool-card-desc">${esc(p.description)}</div>
      <div class="tool-card-caps">${caps || '<span style="color:var(--muted)">无</span>'}</div>
      <div class="tool-card-safety">
        <span>需审批: ${s.requires_human_approval ? '<b style="color:#e94560">是</b>' : '否'}</span>
        <span>并发: ${s.max_concurrency || '-'}</span>
        <span>网络: ${s.network_allowed ? '✓' : '✗'}</span>
        <span>文件: ${s.filesystem_allowed ? '✓' : '✗'}</span>
      </div>
      <button class="tool-reload-btn" data-name="${esc(p.name)}">🔄 重载</button>
      <span class="tool-reload-msg" data-name="${esc(p.name)}"></span>
    </div>`;
}

/** 重载插件 — 委托事件 */
document.addEventListener('click', async (e) => {
  if (!e.target.classList.contains('tool-reload-btn')) return;
  const btn = e.target;
  const name = btn.dataset.name;
  const msgEl = document.querySelector(`.tool-reload-msg[data-name="${CSS.escape(name)}"]`);
  btn.disabled = true;
  msgEl.textContent = '重载中…';
  msgEl.className = 'tool-reload-msg';
  try {
    const res = await fetch('/tools/reload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (data.ok) {
      msgEl.textContent = '✓ 重载成功';
      msgEl.className = 'tool-reload-msg ok';
    } else {
      msgEl.textContent = '✗ ' + (data.error || '失败');
      msgEl.className = 'tool-reload-msg err';
    }
  } catch (_) {
    msgEl.textContent = '✗ 请求失败';
    msgEl.className = 'tool-reload-msg err';
  }
  btn.disabled = false;
  setTimeout(() => { loadPlugins(); }, 2000);
});
