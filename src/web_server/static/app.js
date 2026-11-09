/* 本地资源 · 网页端
 *
 * 单文件、无依赖：手机/平板上打开就能用，不打包、不请求外网。
 *
 * 三条设计约束：
 * 1. 文件名可能含 < > & 等字符 —— 一律用 textContent 拼 DOM，绝不用 innerHTML
 *    塞数据（CSP 也只允许同源脚本）。
 * 2. 播放交给原生 <audio>/<video>：iOS/Android 的手势、锁屏、画中画都靠它，
 *    自己写控件在手机上一定是坑。
 * 3. 路由用 hash，安卓返回键/浏览器后退能逐级退出，不会一下就关掉页面。
 */
(() => {
  'use strict';

  // ---------------- 图标（常量 HTML，仅用于 innerHTML） ----------------
  const ICON = {
    play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.4v13.2L19 12z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="5" width="3.4" height="14" rx="1.2"/><rect x="13.6" y="5" width="3.4" height="14" rx="1.2"/></svg>',
    prev: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5h2v14H7zM19 5.4v13.2L9.6 12z"/></svg>',
    next: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M15 5h2v14h-2zM5 5.4v13.2L14.4 12z"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    photo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="15" rx="3"/><circle cx="8.6" cy="9.6" r="1.6"/><path d="M4 17.2l5-4.4 4 3.4 3-2.4 4 3.4"/></svg>',
    audio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M9 17.6V6.4l10-2v11.2"/><circle cx="6.6" cy="17.8" r="2.6"/><circle cx="16.6" cy="15.8" r="2.6"/></svg>',
    video: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10.6 9.3l4.8 2.7-4.8 2.7z"/></svg>',
    chevron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg>',
    left: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>',
    right: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg>',
    full: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V5.6A1.6 1.6 0 0 1 5.6 4H9M15 4h3.4A1.6 1.6 0 0 1 20 5.6V9M20 15v3.4A1.6 1.6 0 0 1 18.4 20H15M9 20H5.6A1.6 1.6 0 0 1 4 18.4V15"/></svg>',
  };

  const $ = (id) => document.getElementById(id);
  const view = $('view');
  const audioEl = $('audio');
  const videoEl = $('video');

  // ---------------- DOM 工具 ----------------
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value; // 仅限常量图标
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'style') Object.assign(node.style, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat(4)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function fmtDuration(ms) {
    const total = Math.floor((Number(ms) || 0) / 1000);
    if (!total) return '--:--';
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function fmtClock(sec) {
    if (!Number.isFinite(sec) || sec < 0) return '00:00';
    return fmtDuration(sec * 1000);
  }

  function fmtSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(0)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  /** 只在内容真的变了才写 DOM
   *
   * 播放时 timeupdate 每秒来 4 次，每次都改 textContent / range.value 会让
   * 老平板（Tegra K1 那级）反复重排进度条，纯属白烧性能。
   */
  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function toast(text, ms = 2200) {
    const box = $('toast');
    box.textContent = text;
    box.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => box.classList.remove('show'), ms);
  }

  // ---------------- 请求 ----------------
  async function api(path, options) {
    // 这里绝不能写成对象展开 `...(options || {})`：那是 ES2018 语法，
    // Android 7 自带的老 WebView（Chrome 51~59）会直接**解析失败**，
    // 整份 app.js 一句都不执行，页面上只剩顶栏文字、按钮全不出来。
    const init = { credentials: 'same-origin' };
    if (options) Object.assign(init, options);
    const response = await fetch(path, init);
    if (response.status === 401) {
      // 令牌失效（电脑上重新生成过）：回根路径让服务端给登录页
      location.replace('/');
      throw new Error('需要重新登录');
    }
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try {
        const data = await response.json();
        if (data && data.error) message = data.error;
      } catch (e) {
        /* 非 JSON 错误体：用默认文案 */
      }
      throw new Error(message);
    }
    return response.json();
  }

  // ---------------- 状态 ----------------
  const state = {
    search: '',
    summary: null,
    token: 0,
    // 当前列表页的「重绘」（搜索用）与「滚到底续页」
    repaint: null,
    autoLoad: null,
    audio: { queue: [], index: -1, playlist: null, cues: [], cuePos: -1 },
    video: { workId: null, videos: [], index: 0, info: null, remux: false, resume: 0, savedAt: 0 },
    // source 可以是 pagedGrid（带 items/total/loadMore），也可以是普通数组
    lightbox: { source: null, index: 0, orig: '', swipe: null },
  };

  // ---------------- 通用组件 ----------------
  function coverBox(src, extraClass, placeholder) {
    const wrap = el('div', { class: `cover ${extraClass || ''}`.trim() });
    wrap.append(el('div', { class: 'ph', text: placeholder || '·' }));
    if (src) {
      const img = el('img', { src, alt: '', loading: 'lazy', decoding: 'async' });
      img.addEventListener('load', () => img.classList.add('ready'));
      img.addEventListener('error', () => img.remove());
      wrap.append(img);
    }
    return wrap;
  }

  function emptyState(title, lines) {
    return el(
      'div',
      { class: 'state' },
      el('strong', { text: title }),
      ...(lines || []).map((line) => el('div', { text: line })),
    );
  }

  function loadingState() {
    return el(
      'div',
      { class: 'skeleton-row' },
      ...Array.from({ length: 5 }, () => el('div', { class: 'skeleton' })),
    );
  }

  function sectionTitle(text, extra) {
    return el('div', { class: 'section-title' }, el('strong', { text }), extra ? el('span', { text: extra }) : null);
  }

  // ---------------- 顶栏 / 标签 ----------------
  const TAB_LABEL = { photo: '写真', audio: '音频', video: '视频' };

  function setHeader({ tab, crumb, searchable, back }) {
    document.querySelectorAll('#tabs button').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    $('tabs').hidden = !tab;
    $('brand-title').textContent = tab ? TAB_LABEL[tab] || '本地资源' : '本地资源';
    $('crumb').textContent = crumb ? ` / ${crumb}` : '';

    // 详情页的返回目标是显式算出来的，不依赖 history：从二维码直接打开某一页时
    // history 里没有上一级，history.back() 会把人弹出站点
    const backBtn = $('back-btn');
    backBtn.hidden = !back;
    backBtn.dataset.target = back || '';
    backBtn.innerHTML = back ? ICON.left : '';

    const wrap = $('search-wrap');
    const toggle = $('search-toggle');
    toggle.hidden = !searchable;
    if (!searchable) {
      wrap.hidden = true;
      state.search = '';
      $('search-input').value = '';
    }
  }

  $('back-btn').addEventListener('click', () => {
    const target = $('back-btn').dataset.target;
    if (target) location.hash = target;
  });

  // 各模块上一次停留的列表页（带作者筛选时返回也能回到筛选结果）
  const lastListHash = { photo: '#/photo', audio: '#/audio', video: '#/video' };

  $('tabs').addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-tab]');
    if (!btn) return;
    state.search = '';
    $('search-input').value = '';
    location.hash = `#/${btn.dataset.tab}`;
  });

  $('brand-title').addEventListener('click', () => {
    location.hash = '#/';
  });

  $('search-toggle').addEventListener('click', () => {
    const wrap = $('search-wrap');
    wrap.hidden = !wrap.hidden;
    if (!wrap.hidden) {
      $('search-input').focus();
      setSearchIcon(true);
    } else {
      $('search-input').value = '';
      state.search = '';
      setSearchIcon(false);
      if (state.repaint) state.repaint();
    }
  });

  function setSearchIcon(open) {
    $('search-toggle').textContent = open ? '取消' : '搜索';
  }

  let searchTimer = null;
  $('search-input').addEventListener('input', (event) => {
    // 老 WebView 的输入法组合事件偶发 target 不是 input，value 会取到 undefined
    const raw = event && event.target ? event.target.value : '';
    const value = String(raw === undefined || raw === null ? '' : raw).trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = value;
      if (state.repaint) state.repaint();
    }, 220);
  });

  // ---------------- 路由 ----------------
  function parseHash() {
    const raw = (location.hash || '#/').replace(/^#/, '');
    const [pathPart, queryPart] = raw.split('?');
    const parts = pathPart.split('/').filter(Boolean);
    return { parts, query: new URLSearchParams(queryPart || '') };
  }

  window.addEventListener('hashchange', () => {
    render();
  });

  async function render() {
    const token = ++state.token;
    const { parts, query } = parseHash();
    state.repaint = null;
    state.autoLoad = null;

    try {
      if (parts.length === 0) {
        setHeader({ tab: '', crumb: '', searchable: false });
        await viewHome(token);
      } else if (parts[0] === 'photo' && parts[1] === 'w') {
        setHeader({ tab: 'photo', crumb: '作品', searchable: false, back: lastListHash.photo });
        await viewPhotoWork(Number(parts[2]), token);
      } else if (parts[0] === 'photo') {
        lastListHash.photo = location.hash || '#/photo';
        setHeader({ tab: 'photo', crumb: '', searchable: true });
        await viewPhotoWorks(query, token);
      } else if (parts[0] === 'audio' && parts[1] === 'p') {
        setHeader({ tab: 'audio', crumb: '歌单', searchable: false, back: lastListHash.audio });
        await viewAudioPlaylist(Number(parts[2]), token);
      } else if (parts[0] === 'audio') {
        lastListHash.audio = '#/audio';
        setHeader({ tab: 'audio', crumb: '', searchable: false });
        await viewAudioRoot(token);
      } else if (parts[0] === 'video' && parts[1] === 'w') {
        setHeader({ tab: 'video', crumb: '作品', searchable: false, back: lastListHash.video });
        await viewVideoWork(Number(parts[2]), token, Number(parts[3]) || 0);
      } else if (parts[0] === 'video') {
        lastListHash.video = location.hash || '#/video';
        setHeader({ tab: 'video', crumb: '', searchable: true });
        await viewVideoWorks(query, token);
      } else {
        location.hash = '#/';
        return;
      }
      window.scrollTo(0, 0);
    } catch (error) {
      if (token !== state.token) return;
      view.replaceChildren(emptyState('加载失败', [error.message, '请确认电脑上的服务仍在运行']));
    }
  }

  // ---------------- 首页 ----------------
  async function viewHome(token) {
    view.replaceChildren(loadingState());
    const summary = await api('/api/summary');
    if (token !== state.token) return;
    state.summary = summary;

    const cards = [
      {
        tab: 'photo',
        icon: ICON.photo,
        title: '写真',
        desc: summary.photo.works
          ? `${summary.photo.works} 个作品 · ${summary.photo.images} 张图`
          : '还没有内容',
        ready: summary.photo.works > 0,
      },
      {
        tab: 'audio',
        icon: ICON.audio,
        title: '音频',
        desc: summary.audio.tracks
          ? `${summary.audio.playlists} 个歌单 · ${summary.audio.tracks} 首`
          : '还没有内容',
        ready: summary.audio.tracks > 0,
      },
      {
        tab: 'video',
        icon: ICON.video,
        title: '视频',
        desc: summary.video.videos
          ? `${summary.video.works} 个作品 · ${summary.video.videos} 个视频`
          : '还没有内容',
        ready: summary.video.videos > 0,
      },
    ];

    view.replaceChildren(
      el(
        'div',
        { class: 'home-cards' },
        ...cards.map((card) =>
          el(
            'button',
            { class: 'home-card', type: 'button', onclick: () => { location.hash = `#/${card.tab}`; } },
            el('span', { class: 'home-icon', html: card.icon }),
            el(
              'span',
              { class: 'home-text' },
              el('h2', { text: card.title }),
              el('p', { text: card.desc }),
            ),
            el('span', { class: 'home-go', html: ICON.chevron }),
          ),
        ),
      ),
      el('div', { class: 'section-title', style: { marginTop: '22px' } },
        el('span', { text: `本地资源管理器 v${summary.version} · 仅在同一 Wi-Fi 下可用` })),
    );
  }

  // ---------------- 分页作品墙（写真/视频/音频/作品内图片共用） ----------------
  /* 切换模块时只拉第一页（服务端分页），滚到接近底部自动续，
   * 点「加载更多」也能手动续（自动加载被关或请求失败时的兜底）。
   * 原来一次拉全量：几千个作品瞬间建几千个卡片节点 + 同时请求全部封面，
   * 手机上切个标签要卡好几秒，这就是「切类型很卡」的根因。
   */
  function pagedGrid(options) {
    const titleEl = sectionTitle('加载中…');
    const grid = el('div', { class: 'grid' });
    const more = el('button', { class: 'more', type: 'button', text: '加载更多', hidden: true });
    const node = el('div', null, titleEl, grid, more);
    let page = 0;
    let pages = 1;
    let total = 0;
    let version = 0;
    let busy = false;
    let items = [];

    more.addEventListener('click', () => { loadMore(); });

    function fetchPage(next) {
      const params = new URLSearchParams();
      Object.keys(options.params || {}).forEach((key) => {
        if (options.params[key]) params.set(key, String(options.params[key]));
      });
      if (state.search) params.set('q', state.search);
      params.set('page', String(next));
      if (options.pageSize) params.set('page_size', String(options.pageSize));
      return api(`${options.endpoint}?${params.toString()}`);
    }

    function showEmpty() {
      grid.replaceChildren(
        emptyState(
          state.search && options.emptySearchTitle ? options.emptySearchTitle : options.emptyTitle,
          state.search && options.emptySearchTitle ? ['换个关键词试试'] : options.emptyHints,
        ),
      );
    }

    function applyTitle() {
      titleEl.replaceChildren(
        el('strong', { text: total ? `${options.unit(total)}，已显示 ${items.length}` : '' }),
      );
    }

    function updateMore() {
      if (page < pages) {
        more.textContent = `已显示 ${items.length} / ${total}，加载更多`;
        more.disabled = false;
        more.hidden = false;
      } else {
        more.hidden = true;
      }
    }

    async function reload() {
      const v = ++version;
      // busy 时不直接 return：搜索防抖会在上一页还在路上时再触发 reload，
      // 两个请求并发，靠 version 让旧响应被丢弃，最后一次的结果生效
      busy = true;
      page = 0;
      pages = 1;
      total = 0;
      items = [];
      grid.replaceChildren(loadingState());
      more.hidden = true;
      try {
        const data = await fetchPage(1);
        if (v !== version) return;
        page = data.page || 1;
        pages = data.pages || 1;
        total = data.total || 0;
        if (options.onFirst) options.onFirst(data);
        if (!data[options.listKey].length) {
          grid.replaceChildren();
          showEmpty();
          titleEl.replaceChildren(el('strong', { text: '' }));
          return;
        }
        items = data[options.listKey];
        grid.replaceChildren(...items.map((item, index) => options.renderCard(item, index)));
        applyTitle();
        updateMore();
      } finally {
        busy = false;
      }
      // 平板横屏一屏就能装下一页时，不补一页会看到「到底了却还有更多」
      maybeAutoLoad();
    }

    async function loadMore() {
      if (busy || page >= pages) return null;
      const v = version;
      busy = true;
      more.disabled = true;
      more.textContent = '加载中…';
      try {
        const data = await fetchPage(page + 1);
        // 拉取期间切换了搜索/筛选：这页数据作废
        if (v !== version || data.page !== page + 1) return null;
        page = data.page;
        pages = data.pages;
        total = data.total;
        const added = data[options.listKey];
        const base = items.length;
        items = items.concat(added);
        added.forEach((item, index) => grid.append(options.renderCard(item, base + index)));
        applyTitle();
        updateMore();
        return added;
      } catch (error) {
        more.textContent = `加载失败：${error.message}`;
        more.disabled = false;
        return null;
      } finally {
        busy = false;
      }
    }

    function maybeAutoLoad() {
      if (busy || more.hidden || page >= pages) return;
      // 距底部还有一屏多就预取，滚到底时数据已经到了，不会看到空白
      const rect = more.getBoundingClientRect();
      if (rect.top > window.innerHeight + 600) return;
      loadMore();
    }

    return {
      node,
      reload,
      loadMore,
      maybeAutoLoad,
      items: () => items,
      total: () => total,
    };
  }

  // 自动续页只挂一个全局滚动监听：每个页面各挂一个的话，来回切换会越积越多
  window.addEventListener('scroll', () => {
    if (state.autoLoad) state.autoLoad();
  }, { passive: true });

  // ---------------- 写真 ----------------
  async function viewPhotoWorks(query, token) {
    const authorId = query.get('author') || '';
    const wall = pagedGrid({
      endpoint: '/api/photos/works',
      listKey: 'works',
      unit: (n) => `${n} 个作品`,
      params: authorId ? { author_id: authorId } : {},
      emptyTitle: '写真库还是空的',
      emptySearchTitle: '没有匹配的作品',
      emptyHints: ['在电脑上打开「写真」模块并扫描一次'],
      renderCard: (work) =>
        el(
          'button',
          { class: 'card', type: 'button', onclick: () => { location.hash = `#/photo/w/${work.id}`; } },
          (() => {
            const box = coverBox(work.cover, '', (work.name || '·').slice(0, 1));
            box.append(el('span', { class: 'badge', text: `${work.image_count} 张` }));
            return box;
          })(),
          el(
            'div',
            { class: 'meta' },
            el('div', { class: 'title', text: work.name }),
            el('div', { class: 'sub', text: work.author || '' }),
          ),
        ),
    });

    view.replaceChildren(loadingState());
    const authors = await api('/api/photos/authors');
    if (token !== state.token) return;

    const chips = el(
      'div',
      { class: 'chips' },
      el(
        'button',
        {
          class: `chip ${authorId ? '' : 'active'}`.trim(),
          type: 'button',
          onclick: () => { location.hash = '#/photo'; },
        },
        '全部',
        el('span', { class: 'count', text: String(authors.authors.reduce((sum, a) => sum + a.work_count, 0)) }),
      ),
      ...authors.authors.map((author) =>
        el(
          'button',
          {
            class: `chip ${String(author.id) === authorId ? 'active' : ''}`.trim(),
            type: 'button',
            onclick: () => { location.hash = `#/photo?author=${author.id}`; },
          },
          author.name,
          el('span', { class: 'count', text: String(author.work_count) }),
        ),
      ),
    );

    view.replaceChildren(chips, wall.node);
    state.repaint = wall.reload;
    state.autoLoad = wall.maybeAutoLoad;
    await wall.reload();
  }

  async function viewPhotoWork(workId, token) {
    view.replaceChildren(loadingState());

    // 作品内图片也走服务端分页：最大的一册 2025 张，全量渲染要建一万多个节点，
    // 老平板上打开就是好几秒白屏。看图浮层翻到已加载末尾时会自动续页。
    const wall = pagedGrid({
      endpoint: `/api/photos/works/${workId}/images`,
      listKey: 'images',
      unit: (n) => `${n} 张`,
      pageSize: 120,
      emptyTitle: '这个作品里没有图片',
      onFirst: (data) => {
        // 书名号用作品名，省一次单独的请求
        const name = data.work ? data.work.name : '';
        $('crumb').textContent = name ? ` / ${name}` : '';
      },
      renderCard: (image, index) =>
        el(
          'button',
          {
            class: 'card',
            type: 'button',
            // 传的是「整册里第几张」而不是「本页第几张」，
            // 否则浮层计数会从 1 重新开始、翻页也会翻错位置
            onclick: () => openLightbox(wall, index),
          },
          coverBox(image.thumb, '', String(index + 1)),
        ),
    });

    view.replaceChildren(wall.node);
    state.autoLoad = wall.maybeAutoLoad;
    await wall.reload();
  }

  // ---------------- 看图 ----------------
  const lb = $('lightbox');
  const lbOrig = $('lb-orig');
  const lbStage = $('lb-stage');
  /* 两层轮换：一层显示当前图，另一层接下一张。翻页时「一张推走、一张推进来」，
   * 只有单张 <img> 换 src 是做不出来的（没有第二张图可以移动）。
   * 缩放写在 <img> 的 transform 上，翻页位移写在 .lb-layer 上，两套互不打架。 */
  const lbLayers = [$('lb-layer-a'), $('lb-layer-b')];
  const lbImgs = [$('lb-img'), $('lb-img-b')];
  const FLIP_MS = 260;
  let lbFront = 0;          // 当前显示的是哪一层
  let lbFlipTimer = null;
  // 系统开了「减弱动态效果」就只换图不做位移
  const LB_REDUCED = Boolean(
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );

  function lbImgNow() { return lbImgs[lbFront]; }

  /** 当前可翻的图片列表：分页后是「已加载的那些」，翻到末尾会自动续页 */
  function lightboxItems() {
    const source = state.lightbox.source;
    if (!source) return [];
    if (typeof source.items === 'function') return source.items();
    return source;
  }

  function lightboxTotal() {
    const source = state.lightbox.source;
    const total = source && typeof source.total === 'function' ? source.total() : 0;
    return total || lightboxItems().length;
  }

  function openLightbox(source, index) {
    const items = typeof source.items === 'function' ? source.items() : source;
    state.lightbox.source = source;
    state.lightbox.index = Math.max(0, Math.min(index, items.length - 1));
    state.lightbox.swipe = null;
    lb.hidden = false;
    document.body.style.overflow = 'hidden';
    settleFlip();
    showLightboxImage({ dir: 0 });
  }

  function resetOrigButton() {
    lbOrig.textContent = '原图';
    lbOrig.disabled = false;
    lbOrig.hidden = false;
  }

  /** 把某张图挂到某一层：先上 640 缩图（网格里多半已缓存），1600 主图解码完再替换 */
  function paintLayer(imgEl, image) {
    imgEl.__image = image;          // 这层现在画的是哪张，回填主图时用它认人
    delete imgEl.dataset.full;
    imgEl.classList.add('loading');
    imgEl.src = image.thumb;
    loadFull(imgEl, image);
  }

  /* 主图能不能贴、贴到哪一层，只认「这一层自己的图」：
   * 用全局 index 判断会漏——翻页动画还没提交索引时，主图就已经解码完了。 */
  function loadFull(imgEl, image) {
    const big = new Image();
    big.onload = () => {
      if (imgEl.__image !== image) return;      // 这层换人了，别贴旧图
      if (!imgEl.getAttribute('src')) return;   // 已被 settleFlip 清空，说明这层暂时不用
      imgEl.src = image.full;
      imgEl.dataset.full = image.full;
      imgEl.classList.remove('loading');
    };
    big.src = image.full;
  }

  /** 当前层还停在缩图上就把主图补上（提交索引后调用；浏览器有缓存，重发不走网络） */
  function ensureFull() {
    const imgEl = lbImgNow();
    const image = imgEl.__image;
    if (!image || !imgEl.getAttribute('src')) return;
    if (imgEl.dataset.full === image.full) return;
    loadFull(imgEl, image);
  }

  /** 藏起非当前层：清位移、清旧图，否则它盖在当前图上面（DOM 里它在后面，天然在上） */
  function hideBackLayer() {
    const back = lbFront ^ 1;
    lbLayers[back].classList.remove('flipping');
    lbLayers[back].style.transition = '';
    lbLayers[back].style.transform = '';
    lbLayers[back].style.opacity = '0';
    lbImgs[back].removeAttribute('src');
  }

  /** 收尾一段翻页动画，把两层恢复到「只有当前层可见」的静止状态 */
  function settleFlip() {
    if (lbFlipTimer) {
      clearTimeout(lbFlipTimer);
      lbFlipTimer = null;
    }
    state.lightbox.swipe = null;
    hideBackLayer();
    const front = lbLayers[lbFront];
    front.classList.remove('flipping');
    front.style.transition = '';
    front.style.transform = '';
    front.style.opacity = '1';
    // 动画期间到货的主图可能因为索引还没提交被丢掉了，这里补一次
    ensureFull();
  }

  /** 计数、左右箭头、相邻两张预热——翻页和跟手拖动都要走这里 */
  function updateLightboxChrome() {
    const images = lightboxItems();
    const index = state.lightbox.index;
    state.lightbox.orig = '';
    resetOrigButton();
    setText($('lb-count'), `${index + 1} / ${lightboxTotal()}`);
    $('lb-prev').style.opacity = index > 0 ? '1' : '0.2';
    $('lb-next').style.opacity = index < images.length - 1 || hasMorePages() ? '1' : '0.2';
    // 预热相邻两张的主图，翻过去不用等
    [index - 1, index + 1].forEach((i) => {
      if (images[i]) {
        const preload = new Image();
        preload.src = images[i].full;
      }
    });
  }

  /**
   * 换图。options.dir：0 = 直接显示（刚打开 / 减弱动画），
   * 1 = 下一张（从右边推进来），-1 = 上一张（从左边推进来）。
   */
  function showLightboxImage(options) {
    const images = lightboxItems();
    const index = state.lightbox.index;
    const image = images[index];
    if (!image) return;
    const dir = (options && options.dir) || 0;

    if (dir && !LB_REDUCED) {
      flipTo(image, dir);
    } else {
      settleFlip();
      // 换图要把缩放复位，否则上一张的放大倍数会带到下一张上
      resetZoom(false);
      paintLayer(lbImgNow(), image);
    }
    updateLightboxChrome();
  }

  /** 翻页动画：当前层往 dir 的反方向推出去并淡出，另一层从 dir 方向推进来 */
  function flipTo(image, dir) {
    settleFlip();          // 上一段动画还在跑就先收尾，免得两层抢同一个位移
    const outLayer = lbLayers[lbFront];
    const inLayer = lbLayers[lbFront ^ 1];
    const shift = flipShift();

    resetZoom(false);
    paintLayer(lbImgs[lbFront ^ 1], image);

    // 先把来图摆到侧面（改初始位置时必须关掉 transition，否则会被合成一次瞬移），
    // 强制重排之后再打开过渡
    inLayer.classList.remove('flipping');
    inLayer.style.transition = 'none';
    inLayer.style.transform = `translate3d(${dir * shift}px, 0, 0)`;
    inLayer.style.opacity = '0';
    outLayer.style.transition = 'none';
    void inLayer.offsetWidth;

    inLayer.style.transition = '';
    outLayer.style.transition = '';
    inLayer.classList.add('flipping');
    outLayer.classList.add('flipping');
    inLayer.style.transform = 'translate3d(0, 0, 0)';
    inLayer.style.opacity = '1';
    outLayer.style.transform = `translate3d(${-dir * shift}px, 0, 0)`;
    outLayer.style.opacity = '0';

    // 立刻切到新层：动画期间再翻一次走的是另一层，两层不会互相踩
    lbFront ^= 1;
    clearTimeout(lbFlipTimer);
    lbFlipTimer = setTimeout(settleFlip, FLIP_MS + 30);
  }

  function hasMorePages() {
    const source = state.lightbox.source;
    return Boolean(source && typeof source.loadMore === 'function');
  }

  // 原图按需加载：只在用户真的想看 100% 细节时才拉几十 MB
  lbOrig.addEventListener('click', () => {
    const image = lightboxItems()[state.lightbox.index];
    if (!image || !image.orig || state.lightbox.orig === image.orig) return;
    lbOrig.disabled = true;
    lbOrig.textContent = '加载中…';
    const big = new Image();
    big.onload = () => {
      if (lightboxItems()[state.lightbox.index] !== image) return;
      state.lightbox.orig = image.orig;
      lbImgNow().src = image.orig;
      lbOrig.textContent = '已是原图';
    };
    big.onerror = () => {
      lbOrig.disabled = false;
      lbOrig.textContent = '原图加载失败';
    };
    big.src = image.orig;
  });

  lbImgs.forEach((imgEl) => {
    imgEl.addEventListener('load', () => imgEl.classList.remove('loading'));
  });

  async function stepLightbox(delta) {
    const next = state.lightbox.index + delta;
    if (next < 0) return;
    if (next >= lightboxItems().length) {
      // 已加载的看到头了：服务端还有更多就先续一页，否则最后一张永远翻不过去
      const source = state.lightbox.source;
      if (!source || typeof source.loadMore !== 'function') return;
      await source.loadMore();
      if (next >= lightboxItems().length) return;
    }
    state.lightbox.index = next;
    showLightboxImage({ dir: delta > 0 ? 1 : -1 });
  }

  function closeLightbox() {
    lb.hidden = true;
    settleFlip();
    lbImgs.forEach((imgEl) => imgEl.removeAttribute('src'));
    resetZoom(false);
    document.body.style.overflow = '';
  }

  // ---------------- 看图缩放 ----------------
  /* 双指捏合 / 双击放大都在这里自己算。
   * 不能交给浏览器：整页缩放会把顶栏一起放大，而且固定定位的浮层里
   * 缩放后平移范围算错，手指划两下就找不回图了。 */
  const lbZoom = { scale: 1, x: 0, y: 0 };
  const ZOOM_MAX = 5;
  const ZOOM_STEP = 2.5;

  function applyZoom(animate) {
    const img = lbImgNow();
    img.style.transform =
      `translate3d(${lbZoom.x}px, ${lbZoom.y}px, 0) scale(${lbZoom.scale})`;
    lbImgs.forEach((el) => el.classList.remove('zooming'));
    if (animate) {
      img.classList.add('zooming');
      clearTimeout(applyZoom._timer);
      applyZoom._timer = setTimeout(() => img.classList.remove('zooming'), 220);
    } else {
      clearTimeout(applyZoom._timer);
    }
  }

  function resetZoom(animate) {
    lbZoom.scale = 1;
    lbZoom.x = 0;
    lbZoom.y = 0;
    applyZoom(animate);
  }

  function clampZoom() {
    // 只允许在「放大出来的那块」里平移，否则图能被拖出屏幕再也找不回来
    const img = lbImgNow();
    const maxX = Math.max(0, (img.offsetWidth * (lbZoom.scale - 1)) / 2);
    const maxY = Math.max(0, (img.offsetHeight * (lbZoom.scale - 1)) / 2);
    lbZoom.x = Math.max(-maxX, Math.min(maxX, lbZoom.x));
    lbZoom.y = Math.max(-maxY, Math.min(maxY, lbZoom.y));
  }

  /** 以屏幕坐标 (cx, cy) 为锚点缩到 scale：那一点尽量待在原地 */
  function zoomAt(cx, cy, scale) {
    const rect = lbStage.getBoundingClientRect();
    const dx = cx - (rect.left + rect.width / 2);
    const dy = cy - (rect.top + rect.height / 2);
    const ratio = scale / lbZoom.scale;
    lbZoom.x = dx - (dx - lbZoom.x) * ratio;
    lbZoom.y = dy - (dy - lbZoom.y) * ratio;
    lbZoom.scale = scale;
    clampZoom();
    applyZoom(true);
  }

  let lbGesture = null;
  let lbLastTap = 0;
  let lbTapTimer = null;
  let lbTouchSeen = false;

  function touchDistance(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.sqrt(dx * dx + dy * dy) || 1;
  }

  // ---------------- 翻页手势：手指拖到哪儿，图就走到哪儿 ----------------
  /* 只有未放大（scale≈1）时才是翻页；放大后的单指一律是平移。
   * 松手按拖动距离决定「翻过去」还是「弹回来」——纯按钮式翻页在手感上差很多。 */
  function stageWidth() {
    return lbStage.clientWidth || window.innerWidth;
  }

  /** 来图从多远开始滑：按「当前图的实际宽度 + 24px 缝」，不是整整一屏。
   *  横屏看竖图时图比屏幕窄很多，按一屏算会拖出一大片空白（实测踩到）。 */
  function flipShift() {
    const width = stageWidth();
    const imgWidth = lbImgNow().offsetWidth || 0;
    return Math.min(width, Math.round(Math.max(imgWidth, width * 0.45) + 24));
  }

  /** 备好侧边那张（下一张/上一张）；没有可翻的图就返回 false */
  function beginSwipe(dir) {
    const image = lightboxItems()[state.lightbox.index + dir];
    if (!image) return false;   // 到头了（或服务端还有但还没拉下来）→ 松手走续页逻辑
    settleFlip();
    const outLayer = lbLayers[lbFront];
    const inLayer = lbLayers[lbFront ^ 1];
    const shift = flipShift();
    paintLayer(lbImgs[lbFront ^ 1], image);
    inLayer.classList.remove('flipping');
    outLayer.classList.remove('flipping');
    // 跟手阶段不能有过渡，否则图会慢半拍
    inLayer.style.transition = 'none';
    outLayer.style.transition = 'none';
    inLayer.style.opacity = '1';
    outLayer.style.opacity = '1';
    inLayer.style.transform = `translate3d(${dir * shift}px, 0, 0)`;
    state.lightbox.swipe = { dir, image, shift };
    return true;
  }

  function moveSwipe(dx) {
    const swipe = state.lightbox.swipe;
    if (!swipe) return;
    lbLayers[lbFront].style.transform = `translate3d(${dx}px, 0, 0)`;
    // 来图和当前图同速移动，看起来就是被推着走
    lbLayers[lbFront ^ 1].style.transform =
      `translate3d(${swipe.dir * swipe.shift + dx}px, 0, 0)`;
  }

  /** 松手：拖够 1/5 屏（最多 96px）就翻过去，否则弹回 */
  function endSwipe(dx) {
    const swipe = state.lightbox.swipe;
    if (!swipe) return;
    state.lightbox.swipe = null;
    const width = stageWidth();
    const outLayer = lbLayers[lbFront];
    const inLayer = lbLayers[lbFront ^ 1];
    const passed = Math.abs(dx) > Math.min(width * 0.2, 96);

    inLayer.classList.add('flipping');
    outLayer.classList.add('flipping');
    inLayer.style.transition = '';
    outLayer.style.transition = '';

    if (!passed) {
      // 没拖够：两张都回原位，索引不动
      inLayer.style.transform = `translate3d(${swipe.dir * swipe.shift}px, 0, 0)`;
      outLayer.style.transform = 'translate3d(0, 0, 0)';
      lbFlipTimer = setTimeout(settleFlip, FLIP_MS + 30);
      return;
    }

    inLayer.style.transform = 'translate3d(0, 0, 0)';
    outLayer.style.transform = `translate3d(${-swipe.dir * width}px, 0, 0)`;
    outLayer.style.opacity = '0';
    // 提交：当前层和索引一起前进，计数/箭头/预热走同一条更新
    lbFront ^= 1;
    state.lightbox.index += swipe.dir;
    updateLightboxChrome();
    lbFlipTimer = setTimeout(settleFlip, FLIP_MS + 30);
  }

  lbStage.addEventListener('touchstart', (event) => {
    lbTouchSeen = true;
    if (event.touches.length >= 2) {
      clearTimeout(lbTapTimer);
      // 拖到一半又按上第二根手指：先让那张弹回去，别留在半路
      endSwipe(0);
      lbGesture = {
        type: 'pinch',
        dist: touchDistance(event.touches),
        scale: lbZoom.scale,
      };
      return;
    }
    const touch = event.touches[0];
    lbGesture = {
      type: lbZoom.scale > 1.02 ? 'pan' : 'tap',
      x: touch.clientX,
      y: touch.clientY,
      baseX: lbZoom.x,
      baseY: lbZoom.y,
      at: Date.now(),
      swiping: false,
    };
  }, { passive: true });

  lbStage.addEventListener('touchmove', (event) => {
    if (!lbGesture) return;
    if (event.touches.length >= 2) {
      const touches = event.touches;
      if (lbGesture.type !== 'pinch') {
        lbGesture = { type: 'pinch', dist: touchDistance(touches), scale: lbZoom.scale };
      }
      const scale = Math.max(
        1,
        Math.min(ZOOM_MAX, lbGesture.scale * (touchDistance(touches) / lbGesture.dist)),
      );
      if (Math.abs(scale - lbZoom.scale) > 0.001) {
        zoomAt(
          (touches[0].clientX + touches[1].clientX) / 2,
          (touches[0].clientY + touches[1].clientY) / 2,
          scale,
        );
      }
      return;
    }
    const touch = event.touches[0];
    if (lbGesture.type === 'pan') {
      lbZoom.x = lbGesture.baseX + (touch.clientX - lbGesture.x);
      lbZoom.y = lbGesture.baseY + (touch.clientY - lbGesture.y);
      clampZoom();
      applyZoom(false);
      return;
    }
    if (lbGesture.type !== 'tap') return;

    const dx = touch.clientX - lbGesture.x;
    const dy = touch.clientY - lbGesture.y;
    if (!lbGesture.swiping) {
      if (Math.abs(dy) > 16 && Math.abs(dy) >= Math.abs(dx)) {
        // 竖向移动：既不是翻页，也不该当轻点（否则松手会误翻一张）
        lbGesture.type = 'idle';
        return;
      }
      if (Math.abs(dx) < 10 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
      // 侧边还拿不到下一张（到末尾了）：先不跟手，松手后由 stepLightbox 续页再翻
      if (!beginSwipe(dx < 0 ? 1 : -1)) return;
      lbGesture.swiping = true;
      lbGesture.dir = dx < 0 ? 1 : -1;
    }
    moveSwipe(dx);
  }, { passive: true });

  lbStage.addEventListener('touchend', (event) => {
    const gesture = lbGesture;
    if (event.touches.length > 0) return;  // 还有手指按着，等最后一根抬起
    lbGesture = null;
    if (!gesture) return;

    if (gesture.type === 'pinch') {
      // 缩得比原图还小就弹回原大小，顺手把位移归零
      if (lbZoom.scale <= 1.02) resetZoom(true);
      else clampZoom();
      return;
    }

    const touch = event.changedTouches[0];
    const tapX = touch.clientX;
    const dx = tapX - gesture.x;
    const dy = touch.clientY - gesture.y;

    // 跟手翻页松手：按拖动距离决定翻过去还是弹回
    if (gesture.swiping) {
      endSwipe(dx);
      return;
    }

    const moved = Math.abs(dx) > 12 || Math.abs(dy) > 12;
    const held = Date.now() - gesture.at > 700;

    if (moved || held) {
      // 未放大时的明显横滑 → 翻页；放大状态下单指怎么划都是平移，不翻页
      if (gesture.type === 'tap' && Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.4) {
        stepLightbox(dx < 0 ? 1 : -1);
      } else if (gesture.type !== 'idle') {
        clampZoom();
      }
      return;
    }

    // 轻点：放大状态下也要能识别双击，否则放大后没法再缩回去
    //（起步时按 scale 分了 tap/pan 两种手势，这里只认「没怎么动」）
    const now = Date.now();
    if (now - lbLastTap < 300) {
      lbLastTap = 0;
      clearTimeout(lbTapTimer);
      if (lbZoom.scale > 1.02) resetZoom(true);
      else zoomAt(tapX, touch.clientY, ZOOM_STEP);
      return;
    }
    lbLastTap = now;
    clearTimeout(lbTapTimer);
    if (lbZoom.scale > 1.02) return;   // 放大状态下单击不翻页
    // 单击等 260ms 再翻页：不等的话双击会先翻走一张
    lbTapTimer = setTimeout(() => {
      stepLightbox(tapX < window.innerWidth / 2 ? -1 : 1);
    }, 260);
  }, { passive: true });

  $('lb-prev').addEventListener('click', () => stepLightbox(-1));
  $('lb-next').addEventListener('click', () => stepLightbox(1));
  $('lb-close').addEventListener('click', closeLightbox);
  $('lb-prev').innerHTML = ICON.left;
  $('lb-next').innerHTML = ICON.right;
  $('lb-close').innerHTML = ICON.close;
  $('v-close').innerHTML = ICON.close;

  // 鼠标：点左/右半区翻页。触摸设备已经在上面的 touchend 里处理过了，
  // 这里再处理会变成「一次点击翻两张」。
  lbStage.addEventListener('click', (event) => {
    if (lbTouchSeen) return;
    if (event.target.closest('.lb-nav')) return;
    stepLightbox(event.clientX < window.innerWidth / 2 ? -1 : 1);
  });

  // 桌面端键盘：← → 翻页，Esc 关闭（比去找箭头快）
  document.addEventListener('keydown', (event) => {
    if (lb.hidden) return;
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      stepLightbox(-1);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      stepLightbox(1);
    } else if (event.key === 'Escape') {
      closeLightbox();
    }
  });

  // ---------------- 音频 ----------------
  async function viewAudioRoot(token) {
    const wall = pagedGrid({
      endpoint: '/api/audio/playlists',
      listKey: 'playlists',
      unit: (n) => `${n} 个歌单`,
      params: {},
      emptyTitle: '音频库还是空的',
      emptyHints: ['在电脑上打开「音频」模块并扫描一次', '歌单按目录层级自动生成'],
      renderCard: (playlist) =>
        el(
          'button',
          {
            class: 'card',
            type: 'button',
            onclick: () => { location.hash = `#/audio/p/${playlist.id}`; },
          },
          coverBox(playlist.has_cover ? playlist.cover : '', 'square', '♪'),
          el(
            'div',
            { class: 'meta' },
            el('div', { class: 'title', text: playlist.name }),
            el('div', {
              class: 'sub',
              text: [
                playlist.track_count ? `${playlist.track_count} 首` : '',
                playlist.child_count ? `${playlist.child_count} 个子集` : '',
              ].filter(Boolean).join(' · ') || '空歌单',
            }),
          ),
        ),
    });

    view.replaceChildren(wall.node);
    state.repaint = wall.reload;
    state.autoLoad = wall.maybeAutoLoad;
    await wall.reload();
  }

  async function viewAudioPlaylist(playlistId, token) {
    view.replaceChildren(loadingState());
    const data = await api(`/api/audio/playlists/${playlistId}`);
    if (token !== state.token) return;

    $('crumb').textContent = ` / ${data.playlist.name}`;
    // 子歌单的返回目标是「父歌单」，不是模块首页——一层层退回去才符合直觉
    $('back-btn').dataset.target = data.playlist.parent_id
      ? `#/audio/p/${data.playlist.parent_id}`
      : lastListHash.audio;

    const nodes = [];
    if (data.playlist.has_cover || data.playlist.tags) {
      nodes.push(
        el(
          'div',
          { class: 'section-title', style: { alignItems: 'center', marginBottom: '16px' } },
          data.playlist.has_cover
            ? el('img', {
                src: data.playlist.cover,
                alt: '',
                style: { width: '58px', height: '58px', borderRadius: '12px', objectFit: 'cover' },
              })
            : null,
          el(
            'span',
            { style: { minWidth: '0' } },
            el('strong', { text: data.playlist.name, style: { display: 'block', fontSize: '15px' } }),
            el('span', { text: data.playlist.tags || '' }),
          ),
        ),
      );
    }

    if (data.children.length) {
      nodes.push(sectionTitle('子歌单'));
      nodes.push(
        el(
          'div',
          { class: 'grid', style: { marginBottom: '18px' } },
          ...data.children.map((child) =>
            el(
              'button',
              {
                class: 'card',
                type: 'button',
                onclick: () => { location.hash = `#/audio/p/${child.id}`; },
              },
              coverBox(child.has_cover ? child.cover : '', 'square', '♪'),
              el(
                'div',
                { class: 'meta' },
                el('div', { class: 'title', text: child.name }),
                el('div', {
                  class: 'sub',
                  text: [
                    child.track_count ? `${child.track_count} 首` : '',
                    child.child_count ? `${child.child_count} 个子集` : '',
                  ].filter(Boolean).join(' · ') || '空歌单',
                }),
              ),
            ),
          ),
        ),
      );
    }

    if (data.tracks.length) {
      nodes.push(sectionTitle(`${data.tracks.length} 首`, '点任意一首开始播放'));
      nodes.push(
        el(
          'div',
          { class: 'list' },
          ...data.tracks.map((track) =>
            el(
              'button',
              {
                class: 'row',
                type: 'button',
                dataset: { trackId: String(track.id) },
                onclick: () => playTrack(data.tracks, track.index, data.playlist),
              },
              el('span', { class: 'idx', text: String(track.index + 1).padStart(2, '0') }),
              // 右侧已经有时长，行内不再重复一遍（副标题留给「正在播放」之类的状态）
              el('span', { class: 'body' },
                el('span', { class: 'name', text: track.title }),
              ),
              el('span', { class: 'dur', text: fmtDuration(track.duration_ms) }),
            ),
          ),
        ),
      );
    }

    if (!nodes.length) {
      view.replaceChildren(emptyState('这个歌单是空的'));
      return;
    }
    view.replaceChildren(...nodes);
  }

  function markPlayingRow(trackId) {
    document.querySelectorAll('.row.playing').forEach((row) => row.classList.remove('playing'));
    if (!trackId) return;
    const row = document.querySelector(`.row[data-track-id="${trackId}"]`);
    if (row) row.classList.add('playing');
  }

  async function playTrack(queue, index, playlist) {
    const track = queue[index];
    if (!track) return;
    const state_ = state.audio;
    state_.queue = queue;
    state_.index = index;
    state_.playlist = playlist || state_.playlist;

    audioEl.src = track.url;
    audioEl.play().catch((error) => {
      toast(`播放失败：${error.message}`);
    });

    markPlayingRow(track.id);
    $('player').hidden = false;
    view.classList.add('with-player');
    $('p-title').textContent = track.title;
    $('p-sub').textContent = [state_.playlist ? state_.playlist.name : '', `${index + 1}/${queue.length}`]
      .filter(Boolean).join(' · ');
    const hasCover = Boolean(state_.playlist && state_.playlist.has_cover);
    if (hasCover) $('p-cover').src = state_.playlist.cover;
    else $('p-cover').removeAttribute('src');
    $('p-cover').style.visibility = hasCover ? 'visible' : 'hidden';
    $('p-play').innerHTML = ICON.pause;

    updateMediaSession(track);
    loadSubtitle(track.id);
  }

  function updateMediaSession(track) {
    if (!('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: track.title,
        artist: state.audio.playlist ? state.audio.playlist.name : '本地资源',
      });
    } catch (e) {
      /* 旧浏览器不支持 MediaMetadata，忽略 */
    }
  }

  if ('mediaSession' in navigator) {
    const handlers = {
      play: () => audioEl.play(),
      pause: () => audioEl.pause(),
      previoustrack: () => stepTrack(-1),
      nexttrack: () => stepTrack(1),
    };
    Object.entries(handlers).forEach(([name, fn]) => {
      try {
        navigator.mediaSession.setActionHandler(name, fn);
      } catch (e) {
        /* 不支持的 action 直接跳过 */
      }
    });
  }

  function stepTrack(delta) {
    const next = state.audio.index + delta;
    if (next < 0 || next >= state.audio.queue.length) {
      toast(delta > 0 ? '已经是最后一首' : '已经是第一首');
      return;
    }
    playTrack(state.audio.queue, next, state.audio.playlist);
  }

  audioEl.addEventListener('play', () => { $('p-play').innerHTML = ICON.pause; });
  audioEl.addEventListener('pause', () => { $('p-play').innerHTML = ICON.play; });

  audioEl.addEventListener('loadedmetadata', () => {
    $('p-dur').textContent = fmtClock(audioEl.duration);
  });

  audioEl.addEventListener('timeupdate', () => {
    const duration = audioEl.duration || 0;
    const seek = $('p-seek');
    if (duration > 0 && !seek.dataset.dragging) {
      const value = String(Math.round((audioEl.currentTime / duration) * 1000));
      if (seek.value !== value) seek.value = value;
    }
    setText($('p-pos'), fmtClock(audioEl.currentTime));
    syncSubtitle(audioEl.currentTime * 1000);
  });

  audioEl.addEventListener('ended', () => {
    if (state.audio.index + 1 < state.audio.queue.length) stepTrack(1);
    else {
      $('p-play').innerHTML = ICON.play;
      clearSubtitle();
    }
  });

  audioEl.addEventListener('error', () => {
    if (!audioEl.src) return;
    toast('这首播不了（格式不支持或文件已移走）', 3200);
  });

  $('p-play').addEventListener('click', () => {
    if (!audioEl.src) return;
    if (audioEl.paused) audioEl.play().catch(() => {});
    else audioEl.pause();
  });
  $('p-prev').addEventListener('click', () => stepTrack(-1));
  $('p-next').addEventListener('click', () => stepTrack(1));

  const seek = $('p-seek');
  const startDrag = () => { seek.dataset.dragging = '1'; };
  const endDrag = () => {
    delete seek.dataset.dragging;
    const duration = audioEl.duration || 0;
    if (duration > 0) audioEl.currentTime = (Number(seek.value) / 1000) * duration;
  };
  seek.addEventListener('pointerdown', startDrag);
  seek.addEventListener('pointerup', endDrag);
  seek.addEventListener('change', endDrag);
  seek.addEventListener('input', () => {
    const duration = audioEl.duration || 0;
    if (duration > 0) $('p-pos').textContent = fmtClock((Number(seek.value) / 1000) * duration);
  });

  const SPEEDS = [0.75, 1, 1.25, 1.5, 2];
  let speedIndex = 1;
  $('p-speed').addEventListener('click', () => {
    speedIndex = (speedIndex + 1) % SPEEDS.length;
    audioEl.playbackRate = SPEEDS[speedIndex];
    $('p-speed').textContent = `${SPEEDS[speedIndex].toFixed(2).replace(/0$/, '')}×`;
  });

  // 字幕：解析好的 cue 列表由服务端给出，前端只负责按时间找当前句
  async function loadSubtitle(trackId) {
    state.audio.cues = [];
    state.audio.cuePos = -1;
    clearSubtitle();
    try {
      const data = await api(`/api/audio/tracks/${trackId}/subtitles`);
      if (!data.cues || !data.cues.length) return;
      state.audio.cues = data.cues;
    } catch (e) {
      /* 没字幕是常态，不提示 */
    }
  }

  function cueAt(cues, ms) {
    let low = 0;
    let high = cues.length - 1;
    let found = -1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (cues[mid].start_ms <= ms) {
        found = mid;
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    if (found < 0) return null;
    const cue = cues[found];
    if (cue.end_ms && ms >= cue.end_ms) return null;
    return cue;
  }

  function syncSubtitle(ms) {
    const cues = state.audio.cues;
    if (!cues.length) return;
    const cue = cueAt(cues, ms);
    const line = $('subtitle-line');
    if (!cue) {
      clearSubtitle();
      return;
    }
    if (line.textContent !== cue.text) line.textContent = cue.text;
    line.hidden = false;
  }

  function clearSubtitle() {
    const line = $('subtitle-line');
    line.hidden = true;
    line.textContent = '';
  }

  // ---------------- 视频 ----------------
  /** 视频作品墙 */
  function videoWorksWall(authorId) {
    return pagedGrid({
      endpoint: '/api/video/works',
      listKey: 'works',
      unit: (n) => `${n} 个作品`,
      params: authorId ? { author_id: authorId } : {},
      emptyTitle: '视频库还是空的',
      emptySearchTitle: '没有匹配的作品',
      emptyHints: ['在电脑上打开「视频」模块并扫描一次'],
      renderCard: (work) =>
        el(
          'button',
          {
            class: 'card',
            type: 'button',
            onclick: () => { location.hash = `#/video/w/${work.id}`; },
          },
          (() => {
            const box = coverBox(work.cover, 'wide', '▶');
            box.append(el('span', { class: 'badge', text: `${work.video_count} 个` }));
            return box;
          })(),
          el(
            'div',
            { class: 'meta' },
            el('div', { class: 'title', text: work.name }),
            el('div', {
              class: 'sub',
              text: [work.author, work.total_duration_ms ? fmtDuration(work.total_duration_ms) : '']
                .filter(Boolean).join(' · '),
            }),
          ),
        ),
    });
  }

  /** 「继续观看」：按最后观看时间倒序，点卡片直接跳进那一集（带续播点） */
  function videoResumeWall() {
    return pagedGrid({
      endpoint: '/api/video/resume',
      listKey: 'items',
      unit: (n) => `${n} 个未看完`,
      emptyTitle: '还没有未看完的视频',
      emptySearchTitle: '没有匹配的记录',
      emptyHints: ['随便点开一集，看一半退出就会出现在这里'],
      renderCard: (item) => {
        const box = coverBox(item.cover, 'wide', '▶');
        // 进度条：时长未知时只显示已看时间，不画条（画 0% 会像「刚开头」）
        if (item.duration_ms > 0) {
          const ratio = Math.max(0, Math.min(1, item.position_ms / item.duration_ms));
          const fill = el('i');
          fill.style.width = `${Math.round(ratio * 100)}%`;
          box.append(el('span', { class: 'progress-line' }, fill));
        }
        box.append(el('span', { class: 'badge', text: item.video_name }));
        const remain = item.duration_ms > 0
          ? `还剩 ${fmtDuration(Math.max(0, item.duration_ms - item.position_ms))}`
          : `已看 ${fmtDuration(item.position_ms)}`;
        return el(
          'button',
          {
            class: 'card',
            type: 'button',
            onclick: () => {
              location.hash = `#/video/w/${item.work_id}/${item.video_id}`;
            },
          },
          box,
          el(
            'div',
            { class: 'meta' },
            el('div', { class: 'title', text: item.work_name }),
            el('div', {
              class: 'sub',
              text: [item.author, remain].filter(Boolean).join(' · '),
            }),
          ),
        );
      },
    });
  }

  async function viewVideoWorks(query, token) {
    const authorId = query.get('author') || '';
    const resumeMode = query.get('resume') === '1';
    const wall = resumeMode ? videoResumeWall() : videoWorksWall(authorId);

    view.replaceChildren(loadingState());
    // 续播计数与作者列表并发取，两条都是小 JSON
    const [authors, resume] = await Promise.all([
      api('/api/video/authors'),
      api('/api/video/resume?page_size=1'),
    ]);
    if (token !== state.token) return;

    const chips = el(
      'div',
      { class: 'chips' },
      el(
        'button',
        {
          class: `chip ${resumeMode ? 'active' : ''}`.trim(),
          type: 'button',
          onclick: () => { location.hash = '#/video?resume=1'; },
        },
        '继续观看',
        el('span', { class: 'count', text: String(resume.total || 0) }),
      ),
      el(
        'button',
        {
          class: `chip ${!authorId && !resumeMode ? 'active' : ''}`.trim(),
          type: 'button',
          onclick: () => { location.hash = '#/video'; },
        },
        '全部',
        el('span', { class: 'count', text: String(authors.authors.reduce((sum, a) => sum + a.work_count, 0)) }),
      ),
      ...authors.authors.map((author) =>
        el(
          'button',
          {
            class: `chip ${String(author.id) === authorId && !resumeMode ? 'active' : ''}`.trim(),
            type: 'button',
            onclick: () => { location.hash = `#/video?author=${author.id}`; },
          },
          author.name,
          el('span', { class: 'count', text: String(author.video_count) }),
        ),
      ),
    );

    view.replaceChildren(chips, wall.node);
    state.repaint = wall.reload;
    state.autoLoad = wall.maybeAutoLoad;
    await wall.reload();
  }

  async function viewVideoWork(workId, token, playVideoId) {
    view.replaceChildren(loadingState());
    const data = await api(`/api/video/works/${workId}/videos`);
    if (token !== state.token) return;

    $('crumb').textContent = ` / ${data.work.name}`;

    if (!data.videos.length) {
      view.replaceChildren(emptyState('这个作品里没有视频'));
      return;
    }

    const rows = data.videos.map((video) => {
      const progress = video.duration_ms > 0
        ? Math.min(1, video.progress_ms / video.duration_ms)
        : 0;
      const tags = [];
      if (!video.playable) tags.push(el('span', { class: 'pill warn', text: '需转封装' }));
      if (video.codec) tags.push(el('span', { class: 'pill', text: video.codec.toUpperCase() }));
      if (video.height) tags.push(el('span', { class: 'pill', text: `${video.height}p` }));
      if (video.size) tags.push(el('span', { text: fmtSize(video.size) }));

      return el(
        'button',
        {
          class: 'row',
          type: 'button',
          onclick: () => openVideo(workId, data.videos, video.index),
        },
        el('span', { class: 'idx', text: String(video.index + 1).padStart(2, '0') }),
        el('span', { class: 'body' },
          el('span', { class: 'name', text: video.name }),
          el('span', { class: 'sub' },
            ...tags,
            progress > 0.01 && progress < 0.99
              ? el('span', { text: `已看 ${fmtDuration(video.progress_ms)}` })
              : null,
          ),
        ),
        el('span', { class: 'dur', text: fmtDuration(video.duration_ms) }),
      );
    });

    view.replaceChildren(
      sectionTitle(`${data.videos.length} 个视频`, '点任意一集开始播放'),
      el('div', { class: 'list' }, ...rows),
    );

    // 从「继续观看」点进来：直接开那一集，续播点由播放器按路径读库
    if (playVideoId) {
      const target = data.videos.filter((v) => v.id === playVideoId)[0];
      if (target) await openVideo(workId, data.videos, target.index);
    }
  }

  /** 把 <video> 硬拉回全屏
   *
   * Android 7 那代 WebView 用独立图层渲染 <video>，个别机型按文档流定位，
   * 画面就落到列表下方（用户看到的「视频在列表最下面播」）。
   * 这里量一次真实几何，不对就写内联样式覆盖——CSS 生效时这段不会动手。
   */
  function ensureVideoFullscreen() {
    const v = videoEl;
    const style = window.getComputedStyle(v);
    const rect = v.getBoundingClientRect();
    const width = window.innerWidth || document.documentElement.clientWidth;
    const height = window.innerHeight || document.documentElement.clientHeight;
    const bad =
      style.position !== 'fixed' ||
      Math.abs(rect.top) > 1 ||
      Math.abs(rect.left) > 1 ||
      rect.width < width - 1 ||
      rect.height < height - 1;
    if (!bad) return;
    v.style.position = 'fixed';
    v.style.top = '0';
    v.style.left = '0';
    v.style.right = 'auto';
    v.style.bottom = 'auto';
    v.style.width = '100%';
    v.style.height = '100%';
    console.warn('[video] 播放器位置异常，已强制全屏', style.position, rect, width, height);
  }

  async function openVideo(workId, videos, index) {
    const overlay = $('video-overlay');
    overlay.hidden = false;
    overlay.classList.remove('v-idle');
    document.body.style.overflow = 'hidden';
    document.body.classList.add('video-open');
    state.video.workId = workId;
    state.video.videos = videos;
    ensureVideoFullscreen();
    await loadVideo(index);
  }

  async function loadVideo(index) {
    const video = state.video.videos[index];
    if (!video) return;
    state.video.index = index;
    state.video.remux = false;
    setVideoSeekable(true);
    syncVideoPlay();
    $('video-overlay').querySelector('.v-title').textContent = video.name;
    $('v-note').hidden = true;

    let info;
    try {
      info = await api(`/api/video/videos/${video.id}`);
    } catch (error) {
      showVideoNote(error.message);
      return;
    }
    state.video.info = info;
    state.video.resume = info.position_ms || 0;
    state.video.savedAt = 0;

    if (info.playable) {
      videoEl.src = info.url;
    } else {
      useRemux();
    }
    ensureVideoFullscreen();
    videoEl.play().catch(() => {
      /* 自动播放被拦截时用户点一下即可 */
    });
  }

  function useRemux() {
    const info = state.video.info;
    if (!info) return;
    state.video.remux = true;
    setVideoSeekable(false);
    videoEl.src = info.remux_url;
    showVideoNote('正在转封装播放：需要几秒才开始，此模式不能拖动进度条。', [
      { label: '用原格式试播', onclick: () => {
        state.video.remux = false;
        setVideoSeekable(true);
        videoEl.src = info.url;
        videoEl.play().catch(() => {});
        $('v-note').hidden = true;
      } },
    ]);
  }

  function showVideoNote(text, buttons) {
    const note = $('v-note');
    note.replaceChildren(
      el('div', { text }),
      ...(buttons || []).map((button) =>
        el('button', { type: 'button', onclick: button.onclick, text: button.label }),
      ),
    );
    note.hidden = false;
  }

  function saveVideoProgress(beacon) {
    const info = state.video.info;
    if (!info) return;
    const position = Math.round((videoEl.currentTime || 0) * 1000);
    if (!position) return;
    const body = JSON.stringify({
      position_ms: position,
      duration_ms: Math.round((videoEl.duration || 0) * 1000),
    });
    const url = `/api/video/videos/${info.id}/progress`;
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
      return;
    }
    fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).catch(() => {});
  }

  // ---------------- 视频控件（自绘进度条） ----------------
  //
  // 为什么不用原生 controls：原生进度条是「拖一格 seek 一次」，而每次 seek 都得
  // 从上一个关键帧开始重新解码。实测 1080p（GOP 250）单次 seek 198ms、p90 357ms，
  // 同片改成 GOP 30 只要 34ms —— 拖 30 格就是 6 秒的连续卡顿。
  // 这里拖动全程只动滑块和数字，松手才真的写一次 currentTime。
  const vSeek = $('v-seek');
  const vOverlay = $('video-overlay');
  let vIdleTimer = 0;

  function videoDuration() {
    const value = videoEl.duration;
    return Number.isFinite(value) && value > 0 ? value : 0;
  }

  /** 滑块位置（0~1000 千分比）→ 目标秒数 */
  function videoTarget() {
    const duration = videoDuration();
    return duration ? (Number(vSeek.value) / 1000) * duration : 0;
  }

  function syncVideoPlay() {
    $('v-play').innerHTML = videoEl.paused ? ICON.play : ICON.pause;
  }

  function syncVideoTime() {
    const duration = videoDuration();
    setText($('v-dur'), duration ? fmtClock(duration) : '--:--');
    if (!vSeek.dataset.dragging) {
      const now = videoEl.currentTime || 0;
      setText($('v-pos'), fmtClock(now));
      if (duration) {
        const value = String(Math.round((now / duration) * 1000));
        if (vSeek.value !== value) vSeek.value = value;
      }
    }
  }

  function showVideoControls() {
    vOverlay.classList.remove('v-idle');
    if (vIdleTimer) { clearTimeout(vIdleTimer); vIdleTimer = 0; }
    // 播放中才淡出：暂停时控件必须留着，否则用户找不到播放按钮
    if (!videoEl.paused && !state.video.remux) {
      vIdleTimer = setTimeout(() => {
        vIdleTimer = 0;
        vOverlay.classList.add('v-idle');
      }, 2600);
    }
  }

  /** 松手才提交：一次拖动只产生一次真正的 seek */
  function commitVideoSeek() {
    if (!vSeek.dataset.dragging) return;
    delete vSeek.dataset.dragging;
    const target = videoTarget();
    if (target > 0) videoEl.currentTime = target;
    syncVideoTime();
    showVideoControls();
  }

  function setVideoSeekable(ok) {
    vSeek.disabled = !ok;
    vSeek.value = '0';
    $('v-pos').textContent = '00:00';
    $('v-dur').textContent = '--:--';
  }

  vSeek.addEventListener('pointerdown', () => {
    if (vSeek.disabled) return;
    vSeek.dataset.dragging = '1';
    showVideoControls();
  });
  vSeek.addEventListener('input', () => {
    if (vSeek.disabled) return;
    // 万一某个内核没先发 pointerdown，这里补上标记，免得到松手时提交不了
    vSeek.dataset.dragging = '1';
    // 这里绝对不能写 currentTime：拖动的每一格都 seek，就是卡顿本身
    if (videoDuration()) setText($('v-pos'), fmtClock(videoTarget()));
    showVideoControls();
  });
  // 触摸屏和 WebView 上「松手」可能是 change / pointerup / pointercancel / touchend
  // 里的任意一个：全接上，谁先来谁提交，其余因为 dragging 已清掉而空操作
  vSeek.addEventListener('change', commitVideoSeek);
  vSeek.addEventListener('pointerup', commitVideoSeek);
  vSeek.addEventListener('pointercancel', commitVideoSeek);
  vSeek.addEventListener('touchend', commitVideoSeek);

  videoEl.addEventListener('play', () => { syncVideoPlay(); showVideoControls(); });
  videoEl.addEventListener('pause', () => { syncVideoPlay(); showVideoControls(); });
  videoEl.addEventListener('timeupdate', syncVideoTime);
  videoEl.addEventListener('durationchange', syncVideoTime);
  videoEl.addEventListener('loadedmetadata', syncVideoTime);

  // 点画面：控件已淡出就先唤回控件，否则才播/暂停（与常见播放器一致）
  videoEl.addEventListener('click', () => {
    if (vOverlay.classList.contains('v-idle')) {
      showVideoControls();
      return;
    }
    if (videoEl.paused) videoEl.play().catch(() => {});
    else videoEl.pause();
    showVideoControls();
  });

  $('v-play').addEventListener('click', () => {
    if (videoEl.paused) videoEl.play().catch(() => {});
    else videoEl.pause();
    showVideoControls();
  });

  /** 兜底：交给系统播放器全屏
   *
   * 这台平板是 Android 7 的 WebView（UA 带 `wv`），宿主 App 不实现
   * `onShowCustomView` 时元素级全屏会「调用成功但什么也不发生」，
   * 只剩下 <video> 自己的全屏通道还可能通。
   */
  function enterNativeVideoFullscreen() {
    try {
      if (videoEl.webkitEnterFullscreen) videoEl.webkitEnterFullscreen();
      else if (videoEl.webkitRequestFullscreen) videoEl.webkitRequestFullscreen();
      else toast('这个浏览器不支持全屏，横过来看更舒服', 2600);
    } catch (error) {
      toast('这个浏览器不支持全屏，横过来看更舒服', 2600);
    }
  }

  $('v-full').addEventListener('click', () => {
    const doc = document;
    if (doc.fullscreenElement || doc.webkitFullscreenElement) {
      try {
        (doc.exitFullscreen || doc.webkitExitFullscreen).call(doc);
      } catch (error) {
        /* 退出失败也只能算了 */
      }
      showVideoControls();
      return;
    }

    let asked = false;
    try {
      if (vOverlay.requestFullscreen) {
        const pending = vOverlay.requestFullscreen();
        if (pending && pending.catch) pending.catch(() => {});
        asked = true;
      } else if (vOverlay.webkitRequestFullscreen) {
        vOverlay.webkitRequestFullscreen();
        asked = true;
      }
    } catch (error) {
      asked = false;
    }

    if (!asked) {
      enterNativeVideoFullscreen();
    } else {
      // 静默失败（WebView 常见）就退到原生视频全屏，别让按钮看着像坏了
      setTimeout(() => {
        if (!doc.fullscreenElement && !doc.webkitFullscreenElement) enterNativeVideoFullscreen();
      }, 800);
    }
    showVideoControls();
  });

  videoEl.addEventListener('loadedmetadata', () => {
    const resume = state.video.resume;
    if (state.video.remux || !resume) return;
    const durationMs = (videoEl.duration || 0) * 1000;
    if (resume > 3000 && (!durationMs || resume < durationMs - 5000)) {
      videoEl.currentTime = resume / 1000;
      toast(`已从 ${fmtDuration(resume)} 继续`);
    }
    state.video.resume = 0;
  });

  videoEl.addEventListener('timeupdate', () => {
    const now = Date.now();
    if (state.video.remux) return;
    if (now - state.video.savedAt > 10000) {
      state.video.savedAt = now;
      saveVideoProgress(false);
    }
  });

  videoEl.addEventListener('pause', () => { if (!state.video.remux) saveVideoProgress(false); });
  videoEl.addEventListener('ended', () => {
    if (!state.video.remux) saveVideoProgress(false);
    // ended 时浏览器不发 pause 事件，图标和控件条得自己收尾（最后一集不会切集）
    syncVideoPlay();
    showVideoControls();
    const next = state.video.index + 1;
    if (next < state.video.videos.length) loadVideo(next);
  });

  videoEl.addEventListener('error', () => {
    if (!videoEl.src) return;
    if (!state.video.remux) {
      showVideoNote('浏览器打不开这个视频的原格式，可以试试转封装播放。', [
        { label: '转封装播放', onclick: useRemux },
      ]);
    } else {
      showVideoNote('转封装也失败了，这个编码可能手机不支持，回电脑上看吧。');
    }
  });

  window.addEventListener('resize', () => {
    if (!$('video-overlay').hidden) ensureVideoFullscreen();
  });

  window.addEventListener('pagehide', () => {
    if (!$('video-overlay').hidden && !state.video.remux) saveVideoProgress(true);
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden && !$('video-overlay').hidden && !state.video.remux) {
      saveVideoProgress(false);
    }
  });

  function closeVideo() {
    if (!state.video.remux) saveVideoProgress(false);
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl.load();
    vOverlay.classList.remove('v-idle');
    if (vIdleTimer) { clearTimeout(vIdleTimer); vIdleTimer = 0; }
    delete vSeek.dataset.dragging;
    $('video-overlay').hidden = true;
    $('v-note').hidden = true;
    state.video.info = null;
    document.body.style.overflow = '';
    document.body.classList.remove('video-open');
    // 清掉 ensureVideoFullscreen() 写下的内联样式，别影响下一次
    videoEl.style.position = '';
    videoEl.style.top = '';
    videoEl.style.left = '';
    videoEl.style.right = '';
    videoEl.style.bottom = '';
    videoEl.style.width = '';
    videoEl.style.height = '';
  }

  $('v-close').addEventListener('click', closeVideo);

  // ---------------- 快捷键（平板接键盘也能用） ----------------
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!lb.hidden) closeLightbox();
      else if (!$('video-overlay').hidden) closeVideo();
      return;
    }
    if (!lb.hidden) {
      if (event.key === 'ArrowLeft') stepLightbox(-1);
      if (event.key === 'ArrowRight') stepLightbox(1);
    }
  });

  // ---------------- 启动 ----------------
  $('p-play').innerHTML = ICON.play;
  $('p-prev').innerHTML = ICON.prev;
  $('p-next').innerHTML = ICON.next;
  $('v-play').innerHTML = ICON.play;
  $('v-full').innerHTML = ICON.full;

  if (!location.hash) location.hash = '#/';
  render();
})();
