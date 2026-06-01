/* Vinu — AI Workspace Builder sidebar */

let vinuConversationId = null;
let vinuPhase = 'gathering';
let vinuPlan = null;
let vinuOpen = false;
let vinuView = 'list'; // 'setup' | 'list' | 'chat'
let vinuAgentName = null;
let vinuNextCursor = null;
let vinuHasNext = false;
let vinuStreaming = false;
let vinuSidebarWidth = 420;
let _vinuResizing = false;

function _vesc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function _vRelTime(iso) {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// ---------------------------------------------------------------------------
// Open / close
// ---------------------------------------------------------------------------

function _vinuApplyWidth(w) {
  vinuSidebarWidth = Math.max(280, Math.min(w, Math.floor(window.innerWidth * 0.85)));
  const sidebar = document.getElementById('vinu-sidebar');
  sidebar.style.width = vinuSidebarWidth + 'px';
  if (!vinuOpen) sidebar.style.right = `-${vinuSidebarWidth + 20}px`;
}

async function toggleVinu() {
  vinuOpen = !vinuOpen;
  const sidebar = document.getElementById('vinu-sidebar');
  const backdrop = document.getElementById('vinu-backdrop');
  sidebar.style.right = vinuOpen ? '0' : `-${vinuSidebarWidth + 20}px`;
  if (backdrop) {
    if (vinuOpen) {
      backdrop.style.display = 'block';
      requestAnimationFrame(() => {
        backdrop.style.background = 'rgba(0,0,0,0.28)';
        backdrop.style.backdropFilter = 'blur(3px)';
      });
    } else {
      backdrop.style.background = 'rgba(0,0,0,0)';
      backdrop.style.backdropFilter = 'blur(0px)';
      setTimeout(() => { backdrop.style.display = 'none'; }, 260);
    }
  }
  if (vinuOpen && vinuView === 'list') {
    await _vinuLoadSettings();
  }
}

async function _vinuLoadSettings() {
  try {
    const data = await apiGet('vinu/settings');
    vinuAgentName = data.vinu_agent_name || null;
    _vinuUpdateTitle();
    if (!vinuAgentName) {
      _vinuShowSetup();
    } else {
      await showVinuList();
    }
  } catch (e) {
    toast('Could not load Vinu settings', 'error');
  }
}

function _vinuUpdateTitle() {
  const name = vinuAgentName || 'Vinu';
  const title = document.getElementById('vinu-title');
  if (title) title.textContent = name;
  const btn = document.getElementById('vinu-toggle-btn');
  if (btn) btn.textContent = name;
}

// Fetch name on page load so nav button shows correct name immediately
(async function _vinuPrefetchName() {
  try {
    const data = await apiGet('vinu/settings');
    vinuAgentName = data.vinu_agent_name || null;
    _vinuUpdateTitle();
  } catch {}
})();

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

function _vinuShowView(view, convTitle) {
  vinuView = view;
  document.getElementById('vinu-setup-view').style.display = view === 'setup' ? 'flex' : 'none';
  document.getElementById('vinu-list-view').style.display = view === 'list' ? 'flex' : 'none';
  document.getElementById('vinu-chat-view').style.display = view === 'chat' ? 'flex' : 'none';
  document.getElementById('vinu-back-btn').style.display = view === 'chat' ? 'inline' : 'none';
  const title = document.getElementById('vinu-title');
  const subtitle = document.getElementById('vinu-subtitle');
  if (view === 'chat') {
    if (title) title.textContent = convTitle || 'New Chat';
    if (subtitle) subtitle.textContent = vinuAgentName || 'Vinu';
  } else {
    if (title) title.textContent = vinuAgentName || 'Vinu';
    if (subtitle) subtitle.textContent = 'AI Workspace Builder';
  }
}

function _vinuShowSetup() {
  _vinuShowView('setup');
}

async function saveVinuName(name) {
  name = (name || '').trim() || 'Vinu';
  try {
    await apiPatch('vinu/settings', { vinu_agent_name: name });
    vinuAgentName = name;
    _vinuUpdateTitle();
    await showVinuList();
  } catch (e) {
    toast('Could not save name', 'error');
  }
}

async function showVinuList() {
  _vinuShowView('list');
  vinuConversationId = null;
  vinuPlan = null;
  vinuNextCursor = null;
  vinuHasNext = false;
  document.getElementById('vinu-conv-list').innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px 0;">Loading…</div>';
  await _vinuLoadConversations(true);
}

async function _vinuLoadConversations(reset = true) {
  try {
    const params = new URLSearchParams({ limit: 20 });
    if (!reset && vinuNextCursor) params.set('cursor', vinuNextCursor);
    const data = await apiGet(`vinu/conversations?${params}`);
    const list = document.getElementById('vinu-conv-list');
    if (reset) list.innerHTML = '';

    if (!data.items.length && reset) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px 0;text-align:center;">No chats yet</div>';
    }
    data.items.forEach(c => _vinuAppendConvRow(c));
    vinuNextCursor = data.next_cursor;
    vinuHasNext = data.has_next;
    const more = document.getElementById('vinu-load-more');
    if (more) more.style.display = vinuHasNext ? 'block' : 'none';
  } catch (e) {
    toast('Could not load conversations', 'error');
  }
}

function _vinuAppendConvRow(conv) {
  const list = document.getElementById('vinu-conv-list');
  const row = document.createElement('div');
  row.className = 'vinu-conv-row';
  row.onclick = () => openVinuChat(conv.id, conv.name);
  row.innerHTML = `
    <div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_vesc(conv.name)}</div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-top:2px;">
      <span style="font-size:11px;color:var(--text-muted);">${_vRelTime(conv.updated_at)}</span>
      <button onclick="event.stopPropagation();deleteVinuConv('${conv.id}',this)" style="font-size:14px;color:var(--text-muted);background:none;border:none;cursor:pointer;padding:0 2px;line-height:1;">×</button>
    </div>`;
  list.appendChild(row);
}

function deleteVinuConv(id, btn) {
  confirmDialog({
    title: 'Delete chat?',
    message: 'This conversation and all its messages will be permanently deleted.',
    confirmText: 'Delete',
    onConfirm: async () => {
      btn.disabled = true;
      try {
        await apiDelete(`vinu/conversations/${id}`);
        await _vinuLoadConversations(true);
      } catch (e) {
        toast(e.message, 'error');
        btn.disabled = false;
      }
    },
  });
}

// ---------------------------------------------------------------------------
// Chat / history views
// ---------------------------------------------------------------------------

async function startNewVinuChat() {
  _vinuShowView('chat', 'New Chat');
  vinuConversationId = null;
  vinuPlan = null;
  vinuPhase = 'gathering';
  document.getElementById('vinu-messages').innerHTML = '';
  document.getElementById('vinu-plan').style.display = 'none';
  document.getElementById('vinu-build-progress').style.display = 'none';
  document.getElementById('vinu-input').value = '';
  _vinuAppendMsg('assistant', `Hey there! I'm ${vinuAgentName || 'Vinu'}, your AI workspace builder. Tell me — what do you want to build? I can help with support bots, research assistants, sales agents, code reviewers, and more!`);
  document.getElementById('vinu-input').focus();
}

async function openVinuChat(convId, convName) {
  vinuConversationId = convId;
  vinuPlan = null;
  vinuPhase = 'gathering';
  _vinuShowView('chat', convName || 'Loading…');
  const msgs = document.getElementById('vinu-messages');
  msgs.innerHTML = '';
  document.getElementById('vinu-plan').style.display = 'none';
  document.getElementById('vinu-build-progress').style.display = 'none';

  const loadingNote = document.createElement('div');
  loadingNote.style.cssText = 'text-align:center;color:var(--text-muted);font-size:11px;padding:8px 0;';
  loadingNote.textContent = 'Loading conversation…';
  msgs.appendChild(loadingNote);

  try {
    const data = await apiGet(`vinu/conversations/${convId}/messages?limit=30`);
    loadingNote.remove();
    if (data && data.messages && data.messages.length) {
      [...data.messages].reverse().forEach(m => _vinuAppendMsg(m.role, m.content));
      if (data.has_more) _vinuInsertLoadOlderBtn(convId, data.next_cursor);
    } else {
      _vinuAppendMsg('assistant', `Hey there! I'm ${vinuAgentName || 'Vinu'}, your AI workspace builder. Tell me — what do you want to build?`);
    }
    if (data && data.last_plan) {
      vinuPlan = data.last_plan;
      _vinuAppendPlanMsg(data.last_plan, data.last_build || null);
    }
  } catch (e) {
    loadingNote.textContent = 'Could not load history — continue the conversation below.';
  }
  document.getElementById('vinu-input').focus();
}

function _vinuInsertLoadOlderBtn(convId, cursor) {
  const msgs = document.getElementById('vinu-messages');
  const btn = document.createElement('button');
  btn.className = 'btn btn-secondary btn-sm';
  btn.style.cssText = 'width:100%;margin-bottom:10px;font-size:11px;';
  btn.textContent = 'Load older messages';
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = 'Loading…';
    try {
      const data = await apiGet(`vinu/conversations/${convId}/messages?limit=30&cursor=${encodeURIComponent(cursor)}`);
      btn.remove();
      if (data && data.messages && data.messages.length) {
        // API returns DESC (newest first); inserting each before firstChild one-by-one
        // reverses into correct chronological (ASC) order at the top.
        data.messages.forEach(m => msgs.insertBefore(_vinuBuildMsgEl(m.role, m.content), msgs.firstChild));
        if (data.has_more) _vinuInsertLoadOlderBtn(convId, data.next_cursor);
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Load older messages';
      toast('Could not load older messages', 'error');
    }
  };
  msgs.insertBefore(btn, msgs.firstChild);
}

function _vinuBuildMsgEl(role, content) {
  const wrap = document.createElement('div');
  wrap.className = `vinu-msg ${role}`;
  const avatar = document.createElement('div');
  avatar.className = 'vinu-msg-avatar';
  avatar.textContent = role === 'assistant' ? 'V' : 'U';
  const body = document.createElement('div');
  body.className = 'vinu-msg-body';
  if (content) {
    if (role === 'assistant' && window.marked) {
      body.innerHTML = DOMPurify.sanitize(marked.parse(content));
    } else {
      body.textContent = content;
    }
  }
  wrap.appendChild(avatar);
  wrap.appendChild(body);
  return wrap;
}

// ---------------------------------------------------------------------------
// Chat streaming
// ---------------------------------------------------------------------------

async function sendVinuMessage(overrideText) {
  if (vinuStreaming) return;
  const input = document.getElementById('vinu-input');
  const text = overrideText !== undefined ? overrideText : input.value.trim();
  if (!text) return;
  input.value = '';
  await _streamVinuChat(text);
}

async function _streamVinuChat(text) {
  if (vinuStreaming) return;
  vinuStreaming = true;
  const btn = document.getElementById('vinu-send-btn');
  if (btn) btn.disabled = true;

  const msgs = document.getElementById('vinu-messages');

  _vinuAppendMsg('user', text);

  const aMsg = _vinuAppendMsg('assistant', '');
  const bodyEl = aMsg.querySelector('.vinu-msg-body');

  // Typing indicator while waiting for LLM response
  bodyEl.innerHTML = '<div class="vinu-typing"><span></span><span></span><span></span></div>';

  let fullReply = '';
  let isFirstToken = true;

  try {
    const payload = { message: text };
    if (vinuConversationId) payload.conversation_id = vinuConversationId;

    for await (const { event, data } of sseStream('vinu/chat', payload)) {
      if (event === 'meta') {
        vinuPhase = data.phase || vinuPhase;
        if (!vinuConversationId && data.conversation_id) {
          vinuConversationId = data.conversation_id;
        }
      } else if (event === 'compacting') {
        isFirstToken = false;
        bodyEl.innerHTML = '';
        const note = document.createElement('div');
        note.style.cssText = 'color:var(--text-muted);font-size:12px;padding:4px 0;';
        note.textContent = '🔄 Summarising earlier conversation…';
        bodyEl.appendChild(note);
      } else if (event === 'plan') {
        vinuPlan = data;
        _vinuRenderPlan(data);
      } else if (event === 'clarification_required') {
        _vinuRenderClarification(aMsg, data.questions || []);
      } else if (event === 'conversation_title') {
        if (data.title) {
          const titleEl = document.getElementById('vinu-title');
          if (titleEl) titleEl.textContent = data.title;
        }
      } else if (event === 'done') {
        vinuPhase = data.phase || vinuPhase;
        break;
      } else if (event === 'error') {
        isFirstToken = false;
        bodyEl.innerHTML = '';
        bodyEl.style.color = '#dc2626';
        bodyEl.textContent = data.message || 'Something went wrong. Please try again.';
        break;
      } else if (!event || event === 'message') {
        if (typeof data === 'string') {
          if (isFirstToken) {
            isFirstToken = false;
            bodyEl.innerHTML = '';
          }
          fullReply += data;
          if (window.marked) {
            bodyEl.innerHTML = marked.parse(fullReply);
          } else {
            bodyEl.textContent = fullReply;
          }
          msgs.scrollTop = msgs.scrollHeight;
        }
      }
    }
  } catch (e) {
    isFirstToken = false;
    bodyEl.innerHTML = '';
    bodyEl.style.color = '#dc2626';
    bodyEl.textContent = e.message || 'Something went wrong. Please try again.';
  } finally {
    if (isFirstToken) {
      bodyEl.innerHTML = '';
    }
    vinuStreaming = false;
    if (btn) btn.disabled = false;
    msgs.scrollTop = msgs.scrollHeight;
  }
}

function _vinuAppendMsg(role, content) {
  const msgs = document.getElementById('vinu-messages');
  const wrap = _vinuBuildMsgEl(role, content);
  msgs.appendChild(wrap);
  msgs.scrollTop = msgs.scrollHeight;
  return wrap;
}

// ---------------------------------------------------------------------------
// Clarification chips
// ---------------------------------------------------------------------------

function _vinuRenderClarification(msgWrap, questions) {
  const form = document.createElement('div');
  form.style.cssText = 'margin-top:10px;display:flex;flex-direction:column;gap:10px;max-width:320px;';
  const answers = {};

  questions.forEach((q, i) => {
    answers[i] = null;
    const qDiv = document.createElement('div');
    qDiv.innerHTML = `<div style="font-size:12px;font-weight:500;margin-bottom:4px;">${_vesc(q.question)}</div>`;

    const chipsDiv = document.createElement('div');
    (q.options || []).forEach(opt => {
      const chip = document.createElement('button');
      chip.className = 'clarif-chip';
      chip.textContent = opt;
      chip.onclick = () => {
        chipsDiv.querySelectorAll('.clarif-chip').forEach(c => c.classList.remove('selected'));
        chip.classList.add('selected');
        answers[i] = opt;
      };
      chipsDiv.appendChild(chip);
    });

    const otherChip = document.createElement('button');
    otherChip.className = 'clarif-chip';
    otherChip.textContent = '✏ Other';
    const otherInput = document.createElement('input');
    otherInput.className = 'input';
    otherInput.style.cssText = 'display:none;margin-top:4px;font-size:12px;';
    otherChip.onclick = () => {
      chipsDiv.querySelectorAll('.clarif-chip').forEach(c => c.classList.remove('selected'));
      otherChip.classList.add('selected');
      otherInput.style.display = 'block';
      otherInput.oninput = () => { answers[i] = otherInput.value; };
    };
    chipsDiv.appendChild(otherChip);
    qDiv.appendChild(chipsDiv);
    qDiv.appendChild(otherInput);
    form.appendChild(qDiv);
  });

  const submitBtn = document.createElement('button');
  submitBtn.className = 'btn btn-primary btn-sm';
  submitBtn.style.marginTop = '6px';
  submitBtn.textContent = 'Submit answers';
  submitBtn.onclick = () => {
    const unanswered = Object.entries(answers).filter(([, v]) => !v);
    if (unanswered.length) { toast('Please answer all questions', 'error'); return; }
    const block = questions.map((q, i) => `Q: ${q.question}\nA: ${answers[i]}`).join('\n\n');
    form.remove();
    sendVinuMessage(`[Answers to clarification questions]\n${block}`);
  };
  form.appendChild(submitBtn);
  msgWrap.querySelector('.vinu-msg-body').appendChild(form);
}

// ---------------------------------------------------------------------------
// Plan card
// ---------------------------------------------------------------------------

function _vinuPlanCardHTML(plan, buildResult) {
  const agents = (plan.agents || []).map(a => `
    <div style="border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;margin-bottom:6px;">
      <div style="font-weight:600;font-size:12px;">${_vesc(a.name)}</div>
      <div style="color:var(--text-muted);font-size:11px;margin:2px 0;">${_vesc(a.role || '')}</div>
      ${a.why ? `<div style="font-size:11px;color:var(--text-muted);font-style:italic;margin:2px 0 4px;">${_vesc(a.why)}</div>` : ''}
      <div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;">
        ${(a.tools || []).map(t => `<span class="badge">${_vesc(t)}</span>`).join('')}
        ${a.model ? `<span class="badge" style="background:var(--accent-light);color:var(--accent);">${_vesc(a.model)}</span>` : ''}
      </div>
    </div>`).join('');

  const kbs = (plan.kbs_needed || []).map(k =>
    `<div style="font-size:11px;padding:4px 8px;background:var(--surface);border-radius:4px;margin-bottom:3px;">
      <strong>${_vesc(k.name)}</strong> <span style="color:var(--text-muted);">— upload docs after build</span>
      ${k.why ? `<div style="color:var(--text-muted);font-style:italic;margin-top:2px;">${_vesc(k.why)}</div>` : ''}
    </div>`
  ).join('');

  const wcs = (plan.wcs_needed || []).map(w =>
    `<div style="font-size:11px;padding:4px 8px;background:var(--surface);border-radius:4px;margin-bottom:3px;">
      <strong>${_vesc(w.name)}</strong>${w.url ? ` <span style="color:var(--text-muted);">(${_vesc(w.url)})</span>` : ''} <span style="color:var(--text-muted);">— add URLs after build</span>
      ${w.why ? `<div style="color:var(--text-muted);font-style:italic;margin-top:2px;">${_vesc(w.why)}</div>` : ''}
    </div>`
  ).join('');

  const actions = buildResult
    ? `<div style="display:flex;gap:8px;margin-top:12px;align-items:center;">
        <span style="font-size:12px;color:var(--text-muted);font-weight:600;">Built</span>
        ${buildResult.workspace_id ? `<a href="/workspace.html?id=${encodeURIComponent(buildResult.workspace_id)}" class="btn btn-primary btn-sm" style="flex:1;text-align:center;">Open Workspace</a>` : ''}
      </div>
      ${_vinuBuildActionCards(buildResult)}`
    : `<div style="display:flex;gap:8px;margin-top:12px;">
        <button onclick="buildVinuWorkspace()" class="btn btn-primary btn-sm" style="flex:1;">Build Workspace</button>
        <button onclick="sendVinuMessage('Please refine the plan.')" class="btn btn-secondary btn-sm">Tweak</button>
      </div>`;

  return `
    <div style="font-size:13px;font-weight:700;margin-bottom:4px;letter-spacing:-.02em;">${_vesc(plan.workspace_name || 'Workspace Plan')}</div>
    <div style="color:var(--text-muted);font-size:12px;margin-bottom:4px;">${_vesc(plan.workspace_description || '')}</div>
    ${plan.plan_reasoning ? `<div style="font-size:11px;padding:6px 8px;background:var(--surface);border-left:2px solid var(--accent);border-radius:2px;margin-bottom:10px;color:var(--text-muted);">${_vesc(plan.plan_reasoning)}</div>` : '<div style="margin-bottom:10px;"></div>'}
    ${agents}
    ${kbs || wcs ? `<div style="margin-top:8px;">${kbs}${wcs}</div>` : ''}
    ${actions}`;
}

function _vinuBuildActionCards(buildResult) {
  let html = '';

  // Per-agent breakdown — which agent uses what
  if (buildResult.agents_summary && buildResult.agents_summary.length) {
    html += `<div style="margin-top:10px;font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">Agents Built</div>`;
    html += buildResult.agents_summary.map(a => {
      const toolBadges = (a.tools || []).map(t => `<span class="badge" style="font-size:10px;">${_vesc(t)}</span>`).join('');
      const kbBadges = (a.kb_names || []).map(n => `<span class="badge" style="font-size:10px;">KB: ${_vesc(n)}</span>`).join('');
      const wcBadges = (a.wc_names || []).map(n => `<span class="badge" style="font-size:10px;">WC: ${_vesc(n)}</span>`).join('');
      return `<div style="padding:6px 8px;border:1px solid var(--border);border-radius:4px;margin-bottom:4px;">
        <div style="font-weight:600;font-size:11px;">${_vesc(a.name)}</div>
        ${a.role ? `<div style="color:var(--text-muted);font-size:10px;margin-bottom:3px;">${_vesc(a.role)}</div>` : ''}
        <div style="display:flex;flex-wrap:wrap;gap:3px;">${toolBadges}${kbBadges}${wcBadges}</div>
      </div>`;
    }).join('');
  }

  // KBs — user must upload documents
  if (buildResult.kbs_created && buildResult.kbs_created.length) {
    html += `<div style="margin-top:10px;font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">Upload Documents</div>`;
    html += buildResult.kbs_created.map(kb => `
      <div style="padding:8px 10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);font-size:11px;margin-bottom:4px;">
        <div style="font-weight:600;margin-bottom:2px;">${_vesc(kb.name)}</div>
        ${kb.description ? `<div style="color:var(--text-muted);margin-bottom:4px;">${_vesc(kb.description)}</div>` : ''}
        <div style="color:var(--text-muted);margin-bottom:4px;">Upload your PDF, Word, Excel, or CSV files to this knowledge base.</div>
        <a href="/knowledge-bases.html?id=${encodeURIComponent(kb.id)}" style="color:var(--accent);font-weight:600;text-decoration:underline;text-underline-offset:2px;">Open Knowledge Base</a>
      </div>`).join('');
  }

  // WCs — user must add URLs and crawl
  if (buildResult.wcs_created && buildResult.wcs_created.length) {
    html += `<div style="margin-top:10px;font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">Add Website URLs</div>`;
    html += buildResult.wcs_created.map(wc => `
      <div style="padding:8px 10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);font-size:11px;margin-bottom:4px;">
        <div style="font-weight:600;margin-bottom:2px;">${_vesc(wc.name)}</div>
        ${wc.description ? `<div style="color:var(--text-muted);margin-bottom:4px;">${_vesc(wc.description)}</div>` : ''}
        <div style="color:var(--text-muted);margin-bottom:4px;">Add your website URL(s) and start the crawl — the agent will search this content.</div>
        <a href="/website-collections.html?id=${encodeURIComponent(wc.id)}" style="color:var(--accent);font-weight:600;text-decoration:underline;text-underline-offset:2px;">Open Web Collection</a>
      </div>`).join('');
  }

  // OAuth/credential connectors — user must authenticate before agents can use them
  if (buildResult.connectors_needed && buildResult.connectors_needed.length) {
    html += `<div style="margin-top:10px;font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">Connect Required Services</div>`;
    html += buildResult.connectors_needed.map(c => `
      <div style="padding:8px 10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);font-size:11px;margin-bottom:4px;">
        <div style="font-weight:600;margin-bottom:2px;">${_vesc(c.display_name)}</div>
        <div style="color:var(--text-muted);margin-bottom:4px;">Needs authentication before your agents can use it.</div>
        <span style="color:var(--accent);font-weight:600;">Workspace &rsaquo; Connectors &rsaquo; Connect ${_vesc(c.display_name)}</span>
      </div>`).join('');
  }

  return html;
}

function _vinuRenderPlan(plan) {
  const planEl = document.getElementById('vinu-plan');
  planEl.style.display = 'block';
  planEl.innerHTML = _vinuPlanCardHTML(plan, null);
}

function _vinuAppendPlanMsg(plan, buildResult) {
  const msgs = document.getElementById('vinu-messages');
  const card = document.createElement('div');
  card.style.cssText = 'border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;background:var(--surface);font-size:13px;';
  card.innerHTML = _vinuPlanCardHTML(plan, buildResult || null);
  msgs.appendChild(card);
  msgs.scrollTop = msgs.scrollHeight;
}

function _vinuShowBuiltState(buildResult) {
  document.getElementById('vinu-plan').style.display = 'none';
  document.getElementById('vinu-build-progress').style.display = 'none';
  const msgs = document.getElementById('vinu-messages');
  const card = document.createElement('div');
  card.style.cssText = 'border:1px solid #6ee7b7;border-radius:var(--radius);padding:14px 16px;background:#f0fdf4;font-size:13px;';
  const wsLink = buildResult.workspace_id
    ? `<a href="/workspace.html?id=${encodeURIComponent(buildResult.workspace_id)}" style="color:var(--accent);font-weight:600;display:inline-block;margin-top:4px;">Open Workspace →</a>`
    : '';
  card.innerHTML = `
    <div style="font-weight:600;color:#065f46;margin-bottom:4px;">✅ ${_vesc(buildResult.workspace_name || 'Workspace')} built!</div>
    ${wsLink}
    ${_vinuBuildActionCards(buildResult)}`;
  msgs.appendChild(card);
  msgs.scrollTop = msgs.scrollHeight;
}

// ---------------------------------------------------------------------------
// Build
// ---------------------------------------------------------------------------

async function buildVinuWorkspace() {
  if (!vinuPlan) { toast('No plan to build', 'error'); return; }

  // Move progress inline into messages area
  const msgs = document.getElementById('vinu-messages');
  const progressEl = document.createElement('div');
  progressEl.style.cssText = 'border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;background:var(--surface);font-size:12px;';
  progressEl.innerHTML = '<div style="font-weight:600;margin-bottom:8px;">Building workspace…</div>';
  msgs.appendChild(progressEl);
  msgs.scrollTop = msgs.scrollHeight;

  document.getElementById('vinu-plan').style.display = 'none';

  const payload = { plan: vinuPlan };
  if (vinuConversationId) payload.conversation_id = vinuConversationId;

  try {
    for await (const { event, data } of sseStream('vinu/build', payload)) {
      if (event === 'step') {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:6px;padding:3px 0;';
        const icon = data.status === 'done' ? '✅' : data.status === 'error' ? '❌' : '⏳';
        row.innerHTML = `<span>${icon}</span><span>${_vesc(data.step || '')}</span>`;
        progressEl.appendChild(row);
        msgs.scrollTop = msgs.scrollHeight;
      } else if (event === 'done') {
        _vinuShowBuiltState(data);
      } else if (event === 'error') {
        const errRow = document.createElement('div');
        errRow.style.cssText = 'color:#dc2626;margin-top:8px;';
        errRow.textContent = `❌ ${data.message || 'Build failed'}`;
        progressEl.appendChild(errRow);
        // Re-show plan with a retry option
        if (vinuPlan) {
          const retryCard = document.createElement('div');
          retryCard.style.cssText = 'border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;background:var(--surface);font-size:13px;margin-top:8px;';
          retryCard.innerHTML = _vinuPlanCardHTML(vinuPlan, null);
          msgs.appendChild(retryCard);
          msgs.scrollTop = msgs.scrollHeight;
        }
      }
    }
  } catch (e) {
    const errRow = document.createElement('div');
    errRow.style.cssText = 'color:#dc2626;margin-top:8px;';
    errRow.textContent = `❌ ${e.message}`;
    progressEl.appendChild(errRow);
  }
}


// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

function vinuKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendVinuMessage();
  }
}

// ---------------------------------------------------------------------------
// Sidebar resize
// ---------------------------------------------------------------------------

(function () {
  const handle = document.getElementById('vinu-resize-handle');
  if (!handle) return;
  let startX = 0, startW = 0;

  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    _vinuResizing = true;
    startX = e.clientX;
    startW = vinuSidebarWidth;
    handle.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    // Disable transition while dragging
    document.getElementById('vinu-sidebar').style.transition = 'none';
  });

  document.addEventListener('mousemove', e => {
    if (!_vinuResizing) return;
    _vinuApplyWidth(startW + (startX - e.clientX));
  });

  document.addEventListener('mouseup', () => {
    if (!_vinuResizing) return;
    _vinuResizing = false;
    handle.classList.remove('dragging');
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    document.getElementById('vinu-sidebar').style.transition = 'right .25s ease';
  });
})();
