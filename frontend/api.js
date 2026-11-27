/* 前端统一请求层（唯一事实源）
 *
 * 改造前 app.js / ops.js / agent.js 各写了一份 fetch 封装，行为还不一致：
 * 有的自动序列化 body，有的不；报错文案各不相同。现在统一到这里，
 * 三个模块只保留别名，新增的 admin.js 也直接复用。
 *
 * 401 跳转由 app.js 顶部的 fetch 拦截器负责，这里不重复处理。
 */
(function () {
  "use strict";

  window.requestJSON = async function requestJSON(url, options) {
    const opts = Object.assign({ credentials: "same-origin" }, options || {});
    // 所有非 FormData 的 body 都标 application/json：存量调用方传的是已 JSON.stringify
    // 的字符串，只对"非字符串"设置头的旧写法会让 FastAPI 按 text/plain 收到 raw 字节 → 422
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
      if (typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, opts);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message || data.error)) || text || ("HTTP " + res.status);
      throw new Error(String(detail).slice(0, 300));
    }
    return data;
  };

  window.downloadJSON = function downloadJSON(data, filename) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "export.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
})();
