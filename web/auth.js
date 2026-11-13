/* Login/register pages: error message, Turnstile widget and form submission. */
(function () {
  "use strict";

  var form = document.getElementById("authform");
  var box = document.getElementById("tsbox");
  var sitekey = (box && box.getAttribute("data-sitekey")) || "";
  var isRegister = !!document.getElementById("invite");

  /* --- title --- */
  document.title = FLX.t(isRegister ? "rg_title" : "lg_title") + " · " +
    FLX.t("title");

  /* --- translates the ?err= code --- */
  var ERR = {
    auth: "lg_err_auth", lock: "lg_err_lock", ts: "lg_err_ts",
    code: "rg_err_code", user: "rg_err_user", short: "rg_err_short",
    mismatch: "rg_err_mismatch", fmt: "rg_err_userfmt"
  };
  var code = new URLSearchParams(location.search).get("err") || "";
  if (code && ERR[code]) {
    var em = document.getElementById("errmsg");
    em.textContent = FLX.t(ERR[code]);
    em.hidden = false;
  }

  /* --- Turnstile (if present) --- */
  var widgetId = null;
  if (sitekey) {
    var s = document.createElement("script");
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    s.async = true;
    s.defer = true;
    s.onload = function () {
      try {
        widgetId = window.turnstile.render(box, {
          sitekey: sitekey,
          theme: "dark"
        });
      } catch (e) { /* widget could not load; the server verifies anyway */ }
    };
    document.head.appendChild(s);
  }

  form.addEventListener("submit", function (ev) {
    if (!sitekey) { return; }
    var token = "";
    try {
      token = window.turnstile.getResponse(widgetId) || "";
    } catch (e) { token = ""; }
    if (!token) {
      ev.preventDefault();
      var em = document.getElementById("errmsg");
      em.textContent = FLX.t("lg_err_ts");
      em.hidden = false;
      return;
    }
    var input = form.querySelector("input[name='cf-turnstile-response']");
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "cf-turnstile-response";
      form.appendChild(input);
    }
    input.value = token;
  });
}());
