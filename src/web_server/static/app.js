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
    audio: { queue: [], index: -1, playlist: null, cues: [], cuePos: -1 },
    video: { workId: null, videos: [], index: 0, info: null, remux: false, resume: 0, savedAt: 0 },
    lightbox: { images: [], index: 0 },
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

  function setHeader({ tab, crumb, searchable }) {
    document.querySelectorAll('#tabs button').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    $('tabs').hidden = !tab;
    $('brand-title').textContent = tab ? TAB_LABEL[tab] || '本地资源' : '本地资源';
    $('crumb').textContent = crumb ? ` / ${crumb}` : '';

    const wrap = $('search-wrap');
    const toggle = $('search-toggle');
    toggle.hidden = !searchable;
    if (!searchable) {
      wrap.hidden = true;
      state.search = '';
      $('search-input').value = '';
    }
  }

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
    const value = event.target.value.trim();
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

    try {
      if (parts.length === 0) {
        setHeader({ tab: '', crumb: '', searchable: false });
        await viewHome(token);
      } else if (parts[0] === 'photo' && parts[1] === 'w') {
        setHeader({ tab: 'photo', crumb: '作品', searchable: false });
        await viewPhotoWork(Number(parts[2]), token);
      } else if (parts[0] === 'photo') {
        setHeader({ tab: 'photo', crumb: '', searchable: true });
        await viewPhotoWorks(query, token);
      } else if (parts[0] === 'audio' && parts[1] === 'p') {
        setHeader({ tab: 'audio', crumb: '歌单', searchable: false });
        await viewAudioPlaylist(Number(parts[2]), token);
      } else if (parts[0] === 'audio') {
        setHeader({ tab: 'audio', crumb: '', searchable: false });
        await viewAudioRoot(token);
      } else if (parts[0] === 'video' && parts[1] === 'w') {
        setHeader({ tab: 'video', crumb: '作品', searchable: false });
        await viewVideoWork(Number(parts[2]), token);
      } else if (parts[0] === 'video') {
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

  // ---------------- 写真 ----------------
  async function viewPhotoWorks(query, token) {
    const authorId = query.get('author') || '';
    const listWrap = el('div');

    const repaint = async () => {
      listWrap.replaceChildren(loadingState());
      const params = new URLSearchParams();
      if (authorId) params.set('author_id', authorId);
      if (state.search) params.set('q', state.search);
      const data = await api(`/api/photos/works?${params.toString()}`);
      if (token !== state.token) return;

      if (!data.works.length) {
        listWrap.replaceChildren(
          emptyState(state.search ? '没有匹配的作品' : '写真库还是空的', [
            state.search ? '换个关键词试试' : '在电脑上打开「写真」模块并扫描一次',
          ]),
        );
        return;
      }

      listWrap.replaceChildren(
        sectionTitle(`${data.works.length} 个作品`),
        el(
          'div',
          { class: 'grid' },
          ...data.works.map((work) =>
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
          ),
        ),
      );
    };

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

    view.replaceChildren(chips, listWrap);
    state.repaint = repaint;
    await repaint();
  }

  async function viewPhotoWork(workId, token) {
    view.replaceChildren(loadingState());
    const data = await api(`/api/photos/works/${workId}/images`);
    if (token !== state.token) return;

    $('crumb').textContent = ` / ${data.work.name}`;

    if (!data.images.length) {
      view.replaceChildren(emptyState('这个作品里没有图片'));
      return;
    }

    view.replaceChildren(
      sectionTitle(`${data.images.length} 张`, '点开可左右滑动'),
      el(
        'div',
        { class: 'grid' },
        ...data.images.map((image, index) =>
          el(
            'button',
            {
              class: 'card',
              type: 'button',
              onclick: () => openLightbox(data.images, index),
            },
            coverBox(image.thumb, '', String(index + 1)),
          ),
        ),
      ),
    );
  }

  // ---------------- 看图 ----------------
  const lb = $('lightbox');
  const lbImg = $('lb-img');

  function openLightbox(images, index) {
    state.lightbox.images = images;
    state.lightbox.index = Math.max(0, Math.min(index, images.length - 1));
    lb.hidden = false;
    document.body.style.overflow = 'hidden';
    showLightboxImage();
  }

  function showLightboxImage() {
    const { images, index } = state.lightbox;
    const image = images[index];
    if (!image) return;
    lbImg.classList.add('loading');
    lbImg.src = image.full;
    $('lb-count').textContent = `${index + 1} / ${images.length}`;
    $('lb-prev').style.opacity = index > 0 ? '1' : '0.2';
    $('lb-next').style.opacity = index < images.length - 1 ? '1' : '0.2';
    // 预热相邻两张，滑动时不用等
    [index - 1, index + 1].forEach((i) => {
      if (images[i]) {
        const preload = new Image();
        preload.src = images[i].full;
      }
    });
  }

  lbImg.addEventListener('load', () => lbImg.classList.remove('loading'));

  function stepLightbox(delta) {
    const next = state.lightbox.index + delta;
    if (next < 0 || next >= state.lightbox.images.length) return;
    state.lightbox.index = next;
    showLightboxImage();
  }

  function closeLightbox() {
    lb.hidden = true;
    lbImg.removeAttribute('src');
    document.body.style.overflow = '';
  }

  $('lb-prev').addEventListener('click', () => stepLightbox(-1));
  $('lb-next').addEventListener('click', () => stepLightbox(1));
  $('lb-close').addEventListener('click', closeLightbox);
  $('lb-prev').innerHTML = ICON.left;
  $('lb-next').innerHTML = ICON.right;
  $('lb-close').innerHTML = ICON.close;
  $('v-close').innerHTML = ICON.close;

  // 点击图片左右半区翻页（手机上比找箭头快）
  $('lb-stage').addEventListener('click', (event) => {
    if (event.target.closest('.lb-nav')) return;
    const half = window.innerWidth / 2;
    stepLightbox(event.clientX < half ? -1 : 1);
  });

  let touchStartX = 0;
  let touchStartY = 0;
  $('lb-stage').addEventListener('touchstart', (event) => {
    touchStartX = event.touches[0].clientX;
    touchStartY = event.touches[0].clientY;
  }, { passive: true });

  $('lb-stage').addEventListener('touchend', (event) => {
    const touch = event.changedTouches[0];
    const dx = touch.clientX - touchStartX;
    const dy = touch.clientY - touchStartY;
    if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.4) {
      stepLightbox(dx < 0 ? 1 : -1);
    }
  }, { passive: true });

  // ---------------- 音频 ----------------
  async function viewAudioRoot(token) {
    view.replaceChildren(loadingState());
    const data = await api('/api/audio/playlists');
    if (token !== state.token) return;

    if (!data.playlists.length) {
      view.replaceChildren(
        emptyState('音频库还是空的', ['在电脑上打开「音频」模块并扫描一次', '歌单按目录层级自动生成']),
      );
      return;
    }

    view.replaceChildren(
      sectionTitle(`${data.playlists.length} 个歌单`),
      el(
        'div',
        { class: 'grid' },
        ...data.playlists.map((playlist) =>
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
        ),
      ),
    );
  }

  async function viewAudioPlaylist(playlistId, token) {
    view.replaceChildren(loadingState());
    const data = await api(`/api/audio/playlists/${playlistId}`);
    if (token !== state.token) return;

    $('crumb').textContent = ` / ${data.playlist.name}`;

    const nodes = [];
    if (data.playlist.has_cover || data.playlist.tags) {
      nodes.push(
        el(
          'div',
          { class: 'section-title', style: { alignItems: 'center', gap: '12px', marginBottom: '16px' } },
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
      seek.value = String(Math.round((audioEl.currentTime / duration) * 1000));
    }
    $('p-pos').textContent = fmtClock(audioEl.currentTime);
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
  async function viewVideoWorks(query, token) {
    const authorId = query.get('author') || '';
    const listWrap = el('div');

    const repaint = async () => {
      listWrap.replaceChildren(loadingState());
      const params = new URLSearchParams();
      if (authorId) params.set('author_id', authorId);
      if (state.search) params.set('q', state.search);
      const data = await api(`/api/video/works?${params.toString()}`);
      if (token !== state.token) return;

      if (!data.works.length) {
        listWrap.replaceChildren(
          emptyState(state.search ? '没有匹配的作品' : '视频库还是空的', [
            state.search ? '换个关键词试试' : '在电脑上打开「视频」模块并扫描一次',
          ]),
        );
        return;
      }

      listWrap.replaceChildren(
        sectionTitle(`${data.works.length} 个作品`),
        el(
          'div',
          { class: 'grid' },
          ...data.works.map((work) =>
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
          ),
        ),
      );
    };

    view.replaceChildren(loadingState());
    const authors = await api('/api/video/authors');
    if (token !== state.token) return;

    const chips = el(
      'div',
      { class: 'chips' },
      el(
        'button',
        {
          class: `chip ${authorId ? '' : 'active'}`.trim(),
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
            class: `chip ${String(author.id) === authorId ? 'active' : ''}`.trim(),
            type: 'button',
            onclick: () => { location.hash = `#/video?author=${author.id}`; },
          },
          author.name,
          el('span', { class: 'count', text: String(author.video_count) }),
        ),
      ),
    );

    view.replaceChildren(chips, listWrap);
    state.repaint = repaint;
    await repaint();
  }

  async function viewVideoWork(workId, token) {
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
  }

  async function openVideo(workId, videos, index) {
    const overlay = $('video-overlay');
    overlay.hidden = false;
    document.body.style.overflow = 'hidden';
    state.video.workId = workId;
    state.video.videos = videos;
    await loadVideo(index);
  }

  async function loadVideo(index) {
    const video = state.video.videos[index];
    if (!video) return;
    state.video.index = index;
    state.video.remux = false;
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
    videoEl.play().catch(() => {
      /* 自动播放被拦截时用户点一下即可 */
    });
  }

  function useRemux() {
    const info = state.video.info;
    if (!info) return;
    state.video.remux = true;
    videoEl.src = info.remux_url;
    showVideoNote('正在转封装播放：需要几秒才开始，此模式不能拖动进度条。', [
      { label: '用原格式试播', onclick: () => {
        state.video.remux = false;
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
    $('video-overlay').hidden = true;
    $('v-note').hidden = true;
    state.video.info = null;
    document.body.style.overflow = '';
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

  if (!location.hash) location.hash = '#/';
  render();
})();
