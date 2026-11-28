(function () {
  var form = document.getElementById("loginForm");
  var userInput = document.getElementById("username");
  var pwd = document.getElementById("password");
  var err = document.getElementById("error");
  var btn = document.getElementById("submitBtn");
  var hint = document.getElementById("hint");
  var switchLine = document.getElementById("switchLine");
  var toggle = document.getElementById("toggleMode");
  var cardTitle = document.getElementById("cardTitle");
  var cardSubtitle = document.getElementById("cardSubtitle");

  var mode = "login"; // login | register
  var allowRegister = false;

  function showError(msg) { err.textContent = msg; err.hidden = false; }
  function clearError() { err.hidden = true; err.textContent = ""; }
  function setLoading(loading) {
    btn.disabled = loading;
    btn.textContent = loading
      ? (mode === "register" ? "注册中…" : "登录中…")
      : (mode === "register" ? "注 册" : "登 录");
  }

  function applyMode(next) {
    mode = next;
    var isRegister = mode === "register";
    cardTitle.textContent = isRegister ? "注册账号" : "股票监控台";
    cardSubtitle.textContent = isRegister ? "每个账号独立的自选、持仓与流水" : "Monitor Stock · 登录后进入";
    btn.textContent = isRegister ? "注 册" : "登 录";
    hint.textContent = isRegister
      ? "用户名 2-32 位；口令至少 8 位。注册后自动登录。"
      : "首次部署默认用户名 admin；口令见服务端 APP_PASSWORD。";
    toggle.textContent = isRegister ? "已有账号？返回登录" : "没有账号？注册一个";
    clearError();
  }

  // 已登录则直接进入主站
  fetch("/api/auth/me")
    .then(function (r) { return r.json(); })
    .then(function (d) { if (d && d.authenticated) window.location.href = "/"; })
    .catch(function () {});

  // 是否开放注册
  fetch("/api/auth/status")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      allowRegister = !!(d && d.allow_register);
      switchLine.hidden = !allowRegister;
    })
    .catch(function () {});

  // 记住上次用户名，减少重复输入
  try {
    var last = localStorage.getItem("ms_last_user");
    if (last) userInput.value = last;
  } catch (_) {}

  toggle.addEventListener("click", function (e) {
    e.preventDefault();
    applyMode(mode === "login" ? "register" : "login");
  });

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    var username = (userInput.value || "").trim();
    var password = pwd.value || "";
    if (!username) { showError("请输入用户名"); userInput.focus(); return; }
    if (!password) { showError("请输入访问口令"); pwd.focus(); return; }
    if (mode === "register") {
      if (password.length < 8) { showError("口令至少 8 位"); pwd.focus(); return; }
    }

    setLoading(true);
    clearError();
    var endpoint = mode === "register" ? "/api/auth/register" : "/api/auth/login";
    try {
      var res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ username: username, password: password }),
      });
      if (res.ok) {
        try { localStorage.setItem("ms_last_user", username); } catch (_) {}
        window.location.href = "/";
        return;
      }
      var msg = mode === "register" ? "注册失败" : "登录失败";
      // 后端统一错误体为 {ok:false, error:{code,message}, request_id}；
      // 兼容旧版 detail，否则 429「请 N 秒后再试」这类提示会被吞掉
      try {
        var j = await res.json();
        var detail = j && (j.detail || (j.error && j.error.message));
        if (detail) msg = detail;
      } catch (_) {}
      showError(msg);
    } catch (_) {
      showError("网络错误，请重试");
    } finally {
      setLoading(false);
    }
  });
})();
