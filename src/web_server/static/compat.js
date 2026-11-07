/* 老浏览器兼容垫片（ES5，必须最先加载）
 *
 * 为什么需要它：Android 7 时代的 WebView/Chrome 是 51~59 那一代，
 * 下面这些函数它一个都没有，而 app.js 里全都是——
 *   replaceChildren（Chrome 86+）、append/prepend（54+）、
 *   Array.prototype.flat（69+）、String.padStart（57+）、Object.entries（54+）。
 * 缺一个就会在执行到那行时抛异常，页面停在半截：只剩顶栏文字、按钮全不出来。
 *
 * 这里只用 ES5 写，所以它自己永远不会「因为语法太新」而挂。
 * 现代浏览器上用不到（判空跳过），不会拖慢启动。
 */
(function () {
  'use strict';

  /** 定义方法时用 defineProperty，避免可枚举属性污染 for...in */
  function def(obj, name, fn) {
    if (obj[name]) return;
    try {
      Object.defineProperty(obj, name, {
        value: fn,
        writable: true,
        configurable: true,
        enumerable: false,
      });
    } catch (e) {
      obj[name] = fn;
    }
  }

  // ---------------- Object ----------------

  def(Object, 'assign', function (target) {
    if (target === null || target === undefined) {
      throw new TypeError('Object.assign 的目标不能是 null/undefined');
    }
    var to = Object(target);
    for (var i = 1; i < arguments.length; i++) {
      var source = arguments[i];
      if (!source) continue;
      for (var key in source) {
        if (Object.prototype.hasOwnProperty.call(source, key)) to[key] = source[key];
      }
    }
    return to;
  });

  def(Object, 'entries', function (obj) {
    var out = [];
    for (var key in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, key)) out.push([key, obj[key]]);
    }
    return out;
  });

  def(Object, 'values', function (obj) {
    var out = [];
    for (var key in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, key)) out.push(obj[key]);
    }
    return out;
  });

  // ---------------- Array / String ----------------

  def(Array.prototype, 'flat', function (depth) {
    var level = depth === undefined ? 1 : Number(depth);
    var out = [];
    (function walk(list, left) {
      for (var i = 0; i < list.length; i++) {
        var item = list[i];
        if (Array.isArray(item) && left > 0) walk(item, left - 1);
        else out.push(item);
      }
    })(this, isNaN(level) ? 1 : level);
    return out;
  });

  def(Array.prototype, 'includes', function (needle, from) {
    var start = from === undefined ? 0 : Number(from) || 0;
    if (start < 0) start = Math.max(this.length + start, 0);
    for (var i = start; i < this.length; i++) {
      // SameValueZero：NaN 也要算命中
      if (this[i] === needle || (this[i] !== this[i] && needle !== needle)) return true;
    }
    return false;
  });

  def(String.prototype, 'padStart', function (length, filler) {
    var target = Number(length) || 0;
    var text = String(this);
    var pad = filler === undefined ? ' ' : String(filler);
    if (!pad || text.length >= target) return text;
    var need = target - text.length;
    var prefix = '';
    while (prefix.length < need) prefix += pad;
    return prefix.slice(0, need) + text;
  });

  def(String.prototype, 'padEnd', function (length, filler) {
    var target = Number(length) || 0;
    var text = String(this);
    var pad = filler === undefined ? ' ' : String(filler);
    if (!pad || text.length >= target) return text;
    var need = target - text.length;
    var suffix = '';
    while (suffix.length < need) suffix += pad;
    return text + suffix.slice(0, need);
  });

  def(String.prototype, 'includes', function (needle) {
    return String(this).indexOf(String(needle)) !== -1;
  });

  def(String.prototype, 'startsWith', function (needle, at) {
    var pos = at || 0;
    return String(this).substr(pos, String(needle).length) === String(needle);
  });

  // ---------------- DOM ----------------

  def(Element.prototype, 'matches', function (selector) {
    var fn = Element.prototype.msMatchesSelector || Element.prototype.webkitMatchesSelector;
    return fn ? fn.call(this, selector) : false;
  });

  def(Element.prototype, 'closest', function (selector) {
    var node = this;
    while (node && node.nodeType === 1) {
      if (node.matches(selector)) return node;
      node = node.parentElement || node.parentNode;
    }
    return null;
  });

  function toNode(item) {
    return item instanceof Node ? item : document.createTextNode(String(item));
  }

  def(Element.prototype, 'append', function () {
    for (var i = 0; i < arguments.length; i++) this.appendChild(toNode(arguments[i]));
  });

  def(Element.prototype, 'prepend', function () {
    var first = this.firstChild;
    for (var i = 0; i < arguments.length; i++) {
      this.insertBefore(toNode(arguments[i]), first);
    }
  });

  def(Element.prototype, 'replaceChildren', function () {
    while (this.firstChild) this.removeChild(this.firstChild);
    for (var i = 0; i < arguments.length; i++) this.appendChild(toNode(arguments[i]));
  });

  def(NodeList.prototype, 'forEach', function (callback, thisArg) {
    for (var i = 0; i < this.length; i++) callback.call(thisArg, this[i], i, this);
  });

  // ---------------- 出错时在页面上把原因说出来 ----------------
  // 服务端 CSP 是 script-src 'self'，内联脚本会被拦，所以这段必须放在外部文件里，
  // 且必须在 app.js 之前执行——不然 app.js 解析失败时没人接，用户只能看到一片空白。

  function showError(detail) {
    var bar = document.getElementById('compat-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'compat-bar';
      bar.setAttribute(
        'style',
        'position:fixed;left:0;right:0;bottom:0;z-index:99999;padding:10px 12px;' +
          'background:#8c3b2f;color:#fff;font:12px/1.6 sans-serif;' +
          'white-space:pre-wrap;word-break:break-all;max-height:45%;overflow:auto'
      );
      bar.title = '点一下收起';
      bar.onclick = function () {
        if (bar.parentNode) bar.parentNode.removeChild(bar);
      };
      (document.body || document.documentElement).appendChild(bar);
    }
    bar.textContent =
      '页面没能正常显示：' + detail + '\n浏览器：' + navigator.userAgent;
    // 提示条自己也会挡住底部内容，12 秒后自动收起
    clearTimeout(showError.timer);
    showError.timer = setTimeout(function () {
      if (bar.parentNode) bar.parentNode.removeChild(bar);
    }, 12000);
  }

  /** 这条报错是不是我们自己的脚本发的
   *
   * Android 上不少浏览器/输入法会往页面里注入脚本，它们自己出错时
   * window.onerror 照样会收到——那不是本页的问题，弹红条只会把人吓一跳。
   */
  function isOurScript(source) {
    var text = String(source || '');
    if (!text) return true; // 匿名（eval/inline）无法判断，当自己的处理
    return text.indexOf('/static/') !== -1;
  }

  window.onerror = function (message, source, line, column) {
    if (!isOurScript(source)) {
      try {
        console.warn('[外部脚本报错，已忽略]', message, source, line);
      } catch (e) {
        /* 没有 console 就算了 */
      }
      return false;
    }
    var file = String(source || '').split('/').pop();
    var where = file ? '（' + file + ':' + line + (column ? ':' + column : '') + '）' : '';
    var detail = String(message) + where;
    // 界面已经渲染出来了就别弹条：这条提示是给「整页空白」用的，
    // 一个局部小错也把它挂到底部，只会挡住内容、让人以为页面坏了。
    var view = document.getElementById('view');
    if (view && view.children.length) {
      try {
        console.error('[页面局部报错]', detail);
      } catch (e) {
        /* ignore */
      }
      return false;
    }
    showError(detail);
    return false;
  };

  // 兜底：JS 若一声不响地没跑起来（例如更老的语法错误），6 秒后 #view 仍是空的
  setTimeout(function () {
    var view = document.getElementById('view');
    var bar = document.getElementById('compat-bar');
    if (view && !view.children.length && !bar) {
      showError('界面没有渲染出来，可能是浏览器内核版本过旧');
    }
  }, 6000);
})();
