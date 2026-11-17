/* 值班台前端脚本（任务 id 50）
 *
 * **为什么有这一小段脚本**
 *
 * 值班台是服务端渲染的（检验科值班机上装个 Python 就能跑，不引前端构建链），
 * 但有两件事纯服务端做不了：
 *
 *   1. **写操作反馈要能自己走掉** —— toast 不能一直挂在那儿挡着表格；
 *   2. **破坏性操作要二次确认** —— 转人工与补写回写都得让人「再想一下」，
 *      而且**不能用浏览器原生 confirm**：原生弹窗会把人从处置卡上拉走，
 *      也拦不住随手点掉。
 *
 * **三原则**
 *
 *   * **页面没有 JS 也能用**：二次确认只是**多一道**确认，少了它表单照常
 *     提交，服务端仍然强制校验原因与状态跃迁 —— 安全边界在服务端，不在
 *     这段脚本里；
 *   * **不在浏览器端拼业务 HTML**：刷新台账取回的是**服务端渲染好的
 *     片段**，替换进去的与首屏那张表出自同一个渲染函数；
 *   * **不引依赖**：原生 JS，没有框架，没有构建。
 */
(function () {
  "use strict";

  /* ---------------------------------------------------------------------
   * toast：自动消失 + 手动关掉
   * ------------------------------------------------------------------- */

  function dismissToast(node) {
    if (!node) {
      return;
    }
    node.style.opacity = "0";
    node.style.transform = "translateY(4px)";
    window.setTimeout(function () {
      if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    }, 200);
  }

  function initToasts() {
    var toasts = document.querySelectorAll(".cv-toast");
    Array.prototype.forEach.call(toasts, function (toast) {
      var close = toast.querySelector("[data-cv-toast-close]");
      if (close) {
        close.addEventListener("click", function () {
          dismissToast(toast);
        });
      }
      /* 失败类的提示留久一点 —— 它要被人读完再走 */
      var kind = toast.getAttribute("data-cv-feedback") || "";
      var delay =
        kind === "notify_failed" || kind === "writeback_failed" ? 12000 : 6000;
      window.setTimeout(function () {
        dismissToast(toast);
      }, delay);
    });
  }

  /* ---------------------------------------------------------------------
   * 破坏性操作二次确认（站内模态框）
   * ------------------------------------------------------------------- */

  var modal = null;
  var pendingForm = null;

  function modalPart(selector) {
    return modal ? modal.querySelector(selector) : null;
  }

  function openModal(form) {
    if (!modal) {
      return false;
    }
    pendingForm = form;
    var title = form.getAttribute("data-cv-confirm-title") || "确认操作";
    var message = form.getAttribute("data-cv-confirm") || "确认执行这个操作？";
    var label = form.getAttribute("data-cv-confirm-label") || "确认";
    var variant = form.getAttribute("data-cv-confirm-variant") || "danger";

    var titleNode = modalPart("[data-cv-modal-title]");
    var messageNode = modalPart("[data-cv-modal-message]");
    var reasonNode = modalPart("[data-cv-modal-reason]");
    var errorNode = modalPart("[data-cv-modal-error]");
    var confirmNode = modalPart("[data-cv-modal-confirm]");

    if (titleNode) {
      titleNode.textContent = title;
    }
    if (messageNode) {
      messageNode.textContent = message;
    }
    if (reasonNode) {
      var field = form.querySelector("[name=reason]");
      reasonNode.value = field ? field.value || "" : "";
      reasonNode.focus();
    }
    if (errorNode) {
      errorNode.hidden = true;
    }
    if (confirmNode) {
      confirmNode.textContent = label;
      confirmNode.className = "cv-btn cv-btn--" + variant;
    }
    modal.hidden = false;
    document.body.classList.add("cv-modal-open");
    return true;
  }

  function closeModal() {
    if (!modal) {
      return;
    }
    modal.hidden = true;
    document.body.classList.remove("cv-modal-open");
    pendingForm = null;
  }

  function initConfirmGuard() {
    var forms = document.querySelectorAll("form[data-cv-confirm]");
    Array.prototype.forEach.call(forms, function (form) {
      form.addEventListener("submit", function (event) {
        if (form.getAttribute("data-cv-confirmed") === "1") {
          return;
        }
        event.preventDefault();
        if (!openModal(form)) {
          /* 没有模态框（模板缺失）→ 退回原生提交，服务端照样校验 */
          form.removeAttribute("data-cv-confirmed");
          form.submit();
        }
      });
    });
  }

  function initModal() {
    modal = document.querySelector("[data-cv-modal]");
    if (!modal) {
      return;
    }
    var confirmNode = modalPart("[data-cv-modal-confirm]");
    if (confirmNode) {
      confirmNode.addEventListener("click", function () {
        if (!pendingForm) {
          closeModal();
          return;
        }
        var reasonNode = modalPart("[data-cv-modal-reason]");
        var errorNode = modalPart("[data-cv-modal-error]");
        var text = reasonNode ? (reasonNode.value || "").trim() : "";
        if (!text) {
          if (errorNode) {
            errorNode.hidden = false;
          }
          if (reasonNode) {
            reasonNode.focus();
          }
          return;
        }
        var field = pendingForm.querySelector("[name=reason]");
        if (field) {
          field.value = text;
        }
        var form = pendingForm;
        closeModal();
        form.setAttribute("data-cv-confirmed", "1");
        /* 原因已由模态框校验过（空的话根本走不到这里）——
           关掉浏览器的原生校验，免得它拿必填项再拦一道，
           拦在服务端之前、却给不出「为什么」之外的信息。 */
        form.noValidate = true;
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      });
    }
    var closers = modal.querySelectorAll("[data-cv-modal-close]");
    Array.prototype.forEach.call(closers, function (node) {
      node.addEventListener("click", closeModal);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modal && !modal.hidden) {
        closeModal();
      }
    });
  }

  /* ---------------------------------------------------------------------
   * 折叠导航（768 档：侧边导航收起成可展开的面板）
   * ------------------------------------------------------------------- */

  function initNav() {
    var toggle = document.querySelector("[data-cv-nav-toggle]");
    var nav = document.querySelector("#cv-sidenav");
    if (!toggle || !nav) {
      return;
    }
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  /* ---------------------------------------------------------------------
   * 刷新台账：先摆骨架（加载态），再换真数据
   * ------------------------------------------------------------------- */

  function refreshFailedHtml() {
    return (
      '<div class="cv-state cv-state--error" data-cv-state="error" role="alert">' +
      '<div class="cv-state__head"><div class="cv-state__headtext">' +
      '<h2 class="cv-state__title">刷新失败</h2>' +
      '<p class="cv-state__desc">台账没能刷出来（网络或服务不可达）。</p>' +
      "</div></div>" +
      '<div class="cv-state__advice"><span class="cv-state__advicelabel">怎么办</span>' +
      '<p class="cv-state__advicetext">重新打开台账页，或让信息科查守护进程是否还在跑。</p>' +
      "</div>" +
      '<div class="cv-state__actions"><a class="cv-btn cv-btn--primary" href="/events">重新加载台账</a></div>' +
      "</div>"
    );
  }

  function initRefresh() {
    var buttons = document.querySelectorAll("[data-cv-refresh]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var region = document.querySelector("[data-cv-ledger-region]");
        if (!region) {
          return;
        }
        var skeleton = document.querySelector("[data-cv-ledger-skeleton]");
        if (skeleton && skeleton.content) {
          region.innerHTML = skeleton.content.cloneNode(true);
        }
        button.setAttribute("disabled", "disabled");
        button.classList.add("is-loading");

        var url = new URL(window.location.href);
        url.searchParams.set("fragment", "region");
        url.searchParams.delete("bootstrap");

        window
          .fetch(url.toString(), {
            credentials: "same-origin",
            headers: { "X-Requested-With": "cv-console" },
          })
          .then(function (response) {
            if (!response.ok) {
              throw new Error("HTTP " + response.status);
            }
            return response.text();
          })
          .then(function (html) {
            region.innerHTML = html;
          })
          .catch(function () {
            region.innerHTML = refreshFailedHtml();
          })
          .then(function () {
            button.removeAttribute("disabled");
            button.classList.remove("is-loading");
          });
      });
    });
  }

  function init() {
    initToasts();
    initModal();
    initConfirmGuard();
    initNav();
    initRefresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
