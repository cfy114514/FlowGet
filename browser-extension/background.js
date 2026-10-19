"use strict";
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({id: "flowget-link", title: "用 FlowGet 下载此链接", contexts: ["link"], targetUrlPatterns: ["http://*/*", "https://*/*"]});
});

async function send(url) {
  const parsed = new URL(url);
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error("仅支持不含内嵌密码的 HTTP / HTTPS 下载地址");
  }
  const result = await chrome.runtime.sendNativeMessage("com.flowget.bridge", {action: "add", url: parsed.href});
  if (!result?.ok) throw new Error(result?.error || "桌面程序没有接受任务");
  return result;
}

chrome.contextMenus.onClicked.addListener(async info => {
  if (info.menuItemId !== "flowget-link") return;
  try {
    await send(info.linkUrl);
    await chrome.action.setBadgeText({text: "OK"});
    await chrome.action.setBadgeBackgroundColor({color: "#287e6e"});
  } catch (error) {
    await chrome.action.setBadgeText({text: "!"});
    await chrome.action.setTitle({title: "FlowGet：" + error.message});
  }
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || message?.action !== "add") return false;
  send(message.url).then(result => respond(result), error => respond({ok: false, error: error.message}));
  return true;
});
