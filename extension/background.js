console.log("[SafeScholar] Background service worker started.");

chrome.runtime.onInstalled.addListener(() => {
    console.log("[SafeScholar] Extension installed successfully.");
});