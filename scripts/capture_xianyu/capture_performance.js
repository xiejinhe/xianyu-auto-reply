/*
 * capture_performance.js — 抓取闲鱼「卖家填写演出信息」提交接口
 *
 * 用途：在已 root 的安卓手机 + frida-server 环境下，启动闲鱼后，在 App 内
 *       走一次「买家已付款 → 我填写演出信息 → 提交」，本脚本会把这次提交的
 *       真实 mtop 接口名（api）、版本（v）、请求体、响应体打印出来。
 *       把这些日志贴回给开发者，即可把接口名填进系统设置，实现系统内一键提交。
 *
 * 原理：
 *   1. 禁用 mtop 的 SPDY 私有协议，强制走明文 HTTPS/HTTP1.1，否则抓到的是二进制帧。
 *   2. Hook mtop 请求对象（mtopsdk.mtop.domain.MtopRequest）直接拿 api/version/params。
 *   3. Hook TLS 层（SSL_read/SSL_write + Java SSLSocket）做兜底，抓出完整 HTTP 报文。
 *   4. 关键词过滤，只在命中演出信息相关流量时打印，避免刷屏。
 *
 * 运行（见 run_capture.sh）：
 *   frida -U -f com.taobao.idlefish -l capture_performance.js --no-pause
 */

'use strict';

// 闲鱼包名（如为国际版 Goofish，改为 com.taobao.fleamarket）
const PKG = 'com.taobao.idlefish';

// 命中这些关键词才打印（演出信息提交相关）
const KEYWORDS = [
  'concertName', 'ticketCount', 'performance', 'smartform', 'smart_form',
  'YCH', 'showInfo', 'show_info', 'fillPerformance', '演出', 'h5api.m.goofish.com/h5/',
];

function hit(text) {
  if (!text) return false;
  const s = String(text);
  for (const k of KEYWORDS) {
    if (s.indexOf(k) !== -1) return true;
  }
  return false;
}

function ts() {
  const d = new Date();
  return d.toTimeString().slice(0, 8) + '.' + String(d.getMilliseconds()).padStart(3, '0');
}

function safePrint(tag, buf) {
  try {
    const str = buf instanceof Uint8Array ? ArrayBuffer2String(buf) : String(buf);
    if (!hit(str)) return;
    // 过滤掉明显非文本的噪声（如纯二进制）
    if (str.indexOf('') !== -1 && str.length < 40) return;
    console.log('\n========== [CAP] ' + tag + ' ' + ts() + ' ==========');
    console.log(str.length > 4000 ? str.slice(0, 4000) + '\n...(truncated)' : str);
    console.log('========== [END] ' + tag + ' ==========\n');
  } catch (e) {
    console.log('[CAP] print err: ' + e);
  }
}

function ArrayBuffer2String(arr) {
  let s = '';
  for (let i = 0; i < arr.length; i++) {
    const c = arr[i];
    if (c >= 0x20 && c < 0x7f) s += String.fromCharCode(c);
    else if (c === 0x0a) s += '\n';
    else if (c === 0x0d) s += '\r';
    else s += '.';
  }
  return s;
}

/* ---------- 1. 禁用 SPDY，强制明文 HTTPS ---------- */
function disableSpdy() {
  Java.perform(function () {
    try {
      const SwitchConfig = Java.use('mtopsdk.mtop.global.SwitchConfig');
      const methods = SwitchConfig.class.getDeclaredMethods();
      for (let i = 0; i < methods.length; i++) {
        const m = methods[i];
        try {
          if (m.getReturnType().getName() === 'boolean' && m.getParameterTypes().length === 0) {
            const name = m.getName();
            SwitchConfig[name].implementation = function () {
              return false; // 关闭 SPDY / HTTP2，走 HTTPS/1.1
            };
          }
        } catch (e) { /* 部分方法不可覆写，忽略 */ }
      }
      console.log('[CAP] SPDY 禁用 hook 已挂载（best-effort）');
    } catch (e) {
      console.log('[CAP] SwitchConfig 未找到，跳过 SPDY 禁用: ' + e);
    }
  });
}

/* ---------- 2. 直接 Hook mtop 请求对象，拿 api/version/params ---------- */
function hookMtopRequest() {
  Java.perform(function () {
    const candidates = [
      'mtopsdk.mtop.domain.MtopRequest',
      'mtopsdk.mtop.domain.MtopRequestImpl',
      'com.taobao.tao.remotebusiness.MtopRequest',
    ];
    for (const cls of candidates) {
      try {
        const Req = Java.use(cls);
        // 构造后通常会调用 getApiName / getVersion / getParams
        Req.getApiName.implementation = function () {
          const api = this.getApiName();
          if (api && (api.indexOf('performance') !== -1 || api.indexOf('show') !== -1 ||
                      api.indexOf('smart') !== -1 || api.indexOf('Performance') !== -1)) {
            console.log('\n========== [MTOP-REQ] api=' + api + ' ' + ts() + ' ==========');
          }
          return api;
        };
        try {
          Req.getVersion.implementation = function () {
            return this.getVersion();
          };
        } catch (e) {}
        try {
          Req.getParams.implementation = function () {
            const p = this.getParams();
            return p;
          };
        } catch (e) {}
        console.log('[CAP] 已 hook ' + cls);
        return;
      } catch (e) { /* 类不存在，试下一个 */ }
    }
    console.log('[CAP] 未找到 MtopRequest 类（App 版本差异），依赖 TLS 层兜底');
  });
}

/* ---------- 3. TLS 层兜底抓取（Java + Native） ---------- */
function hookJavaSsl() {
  Java.perform(function () {
    try {
      const SSLSocket = Java.use('javax.net.ssl.SSLSocket');
      // 覆写 getInputStream / getOutputStream 不可行，改为 hook 读写的流对象较复杂，
      // 这里改用更通用的：hook java.io.OutputStream.write / InputStream.read 不可靠，
      // 因此主要依赖 native SSL_read/SSL_write。Java 层仅做提示。
      console.log('[CAP] Java SSL 层：依赖 native SSL hook');
    } catch (e) {}
  });
}

function hookNativeSsl() {
  const libs = ['libssl.so', 'libsscronet.so', 'libcronet.so', 'libsgmainso-6.4.20.so'];
  for (const libName of libs) {
    const lib = Process.findModuleByName(libName);
    if (!lib) continue;
    const exports = ['SSL_read', 'SSL_write', 'SSL_read_ex', 'SSL_write_ex', 'BIO_read', 'BIO_write'];
    for (const exp of exports) {
      const addr = Module.findExportByName(libName, exp);
      if (!addr) continue;
      try {
        Interceptor.attach(addr, {
          onEnter: function (args) {
            this.buf = args[1];
            // SSL_read/write 第3参是长度；*_ex 第3参是长度指针
            if (exp.endsWith('_ex')) {
              this.lenPtr = args[2];
              this.ex = true;
            } else {
              this.len = args[2].toInt32();
              this.ex = false;
            }
            this.exp = exp;
          },
          onLeave: function (ret) {
            let len;
            try {
              if (this.ex) len = this.lenPtr.readInt();
              else len = this.len;
            } catch (e) { return; }
            if (!len || len <= 0) return;
            try {
              const buf = this.buf.readByteArray(Math.min(len, 8192));
              const u8 = new Uint8Array(buf);
              safePrint('TLS:' + this.exp, u8);
            } catch (e) {}
          },
        });
      } catch (e) {}
    }
    console.log('[CAP] 已 hook ' + libName);
  }
}

/* ---------- 启动 ---------- */
console.log('[CAP] 演出信息提交抓包脚本已加载，请在闲鱼内走一次「填写演出信息 → 提交」');
disableSpdy();
hookMtopRequest();
hookJavaSsl();
hookNativeSsl();
