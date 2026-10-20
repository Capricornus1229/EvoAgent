/* EvoAgent UI · existing HTTP contracts only. No backend logic is implemented here. */
(() => {
    "use strict";
    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
    const ICONS = {
        grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
        "plus-square": '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M12 8v8M8 12h8"/>',
        plus: '<path d="M12 5v14M5 12h14"/>',
        layers: '<path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5"/>',
        sparkles: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3ZM20 3v4M18 5h4M3 18v3M1.5 19.5h3"/>',
        flask: '<path d="M9 3h6M10 3v6l-5.5 9.5A2.3 2.3 0 0 0 6.5 22h11a2.3 2.3 0 0 0 2-3.5L14 9V3M8 14h8M9 18h.01M14 17h.01"/>',
        "git-branch": '<circle cx="6" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M6 7.5v9M18 8.5c0 6-12 2-12 8"/>',
        "git-pull-request": '<circle cx="6" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="M6 7.5v9M18 16.5V8a3 3 0 0 0-3-3h-3M14 3l-2 2 2 2"/>',
        activity: '<path d="M2 12h4l3-8 5 16 3-8h5"/>',
        refresh: '<path d="M20 7a9 9 0 0 0-15-2L2 8m0-5v5h5M4 17a9 9 0 0 0 15 2l3-3m0 5v-5h-5"/>',
        moon: '<path d="M20 14a8.5 8.5 0 0 1-10-10A9 9 0 1 0 20 14Z"/>',
        sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19"/>',
        "log-out": '<path d="M9 4H4v16h5M13 8l4 4-4 4M8 12h13"/>',
        menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
        "arrow-right": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
        "arrow-up-right": '<path d="M6 18 18 6M6 6h12v12"/>',
        shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 2.5 2.5L16 9"/>',
        command: '<path d="M9 7V5a2 2 0 1 0-2 2h10a2 2 0 1 0-2-2v12a2 2 0 1 0 2-2H7a2 2 0 1 0 2 2V7"/>',
        "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
        scan: '<path d="M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5M7 12h10M9 8h6M9 16h6"/>',
        play: '<path d="m8 4 12 8-12 8V4Z"/>',
        terminal: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 8 4 4-4 4M13 16h4"/>',
        search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
        copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/>',
        download: '<path d="M12 3v12m-4-4 4 4 4-4M4 15v6h16v-6"/>',
        wrench: '<path d="m14 6 4 4 3-3a6 6 0 0 1-8 8l-7 7-4-4 7-7a6 6 0 0 1 8-8l-3 3Z"/>',
        "file-search": '<path d="M14 2H4v20h10M14 2v6h6l-6-6Z"/><circle cx="16" cy="15" r="4"/><path d="m19 18 3 3M8 7h2M8 11h3"/>',
        cpu: '<rect x="6" y="6" width="12" height="12" rx="3"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
        eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
        "eye-off": '<path d="m3 3 18 18M10 5a11 11 0 0 1 2 0c6 0 10 7 10 7a15 15 0 0 1-4 4M6 6a20 20 0 0 0-4 6s4 7 10 7c2 0 4-.8 5-1.7M10 10a3 3 0 0 0 4 4"/>',
        lock: '<rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 1 1 8 0v3M12 14v3"/>',
        clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        alert: '<path d="M10.2 4.5 2.6 18a2 2 0 0 0 1.8 3h15.2a2 2 0 0 0 1.8-3L13.8 4.5a2 2 0 0 0-3.6 0Z"/><path d="M12 9v5M12 17h.01"/>',
        percent: '<path d="M5 19 19 5"/><circle cx="6.5" cy="6.5" r="3"/><circle cx="17.5" cy="17.5" r="3"/>',
        message: '<path d="M21 15a3 3 0 0 1-3 3H9l-6 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v9Z"/><path d="M7 8h10M7 12h7"/>',
        code: '<path d="m8 6-6 6 6 6M16 6l6 6-6 6M14 3l-4 18"/>',
        database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
        box: '<path d="m12 2 9 5v10l-9 5-9-5V7l9-5ZM3 7l9 5 9-5M12 12v10M7.5 4.5l9 5"/>',
        check: '<path d="m5 12 4 4L19 6"/>'
    };
    const icon = (name) => `<span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24">${ICONS[name] || ICONS.code}</svg></span>`;
    function decorate(root = document) {
        $$('[data-icon]', root).forEach(el => {
            el.innerHTML = icon(el.dataset.icon);
        });
    }
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
    const json = value => JSON.stringify(value, null, 2);
    const list = value => Array.isArray(value) ? value : [];
    const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const storage = { get(key) {
            try {
                return localStorage.getItem(key);
            }
            catch {
                return null;
            }
        }, set(key, value) {
            try {
                localStorage.setItem(key, value);
            }
            catch {
            }
        }, remove(key) {
            try {
                localStorage.removeItem(key);
            }
            catch {
            }
        } };
    const titles = {
        overview: '运行总览', review: '发起审查', tasks: '任务中心', skills: 'Skills 能力库', evolution: '演进实验室'
    };
    const stateLabels = {
        PENDING: '等待中', PLANNING: '规划中', EXECUTING: '执行中', REVIEWING: '汇总中', SUCCESS: '已完成', FAILED: '失败', CANCELLED: '已取消'
    };
    const severityLabels = {
        critical: '严重', high: '高风险', medium: '中风险', low: '低风险', unknown: '待复核'
    };
    const feedbackLabels = {
        false_positive: '误报', missed_issue: '漏报', bad_fix: '坏修复', accepted: '认可', execution_error: '执行错误'
    };
    const decisionLabels = {
        activated: '已激活', ready: '已通过评测，待激活', rolled_back: '已回滚', experimental_passed: '实验评测通过，未激活', rejected: '已拒绝', deferred: '暂缓', candidate: '候选', blocked: '被门禁拦截'
    };
    const activeStates = new Set(['PENDING', 'PLANNING', 'EXECUTING', 'REVIEWING']);
    const needsTaskRefresh = task => !task || activeStates.has(task.state) ||
        (task.state === 'SUCCESS' && task.publication_status === 'pending');
    const state = {
        view: 'overview', token: storage.get('evoagent_token') || '', authEpoch: 0, login: false, role: '', dashboard: null, tasks: [], skills: [], selectedId: null, task: null, tab: 'findings', taskSeq: 0, taskListSeq: 0, dashboardSeq: 0, skillSeq: 0, evolutionSeq: 0, feedbackSeq: 0, polling: false, feedbackDrafts: new Map(), fixResults: new Map(), publicationRetries: new Set()
    };
    let toastTimer, confirmResolver;
    const number = value => typeof value === 'number' && Number.isFinite(value) ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value) : '—';
    const asNumber = value => {
        const n = Number(value);
        return value !== null && value !== undefined && value !== '' && Number.isFinite(n) ? n : null;
    };
    function time(value, full = false) {
        const d = new Date(value);
        return value && Number.isFinite(d.getTime()) ? new Intl.DateTimeFormat('zh-CN', full ? {
            year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
        } : {
            month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
        }).format(d) : '时间未知';
    }
    function duration(ms) {
        const n = asNumber(ms);
        if (n === null)
            return '—';
        return n >= 60000 ? `${(n / 60000).toFixed(1)} min` : `${(n / 1000).toFixed(1)} s`;
    }
    function statusBadge(value) {
        const key = Object.hasOwn(stateLabels, value) ? value : 'PENDING';
        return `<span class="status state-${key.toLowerCase()}">${esc(stateLabels[value] || value || '未知状态')}</span>`;
    }
    function riskBadge(value) {
        const key = Object.hasOwn(severityLabels, value) ? value : 'low';
        return `<span class="pill risk-${key}">${esc(severityLabels[value] || value || '未评级')}</span>`;
    }
    function empty(title, description = '', name = 'file-search') {
        return `<div class="empty-state"><span class="empty-icon">${icon(name)}</span><h3>${esc(title)}</h3>${description ? `<p>${esc(description)}</p>` : ''}</div>`;
    }
    function toast(message, error = false) {
        const node = $('#toast');
        node.textContent = message;
        node.classList.toggle('error', error);
        node.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => node.classList.remove('show'), error ? 6500 : 3600);
    }
    function busy(button, on, text = '处理中…') {
        if (!button)
            return;
        if (on) {
            if (button.getAttribute('aria-busy') === 'true')
                return;
            button.dataset.content = button.innerHTML;
            button.dataset.wasDisabled = String(button.disabled);
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
            button.innerHTML = `<span class="busy-label">${esc(text)}</span>`;
        }
        else {
            button.disabled = button.dataset.wasDisabled === 'true';
            button.removeAttribute('aria-busy');
            if (button.dataset.content !== undefined) {
                button.innerHTML = button.dataset.content;
                delete button.dataset.content;
            }
        }
    }
    function parseIdentity() {
        try {
            const body = state.token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
            return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(body.padEnd(Math.ceil(body.length / 4) * 4, '=')), c => c.charCodeAt(0))));
        }
        catch {
            return {};
        }
    }
    // Decoding is only for presentation. Every permission is still checked by the server.
    const can = permission => !state.token || ({ admin: ['read', 'review', 'fix', 'manage', 'audit'], maintainer: ['read', 'review', 'fix'], auditor: ['read', 'audit'] }[state.role] || []).includes(permission);
    function updateIdentity() {
        const p = parseIdentity();
        state.role = p.role || '';
        const name = p.username || '工作区用户';
        $('#user-name').textContent = name;
        $('#user-avatar').textContent = name.slice(0, 2).toUpperCase();
        $('#workspace-name').textContent = p.tenant || '审查工作区';
        $('#logout').classList.toggle('hidden', !state.token);
        $('#local-user').classList.toggle('hidden', Boolean(state.token) || state.login);
        $('#reload-skills').disabled = !can('manage');
        $('#auto-evolve').disabled = !can('manage');
        $('#rollback-prompt').disabled = !can('manage');
        $('button[type=submit]', $('#evolution-form')).disabled = !can('manage');
    }
    function showLogin(message = '') {
        state.login = true;
        $('#login-overlay').classList.remove('hidden');
        $('#app-shell').inert = true;
        document.body.style.overflow = 'hidden';
        $('#login-error').textContent = message;
        $('#local-user').classList.add('hidden');
        closeMenu();
        if ($('#confirm-dialog').open)
            finishConfirm(false);
        setTimeout(() => $('input[name=username]', $('#login-form')).focus(), 0);
    }
    function hideLogin() {
        state.login = false;
        $('#login-overlay').classList.add('hidden');
        $('#app-shell').inert = false;
        document.body.style.overflow = '';
        $('#login-error').textContent = '';
        updateIdentity();
    }
    function expireSession() {
        clearSession();
        showLogin('登录已过期，请重新登录。');
    }
    class ApiError extends Error {
        constructor(message, status = 0) {
            super(message);
            this.status = status;
        }
    }
    async function api(path, options = {}) {
        const { timeout = 25000, ...request } = options;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);
        const epoch = state.authEpoch;
        const headers = { Accept: 'application/json', ...(request.headers || {}) };
        if (state.token)
            headers.Authorization = `Bearer ${state.token}`;
        try {
            const response = await fetch(path, {
                ...request, headers, signal: controller.signal, cache: 'no-store'
            });
            if (epoch !== state.authEpoch)
                throw new ApiError('已忽略上一登录会话的响应', -1);
            const contentType = response.headers.get('content-type') || '';
            const data = contentType.includes('json') ? await response.json() : await response.text();
            if (epoch !== state.authEpoch)
                throw new ApiError('已忽略上一登录会话的响应', -1);
            if (!response.ok) {
                const obj = object(data);
                const details = [obj.error, obj.detail].filter(x => typeof x === 'string' && x.trim());
                const message = details.length ? [...new Set(details)].join('：') : typeof data === 'string' && !/<[a-z]/i.test(data) ? data.slice(0, 700) : `请求失败（HTTP ${response.status}）`;
                if (path !== '/v1/auth/login' && (response.status === 401 || (response.status === 403 && /token has expired|invalid token|Bearer token is required/i.test(message)))) {
                    if (state.token)
                        expireSession();
                    else
                        showLogin();
                }
                throw new ApiError(message || `HTTP ${response.status}`, response.status);
            }
            return data;
        }
        catch (error) {
            if (error instanceof ApiError)
                throw error;
            if (error.name === 'AbortError')
                throw new ApiError(request.method === 'POST' ? '等待服务器响应超时；操作可能仍在执行。请先检查任务或评测记录，不要立即重复提交。' : '连接超时，请检查 EvoAgent 服务与端口转发。');
            throw new ApiError(`网络请求未完成：${error.message}。${request.method === 'POST' ? '操作可能已被接收，请先检查记录再重试。' : '请检查服务连接。'}`);
        }
        finally {
            clearTimeout(timer);
        }
    }
    const post = (path, data, timeout = 180000) => api(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), timeout
    });
    function errorToast(error) {
        if (!state.login && error.status !== -1)
            toast(error.message, true);
    }
    function synced() {
        const now = new Date();
        $('#last-synced').textContent = `已同步 ${now.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' })}`;
    }
    function confirmAction(title, message, label = '确认') {
        if (confirmResolver)
            return Promise.resolve(false);
        $('#confirm-title').textContent = title;
        $('#confirm-message').textContent = message;
        $('#confirm-ok').textContent = label;
        $('#confirm-dialog').showModal();
        $('#confirm-cancel').focus();
        return new Promise(resolve => {
            confirmResolver = resolve;
        });
    }
    function finishConfirm(value) {
        $('#confirm-dialog').close();
        if (confirmResolver) {
            const resolve = confirmResolver;
            confirmResolver = null;
            resolve(value);
        }
    }
    function syncMenuAccessibility() {
        const mobile = window.matchMedia('(max-width:780px)').matches;
        const open = $('#sidebar').classList.contains('open');
        $('#sidebar').inert = mobile && !open;
    }
    function closeMenu() {
        $('#sidebar').classList.remove('open');
        $('#sidebar-scrim').classList.remove('open');
        $('#sidebar-scrim').hidden = true;
        $('#menu-toggle').setAttribute('aria-expanded', 'false');
        syncMenuAccessibility();
    }
    function show(view, updateHash = true, load = true) {
        if (!Object.hasOwn(titles, view))
            view = 'overview';
        state.view = view;
        $$('.view').forEach(node => node.classList.toggle('active', node.id === `view-${view}`));
        $$('.nav-item').forEach(node => {
            const active = node.dataset.view === view;
            node.classList.toggle('active', active);
            if (active)
                node.setAttribute('aria-current', 'page');
            else
                node.removeAttribute('aria-current');
        });
        $('#page-title').textContent = titles[view];
        document.title = `${titles[view]} · EvoAgent`;
        if (updateHash && location.hash !== `#${view}`)
            history.pushState(null, '', `#${view}`);
        closeMenu();
        window.scrollTo({ top: 0, behavior: 'auto' });
        if (load && !state.login)
            return refreshView(view);
        return Promise.resolve(true);
    }
    function renderRuntime(llm = {}, failed = false) {
        const enabled = Boolean(llm.enabled);
        const detail = failed ? '暂时无法读取模型配置' : enabled ? `${llm.provider || 'custom'} / ${llm.model || '未披露模型名称'}` : '尚未配置模型，暂时无法发起 Agentic 审查';
        $('#llm-capability-detail').textContent = detail;
        $('#llm-runtime-model').textContent = detail;
        $('#llm-runtime-status').className = `pill ${enabled ? 'accent-soft' : 'neutral'}`;
        $('#llm-runtime-status').textContent = failed ? '读取失败' : enabled ? '已配置' : '待配置';
        $('#runtime-pill').className = `pill ${enabled ? 'accent-soft' : 'neutral'}`;
        $('#runtime-pill').textContent = failed ? '连接异常' : enabled ? '模型已配置' : '模型未配置';
        $('#execution-chain').innerHTML = `<div class="agent-step"><span class="step-number">01</span><span><strong>Tool / Scanner</strong><small>本地规则扫描；有仓库上下文时提供工具证据</small></span><em>事实</em></div><div class="agent-step ${enabled ? 'is-active' : ''}" id="llm-agent-step"><span class="step-number">02</span><span><strong>Lead · Workers · Critic</strong><small id="llm-agent-detail">${esc(detail)}</small></span><em id="llm-agent-state">${enabled ? '就绪' : '待配置'}</em></div><div class="agent-step"><span class="step-number">03</span><span><strong>Quality Gates</strong><small>格式、证据、置信度与发布门禁</small></span><em>验证</em></div>`;
        $('#review-mode').disabled = !enabled;
        const button = $('button[type=submit]', $('#review-form'));
        if (button.getAttribute('aria-busy') !== 'true')
            button.disabled = !enabled || !can('review');
        $('#review-model-hint').textContent = !can('review') ? '当前角色没有发起审查权限。' : enabled ? `当前模型：${detail}。提交的代码将发送给模型服务，请勿包含密钥。` : '尚未配置模型。请在服务器 .env 配置模型并重启服务。';
    }
    const statCard = (label, value, note, style, name) => `<article class="stat ${style}"><div class="stat-head"><span>${esc(label)}</span>${icon(name)}</div><strong>${esc(value)}</strong><small>${esc(note)}</small></article>`;
    function taskRows(tasks) {
        if (!tasks.length)
            return empty('这里还没有任务', '提交一份 Diff，或等待 GitHub PR 的自动审查。');
        return tasks.map(task => `<button class="task-row ${state.selectedId === task.id ? 'selected' : ''}" data-task="${esc(task.id)}" type="button" ${state.selectedId === task.id ? 'aria-current="true"' : ''}><span class="task-main"><span class="task-glyph">${icon(task.pull_request ? 'git-pull-request' : 'code')}</span><span class="task-copy"><span class="task-name" title="${esc(task.repository)}">${esc(task.repository || '未命名仓库')}</span><span class="task-meta"><span>${task.pull_request ? `PR #${esc(task.pull_request)}` : '手动 Diff'}</span><span>${esc(time(task.created_at))}</span></span></span></span>${statusBadge(task.state)}</button>`).join('');
    }
    function bindTasks(root) {
        $$('[data-task]', root).forEach(button => button.addEventListener('click', () => openTask(button.dataset.task)));
    }
    async function loadDashboard(quiet = false) {
        const seq = ++state.dashboardSeq;
        try {
            const data = await api('/api/dashboard');
            if (seq !== state.dashboardSeq)
                return false;
            state.dashboard = data;
            updateIdentity();
            renderRuntime(object(data.llm));
            $('#system-status').textContent = `${data.queue || '服务在线'}`;
            $('#system-status').title = `${data.queue || ''} · ${data.orchestrator || ''}`;
            $('#service-dot').className = 'live-dot online';
            $('#connection-banner').classList.add('hidden');
            const s = object(data.stats);
            const rate = asNumber(s.success_rate);
            $('#stats').innerHTML = [statCard('累计任务', number(s.tasks_total), '当前租户的审查记录', '', 'layers'), statCard('已完成', number(s.tasks_success), '完成流程，不等于无缺陷', 'success', 'check-circle'), statCard('执行失败', number(s.tasks_failed), '需要检查错误与配置', 'failed', 'alert'), statCard('完成占比', rate === null ? '—' : `${Math.round(rate * 100)}%`, '已完成 / 累计任务', '', 'percent'), statCard('待处理反馈', number(s.unresolved_failure_cases), '未解决的失败与反馈案例', 'warning', 'message'), statCard('已激活版本', number(s.active_skill_versions), '演进版本，非能力总数', '', 'sparkles')].join('');
            $('#nav-task-count').textContent = number(s.tasks_total);
            $('#recent-tasks').innerHTML = taskRows(list(data.tasks).slice(0, 5));
            bindTasks($('#recent-tasks'));
            synced();
            return true;
        }
        catch (error) {
            if (error.status === -1 || seq !== state.dashboardSeq)
                return false;
            renderRuntime({}, true);
            $('#service-dot').className = 'live-dot offline';
            $('#system-status').textContent = state.login ? '等待登录' : '服务连接异常';
            if (!state.dashboard)
                $('#stats').innerHTML = empty('暂时无法读取数据', '登录后或恢复服务连接后重试。', 'database');
            if (!quiet && !state.login) {
                $('#connection-banner').textContent = error.message;
                $('#connection-banner').classList.remove('hidden');
            }
            return false;
        }
    }
    function renderTasks() {
        const query = $('#task-search').value.trim().toLowerCase();
        const filter = $('#task-state-filter').value;
        const filtered = state.tasks.filter(t => `${t.repository} ${t.id} ${t.pull_request ? `PR #${t.pull_request}` : ''}`.toLowerCase().includes(query) && (!filter || (filter === 'ACTIVE' ? activeStates.has(t.state) : t.state === filter)));
        $('#task-count').textContent = number(filtered.length);
        $('#all-tasks').innerHTML = filtered.length ? taskRows(filtered) : empty(state.tasks.length ? '没有匹配的任务' : '暂无审查任务', state.tasks.length ? '试试清空搜索或切换状态。' : '新建审查后会显示在这里。');
        $('#task-list-note').textContent = `已加载最近 ${state.tasks.length} 条，筛选后 ${filtered.length} 条；接口最多返回最近 200 条。`;
        bindTasks($('#all-tasks'));
    }
    async function loadTasks(quiet = false) {
        const seq = ++state.taskListSeq;
        if (!quiet && !state.tasks.length)
            $('#all-tasks').innerHTML = '<div class="list-loading skeleton"></div><div class="list-loading skeleton"></div>';
        try {
            const data = await api(`/api/tasks?limit=${encodeURIComponent($('#task-limit').value)}`);
            if (seq !== state.taskListSeq)
                return false;
            const incoming = list(data.tasks);
            const changed = json(incoming) !== json(state.tasks);
            state.tasks = incoming;
            if (!quiet || changed)
                renderTasks();
            synced();
            return true;
        }
        catch (error) {
            if (seq !== state.taskListSeq || error.status === -1)
                return false;
            if (!state.tasks.length)
                $('#all-tasks').innerHTML = empty('任务加载失败', error.message, 'alert');
            if (!quiet)
                errorToast(error);
            return false;
        }
    }
    function saveFeedbackDraft() {
        if (!state.selectedId)
            return;
        state.feedbackDrafts.set(state.selectedId, Object.fromEntries(new FormData($('#feedback-form')).entries()));
    }
    function renderFeedbackForm(task) {
        const form = $('#feedback-form');
        form.reset();
        const findings = list(task.report?.findings);
        $('#feedback-finding').innerHTML = '<option value="">不关联已有结论</option>' + findings.map((f, i) => `<option value="${i}">${i + 1}. ${esc(f.title || f.rule_id)}${f.path ? ` · ${esc(f.path)}:${esc(f.line)}` : ''}</option>`).join('');
        const draft = state.feedbackDrafts.get(task.id);
        if (draft)
            for (const [key, value] of Object.entries(draft)) {
                const el = form.elements.namedItem(key);
                if (el)
                    el.value = value;
            }
        toggleMissed();
        $('#feedback-result').textContent = '';
    }
    function setReportTab(tab, focus = false) {
        state.tab = ['findings', 'execution', 'json'].includes(tab) ? tab : 'findings';
        $$('[data-report-tab]').forEach(button => {
            const on = button.dataset.reportTab === state.tab;
            button.setAttribute('aria-selected', String(on));
            button.tabIndex = on ? 0 : -1;
            if (focus && on)
                button.focus();
        });
        ['findings', 'execution', 'json'].forEach(name => $(`#report-${name}`).classList.toggle('hidden', name !== state.tab));
    }
    function renderFindings(task) {
        const report = object(task.report);
        const findings = list(report.findings);
        if (!task.report) {
            $('#report-findings').innerHTML = task.error ? `<div class="notice error"><strong>任务未完成</strong><p>${esc(task.error)}</p></div>` : empty(activeStates.has(task.state) ? '审查仍在进行' : task.state === 'CANCELLED' ? '任务已取消' : '暂无完整报告', activeStates.has(task.state) ? '页面会定时更新选中任务。请勿反复提交同一份变更。' : '请查看执行过程；页面不会自动重试这个任务。', 'activity');
            return;
        }
        const withheld = list(report.execution?.rejected_findings);
        const counts = object(report.execution?.finding_counts);
        const countSummary = Object.keys(counts).length ? `<p class="code-label">原始候选 ${number(counts.raw_candidates)} · Lead 接纳 ${number(counts.lead_selected)} · 门禁通过 ${number(counts.gate_accepted)} · 最终结论 ${number(counts.final_published)}</p>` : '';
        const head = withheld.length ? `<div class="notice warning"><strong>${withheld.length} 个候选未通过门禁，仍需人工核验。</strong><p>这些不是已确认问题，不会直接用于自动修复；也不能把它们当成已排除的缺陷。</p><details><summary>查看候选与拒绝理由</summary>${withheld.map(item => `<p><strong>${esc(item.finding?.title || item.rule_id)} · ${esc(item.path)}:${esc(item.line)}</strong></p><pre class="fix-code">${esc(list(item.reasons).join('\n'))}</pre>${item.finding ? `<pre class="json-code">${esc(json(item.finding))}</pre>` : ''}`).join('')}</details></div>` : '';
        $('#report-findings').innerHTML = head + (findings.length ? findings.map((f, i) => `<article class="finding-card"><div class="finding-heading">${riskBadge(f.severity)}<h4>${esc(f.title || f.rule_id || `结论 ${i + 1}`)}</h4></div><div class="finding-locator"><span>${icon('code')}</span><span>${esc(f.path)}:${esc(f.line)}</span><span>${esc(f.cwe || f.rule_id || '')}</span><span>置信度 ${asNumber(f.confidence) === null ? '未披露' : `${Math.round(Number(f.confidence) * 100)}%`}</span></div><p>${esc(f.explanation || '未提供解释')}</p><div class="code-label">${icon('scan')}代码证据</div><pre class="evidence-code">${esc(f.evidence || '未提供代码证据')}</pre><div class="code-label">${icon('wrench')}修复建议 · 尚未执行</div><pre class="fix-code">${esc(f.fix || '未提供建议')}</pre><div class="code-label">${icon('check-circle')}测试建议 · 不代表测试已通过</div><p>${esc(f.test || '未提供测试建议')}</p><details class="finding-extra"><summary>查看原始结论、证据引用与门禁信息</summary><pre class="json-code" tabindex="0">${esc(json(f))}</pre></details></article>`).join('') : '<div class="notice"><strong>本次未保留可操作的问题。</strong><p>这不等于代码已被证明没有缺陷。请结合实际变更、模型执行记录与人工复核判断。</p></div>');
        const files = list(report.files_reviewed);
        if (countSummary)
            $('#report-findings').insertAdjacentHTML('afterbegin', countSummary);
        if (files.length)
            $('#report-findings').insertAdjacentHTML('beforeend', `<p class="code-label">已审查文件</p><div class="file-tags">${files.map(path => `<code>${esc(path)}</code>`).join('')}</div>`);
    }
    function renderExecution(task) {
        const report = object(task.report);
        const e = object(report.execution);
        const trace = list(task.trace);
        const modelCalls = list(e.model_call_log);
        const collaboration = object(report.collaboration);
        const context = object(e.repository_context);
        const modelRows = modelCalls.map(c => `<tr><td>${esc(c.role)}</td><td>${esc(c.model)}</td><td>${number(c.input_tokens)}</td><td>${number(c.output_tokens)}</td><td>${esc(duration(c.duration_ms))}</td><td class="${c.ok ? 'text-success' : 'text-error'}">${c.ok ? '成功' : '失败'}</td></tr>`).join('');
        $('#report-execution').innerHTML = `<div class="execution-section"><div class="notice ${context.available ? '' : 'warning'}">${context.available ? '已提供仓库上下文；具体使用的工具与证据请查看调用记录。' : '未提供可用的完整仓库上下文。本次审查主要依据提交的 Diff，不能等同于全仓检查。'}</div><h4>任务时间线</h4>${trace.length ? `<ol class="timeline">${trace.map(t => `<li><strong>${esc(stateLabels[t.state] || t.state)} · 第 ${esc(t.step)} 步</strong><time>${esc(time(t.created_at, true))}</time><p>${esc(t.message)}</p></li>`).join('')}</ol>` : empty('暂无执行轨迹', '等待任务进入执行阶段。', 'activity')}<h4>模型调用记录</h4>${modelCalls.length ? `<div class="call-table-wrap"><table class="call-table"><thead><tr><th>角色</th><th>模型</th><th>输入 Token</th><th>输出 Token</th><th>耗时</th><th>状态</th></tr></thead><tbody>${modelRows}</tbody></table></div>` : '<p class="muted">报告没有提供逐次模型调用记录。</p>'}<div class="model-tags"><span>模型调用失败 ${number(e.failed_model_calls)}</span><span>工具调用 ${number(e.tool_calls)}</span><span>工具调用失败 ${number(e.failed_tool_calls)}</span><span>记录费用 ${asNumber(e.cost_usd) === null ? '未披露' : `$${Number(e.cost_usd).toFixed(6)}`}</span></div><p class="scroll-note">费用为项目按配置单价计算的记录；0 不代表模型服务免费。</p><h4>Agent 协作</h4><p class="muted">协议 ${esc(collaboration.protocol || '未披露')} · 角色 ${esc(list(collaboration.roles).join(' / ') || '未披露')} · 返工 ${number(collaboration.revision_rounds)} 轮</p><details class="raw-details"><summary>查看完整协作记录</summary><pre class="json-code" tabindex="0">${esc(json(collaboration))}</pre></details><details class="raw-details"><summary>查看工具、上下文、记忆与门禁记录</summary><pre class="json-code" tabindex="0">${esc(json(e))}</pre></details></div>`;
    }
    function executionWarning(report) {
        const e = object(report.execution);
        if (e.review_status === 'needs_review')
            return '<div class="notice warning"><strong>部分候选未完成验证，仍需人工复核。</strong><p>任务已完成表示审查流程执行成功，不代表所有候选都已排除。</p></div>';
        const failedWorkers = list(report.collaboration?.worker_results).filter(w => w.status === 'failed');
        const modelFailures = asNumber(e.failed_model_calls) || 0;
        const toolFailures = asNumber(e.failed_tool_calls) || 0;
        if (!failedWorkers.length && !modelFailures && !toolFailures && report.collaboration?.coverage?.complete_professional_review !== false)
            return '';
        const incomplete = failedWorkers.length > 0 || report.collaboration?.coverage?.complete_professional_review === false;
        return `<div class="notice ${incomplete ? 'error' : 'warning'}" id="execution-warning"><strong>${incomplete ? '专业审查覆盖不完整，请核对执行过程。' : '执行过程中曾有调用失败，请核对重试和工具记录。'}</strong><p>失败 Worker ${failedWorkers.length} 个；失败模型调用 ${modelFailures} 次；失败工具调用 ${toolFailures} 次。历史调用失败与最终必要角色未完成不是同一件事。</p></div>`;
    }
    function renderPublication(task) {
        const status = task.publication_status || 'not_requested';
        if (!task.pull_request && !task.input?.publish_review)
            return '';
        const labels = {sent: 'GitHub 评论已发送', pending: 'GitHub 评论等待发送 / 发送中',
            failed: 'GitHub 评论发送失败', superseded: 'PR 已更新，旧版本报告未发送', not_requested: '本任务未安排 GitHub 评论'};
        const style = status === 'failed' ? 'error' : status === 'sent' ? '' : 'warning';
        const retryable = task.state === 'SUCCESS' && ['pending', 'failed'].includes(status) &&
            task.input?.publish_review && task.input?.github_issue_url && can('review');
        const repo = String(task.repository || '');
        const pr = Number(task.pull_request);
        const url = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) && Number.isSafeInteger(pr) && pr > 0
            ? `https://github.com/${repo}/pull/${pr}` : null;
        const error = String(task.publication_error || '');
        const denied = /Resource not accessible by personal access token/i.test(error);
        return `<div class="notice ${style} publication-panel" data-publication-status="${esc(status)}"><strong>${esc(labels[status] || status)}</strong><p>${status === 'superseded' ? '此报告针对先前的提交版本；请查看新版本对应的审查任务。' : '审查状态与评论状态分开记录；审查完成不等于报告已经发回 GitHub。'}</p>${error ? `<pre class="fix-code">${esc(error)}</pre>` : ''}${denied ? '<p>这次 GitHub 拒绝了 Token 的写权限。请核对当前服务使用的 Token 是否授权到此仓库，并具有 Pull requests 写权限或 Issues 写权限。修好授权后可只重试评论，不需要重新审查。</p>' : ''}${status === 'pending' ? '<p>页面会继续更新。若服务曾中断或长时间未完成，可点击下方重试；已有任务正在发布时，后端不会重复派发。</p>' : ''}<div class="publication-actions">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开 GitHub PR 的 Conversation ↗</a>` : ''}${retryable ? `<button type="button" class="button secondary small" data-retry-publication="${esc(task.id)}" ${state.publicationRetries.has(task.id) ? 'disabled' : ''}>${state.publicationRetries.has(task.id) ? '正在提交重试…' : '仅重试评论发布'}</button>` : ''}</div></div>`;
    }
    async function retryPublication(id) {
        const task = state.task;
        if (state.publicationRetries.has(id) || task?.id !== id || task.state !== 'SUCCESS')
            return;
        if (!(await confirmAction('仅重试 GitHub 评论？', '将向对应 PR 发送这份已完成的报告，不重新调用模型审查。权限错误需要先修正 Token；此操作不会更改代码或创建修复分支。', '重试评论')))
            return;
        if (state.selectedId !== id || state.task?.id !== id)
            return;
        const epoch = state.authEpoch;
        state.publicationRetries.add(id);
        renderTask(state.task);
        try {
            const result = await post(`/v1/tasks/${encodeURIComponent(id)}/resume`, {}, 30000);
            if (epoch !== state.authEpoch)
                return;
            toast(result.active ? '该任务仍在发布中，未重复派发' : '已提交评论发布重试，不重新审查');
            if (state.selectedId === id)
                await refreshSelected();
        }
        catch (error) {
            errorToast(error);
        }
        finally {
            if (epoch === state.authEpoch) {
                state.publicationRetries.delete(id);
                if (state.selectedId === id && state.task)
                    renderTask(state.task);
            }
        }
    }
    function renderTask(task, { initial = false } = {}) {
        const report = object(task.report);
        const e = object(report.execution);
        state.task = task;
        $('#task-report').textContent = json(task);
        $('#task-overview').innerHTML = `<div class="task-title-row"><h3>${esc(task.repository || '未命名仓库')}</h3>${statusBadge(task.state)}</div><div class="task-summary-meta"><span>${task.pull_request ? `PR #${esc(task.pull_request)}` : '手动 Diff 审查'}</span><span>·</span><span>${esc(time(task.created_at, true))}</span><span>·</span><span>${task.input?.source === 'github-webhook' ? 'GitHub Webhook' : esc(task.input?.source || 'API')}</span></div><div class="task-summary-meta"><code>${esc(task.id)}</code></div>${task.report ? `${executionWarning(report)}<p class="report-summary-text">${esc(report.summary || '')}</p><div class="mini-metrics"><div class="mini-metric"><small>最终结论</small><strong>${number(list(report.findings).length)}<span>项</span></strong></div><div class="mini-metric"><small>模型调用</small><strong>${number(e.llm_calls)}<span>次</span></strong></div><div class="mini-metric"><small>Token 总量</small><strong>${number(e.total_tokens)}</strong></div><div class="mini-metric"><small>审查耗时</small><strong>${esc(duration(e.duration_ms))}</strong></div></div><div class="model-tags"><span>实际模式 ${esc(report.run_mode?.effective || '未披露')}</span><span>风险等级 ${esc(severityLabels[report.risk] || report.risk || '未披露')}</span><span>审查器 ${esc(report.reviewer || '未披露')}</span></div>` : task.error ? `<div class="notice error">${esc(task.error)}</div>` : `<div class="notice warning">${activeStates.has(task.state) ? '任务进行中，将自动更新。正在执行的模型调用可能需要一段时间。' : task.state === 'CANCELLED' ? '任务已取消，当前没有完整审查报告。' : '任务未生成完整报告，请查看执行过程。'}</div>`}`;
        if (e.reviewed_revision?.head_sha)
            $('#task-overview').insertAdjacentHTML('beforeend', `<p class="code-label">已审查提交 <code>${esc(e.reviewed_revision.head_sha)}</code></p>`);
        $('#task-overview').insertAdjacentHTML('beforeend', renderPublication(task));
        $('#report-tabs').classList.remove('hidden');
        $('#copy-report').classList.remove('hidden');
        $('#download-report').classList.remove('hidden');
        $('#create-fix').classList.toggle('hidden', !(task.report && task.pull_request && can('fix')));
        renderFindings(task);
        renderExecution(task);
        setReportTab(state.tab);
        const feedbackReady = task.state === 'SUCCESS' && Boolean(task.report) && can('review');
        const previouslyReady = !$('#feedback-panel').classList.contains('hidden');
        $('#feedback-panel').classList.toggle('hidden', !feedbackReady);
        if (feedbackReady && (initial || !previouslyReady)) {
            renderFeedbackForm(task);
            loadTaskFeedback(task.id);
        }
        const cached = state.fixResults.get(task.id);
        if (cached)
            renderFixResult(cached);
        else
            $('#fix-result').classList.add('hidden');
        renderTasks();
    }
    async function openTask(id) {
        saveFeedbackDraft();
        state.selectedId = id;
        state.task = null;
        state.tab = 'findings';
        ++state.feedbackSeq;
        const seq = ++state.taskSeq;
        show('tasks', true, false);
        $('#task-overview').innerHTML = empty('正在读取任务', '加载报告与执行记录…', 'activity');
        $('#report-tabs').classList.add('hidden');
        ['findings', 'execution', 'json'].forEach(name => $(`#report-${name}`).classList.add('hidden'));
        ['feedback-panel', 'create-fix', 'copy-report', 'download-report', 'fix-result'].forEach(name => $(`#${name}`).classList.add('hidden'));
        renderTasks();
        loadTasks(true);
        try {
            const task = await api(`/v1/tasks/${encodeURIComponent(id)}`);
            if (seq !== state.taskSeq || state.selectedId !== id)
                return;
            renderTask(task, { initial: true });
            synced();
        }
        catch (error) {
            if (seq !== state.taskSeq || error.status === -1)
                return;
            $('#task-overview').innerHTML = empty('无法读取任务', error.message, 'alert');
            state.task = null;
            errorToast(error);
        }
    }
    async function refreshSelected() {
        const id = state.selectedId;
        if (!id)
            return true;
        const seq = state.taskSeq;
        try {
            const task = await api(`/v1/tasks/${encodeURIComponent(id)}`);
            if (seq !== state.taskSeq || state.selectedId !== id)
                return false;
            if (json(task) !== json(state.task))
                renderTask(task);
            synced();
            return true;
        }
        catch (error) {
            if (error.status === 404 && id === state.selectedId) {
                state.task = null;
                $('#task-overview').innerHTML = empty('此任务已不存在', '可能已被管理员清除。');
                ['feedback-panel', 'create-fix', 'copy-report', 'download-report', 'report-tabs', 'report-findings', 'report-execution', 'report-json', 'fix-result'].forEach(name => $(`#${name}`).classList.add('hidden'));
                state.selectedId = null;
            }
            return false;
        }
    }
    function toggleMissed() {
        $('#feedback-missed-fields').classList.toggle('hidden', $('#feedback-category').value !== 'missed_issue');
    }
    async function loadTaskFeedback(id) {
        const seq = ++state.feedbackSeq;
        try {
            const data = await api(`/v1/tasks/${encodeURIComponent(id)}/feedback`);
            if (id !== state.selectedId || seq !== state.feedbackSeq)
                return;
            const cases = list(data.cases);
            $('#task-feedback-history').innerHTML = cases.length ? `<p class="list-section-label">本任务反馈 · ${cases.length} 条</p>${cases.map(item => {
                const payload = object(item.payload);
                const f = object(payload.finding);
                return `<div class="feedback-case"><span class="feedback-case-type">${esc(feedbackLabels[item.category] || item.category)}</span><span class="feedback-case-copy"><b>${esc(f.rule_id ? `${f.rule_id}${f.path ? ` · ${f.path}:${f.line || '?'}` : ''}` : '未关联已有结论')}</b><small>${esc(payload.note || '无说明')}</small></span><span class="status ${item.resolved ? 'state-success' : 'state-pending'}">${item.resolved ? '已解决' : '待处理'}</span></div>`;
            }).join('')}` : '<p class="feedback-empty">暂无反馈。提交后会保存在这个任务下。</p>';
        }
        catch (error) {
            if (id === state.selectedId && seq === state.feedbackSeq && error.status !== -1)
                $('#task-feedback-history').innerHTML = `<p class="feedback-empty">无法读取反馈：${esc(error.message)}</p>`;
        }
    }
    function renderSkills() {
        const query = $('#skill-search').value.trim().toLowerCase();
        const kind = $('#skill-kind').value;
        const skills = state.skills.filter(s => (!kind || s.kind === kind) && `${s.name} ${s.description}`.toLowerCase().includes(query));
        $('#skill-count').textContent = String(skills.length);
        $('#skill-list').innerHTML = skills.length ? skills.map(s => {
            const scanner = s.kind === 'scanner';
            const glyph = scanner ? 'scan' : ({
                'security-review': 'shield', 'database-review': 'database', 'performance-review': 'activity', 'test-quality': 'check-circle', 'api-compatibility': 'git-branch', 'reliability-review': 'shield', 'observability-review': 'eye', 'correctness-review': 'code', 'code-quality': 'sparkles'
            }[s.name] || 'box');
            return `<article class="skill-card"><div class="skill-card-header"><span class="skill-icon ${scanner ? 'scanner' : ''}">${icon(glyph)}</span><span class="skill-label">${scanner ? 'REVIEW COMPONENT' : 'AGENT SKILL'}</span></div><h3>${esc(s.name)}</h3><p>${esc(s.description || '暂无能力描述')}</p><div class="skill-meta"><span>v${esc(s.version || '—')}</span><span title="${esc(s.source)}">${esc(s.source || '未披露来源')}</span></div><details><summary>权限与资源信息</summary><div class="skill-resource">${list(s.permissions).length ? list(s.permissions).map(p => `<span>${esc(p)}</span>`).join('') : '<span>未声明工具权限</span>'}</div>${list(s.resources).length ? `<p>资源：${esc(s.resources.join('、'))}</p>` : ''}<p>沙箱标记：${s.sandboxed ? '是' : '否'}（提示词 Skill 本身不等于执行脚本）</p></details></article>`;
        }).join('') : empty('没有匹配的能力', '清空筛选条件，或重新扫描 Skills。', 'sparkles');
    }
    async function loadSkills() {
        const seq = ++state.skillSeq;
        try {
            const data = await api('/api/skills');
            if (seq !== state.skillSeq)
                return false;
            state.skills = list(data.skills);
            renderRuntime(object(data.llm));
            renderSkills();
            synced();
            return true;
        }
        catch (error) {
            if (seq !== state.skillSeq || error.status === -1)
                return false;
            $('#skill-list').innerHTML = empty('无法读取能力库', error.message, 'alert');
            errorToast(error);
            return false;
        }
    }
    function renderEvolutionStatus(s) {
        $('#evolution-status').textContent = json(s);
        const insufficientCases = Number(s.validation_cases || 0) < Number(s.minimum_cases || 0)
            || Number(s.holdout_cases || 0) < Number(s.minimum_holdout_cases || 0);
        $('#evolution-ready').textContent = s.ready ? '可以评测' : insufficientCases ? '评测集不足' : '尚未就绪';
        $('#evolution-ready').className = `pill ${s.ready ? 'accent-soft' : 'neutral'}`;
        $('#evolution-metrics').innerHTML = `<div><small>验证集</small><strong>${number(s.validation_cases)} <span>/ 最少 ${number(s.minimum_cases)}</span></strong><p>Validation · 总计 ${number(s.validation_total_cases ?? s.validation_cases)}，本轮 ${number(s.validation_selected_cases ?? s.validation_cases)}，未选 ${number(s.validation_omitted_cases ?? 0)}</p></div><div><small>保留集</small><strong>${number(s.holdout_cases)} <span>/ 最少 ${number(s.minimum_holdout_cases)}</span></strong><p>Holdout · 总计 ${number(s.holdout_total_cases ?? s.holdout_cases)}，本轮 ${number(s.holdout_selected_cases ?? s.holdout_cases)}，未选 ${number(s.holdout_omitted_cases ?? 0)}</p></div><div><small>最小提升要求</small><strong>${asNumber(s.minimum_improvement) === null ? '—' : (Number(s.minimum_improvement) * 100).toFixed(1)}<span>百分点</span></strong><p>绝对分数差门槛</p></div><div><small>评测模型</small><strong style="font-size:15px">${s.model_configured ? '已配置' : '未配置'}</strong><p>${esc(s.model || s.provider || '请先配置模型')}</p></div>`;
    }
    async function loadEvolution() {
        const seq = ++state.evolutionSeq;
        try {
            const [status, runs, failures] = await Promise.all([api('/v1/evolution/status'), api('/v1/evolution/runs?limit=5'), can('audit') ? api('/api/failures?unresolved_only=true') : Promise.resolve(null)]);
            if (seq !== state.evolutionSeq)
                return false;
            renderEvolutionStatus(status);
            const cases = list(failures?.cases);
            const runsList = list(runs.runs);
            $('#failure-list').innerHTML = (failures === null ? '<div class="permission-note">当前角色无审计权限，不能查看跨任务失败反馈。</div>' : cases.length ? `<p class="list-section-label">未解决案例 · 显示最近 ${Math.min(8, cases.length)} 条</p>${cases.slice(0, 8).map(item => `<button class="task-row" data-task="${esc(item.task_id)}"><span class="task-main"><span class="task-glyph">${icon('message')}</span><span class="task-copy"><span class="task-name">${esc(feedbackLabels[item.category] || item.category)}</span><span class="task-meta">${esc(item.payload?.note || item.payload?.error || item.task_id)}</span></span></span><span class="status state-pending">待处理</span></button>`).join('')}` : empty('暂无待处理反馈', '反馈记录会成为后续候选评测的输入。', 'message')) + `<p class="list-section-label">最近评测</p>` + (runsList.length ? runsList.map(run => `<div class="evolution-run"><div class="evolution-run-top"><strong>${esc(run.skill_name || 'llm-review')} · v${esc(run.candidate_version)}</strong><span class="pill ${run.decision === 'activated' ? 'accent-soft' : 'neutral'}">${esc(decisionLabels[run.decision] || run.decision)}</span></div><p>${esc(time(run.created_at, true))}</p><div class="run-scores"><span>候选分数<strong>${asNumber(run.candidate_score) === null ? '—' : Number(run.candidate_score).toFixed(3)}</strong></span><span>基线分数<strong>${asNumber(run.baseline_score) === null ? '—' : Number(run.baseline_score).toFixed(3)}</strong></span></div><details class="raw-details"><summary>评测原始记录</summary><pre class="json-code" tabindex="0">${esc(json(run))}</pre></details>${readyPromptAction(run)}</div>`).join('') : '<p class="feedback-empty">尚无评测记录。</p>');
            bindTasks($('#failure-list'));
            synced();
            return true;
        }
        catch (error) {
            if (seq !== state.evolutionSeq || error.status === -1)
                return false;
            $('#evolution-status').textContent = error.message;
            $('#evolution-ready').textContent = '读取失败';
            $('#failure-list').innerHTML = empty('暂时无法读取演进数据', error.message, 'alert');
            errorToast(error);
            return false;
        }
    }
    function readyPromptAction(run) {
        return can('manage') && run.decision === 'ready' && (run.skill_name || run.version?.skill_name) === 'llm-review'
            ? `<button class="button secondary small" data-activate-prompt="${esc(run.candidate_version || run.version?.version)}">复测并激活候选</button>` : '';
    }
    async function activatePrompt(event) {
        const button = event.target.closest('[data-activate-prompt]');
        if (!button || button.disabled) return;
        const version = Number(button.dataset.activatePrompt);
        if (!Number.isSafeInteger(version) || version < 1) return;
        if (!(await confirmAction('激活此 Prompt？', '后端会再次核对配置、数据和版本，并运行复测；只有通过后才替换 llm-review。', '复测并激活'))) return;
        busy(button, true, '复测中…');
        try {
            await post(`/v1/skills/llm-review/versions/${version}/activate`, {}, 1800000);
            await loadEvolution();
            $('#evolution-result').textContent = '候选已激活；新的任务使用更新后的 Prompt。';
            toast('候选已激活');
        } catch (error) { errorToast(error); }
        finally { busy(button, false); }
    }
    function renderEvolutionResult(data) {
        const decision = data.decision || 'unknown';
        const experimentalPassed = decision === 'experimental_passed';
        const reason = experimentalPassed ? 'Prompt 候选已通过实验评测并保存，不改变线上运行配置。' : (data.reason || (decision === 'activated' ? '后端已通过门禁并激活版本。' : '请查看原始评测结果和门禁判定。'));
        const evaluationReason = experimentalPassed && data.reason ? `<p>${esc(data.reason)}</p>` : '';
        $('#evolution-result').classList.remove('empty');
        $('#evolution-result').innerHTML = `<div class="notice ${decision === 'activated' || experimentalPassed ? '' : 'warning'}"><strong>${esc(decisionLabels[decision] || decision)}</strong><p>${esc(reason)}</p>${evaluationReason}${readyPromptAction(data)}</div><details class="raw-details" open><summary>完整评测结果</summary><pre class="json-code" tabindex="0">${esc(json(data))}</pre></details>`;
    }
    function safeLink(value) {
        try {
            const url = new URL(value);
            return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
        }
        catch {
            return null;
        }
    }
    function renderFixResult(data) {
        const root = $('#fix-result');
        root.classList.remove('hidden');
        const created = Boolean(data.branch);
        const title = created ? '后端已创建修复分支' : data.status === 'blocked' ? '修复被验证门禁拦截' : '仅生成修复建议，未创建分支';
        const url = safeLink(data.draft_pull_request?.url);
        root.innerHTML = `<div class="notice ${created ? '' : 'warning'}" style="margin-top:20px"><strong>${esc(title)}</strong><p>${esc(data.note || data.reason || data.status || '')}</p>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看 GitHub 草稿 PR ↗</a>` : ''}</div><details class="raw-details"><summary>查看完整修复响应</summary><pre class="json-code" tabindex="0">${esc(json(data))}</pre></details>`;
    }
    async function copyText(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
                return;
            }
            const area = document.createElement('textarea');
            area.value = text;
            area.style.position = 'fixed';
            area.style.left = '-10000px';
            document.body.append(area);
            area.select();
            const ok = document.execCommand('copy');
            area.remove();
            if (!ok)
                throw new Error('请使用导出按钮保存报告');
        }
        catch (error) {
            throw new Error(`复制失败：${error.message}`);
        }
    }
    function downloadJson(value, filename) {
        const blob = new Blob([json(value)], { type: 'application/json;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.style.display = 'none';
        document.body.append(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    async function refreshView(view = state.view) {
        if (state.login)
            return false;
        switch (view) {
            case 'tasks': {
                const results = await Promise.all([loadTasks(), refreshSelected()]);
                return results.every(Boolean);
            }
            case 'skills': return loadSkills();
            case 'evolution': return loadEvolution();
            default: return loadDashboard();
        }
    }
    function setTheme(theme) {
        const actual = theme === 'dark' ? 'dark' : 'light';
        document.documentElement.dataset.theme = actual;
        storage.set('evoagent_theme', actual);
        $('#theme-toggle').innerHTML = icon(actual === 'dark' ? 'sun' : 'moon');
        $('#theme-toggle').setAttribute('aria-label', actual === 'dark' ? '切换浅色外观' : '切换深色外观');
        $('#theme-toggle').title = $('#theme-toggle').getAttribute('aria-label');
        $('meta[name="theme-color"]').content = actual === 'dark' ? '#111d23' : '#f4f7f8';
    }
    // Bind existing operations; each mutation captures its task ID before awaiting.
    decorate();
    syncMenuAccessibility();
    window.matchMedia('(max-width:780px)').addEventListener('change', () => {
        closeMenu();
    });
    setTheme(storage.get('evoagent_theme') || 'light');
    updateIdentity();
    $$('.nav-item').forEach(button => button.addEventListener('click', () => show(button.dataset.view)));
    $$('[data-jump]').forEach(button => button.addEventListener('click', () => show(button.dataset.jump)));
    window.addEventListener('hashchange', () => show(location.hash.slice(1), false));
    $('#theme-toggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
    $('#menu-toggle').addEventListener('click', () => {
        const open = !$('#sidebar').classList.contains('open');
        $('#sidebar').classList.toggle('open', open);
        $('#sidebar-scrim').hidden = !open;
        $('#sidebar-scrim').classList.toggle('open', open);
        $('#menu-toggle').setAttribute('aria-expanded', String(open));
        syncMenuAccessibility();
        if (open)
            $('.nav-item.active').focus();
    });
    $('#sidebar-scrim').addEventListener('click', closeMenu);
    $('#confirm-cancel').addEventListener('click', () => finishConfirm(false));
    $('#confirm-ok').addEventListener('click', () => finishConfirm(true));
    $('#confirm-dialog').addEventListener('cancel', event => {
        event.preventDefault();
        finishConfirm(false);
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            const wasOpen = $('#sidebar').classList.contains('open');
            closeMenu();
            if (wasOpen)
                $('#menu-toggle').focus();
        }
        if (event.key === 'Tab' && !state.login && $('#sidebar').classList.contains('open')) {
            const items = $$('a[href],button:not(:disabled)', $('#sidebar'));
            const first = items[0], last = items.at(-1);
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            }
            else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        }
        if (event.key === 'Tab' && state.login) {
            const focusable = $$('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href]', $('#login-overlay'));
            const first = focusable[0], last = focusable.at(-1);
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            }
            else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        }
    });
    $('#refresh').addEventListener('click', async () => {
        const b = $('#refresh');
        b.disabled = true;
        b.setAttribute('aria-busy', 'true');
        try {
            if (await refreshView())
                toast('当前数据已刷新');
        }
        finally {
            b.disabled = false;
            b.removeAttribute('aria-busy');
        }
    });
    $('#task-search').addEventListener('input', renderTasks);
    $('#task-state-filter').addEventListener('change', renderTasks);
    $('#task-limit').addEventListener('change', () => loadTasks());
    $('#skill-search').addEventListener('input', renderSkills);
    $('#skill-kind').addEventListener('change', renderSkills);
    $$('[data-report-tab]').forEach(button => button.addEventListener('click', () => setReportTab(button.dataset.reportTab)));
    $('#report-tabs').addEventListener('keydown', event => {
        const tabs = $$('[data-report-tab]');
        const index = tabs.indexOf(document.activeElement);
        if (index < 0)
            return;
        let next = index;
        if (event.key === 'ArrowRight')
            next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft')
            next = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === 'Home')
            next = 0;
        else if (event.key === 'End')
            next = tabs.length - 1;
        else
            return;
        event.preventDefault();
        setReportTab(tabs[next].dataset.reportTab, true);
    });
    async function copyTaskJson() {
        if (!state.task)
            return;
        try {
            await copyText(json(state.task));
            toast('完整任务 JSON 已复制');
        }
        catch (error) {
            toast(error.message, true);
        }
    }
    $('#copy-report').addEventListener('click', copyTaskJson);
    $('#copy-json').addEventListener('click', copyTaskJson);
    $('#download-report').addEventListener('click', () => {
        if (state.task)
            downloadJson(state.task, `evoagent-task-${state.task.id}.json`);
    });
    function updateDiffStats() {
        const value = $('#diff-input').value;
        $('#diff-stats').textContent = `${value ? value.split(/\r?\n/).length : 0} 行 · ${value.length} 字符 · ${new TextEncoder().encode(value).length} B`;
    }
    $('#diff-input').addEventListener('input', updateDiffStats);
    updateDiffStats();
    $('#sample-diff').addEventListener('click', async () => {
        if ($('#diff-input').value.trim() && !(await confirmAction('替换当前 Diff？', '载入演示内容将覆盖尚未提交的 Diff。仓库与 PR 编号不会被改动。', '载入示例')))
            return;
        $('#diff-input').value = 'diff --git a/stats.py b/stats.py\nnew file mode 100644\n--- /dev/null\n+++ b/stats.py\n@@ -0,0 +1,3 @@\n+def average(values):\n+    """Return the average; return 0.0 when values is empty."""\n+    return sum(values) / len(values)\n';
        const repo = $('[name=repository]', $('#review-form'));
        if (!repo.value)
            repo.value = 'demo/test';
        updateDiffStats();
        toast('已载入空列表除零示例，尚未提交');
    });
    $('#review-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget, b = $('button[type=submit]', form);
        if (b.disabled)
            return;
        const values = new FormData(form);
        const body = { repository: String(values.get('repository') || '').trim(), diff: String(values.get('diff') || ''), mode: 'agentic' };
        if (!body.repository || !body.diff.trim()) {
            toast('请填写仓库标识和有效 Diff。', true);
            return;
        }
        if (values.get('pull_request')) {
            body.pull_request = Number(values.get('pull_request'));
            if (!Number.isSafeInteger(body.pull_request) || body.pull_request < 1) {
                toast('PR 编号必须是正整数。', true);
                return;
            }
        }
        const asyncMode = Boolean(values.get('async'));
        const output = $('#review-result');
        output.classList.remove('empty');
        output.textContent = asyncMode ? '正在提交到审查队列…' : '正在等待完整审查。模型调用可能需要几分钟…';
        busy(b, true, asyncMode ? '正在提交…' : '正在审查…');
        try {
            const data = await post(`/v1/reviews${asyncMode ? '?async=true' : ''}`, body, asyncMode ? 60000 : 900000);
            output.innerHTML = `<div class="result-title">${icon('check-circle')}${asyncMode ? '已提交审查任务' : '审查响应已返回'}</div><div>${esc(data.task_id || '未返回任务编号')}</div><div>${esc(stateLabels[data.state] || data.state || '')}</div>${data.task_id ? '<button class="button secondary small" id="open-submitted-task">查看任务详情 ↗</button>' : ''}<details class="raw-details"><summary>完整提交响应</summary><pre class="json-code" tabindex="0">${esc(json(data))}</pre></details>`;
            if (data.task_id)
                $('#open-submitted-task').addEventListener('click', () => openTask(data.task_id));
            toast(asyncMode ? '任务已提交，可在任务中心跟踪' : '审查响应已返回');
            await loadDashboard(true);
        }
        catch (error) {
            if (error.status !== -1)
                output.textContent = error.message;
        }
        finally {
            busy(b, false);
            if (state.dashboard)
                renderRuntime(object(state.dashboard.llm));
        }
    });
    $('#feedback-category').addEventListener('change', toggleMissed);
    $('#feedback-form').addEventListener('input', saveFeedbackDraft);
    $('#feedback-form').addEventListener('change', saveFeedbackDraft);
    $('#feedback-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!state.selectedId || !state.task?.report)
            return;
        const form = event.currentTarget, b = $('button[type=submit]', form);
        if (b.disabled)
            return;
        const id = state.selectedId, task = state.task, values = new FormData(form), category = String(values.get('category'));
        const index = String(values.get('finding_index') || '');
        const findings = list(task.report.findings);
        const finding = index === '' ? {} : { ...object(findings[Number(index)]) };
        const note = String(values.get('note') || '').trim();
        if (!note) {
            toast('请填写非空的判断依据。', true);
            return;
        }
        if (category === 'missed_issue') {
            const rule = String(values.get('rule_id') || '').trim(), path = String(values.get('path') || '').trim(), lineValue = values.get('line');
            if (rule)
                finding.rule_id = rule;
            if (path)
                finding.path = path;
            if (lineValue) {
                const line = Number(lineValue);
                if (!Number.isSafeInteger(line) || line <= 0) {
                    toast('行号必须是正整数。', true);
                    return;
                }
                finding.line = line;
            }
        }
        busy(b, true, '正在保存…');
        $('#feedback-result').textContent = '正在保存反馈…';
        try {
            const data = await post(`/v1/tasks/${encodeURIComponent(id)}/feedback`, { category, finding: Object.keys(finding).length ? finding : null, note }, 60000);
            state.feedbackDrafts.delete(id);
            if (state.selectedId === id) {
                form.reset();
                toggleMissed();
                $('#feedback-result').textContent = `${feedbackLabels[data.category] || data.category}已记录。它不会立即改变线上审查。`;
                await loadTaskFeedback(id);
            }
            await loadDashboard(true);
            toast('反馈已保存');
        }
        catch (error) {
            if (id === state.selectedId && error.status !== -1)
                $('#feedback-result').textContent = `提交失败：${error.message}`;
        }
        finally {
            busy(b, false);
        }
    });
    $('#task-overview').addEventListener('click', event => {
        const button = event.target.closest('[data-retry-publication]');
        if (button && !button.disabled)
            retryPublication(button.dataset.retryPublication);
    });
    $('#create-fix').addEventListener('click', async () => {
        const id = state.selectedId;
        if (!id)
            return;
        if (!(await confirmAction('请求生成并验证修复？', '该操作可能调用模型，并在验证通过且授权允许时写入 GitHub 新分支 / 草稿 PR。仅收到建议不代表分支已创建。', '继续生成修复')))
            return;
        if (state.selectedId !== id) {
            toast('选中任务已经变化，请重新确认。', true);
            return;
        }
        const b = $('#create-fix');
        busy(b, true, '验证修复中…');
        try {
            const data = await post(`/v1/tasks/${encodeURIComponent(id)}/fix`, {}, 900000);
            state.fixResults.set(id, data);
            if (state.selectedId === id)
                renderFixResult(data);
            toast(data.branch ? '修复分支已创建，请复核草稿 PR' : data.status === 'blocked' ? '修复被门禁拦截，未创建分支' : '返回修复建议，未创建分支');
        }
        catch (error) {
            errorToast(error);
        }
        finally {
            busy(b, false);
        }
    });
    $('#reload-skills').addEventListener('click', async () => {
        const b = $('#reload-skills');
        busy(b, true, '扫描中…');
        try {
            await post('/v1/skills/reload', {}, 60000);
            await loadSkills();
            toast('Skills 已重新加载');
        }
        catch (error) {
            errorToast(error);
        }
        finally {
            busy(b, false);
        }
    });
    $('#evolution-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget, b = $('button[type=submit]', form);
        if (b.disabled)
            return;
        const values = new FormData(form);
        const skillName = String(values.get('skill_name') || '').trim(), prompt = String(values.get('prompt') || '').trim();
        if (!skillName || !prompt) {
            toast('请填写 Skill 名称和候选提示词。', true);
            return;
        }
        if (!(await confirmAction('开始候选回放评测？', '将实际调用模型比较候选与基线。llm-review 通过验证集、保留集和复测后会自动激活，影响后续审查；失败保留当前版本。其他命名空间只做离线实验。', '开始评测')))
            return;
        busy(b, true, '评测中…');
        try {
            const data = await post('/v1/evolution/propose', { skill_name: skillName, prompt }, 1800000);
            renderEvolutionResult(data);
            await loadEvolution();
            toast(`评测完成：${decisionLabels[data.decision] || data.decision}`);
        }
        catch (error) {
            $('#evolution-result').classList.remove('empty');
            $('#evolution-result').textContent = error.message;
            errorToast(error);
        }
        finally {
            busy(b, false);
        }
    });
    $('#rollback-prompt').addEventListener('click', async () => {
        if (!(await confirmAction('回滚当前 Prompt？', '将复测并恢复当前 llm-review 激活版本保存的基线。不会删除历史记录，也不会改变正在执行的任务。', '复测并回滚')))
            return;
        const button = $('#rollback-prompt');
        busy(button, true, '复测中…');
        try {
            await post('/v1/evolution/rollback', { skill_name: 'llm-review' }, 1800000);
            await loadEvolution();
            toast('已恢复上一个 Prompt 基线');
        } catch (error) { errorToast(error); }
        finally { busy(button, false); }
    });
    $('#failure-list').addEventListener('click', activatePrompt);
    $('#evolution-result').addEventListener('click', activatePrompt);
    $('#auto-evolve').addEventListener('click', async () => {
        if (!(await confirmAction('从反馈生成候选？', '将读取已保存反馈、调用模型生成候选，并进行回放评测。llm-review 候选经过正式多 Agent 回放和复测后，满足门槛才会激活；未通过不会改变当前配置。', '生成并评测')))
            return;
        const b = $('#auto-evolve');
        busy(b, true, '生成与评测中…');
        try {
            const data = await post('/v1/evolution/auto', { skill_name: 'llm-review' }, 1800000);
            renderEvolutionResult(data);
            await loadEvolution();
            toast(`候选处理完成：${decisionLabels[data.decision] || data.decision}`);
        }
        catch (error) {
            $('#evolution-result').classList.remove('empty');
            $('#evolution-result').textContent = error.message;
            errorToast(error);
        }
        finally {
            busy(b, false);
        }
    });
    $('#toggle-password').addEventListener('click', () => {
        const input = $('[name=password]', $('#login-form'));
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        $('#toggle-password').innerHTML = icon(show ? 'eye-off' : 'eye');
        $('#toggle-password').setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    });
    $('#login-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget, b = $('button[type=submit]', form);
        const values = new FormData(form);
        busy(b, true, '正在登录…');
        try {
            const data = await post('/v1/auth/login', { username: String(values.get('username') || '').trim(), password: String(values.get('password') || ''), tenant_id: String(values.get('tenant_id') || '').trim() }, 30000);
            state.authEpoch++;
            state.token = data.access_token;
            storage.set('evoagent_token', state.token);
            $('[name=password]', form).value = '';
            $('[name=password]', form).type = 'password';
            $('#toggle-password').innerHTML = icon('eye');
            $('#toggle-password').setAttribute('aria-label', '显示密码');
            hideLogin();
            const ok = await loadDashboard();
            if (ok && state.view !== 'overview')
                await refreshView();
        }
        catch (error) {
            $('#login-error').textContent = error.message;
        }
        finally {
            busy(b, false);
        }
    });
    function clearSession() {
        state.authEpoch++;
        state.token = '';
        storage.remove('evoagent_token');
        state.task = null;
        state.selectedId = null;
        state.tasks = [];
        state.skills = [];
        state.dashboard = null;
        state.feedbackDrafts.clear();
        state.fixResults.clear();
        state.publicationRetries.clear();
        state.taskSeq++;
        $('#task-overview').innerHTML = empty('选择一个任务', '登录后查看任务详情。');
        $('#task-report').textContent = '请选择一个任务。';
        ['report-findings', 'report-execution', 'task-feedback-history', 'review-result', 'evolution-result', 'failure-list', 'evolution-metrics', 'skill-list', 'recent-tasks', 'all-tasks'].forEach(id => {
            const el = $(`#${id}`);
            if (el)
                el.textContent = '';
        });
        ['feedback-panel', 'create-fix', 'copy-report', 'download-report', 'report-tabs', 'report-findings', 'report-execution', 'report-json', 'fix-result'].forEach(id => $(`#${id}`).classList.add('hidden'));
        $('#review-form').reset();
        $('#feedback-form').reset();
        $('#evolution-form').reset();
        $('#stats').innerHTML = '';
        $('#evolution-status').textContent = '等待登录';
        updateDiffStats();
        updateIdentity();
    }
    $('#logout').addEventListener('click', () => {
        clearSession();
        showLogin();
    });
    // Read-only refresh. Never retry POSTs automatically, and never poll a hidden page.
    const pollTimer = setInterval(async () => {
        if (document.hidden || state.login || state.polling || !state.dashboard)
            return;
        state.polling = true;
        try {
            if (state.view === 'tasks') {
                await loadTasks(true);
                if (state.selectedId && needsTaskRefresh(state.task))
                    await refreshSelected();
            }
            else if (state.view === 'overview') {
                await loadDashboard(true);
            }
        }
        finally {
            state.polling = false;
        }
    }, 12000);
    window.addEventListener('pagehide', () => clearInterval(pollTimer), { once: true });
    show(location.hash.slice(1) || 'overview', false, false);
    loadDashboard().then(ok => {
        if (ok && state.view !== 'overview')
            refreshView();
    });
})();
