"use strict";
document.querySelector("#download").addEventListener("submit", async event => {
  event.preventDefault();
  const status = document.querySelector("#status");
  const button = document.querySelector("button");
  button.disabled = true;
  status.textContent = "正在连接桌面程序…";
  try {
    const result = await chrome.runtime.sendMessage({action: "add", url: document.querySelector("#url").value.trim()});
    status.textContent = result?.ok ? "已发送，请在 FlowGet 中查看。" : "未发送：" + (result?.error || "请检查本机桥接注册");
  } catch (error) {
    status.textContent = "未发送：" + error.message;
  } finally {
    button.disabled = false;
  }
});
