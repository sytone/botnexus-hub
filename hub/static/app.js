(function () {
  'use strict';

  const POLL_INTERVAL_MS = 5000;
  const COMMAND_POLL_INTERVAL_MS = 1000;
  const terminalStatuses = new Set(['succeeded', 'failed', 'uncertain']);
  const isRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const text = (value, fallback = '') => typeof value === 'string' ? value : fallback;
  const records = (value, limit = 100) => Array.isArray(value) ? value.filter(isRecord).slice(0, limit) : [];
  const validDate = (value) => typeof value === 'string' && Number.isFinite(Date.parse(value));

  function safeWorldUrl(value) {
    if (typeof value !== 'string' || /[\u0000-\u001f\u007f]/.test(value)) return null;
    try {
      const url = new URL(value);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
      return url.href;
    } catch (_) { return null; }
  }

  function safeConnectorUrl(value) {
    const safe = safeWorldUrl(value);
    if (!safe) return null;
    const url = new URL(safe);
    if (url.search || url.hash) return null;
    return url.href.replace(/\/+$/, '');
  }

  function externalSession(data) {
    if (!isRecord(data) || data.authenticated !== true || data.authMode !== 'external-preauth') return null;
    const connectorUrl = safeConnectorUrl(data.connectorUrl);
    return connectorUrl ? { connectorUrl } : null;
  }

  function connectorConfiguration(enrollment, token = '<CONNECTOR_TOKEN>') {
    const url = safeConnectorUrl(enrollment.connectorUrl);
    if (!url) throw new Error('The connector address could not be verified. Reload secure access before enrolling a world.');
    const { worldId, name, baseUrl } = enrollment;
    return { gateway: { extensions: { 'botnexus-hub-connector': { enabled: true, url, worldId, worldName: name, worldBaseUrl: baseUrl, worldToken: token, allowedAgents: ['REPLACE_WITH_AGENT_ID'] } } } };
  }

  function conversationUrl(baseUrl, agentId, conversationId) {
    const safe = safeWorldUrl(baseUrl);
    if (!safe || !text(agentId) || !text(conversationId)) return null;
    const url = new URL(safe);
    url.pathname = `${url.pathname.replace(/\/+$/, '')}/agent/${encodeURIComponent(agentId)}/conversation/${encodeURIComponent(conversationId)}`;
    url.search = '';
    url.hash = '';
    return url.href;
  }

  function normalizeWorlds(items) {
    return records(items, 1000).filter((row) => text(row.id)).map((row) => {
      const snapshot = isRecord(row.snapshot) ? row.snapshot : null;
      return {
        id: row.id,
        name: text(row.name, row.id),
        baseUrl: text(row.baseUrl),
        connected: row.connected === true,
        lastSeen: text(row.lastSeen),
        observedAt: snapshot ? text(snapshot.observedAt) : '',
        version: snapshot ? text(snapshot.version) : '',
        hasSnapshot: snapshot !== null,
        agents: snapshot ? records(snapshot.agents).filter((agent) => text(agent.id)) : [],
        conversations: snapshot ? records(snapshot.conversations).filter((conversation) => text(conversation.id)) : [],
        questions: snapshot ? records(snapshot.questions).filter((question) => text(question.requestId) && text(question.conversationId) && text(question.agentId)) : [],
      };
    });
  }

  function collectConversations(worlds, worldId = '') {
    return worlds.filter((world) => !worldId || world.id === worldId)
      .flatMap((world) => world.conversations.map((conversation) => ({ world, conversation })))
      .sort((a, b) => (Date.parse(b.conversation.updatedAt) || 0) - (Date.parse(a.conversation.updatedAt) || 0));
  }

  function collectQuestions(worlds, worldId = '') {
    return worlds.filter((world) => !worldId || world.id === worldId)
      .flatMap((world) => world.questions.map((question) => ({ world, question })));
  }

  function normalizeChoices(choices) {
    if (!Array.isArray(choices)) return [];
    return choices.slice(0, 100).map((choice) => {
      if (typeof choice === 'string') return { label: choice, value: choice };
      if (!isRecord(choice)) return null;
      const value = text(choice.value, text(choice.id, text(choice.label, text(choice.text))));
      return { value, label: text(choice.label, text(choice.text, value)) };
    }).filter((choice) => choice && choice.value !== '');
  }

  function questionMode(question) {
    const type = text(question.inputType).toLowerCase().replace(/-/g, '_');
    if (type === 'free_form' || type === 'text') return 'free_form';
    if (question.allowMultiple === true || ['multiple', 'multiple_choice', 'multi_choice', 'multi_select', 'multiselect'].includes(type)) return 'multiple';
    if (type === 'choice_or_free_form' || question.allowFreeForm === true) return 'mixed';
    return normalizeChoices(question.choices).length ? 'single' : 'free_form';
  }

  function requestKey() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
      const bytes = window.crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    throw new Error('Secure randomness is unavailable. Use a secure browser connection before sending a command.');
  }

  function buildRequestOptions(method = 'GET', body, idempotencyKey) {
    const options = { method, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { 'X-Hub-Request': '1' } };
    if (method !== 'GET' && method !== 'HEAD') options.headers['Idempotency-Key'] = idempotencyKey || requestKey();
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    return options;
  }

  function commandPresentation(status) {
    const states = {
      submitting: { label: 'Sending once', tone: 'info', detail: 'Sending this action once. Waiting for a Hub receipt.' },
      queued: { label: 'Queued', tone: 'info', detail: 'Queued at the Hub. Waiting for the world to accept this action.' },
      sent: { label: 'Waiting for world', tone: 'info', detail: 'Sent to the world. Waiting for its acceptance receipt.' },
      succeeded: { label: 'Accepted', tone: 'positive', detail: 'Accepted into the world’s queue, not completed by the agent. Open the world to follow its work.' },
      failed: { label: 'Not accepted', tone: 'danger', detail: 'This action was not accepted. It was not retried automatically.' },
      uncertain: { label: 'Outcome unknown', tone: 'warning', detail: 'This action may have reached the world. It was not retried. Check the world before sending it again.' },
    };
    return states[status] || states.uncertain;
  }

  function relativeTime(value, now = Date.now()) {
    if (!validDate(value)) return 'unknown';
    const seconds = Math.max(0, Math.floor((now - Date.parse(value)) / 1000));
    if (seconds < 5) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  window.__hub = window.__hub || {};
  window.__hub.helpers = Object.freeze({ safeWorldUrl, safeConnectorUrl, externalSession, connectorConfiguration, conversationUrl, normalizeWorlds, collectConversations, collectQuestions, normalizeChoices, questionMode, buildRequestOptions, commandPresentation, relativeTime });

  if (!document.getElementById('hub-app')) return;

  const byId = (id) => document.getElementById(id);
  const ui = {
    access: byId('access-panel'), accessMessage: byId('access-message'), secureAccess: byId('secure-access-button'),
    dashboard: byId('dashboard'), session: byId('session-status'),
    dashboardError: byId('dashboard-error'), filter: byId('world-filter'), refresh: byId('refresh-button'), sync: byId('sync-status'),
    worldGrid: byId('world-grid'), conversations: byId('conversation-list'), questions: byId('question-list'), activity: byId('activity-list'),
    newConversation: byId('new-conversation-button'), conversationDialog: byId('conversation-dialog'), conversationForm: byId('conversation-form'),
    conversationWorld: byId('conversation-world'), conversationAgent: byId('conversation-agent'), conversationTitle: byId('conversation-title'),
    conversationMessage: byId('conversation-message'), conversationSubmit: byId('conversation-submit'), conversationError: byId('conversation-error'),
    worldDialog: byId('world-dialog'), worldForm: byId('world-form'), worldId: byId('world-id'), worldName: byId('world-name'), worldBaseUrl: byId('world-base-url'),
    worldSubmit: byId('world-submit'), worldError: byId('world-error'), enrollment: byId('world-enrollment'), connectorToken: byId('connector-token'),
    connectorSnippet: byId('connector-snippet'), connectorHeader: byId('connector-header-snippet'), enrollmentFeedback: byId('enrollment-feedback'),
    revealToken: byId('reveal-connector-token'), commandsPanel: byId('commands-panel'), commands: byId('command-list'), announcer: byId('announcer'),
  };
  const state = {
    authenticated: false, connectorUrl: '', epoch: 0, worlds: [], activity: [], filter: '', activeTab: 'conversations',
    loaded: false, refreshing: false, refreshAgain: false, lastRefresh: '', filterOptions: '',
    refreshTimer: null, commandTimer: null, readControllers: new Set(),
    conversationNodes: new Map(), questionNodes: new Map(), commandNodes: new Map(),
    commands: [], locks: new Map(), sequence: 0, starting: false, startBlocked: false,
    enrolling: false, enrollmentUnknown: false, enrollment: null, tokenRevealed: false,
  };

  function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  function showNotice(node, message) {
    node.textContent = message || '';
    node.hidden = !message;
  }

  function announce(message) { ui.announcer.textContent = message; }
  function worldById(id) { return state.worlds.find((world) => world.id === id); }
  function visible() { return document.visibilityState !== 'hidden'; }
  function agentName(world, id) { return text(world.agents.find((agent) => agent.id === id)?.name, text(id, 'Unknown agent')); }
  function routeKey(worldId, agentId, conversationId) { return JSON.stringify([worldId, agentId, conversationId]); }
  function questionKey(worldId, question) { return JSON.stringify([worldId, question.agentId, question.conversationId, question.requestId]); }

  function setExternalLink(anchor, url) {
    anchor.hidden = !url;
    if (url) {
      anchor.href = url;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      anchor.setAttribute('referrerpolicy', 'no-referrer');
    } else anchor.removeAttribute('href');
  }

  function timeElement(value, prefix = '') {
    const node = el('time', '', `${prefix}${relativeTime(value)}`);
    if (validDate(value)) {
      node.dateTime = value;
      node.title = new Date(value).toLocaleString();
    }
    return node;
  }

  function empty(tag, title, detail, symbol = '◇') {
    const node = el(tag, 'empty-state');
    const mark = el('span', 'empty-symbol', symbol);
    mark.setAttribute('aria-hidden', 'true');
    node.append(mark, el('strong', '', title), el('span', '', detail));
    return node;
  }

  class ApiError extends Error {
    constructor(message, status = 0, kind = 'http') {
      super(message);
      this.status = status;
      this.kind = kind;
    }
  }

  const accessDeniedMessage = 'Your external access or session could not be verified. Reload secure access to continue. Check the world before repeating an action; actions are never retried automatically.';
  const accessUnverifiedMessage = 'The Hub could not be reached, or secure access could not be verified. Check your connection, then reload secure access. Check the world before repeating an action; actions are never retried automatically.';

  async function api(path, method = 'GET', body, key) {
    const epoch = state.epoch;
    const options = buildRequestOptions(method, body, key);
    const controller = new AbortController();
    const isRead = method === 'GET';
    if (isRead) state.readControllers.add(controller);
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 12000);
    options.signal = controller.signal;
    function accessDenied(status) {
      if (epoch === state.epoch) showAccessProblem(accessDeniedMessage);
      return new ApiError(accessDeniedMessage, status, 'access');
    }
    try {
      const response = await window.fetch(path, options);
      if (response.status === 401 || response.status === 403 || (response.status >= 300 && response.status < 400) || response.redirected || response.type === 'opaqueredirect') throw accessDenied(response.status);
      if (/text\/html|application\/xhtml\+xml/i.test(response.headers.get('Content-Type') || '')) throw accessDenied(response.status);
      const raw = await response.text();
      let data;
      try { data = JSON.parse(raw); } catch (_) {
        if (/^\s*</.test(raw)) throw accessDenied(response.status);
        throw new ApiError('The Hub returned an unreadable response.', response.status, 'invalid-response');
      }
      if (!response.ok) {
        const detail = text(data?.detail, text(data?.error));
        throw new ApiError(detail.slice(0, 500) || `The Hub rejected the request (HTTP ${response.status}).`, response.status);
      }
      return { data, status: response.status };
    } catch (error) {
      if (error instanceof ApiError) throw error;
      // A blocked redirect and a network failure can both reject fetch with TypeError.
      // Do not call either a sign-out, follow the redirect, or retry it in the background.
      if ((timedOut || !controller.signal.aborted) && epoch === state.epoch) showAccessProblem(accessUnverifiedMessage);
      throw new ApiError('No response was received from the Hub. Check your connection. An action may have an unknown outcome; it was not resent.', 0, 'network');
    } finally {
      window.clearTimeout(timeout);
      state.readControllers.delete(controller);
    }
  }

  function stopPolling() {
    window.clearInterval(state.refreshTimer);
    window.clearInterval(state.commandTimer);
    state.refreshTimer = null;
    state.commandTimer = null;
  }

  function startPolling() {
    stopPolling();
    if (!state.authenticated || !visible()) return;
    state.refreshTimer = window.setInterval(() => { void refreshDashboard(); }, POLL_INTERVAL_MS);
    state.commandTimer = window.setInterval(() => { void pollCommands(); }, COMMAND_POLL_INTERVAL_MS);
  }

  function clearEnrollment() {
    state.enrollment = null;
    state.tokenRevealed = false;
    ui.connectorToken.value = '';
    ui.connectorToken.type = 'password';
    ui.connectorSnippet.textContent = '';
    ui.connectorHeader.textContent = '';
    ui.enrollmentFeedback.textContent = '';
    ui.revealToken.textContent = 'Show';
    ui.revealToken.setAttribute('aria-pressed', 'false');
    ui.enrollment.hidden = true;
  }

  function closeDialog(dialog) {
    if (dialog === ui.worldDialog && state.enrolling) return;
    if (dialog === ui.conversationDialog && state.starting) return;
    if (dialog === ui.worldDialog) clearEnrollment();
    dialog.close();
  }

  function showAccessProblem(message) {
    // Clear private data even when access is uncertain, without claiming SSO ended.
    state.authenticated = false;
    state.connectorUrl = '';
    state.epoch += 1;
    stopPolling();
    for (const controller of state.readControllers) controller.abort();
    state.worlds = [];
    state.activity = [];
    state.commands = [];
    state.locks.clear();
    state.conversationNodes.clear();
    state.questionNodes.clear();
    state.commandNodes.clear();
    state.loaded = false;
    state.filter = '';
    state.filterOptions = '';
    state.lastRefresh = '';
    state.refreshAgain = false;
    state.starting = false;
    state.enrolling = false;
    state.startBlocked = false;
    state.enrollmentUnknown = false;
    clearEnrollment();
    if (ui.worldDialog.open) ui.worldDialog.close();
    if (ui.conversationDialog.open) ui.conversationDialog.close();
    ui.conversationForm.reset();
    ui.worldForm.reset();
    ui.filter.replaceChildren();
    ui.conversationWorld.replaceChildren();
    ui.conversationAgent.replaceChildren();
    ui.worldGrid.replaceChildren();
    ui.conversations.replaceChildren();
    ui.questions.replaceChildren();
    ui.activity.replaceChildren();
    ui.commands.replaceChildren();
    ui.commandsPanel.hidden = true;
    ui.dashboard.hidden = true;
    ui.newConversation.disabled = true;
    for (const id of ['stat-online', 'stat-agents', 'stat-conversations', 'stat-questions', 'world-count', 'conversation-count', 'question-count']) byId(id).textContent = '—';
    byId('stat-worlds-detail').textContent = 'Secure access needs attention';
    byId('activity-scope').textContent = '';
    ui.sync.textContent = '';
    announce('');
    showNotice(ui.dashboardError, '');
    showNotice(ui.conversationError, '');
    showNotice(ui.worldError, '');
    ui.session.hidden = true;
    ui.access.hidden = false;
    ui.accessMessage.textContent = message;
  }

  async function sessionReady(session) {
    state.authenticated = true;
    state.connectorUrl = session.connectorUrl;
    state.epoch += 1;
    ui.access.hidden = true;
    ui.session.hidden = true;
    ui.dashboard.hidden = false;
    showNotice(ui.dashboardError, '');
    renderDashboard();
    startPolling();
    const epoch = state.epoch;
    void api('/health').then(({ data }) => {
      if (state.authenticated && epoch === state.epoch) byId('hub-version').textContent = `v${text(data?.version, 'unknown')}`;
    }).catch(() => {});
    await refreshDashboard();
  }

  async function checkSession() {
    const epoch = state.epoch;
    try {
      const { data } = await api('/api/me');
      if (epoch !== state.epoch) return;
      const session = externalSession(data);
      if (!session) throw new ApiError(accessDeniedMessage, 401, 'access');
      await sessionReady(session);
    } catch (error) {
      if (epoch === state.epoch) showAccessProblem(error.kind === 'network' ? accessUnverifiedMessage : accessDeniedMessage);
    }
  }

  ui.secureAccess.addEventListener('click', () => {
    // Only an explicit user action may start external sign-in. Never forward drafts or action URLs.
    window.location.assign('/');
  });

  async function refreshDashboard() {
    if (!state.authenticated || !visible()) return;
    if (state.refreshing) { state.refreshAgain = true; return; }
    state.refreshing = true;
    state.refreshAgain = false;
    ui.refresh.disabled = true;
    const epoch = state.epoch;
    const filter = state.filter;
    const query = new URLSearchParams({ limit: '50' });
    if (filter) query.set('worldId', filter);
    try {
      const results = await Promise.allSettled([api('/api/worlds'), api(`/api/activity?${query}`)]);
      if (!state.authenticated || epoch !== state.epoch || !visible()) return;
      const errors = [];
      if (results[0].status === 'fulfilled' && Array.isArray(results[0].value.data?.items)) {
        state.worlds = normalizeWorlds(results[0].value.data.items);
        state.loaded = true;
        state.lastRefresh = new Date().toISOString();
      } else errors.push('Worlds could not be refreshed.');
      if (results[1].status === 'fulfilled' && Array.isArray(results[1].value.data?.items)) {
        if (filter === state.filter) state.activity = records(results[1].value.data.items, 50);
      } else errors.push('Activity could not be refreshed.');
      showNotice(ui.dashboardError, errors.length ? `${errors.join(' ')} Showing the last received data, which may be out of date. Read-only refresh will try again; actions are never resent.` : '');
      renderDashboard();
      ui.sync.textContent = errors.length ? 'Refresh interrupted · data may be stale' : 'Updated just now · live';
    } finally {
      state.refreshing = false;
      ui.refresh.disabled = false;
      if (epoch === state.epoch && state.authenticated && visible() && (state.refreshAgain || filter !== state.filter)) {
        state.refreshAgain = false;
        void refreshDashboard();
      }
    }
  }

  function syncFilter() {
    if (state.filter && !worldById(state.filter)) state.filter = '';
    const signature = JSON.stringify(state.worlds.map((world) => [world.id, world.name]));
    if (signature !== state.filterOptions) {
      state.filterOptions = signature;
      const all = el('option', '', 'All worlds');
      all.value = '';
      const options = state.worlds.map((world) => {
        const option = el('option', '', world.name);
        option.value = world.id;
        return option;
      });
      ui.filter.replaceChildren(all, ...options);
    }
    ui.filter.value = state.filter;
  }

  function renderDashboard() {
    syncFilter();
    const online = state.worlds.filter((world) => world.connected).length;
    byId('stat-online').textContent = String(online);
    byId('stat-worlds-detail').textContent = `${online} of ${state.worlds.length} ${state.worlds.length === 1 ? 'world' : 'worlds'} connected`;
    byId('stat-agents').textContent = String(state.worlds.reduce((sum, world) => sum + world.agents.length, 0));
    byId('stat-conversations').textContent = String(collectConversations(state.worlds).length);
    byId('stat-questions').textContent = String(collectQuestions(state.worlds).length);
    ui.newConversation.disabled = !state.worlds.some((world) => world.connected && world.agents.length);
    renderWorlds();
    renderConversations();
    renderQuestions();
    renderActivity();
    renderCommands();
    if (ui.conversationDialog.open && !state.starting) syncConversationAvailability();
  }

  function renderWorlds() {
    const worlds = state.worlds.filter((world) => !state.filter || world.id === state.filter);
    byId('world-count').textContent = String(worlds.length);
    if (!worlds.length) {
      ui.worldGrid.replaceChildren(empty('div', state.loaded ? 'Your next world starts here' : 'Waiting for your worlds', state.loaded ? 'Add a world and connect it to bring your workspace together.' : 'The Hub has not returned a world list yet.'));
      return;
    }
    ui.worldGrid.replaceChildren(...worlds.map((world) => {
      const card = el('article', `world-card${world.connected ? '' : ' is-offline'}`);
      card.setAttribute('data-world-id', world.id);
      card.setAttribute('data-connected', String(world.connected));
      const top = el('div', 'world-top');
      const initials = world.name.trim().split(/\s+/).slice(0, 2).map((part) => part.slice(0, 1)).join('').toUpperCase() || 'W';
      const avatar = el('span', 'world-avatar', initials);
      avatar.setAttribute('aria-hidden', 'true');
      const identity = el('div', 'world-identity');
      identity.append(el('h3', '', world.name), el('span', 'world-id', world.id));
      const badge = el('span', `badge ${world.connected ? 'badge-positive' : 'badge-warning'}`);
      const dot = el('span', 'status-dot');
      dot.setAttribute('aria-hidden', 'true');
      badge.append(dot, document.createTextNode(world.connected ? 'Online' : 'Offline'));
      top.append(avatar, identity, badge);
      const metrics = el('div', 'world-metrics');
      for (const [count, label] of [[world.agents.length, 'agents'], [world.conversations.length, 'conversations'], [world.questions.length, 'questions']]) {
        const metric = el('span');
        metric.append(el('strong', '', count), document.createTextNode(` ${label}`));
        metrics.append(metric);
      }
      const freshness = el('div', 'world-freshness');
      const seen = el('span', '', validDate(world.lastSeen) ? `Last seen ${relativeTime(world.lastSeen)}` : 'Not seen yet');
      seen.setAttribute('data-world-last-seen', '');
      if (validDate(world.lastSeen)) seen.title = new Date(world.lastSeen).toLocaleString();
      const observed = el('span', '', world.hasSnapshot && validDate(world.observedAt) ? `Snapshot ${relativeTime(world.observedAt)}` : 'No snapshot yet');
      observed.setAttribute('data-world-freshness', '');
      const stale = world.hasSnapshot && (!validDate(world.observedAt) || Date.now() - Date.parse(world.observedAt) > 60000);
      if (stale) {
        observed.className = 'warning-text';
        observed.append(document.createTextNode(' · stale'));
      }
      if (validDate(world.observedAt)) observed.title = new Date(world.observedAt).toLocaleString();
      freshness.append(seen, observed);
      const footer = el('div', 'world-footer');
      const portal = safeWorldUrl(world.baseUrl);
      if (portal) {
        const link = el('a', 'text-link', 'Open world ↗');
        setExternalLink(link, portal);
        footer.append(link);
      } else footer.append(el('span', 'muted', 'Portal URL unavailable'));
      footer.append(el('span', 'world-version', world.version ? `v${world.version.replace(/^v/, '')}` : world.connected ? 'Awaiting snapshot' : 'Commands paused'));
      card.append(top, metrics, freshness, footer);
      return card;
    }));
  }

  function keyedList(container, map, rows, keyOf, create, update, placeholder) {
    const keys = new Set(rows.map(keyOf));
    for (const [key, record] of map) {
      if (!keys.has(key)) { record.node.remove(); map.delete(key); }
    }
    if (!rows.length) { container.replaceChildren(placeholder); return; }
    const emptyNode = container.querySelector('.empty-state');
    if (emptyNode) emptyNode.remove();
    rows.forEach((row, index) => {
      const key = keyOf(row);
      let record = map.get(key);
      if (!record) { record = create(row, key); map.set(key, record); }
      update(record, row);
      if (container.children[index] !== record.node) container.insertBefore(record.node, container.children[index] || null);
    });
  }

  function targetBlocked(key) {
    const receipt = state.locks.get(key);
    return receipt && (['submitting', 'queued', 'sent', 'uncertain'].includes(receipt.status) || (receipt.action === 'answer_question' && receipt.status === 'succeeded'));
  }

  function receiptFeedback(node, receipt) {
    if (!receipt) return;
    const presentation = commandPresentation(receipt.status);
    node.textContent = `${presentation.label}. ${receipt.pollError || receipt.error || presentation.detail}`;
    node.setAttribute('data-tone', receipt.pollError ? 'warning' : presentation.tone);
    node.hidden = false;
  }

  function createConversation({ world, conversation }, key) {
    const node = el('li', 'conversation-row');
    node.setAttribute('data-world-id', world.id);
    node.setAttribute('data-conversation-id', conversation.id);
    const top = el('div', 'conversation-top');
    const info = el('div', 'conversation-info');
    const title = el('h3', 'conversation-title');
    const context = el('div', 'context-line');
    info.append(title, context);
    const side = el('div', 'conversation-side');
    const badge = el('span', 'badge');
    const updated = el('span', 'conversation-time');
    side.append(badge, updated);
    top.append(info, side);
    const actions = el('div', 'row-actions');
    const link = el('a', 'text-link', 'Open conversation ↗');
    const offline = el('span', 'offline-note');
    const open = el('button', 'button button-quiet', 'Send message');
    open.type = 'button';
    open.setAttribute('data-action', 'open-message');
    open.setAttribute('aria-expanded', 'false');
    actions.append(link, offline, open);
    const form = el('form', 'message-form');
    form.hidden = true;
    form.setAttribute('autocomplete', 'off');
    const inputId = `message-${++state.sequence}`;
    form.id = `${inputId}-form`;
    open.setAttribute('aria-controls', form.id);
    const label = el('label', '', 'Message this conversation');
    label.htmlFor = inputId;
    const input = el('textarea');
    input.id = inputId;
    input.name = 'message';
    input.rows = 3;
    input.maxLength = 10000;
    input.required = true;
    input.placeholder = 'What would you like to add?';
    const feedback = el('p', 'inline-feedback');
    feedback.setAttribute('role', 'status');
    const controls = el('div', 'inline-actions');
    const close = el('button', 'button button-quiet button-small', 'Close');
    close.type = 'button';
    const send = el('button', 'button button-primary button-small', 'Send message →');
    send.type = 'submit';
    send.setAttribute('data-action', 'send-message');
    controls.append(close, send);
    form.append(label, input, feedback, controls);
    node.append(top, actions, form);
    const record = { node, title, context, badge, updated, link, offline, open, form, input, feedback, send, world, conversation, key };
    open.addEventListener('click', () => {
      form.hidden = !form.hidden;
      open.setAttribute('aria-expanded', String(!form.hidden));
      if (!form.hidden) input.focus();
    });
    close.addEventListener('click', () => { form.hidden = true; open.setAttribute('aria-expanded', 'false'); open.focus(); });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const currentWorld = worldById(record.world.id);
      const message = input.value.trim();
      if (!message || !currentWorld?.connected || targetBlocked(`send:${key}`)) return;
      const receipt = await queueCommand(currentWorld, 'send_message', { agentId: record.conversation.agentId, conversationId: record.conversation.id, message }, `send:${key}`, record.conversation.title);
      if (!state.authenticated) return;
      if (receipt?.id && receipt.status !== 'failed') input.value = '';
      receiptFeedback(feedback, receipt);
      updateConversation(record, { world: worldById(record.world.id) || record.world, conversation: record.conversation });
    });
    return record;
  }

  function updateConversation(record, { world, conversation }) {
    record.world = world;
    record.conversation = conversation;
    record.title.textContent = text(conversation.title, 'Untitled conversation');
    record.context.replaceChildren(el('span', 'context-world', world.name), document.createTextNode(' · '), el('span', '', agentName(world, conversation.agentId)));
    const waiting = world.questions.some((question) => question.conversationId === conversation.id && question.agentId === conversation.agentId) || Boolean(conversation.pendingQuestion);
    record.badge.className = `badge${waiting ? ' badge-warning' : ''}`;
    record.badge.textContent = waiting ? 'Needs your input' : text(conversation.status, 'Active').replace(/^active$/, 'Active');
    record.updated.textContent = relativeTime(conversation.updatedAt);
    if (validDate(conversation.updatedAt)) record.updated.title = new Date(conversation.updatedAt).toLocaleString();
    setExternalLink(record.link, conversationUrl(world.baseUrl, conversation.agentId, conversation.id));
    const enabled = world.connected && world.agents.some((agent) => agent.id === conversation.agentId);
    record.offline.textContent = !world.connected ? 'Offline · messages paused' : !enabled ? 'Agent unavailable' : '';
    record.open.disabled = !enabled;
    record.send.disabled = !enabled || Boolean(targetBlocked(`send:${record.key}`));
    const receipt = state.locks.get(`send:${record.key}`);
    if (receipt) receiptFeedback(record.feedback, receipt);
  }

  function renderConversations() {
    const rows = collectConversations(state.worlds, state.filter);
    byId('conversation-count').textContent = String(rows.length);
    keyedList(ui.conversations, state.conversationNodes, rows,
      ({ world, conversation }) => routeKey(world.id, conversation.agentId, conversation.id), createConversation, updateConversation,
      empty('li', 'Room for a new conversation', 'Start a conversation with an online agent. Your recent work will appear here.', '≡'));
  }

  function createQuestion({ world, question }, key) {
    const node = el('li', 'question-card');
    node.setAttribute('data-world-id', world.id);
    node.setAttribute('data-request-id', question.requestId);
    node.setAttribute('data-conversation-id', question.conversationId);
    const heading = el('div', 'question-heading');
    const identity = el('div');
    const title = el('h3', 'conversation-title');
    const context = el('div', 'context-line');
    identity.append(title, context);
    const link = el('a', 'text-link', 'Open ↗');
    heading.append(identity, link);
    const prompt = el('p', 'prompt-text');
    const form = el('form', 'answer-form');
    form.setAttribute('autocomplete', 'off');
    const fieldset = el('fieldset');
    const legend = el('legend');
    const mode = questionMode(question);
    legend.textContent = mode === 'free_form' ? 'Your answer' : mode === 'mixed' ? 'Choose an option or write an answer' : mode === 'multiple' ? 'Choose one or more' : 'Choose one';
    fieldset.append(legend);
    let input = null;
    const choices = [];
    const inputName = `answer-${++state.sequence}`;
    if (mode === 'free_form' || mode === 'mixed') {
      input = el('textarea');
      input.name = 'text';
      input.rows = 3;
      input.maxLength = 10000;
      input.required = mode === 'free_form';
      input.setAttribute('aria-label', 'Your answer');
      input.placeholder = 'Write your answer…';
      fieldset.append(input);
    }
    if (mode !== 'free_form') {
      for (const choice of normalizeChoices(question.choices)) {
        const label = el('label', 'choice');
        const option = el('input');
        option.type = mode === 'multiple' ? 'checkbox' : 'radio';
        option.name = inputName;
        option.value = choice.value;
        if (mode === 'single') option.required = true;
        label.append(option, el('span', '', choice.label));
        choices.push(option);
        fieldset.append(label);
      }
    }
    const feedback = el('p', 'inline-feedback');
    feedback.setAttribute('role', 'status');
    const actions = el('div', 'inline-actions');
    const offline = el('span', 'offline-note');
    const cancel = el('button', 'button button-quiet button-small', 'Cancel prompt');
    cancel.type = 'button';
    cancel.setAttribute('data-action', 'cancel-question');
    cancel.setAttribute('aria-expanded', 'false');
    const send = el('button', 'button button-primary button-small', 'Send answer →');
    send.type = 'submit';
    send.setAttribute('data-action', 'answer-question');
    actions.append(offline, cancel, send);
    const confirmation = el('div', 'cancel-confirmation');
    confirmation.id = `${inputName}-cancel`;
    confirmation.hidden = true;
    cancel.setAttribute('aria-controls', confirmation.id);
    confirmation.append(el('p', '', 'Cancel this prompt without an answer? This sends a cancellation to the world.'));
    const confirmActions = el('div', 'inline-actions');
    const keep = el('button', 'button button-secondary button-small', 'Keep prompt');
    keep.type = 'button';
    const confirm = el('button', 'button button-secondary button-small', 'Yes, cancel prompt');
    confirm.type = 'button';
    confirm.setAttribute('data-action', 'confirm-cancel-question');
    confirmActions.append(keep, confirm);
    confirmation.append(confirmActions);
    form.append(fieldset, feedback, actions, confirmation);
    node.append(heading, prompt, form);
    const record = { node, title, context, link, prompt, form, fieldset, input, choices, feedback, offline, cancel, send, confirmation, confirm, world, question, key, mode };
    async function answer(cancelled) {
      const currentWorld = worldById(record.world.id);
      if (!currentWorld?.connected || targetBlocked(`answer:${key}`)) return;
      const selectedValues = cancelled ? [] : choices.filter((choice) => choice.checked).map((choice) => choice.value);
      const answerText = cancelled ? '' : input ? input.value.trim() : '';
      if (!cancelled && !answerText && !selectedValues.length) {
        feedback.textContent = mode === 'free_form' ? 'Write an answer first.' : 'Choose an option first.';
        feedback.setAttribute('data-tone', 'danger');
        return;
      }
      confirmation.hidden = true;
      cancel.setAttribute('aria-expanded', 'false');
      const receipt = await queueCommand(currentWorld, 'answer_question', {
        agentId: record.question.agentId, conversationId: record.question.conversationId, requestId: record.question.requestId,
        text: answerText, selectedValues, cancelled,
      }, `answer:${key}`, record.question.title);
      if (!state.authenticated) return;
      receiptFeedback(feedback, receipt);
      updateQuestion(record, { world: worldById(record.world.id) || record.world, question: record.question });
    }
    form.addEventListener('submit', (event) => { event.preventDefault(); void answer(false); });
    cancel.addEventListener('click', () => { confirmation.hidden = false; cancel.setAttribute('aria-expanded', 'true'); keep.focus(); });
    keep.addEventListener('click', () => { confirmation.hidden = true; cancel.setAttribute('aria-expanded', 'false'); cancel.focus(); });
    confirm.addEventListener('click', () => { void answer(true); });
    return record;
  }

  function updateQuestion(record, { world, question }) {
    record.world = world;
    record.question = question;
    record.title.textContent = text(question.title, 'A question for you');
    record.context.replaceChildren(el('span', 'context-world', world.name), document.createTextNode(' · '), el('span', '', agentName(world, question.agentId)));
    record.prompt.textContent = text(question.prompt, 'This agent is waiting for your input.');
    setExternalLink(record.link, conversationUrl(world.baseUrl, question.agentId, question.conversationId));
    const unavailable = !world.connected || !world.agents.some((agent) => agent.id === question.agentId);
    const blocked = unavailable || Boolean(targetBlocked(`answer:${record.key}`));
    record.fieldset.disabled = blocked;
    record.send.disabled = blocked;
    record.cancel.disabled = blocked;
    record.confirm.disabled = blocked;
    record.offline.textContent = !world.connected ? 'Offline · answers paused' : unavailable ? 'Agent unavailable' : '';
    const receipt = state.locks.get(`answer:${record.key}`);
    if (receipt) receiptFeedback(record.feedback, receipt);
  }

  function renderQuestions() {
    const rows = collectQuestions(state.worlds, state.filter);
    byId('question-count').textContent = String(rows.length);
    keyedList(ui.questions, state.questionNodes, rows, ({ world, question }) => questionKey(world.id, question), createQuestion, updateQuestion,
      empty('li', 'You’re all caught up', 'When an agent needs your input, its question will appear here.', '✓'));
  }

  function renderActivity() {
    byId('activity-scope').textContent = state.filter ? `Latest from ${worldById(state.filter)?.name || state.filter}` : 'Latest across all worlds';
    const items = state.activity.filter((item) => !state.filter || item.worldId === state.filter);
    if (!items.length) {
      ui.activity.replaceChildren(empty('li', 'A quiet moment', 'World updates and command receipts will appear here.', '↗'));
      return;
    }
    ui.activity.replaceChildren(...items.slice(0, 30).map((item) => {
      const row = el('li', 'activity-row');
      const kind = text(item.kind);
      const tone = /error|fail/i.test(kind) ? 'danger' : /disconnect|uncertain/i.test(kind) ? 'warning' : /connect|register|admit/i.test(kind) ? 'positive' : 'info';
      const dot = el('span', 'activity-dot');
      dot.setAttribute('data-tone', tone);
      dot.setAttribute('aria-hidden', 'true');
      const content = el('div', 'activity-content');
      const world = worldById(item.worldId);
      const url = world ? conversationUrl(world.baseUrl, item.agentId, item.conversationId) : null;
      const summary = el(url ? 'a' : 'p', 'activity-summary', text(item.summary, 'World update'));
      if (url) setExternalLink(summary, url);
      const meta = el('div', 'activity-meta');
      meta.append(el('span', '', world?.name || text(item.worldId, 'Hub')), document.createTextNode(' · '), timeElement(item.createdAt));
      content.append(summary, meta);
      row.append(dot, content);
      return row;
    }));
  }

  function createCommand(receipt) {
    const node = el('li', 'command-row');
    node.setAttribute('data-command-local-id', receipt.localId);
    const heading = el('div', 'command-heading');
    const title = el('strong');
    const badge = el('span', 'badge');
    heading.append(title, badge);
    const detail = el('p', 'command-detail');
    const error = el('p', 'command-error');
    const link = el('a', 'text-link command-link', 'Open conversation ↗');
    node.append(heading, detail, error, link);
    return { node, title, badge, detail, error, link };
  }

  function updateCommand(record, receipt) {
    const presentation = commandPresentation(receipt.status);
    const world = worldById(receipt.worldId);
    const action = { start_conversation: 'New conversation', send_message: 'Message', answer_question: receipt.cancelled ? 'Prompt cancellation' : 'Answer' }[receipt.action];
    record.title.textContent = `${action} · ${world?.name || receipt.worldName}${receipt.title ? ` · ${receipt.title}` : ''}`;
    record.badge.className = `badge badge-${receipt.pollError ? 'warning' : presentation.tone}`;
    record.badge.textContent = receipt.pollError ? `${presentation.label} · status unavailable` : presentation.label;
    record.detail.textContent = receipt.pollError || presentation.detail;
    record.error.textContent = receipt.error || '';
    record.error.hidden = !receipt.error;
    record.node.setAttribute('data-command-status', receipt.status);
    if (receipt.id) record.node.setAttribute('data-command-id', receipt.id);
    const conversationId = text(receipt.result?.conversationId, receipt.conversationId);
    const url = world && conversationId ? conversationUrl(world.baseUrl, receipt.agentId, conversationId) : null;
    setExternalLink(record.link, url);
  }

  function renderCommands() {
    ui.commandsPanel.hidden = state.commands.length === 0;
    keyedList(ui.commands, state.commandNodes, state.commands, (receipt) => receipt.localId, createCommand, updateCommand, el('li'));
  }

  function updateActionControls() {
    for (const record of state.conversationNodes.values()) updateConversation(record, record);
    for (const record of state.questionNodes.values()) updateQuestion(record, record);
    renderCommands();
  }

  function applyResource(receipt, resource) {
    const previous = receipt.status;
    if (!isRecord(resource) || !text(resource.id) || (receipt.id && receipt.id !== resource.id) || (resource.worldId && resource.worldId !== receipt.worldId)) {
      receipt.status = 'uncertain';
      receipt.error = 'The Hub returned an invalid receipt. This action was not resent.';
      return;
    }
    receipt.id = resource.id;
    receipt.status = ['queued', 'sent', 'succeeded', 'failed', 'uncertain'].includes(resource.status) ? resource.status : 'uncertain';
    receipt.result = isRecord(resource.result) ? resource.result : null;
    receipt.error = text(resource.error).slice(0, 500);
    receipt.pollError = '';
    if (previous !== receipt.status && terminalStatuses.has(receipt.status)) {
      announce(`${commandPresentation(receipt.status).label}. ${commandPresentation(receipt.status).detail}`);
    }
  }

  async function queueCommand(world, action, payload, target, title) {
    if (!state.authenticated || !world.connected || targetBlocked(target)) return null;
    let key;
    try { key = requestKey(); } catch (error) { showNotice(ui.dashboardError, error.message); return null; }
    const receipt = {
      localId: `receipt-${++state.sequence}`, id: null, status: 'submitting', action, worldId: world.id, worldName: world.name,
      agentId: text(payload.agentId), conversationId: text(payload.conversationId), cancelled: payload.cancelled === true,
      title: text(title), target, error: '', pollError: '', result: null, polling: false,
    };
    state.commands.unshift(receipt);
    // Keep every unresolved or unknown receipt. Only old, known terminal receipts may be trimmed.
    if (state.commands.length > 50) {
      const removable = state.commands.findLastIndex((item) => ['succeeded', 'failed'].includes(item.status));
      if (removable >= 0) state.commands.splice(removable, 1);
    }
    state.locks.set(target, receipt);
    updateActionControls();
    const epoch = state.epoch;
    try {
      const response = await api(`/api/worlds/${encodeURIComponent(world.id)}/commands`, 'POST', { action, payload }, key);
      if (!state.authenticated || epoch !== state.epoch) return receipt;
      if (response.status !== 202) {
        receipt.status = 'uncertain';
        receipt.error = 'The Hub did not return the expected command receipt. This action was not resent.';
      } else applyResource(receipt, response.data);
    } catch (error) {
      if (!state.authenticated || epoch !== state.epoch) return receipt;
      receipt.status = !error.status || error.status >= 500 || error.kind === 'invalid-response' ? 'uncertain' : 'failed';
      receipt.error = error.message;
      announce(`${commandPresentation(receipt.status).label}. ${commandPresentation(receipt.status).detail}`);
    }
    if (state.authenticated && epoch === state.epoch) {
      updateActionControls();
      if (visible() && receipt.id && !terminalStatuses.has(receipt.status)) void pollCommands();
    }
    return receipt;
  }

  async function pollCommands() {
    if (!state.authenticated || !visible()) return;
    const epoch = state.epoch;
    const pending = state.commands.filter((receipt) => receipt.id && !receipt.polling && !terminalStatuses.has(receipt.status)).slice(0, 10);
    await Promise.all(pending.map(async (receipt) => {
      receipt.polling = true;
      try {
        const { data } = await api(`/api/commands/${encodeURIComponent(receipt.id)}`);
        if (state.authenticated && epoch === state.epoch && visible()) applyResource(receipt, data);
      } catch (error) {
        if (state.authenticated && epoch === state.epoch && visible()) receipt.pollError = 'The receipt status could not be checked. Showing the last known state; this action has not been resent. Only the status check will try again.';
      } finally { receipt.polling = false; }
    }));
    if (state.authenticated && epoch === state.epoch) updateActionControls();
  }

  function chooseTab(name, focus = false) {
    state.activeTab = name;
    for (const tab of ['conversations', 'questions']) {
      const selected = name === tab;
      const button = byId(`${tab}-tab`);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      byId(`${tab}-panel`).hidden = !selected;
      if (selected && focus) button.focus();
    }
  }
  for (const name of ['conversations', 'questions']) {
    const button = byId(`${name}-tab`);
    button.addEventListener('click', () => chooseTab(name));
    button.addEventListener('keydown', (event) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        chooseTab(event.key === 'Home' ? 'conversations' : event.key === 'End' ? 'questions' : name === 'questions' ? 'conversations' : 'questions', true);
      }
    });
  }

  ui.filter.addEventListener('change', () => {
    state.filter = ui.filter.value;
    state.activity = [];
    renderDashboard();
    void refreshDashboard();
  });
  ui.refresh.addEventListener('click', () => { void refreshDashboard(); void pollCommands(); });

  function syncConversationAgents(preferredAgent = '') {
    const world = worldById(ui.conversationWorld.value);
    const agents = world ? world.agents : [];
    const options = agents.map((agent) => {
      const option = el('option', '', text(agent.name, agent.id));
      option.value = agent.id;
      return option;
    });
    if (!options.length) {
      const option = el('option', '', 'No available agents');
      option.value = '';
      options.push(option);
    }
    ui.conversationAgent.replaceChildren(...options);
    if (agents.some((agent) => agent.id === preferredAgent)) ui.conversationAgent.value = preferredAgent;
    syncConversationAvailability();
  }

  function syncConversationAvailability() {
    const world = worldById(ui.conversationWorld.value);
    const enabled = world?.connected && world.agents.some((agent) => agent.id === ui.conversationAgent.value);
    ui.conversationSubmit.disabled = !enabled || state.starting || state.startBlocked;
    if (!enabled && !state.starting && !state.startBlocked) showNotice(ui.conversationError, 'This world or agent is offline or unavailable. Choose an online world and agent.');
  }

  function openConversationDialog(preferredWorld = '') {
    if (!state.authenticated) return;
    ui.conversationForm.reset();
    showNotice(ui.conversationError, '');
    state.startBlocked = false;
    const available = state.worlds.filter((world) => world.connected && world.agents.length);
    ui.conversationWorld.replaceChildren(...available.map((world) => {
      const option = el('option', '', world.name);
      option.value = world.id;
      return option;
    }));
    const desired = preferredWorld || state.filter;
    if (available.some((world) => world.id === desired)) ui.conversationWorld.value = desired;
    syncConversationAgents();
    ui.conversationDialog.showModal();
    ui.conversationTitle.focus();
  }

  ui.newConversation.addEventListener('click', () => openConversationDialog());
  ui.conversationWorld.addEventListener('change', () => { showNotice(ui.conversationError, ''); syncConversationAgents(); });
  ui.conversationAgent.addEventListener('change', syncConversationAvailability);
  ui.conversationForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const world = worldById(ui.conversationWorld.value);
    const agentId = ui.conversationAgent.value;
    const title = ui.conversationTitle.value.trim();
    const message = ui.conversationMessage.value.trim();
    if (!world?.connected || !world.agents.some((agent) => agent.id === agentId) || !title || !message || state.starting || state.startBlocked) return;
    state.starting = true;
    syncConversationAvailability();
    showNotice(ui.conversationError, '');
    const receipt = await queueCommand(world, 'start_conversation', { agentId, title, message }, `start:${++state.sequence}`, title);
    state.starting = false;
    if (!state.authenticated) return;
    if (receipt?.id && receipt.status !== 'uncertain' && receipt.status !== 'failed') {
      ui.conversationForm.reset();
      closeDialog(ui.conversationDialog);
      announce(`${commandPresentation(receipt.status).label}. Follow the command receipt below.`);
    } else {
      state.startBlocked = receipt?.status === 'uncertain';
      showNotice(ui.conversationError, receipt ? `${commandPresentation(receipt.status).label}. ${receipt.error || commandPresentation(receipt.status).detail} ${receipt.status === 'uncertain' ? 'Do not resend this draft without checking the world.' : ''}` : 'The action was not sent. Check the connection and try again.');
    }
    syncConversationAvailability();
  });

  function renderEnrollment() {
    if (!state.enrollment) return;
    const { token } = state.enrollment;
    const displayToken = state.tokenRevealed ? token : '<CONNECTOR_TOKEN>';
    ui.connectorToken.value = token;
    ui.connectorToken.type = state.tokenRevealed ? 'text' : 'password';
    ui.revealToken.textContent = state.tokenRevealed ? 'Hide' : 'Show';
    ui.revealToken.setAttribute('aria-pressed', String(state.tokenRevealed));
    ui.connectorSnippet.textContent = JSON.stringify(connectorConfiguration(state.enrollment, displayToken), null, 2);
    ui.connectorHeader.textContent = `Authorization: Bearer ${displayToken}`;
  }

  async function copySecret(value) {
    try {
      if (!window.navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await window.navigator.clipboard.writeText(value);
      ui.enrollmentFeedback.textContent = 'Copied. Keep this value private and clear it from your clipboard when you are done.';
    } catch (_) {
      ui.enrollmentFeedback.textContent = 'Clipboard access is unavailable. Use Show, then select and copy the value manually.';
    }
  }

  byId('add-world-button').addEventListener('click', () => {
    if (!state.authenticated) return;
    clearEnrollment();
    ui.worldForm.reset();
    ui.worldForm.hidden = false;
    state.enrollmentUnknown = false;
    ui.worldSubmit.disabled = false;
    showNotice(ui.worldError, '');
    ui.worldDialog.showModal();
    ui.worldId.focus();
  });
  ui.worldForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.authenticated || !state.connectorUrl || state.enrolling || state.enrollmentUnknown) return;
    const id = ui.worldId.value.trim();
    const name = ui.worldName.value.trim();
    const baseUrl = safeWorldUrl(ui.worldBaseUrl.value.trim());
    if (!id || !name || !baseUrl) { showNotice(ui.worldError, 'Enter a world ID, a name, and a full http:// or https:// URL without credentials.'); return; }
    state.enrolling = true;
    ui.worldSubmit.disabled = true;
    ui.worldSubmit.textContent = 'Creating token once…';
    showNotice(ui.worldError, '');
    const epoch = state.epoch;
    try {
      const { data } = await api('/api/worlds', 'POST', { id, name, baseUrl });
      if (!state.authenticated || epoch !== state.epoch) return;
      const token = text(data?.connectorToken);
      if (!token) throw new ApiError('Registration returned no connector token. Check the world list before registering again.', 0, 'invalid-response');
      state.enrollment = { worldId: id, name, baseUrl, token, connectorUrl: state.connectorUrl };
      state.tokenRevealed = false;
      ui.worldForm.hidden = true;
      ui.enrollment.hidden = false;
      renderEnrollment();
      ui.connectorToken.focus();
      announce('World registered. Save its connector token now; it is shown only once.');
      void refreshDashboard();
    } catch (error) {
      if (!state.authenticated || epoch !== state.epoch) return;
      state.enrollmentUnknown = !error.status || error.status >= 500 || error.kind === 'invalid-response';
      showNotice(ui.worldError, state.enrollmentUnknown ? 'Registration outcome unknown. This request was not retried. Check the world list before registering again; a token shown only once cannot be recovered here.' : error.message);
    } finally {
      state.enrolling = false;
      ui.worldSubmit.disabled = state.enrollmentUnknown;
      ui.worldSubmit.textContent = 'Create connector token';
    }
  });
  ui.revealToken.addEventListener('click', () => { state.tokenRevealed = !state.tokenRevealed; renderEnrollment(); });
  byId('copy-connector-token').addEventListener('click', () => { if (state.enrollment) void copySecret(state.enrollment.token); });
  byId('copy-connector-snippet').addEventListener('click', () => {
    if (state.enrollment) {
      void copySecret(JSON.stringify(connectorConfiguration(state.enrollment, state.enrollment.token), null, 2));
    }
  });
  ui.worldDialog.addEventListener('close', clearEnrollment);
  ui.worldDialog.addEventListener('cancel', (event) => { if (state.enrolling) event.preventDefault(); });
  ui.conversationDialog.addEventListener('cancel', (event) => { if (state.starting) event.preventDefault(); });
  for (const button of document.querySelectorAll('[data-close]')) {
    button.addEventListener('click', () => { const dialog = byId(button.getAttribute('data-close')); if (dialog) closeDialog(dialog); });
  }

  document.addEventListener('visibilitychange', () => {
    if (!visible()) {
      stopPolling();
      for (const controller of state.readControllers) controller.abort();
      ui.sync.textContent = 'Refresh paused while this tab is hidden';
    } else if (state.authenticated) {
      startPolling();
      void refreshDashboard();
      void pollCommands();
    }
  });
  window.addEventListener('pagehide', () => { stopPolling(); clearEnrollment(); });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && state.authenticated) { startPolling(); void refreshDashboard(); void pollCommands(); }
  });

  window.__hub.refresh = refreshDashboard;
  void checkSession();
}());
