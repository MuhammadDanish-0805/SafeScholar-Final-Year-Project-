// content.js
// SafeScholar - Content Script with Popup Modal

console.log("=====================================");
console.log("SAFESCHOLAR SCRIPT v3 - PIN PATCH LOADED");
console.log("URL:", window.location.href);
console.log("=====================================");

// ============================================================
// CONFIGURATION
// ============================================================

// Dashboard backend (run: uvicorn main:app --host 0.0.0.0 --port 8000)
// On the Raspberry Pi, replace 127.0.0.1 with the Pi's IP address
// and add that address to host_permissions in manifest.json.
const API_BASE = "http://127.0.0.1:8000";
const API_URL = `${API_BASE}/incidents/analyze`;
let latestCriticalIncidentId = null;
const SESSION_ID = crypto.randomUUID();   // one ID per page load
const RESTRICTION_KEY = "safescholar_rejected_restriction_v1";
let accessRestricted = false;
let activeRestriction = null;
// Prevent duplicate detection requests for the same prompt
let lastCheckedPrompt = "";
let lastCheckedTime = 0;

function shouldCheckPrompt(prompt) {
    const now = Date.now();
    const normalizedPrompt = prompt.trim();

    if (
        normalizedPrompt === lastCheckedPrompt &&
        now - lastCheckedTime < 2000
    ) {
        console.log("[SafeScholar] Duplicate detection skipped.");
        return false;
    }

    lastCheckedPrompt = normalizedPrompt;
    lastCheckedTime = now;

    return true;
}

// ============================================================
// DETECT PLATFORM
// ============================================================

function getPlatform() {
    const host = window.location.hostname;
    if (host.includes("openai") || host.includes("chatgpt")) return "ChatGPT";
    if (host.includes("gemini")) return "Gemini";
    return "Unknown";
}

const PLATFORM = getPlatform();
console.log(`[SafeScholar] Running on: ${PLATFORM}`);

// ============================================================
// FIND INPUT BOX
// ============================================================

const SEND_BUTTON_SELECTOR =
    'button[data-testid="send-button"], button[aria-label="Send prompt"], ' +
    'button[aria-label="Send message"], button[aria-label="Send"], ' +
    'button[aria-label="Submit"], button.send-button, button[mattooltip="Send message"]';

// The composer is the box that holds the text input AND the send button, plus
// two levels above it (sites put attachment previews in a sibling wrapper).
// Chat history sits far outside this, so earlier screenshots are not picked up.
function findComposerRoot() {
    const input = findInputBox();
    if (!input) return null;
    let el = input;
    for (let i = 0; i < 10 && el.parentElement; i++) {
        el = el.parentElement;
        if (el.querySelector(SEND_BUTTON_SELECTOR)) {
            let root = el;
            for (let j = 0; j < 2 && root.parentElement && root.parentElement !== document.body; j++) {
                root = root.parentElement;
            }
            return root;
        }
    }
    return null;
}

function findInputBox() {
    let el = document.querySelector('#prompt-textarea');
    if (el) return el;
    el = document.querySelector('.ProseMirror');
    if (el) return el;
    el = document.querySelector('div[contenteditable="true"]');
    if (el) return el;
    return document.querySelector('textarea');
}

// Find image files selected in the AI chatbot
function findAttachedImages() {
    const images = [];

    // Detect images still held in file inputs.
    document.querySelectorAll('input[type="file"]').forEach(input => {
        Array.from(input.files || []).forEach(file => {
            if (file.type.startsWith("image/")) {
                images.push(file);
            }
        });
    });

    // Detect ChatGPT attachment previews.
    document.querySelectorAll('form img').forEach(img => {
        if (
            img.currentSrc.startsWith("data:image/") &&
            img.naturalWidth > 0 &&
            img.naturalHeight > 0
        ) {
            images.push({
                filename: img.alt || "attachment.png",
                src: img.currentSrc,
                width: img.naturalWidth,
                height: img.naturalHeight
            });
        }
    });

    console.log("[SafeScholar] File inputs:",
        document.querySelectorAll('input[type="file"]').length
    );

    console.log("[SafeScholar] Composer images:",
        document.querySelectorAll('form img').length
    );

    console.log("[SafeScholar] Detected attachments:",
        images.length
);

    return images;
}

function findComposerImagePreviews() {
    // ChatGPT keeps previews inside a <form>; Gemini does not, so also search the composer box.
    const found = new Set(document.querySelectorAll("form img"));
    const root = findComposerRoot();
    if (root) root.querySelectorAll("img").forEach(img => found.add(img));

    const seen = new Set();
    return Array.from(found)
        .filter(img => {
            const src = img.currentSrc || "";
            const isAttachmentSrc =
                (src.startsWith("data:image/") && !src.startsWith("data:image/svg")) ||
                src.startsWith("blob:");
            return isAttachmentSrc &&
                img.naturalWidth >= 32 &&
                img.naturalHeight >= 32;
        })
        .map(img => ({
            filename: img.alt || "uploaded-image.png",
            dataUrl: img.currentSrc,
            width: img.naturalWidth,
            height: img.naturalHeight,
            el: img
        }))
        .filter(p => !seen.has(p.dataUrl) && seen.add(p.dataUrl));
}

async function composerPreviewToFile(preview) {
    const response = await fetch(preview.dataUrl);
    const blob = await response.blob();

    return new File(
        [blob],
        preview.filename || "uploaded-image.png",
        { type: blob.type || "image/png" }
    );
}

async function testComposerImageConversion() {
    const previews = findComposerImagePreviews();

    if (previews.length === 0) {
        console.log("[SafeScholar] No image attached.");
        return;
    }

    try {
        const file = await composerPreviewToFile(previews[0]);

        console.log("[SafeScholar] Image conversion successful!", {
            name: file.name,
            type: file.type,
            size: file.size,
            isFile: file instanceof File
        });
    } catch (error) {
        console.error("[SafeScholar] Image conversion failed:", error);
    }
}

document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.altKey && event.code === "KeyI") {
        testComposerImageConversion();
    }
});

// Detect changes to image attachments in the ChatGPT composer
let lastComposerImageSignature = "";

function checkComposerImages() {
    const images = findComposerImagePreviews();

    const signature = images
        .map(img => `${img.filename}:${img.dataUrl.length}`)
        .join("|");

    if (signature === lastComposerImageSignature) return;

    lastComposerImageSignature = signature;

    console.log("[SafeScholar] Images detected:", images.length);

    images.forEach(img => {
        console.log("[SafeScholar] Attachment:", {
            filename: img.filename,
            width: img.width,
            height: img.height
        });
    });
}

const composerImageObserver = new MutationObserver(() => {
    checkComposerImages();
});

composerImageObserver.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["src", "alt"]
});

checkComposerImages();

console.log(
    "[SafeScholar] Composer image previews:",
    findComposerImagePreviews().map(img => ({
        filename: img.filename,
        width: img.width,
        height: img.height
    }))
);

function getPromptText(inputBox) {
    if (!inputBox) return "";
    if (inputBox.tagName === "TEXTAREA") return inputBox.value.trim();
    if (inputBox.getAttribute("contenteditable") === "true") {
        return inputBox.innerText.trim();
    }
    return "";
}

// ============================================================
// SEND TO BACKEND
// ============================================================

async function checkPromptWithBackend(prompt) {
    try {
        const response = await fetch(API_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                prompt: prompt,
                session_id: SESSION_ID,
                device_label: `${PLATFORM} - Chrome`
            })
        });

        if (!response.ok) return null;

        const data = await response.json();
        console.log("[SafeScholar] Detection result:", data);

        // Convert the dashboard's reply into the shape the popups expect
        return {
            incident_id: data.incident_id,
            severity: data.severity === "none" ? "safe" : data.severity,
            score: data.score,
            message: data.ai_reason || (data.steps && data.steps[0]) || "Your prompt was flagged.",
            blocked: data.blocked
        };

    } catch (error) {
        console.error("[SafeScholar] Backend error:", error);
        return null;
    }
}

// ============================================================
// PDF / WORD DOCUMENTS
// ============================================================
// A chat site never shows us the content of an attached file, so we remember the
// File objects at the moment the student picks, drops or pastes them.
const MAX_DOCS_PER_SUBMISSION = 3;
const capturedDocs = new Map(); // "name|size|modified" -> File

function isReviewableDocument(file) {
    return (
        /\.(pdf|docx)$/i.test(file.name || "") ||
        file.type === "application/pdf" ||
        file.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    );
}

function rememberDocuments(files, via) {
    Array.from(files || []).forEach(file => {
        if (!file || !isReviewableDocument(file)) return;
        const key = [file.name, file.size, file.lastModified].join("|");
        if (!capturedDocs.has(key)) {
            capturedDocs.set(key, file);
            console.log("[SafeScholar] Document captured (" + via + "):", file.name, file.size, "bytes");
        }
    });
}

document.addEventListener("change", event => {
    const target = event.target;
    if (target && target.tagName === "INPUT" && target.type === "file") {
        rememberDocuments(target.files, "file input");
    }
}, true);
document.addEventListener("drop", event => {
    rememberDocuments(event.dataTransfer && event.dataTransfer.files, "drop");
}, true);
document.addEventListener("paste", event => {
    rememberDocuments(event.clipboardData && event.clipboardData.files, "paste");
}, true);

// Files picked through inputs that are not in the page are reported by page-hook.js
window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    if (!event.data || event.data.__safescholar !== true) return;
    rememberDocuments(event.data.files, "page hook: " + event.data.via);
});

// Some sites shorten long file names in the middle (for example "SPG 0562 A…MENT 1 (1)").
// So besides the full name, accept a match on the start and the end of the name.
function fileNameShown(text, fileName) {
    const name = fileName.toLowerCase();
    const base = name.replace(/\.[^.]+$/, "");
    if (text.includes(name) || text.includes(base)) return true;
    if (base.length >= 8) {
        return text.includes(base.slice(0, 6)) && text.includes(base.slice(-6));
    }
    return base.length >= 4 && text.includes(base);
}

// Only documents whose name is still shown in the composer count as attached, so a
// file the student picked and then removed is not uploaded.
function findAttachedDocuments() {
    const all = Array.from(capturedDocs.values());
    if (all.length === 0) return [];

    const root = findComposerRoot();
    if (!root) return all.slice(0, MAX_DOCS_PER_SUBMISSION);

    const text = (root.innerText || "").toLowerCase();
    const attached = all.filter(file => fileNameShown(text, file.name));
    console.log("[SafeScholar] Documents captured:", all.length, "| shown in composer:", attached.length);
    return attached.slice(0, MAX_DOCS_PER_SUBMISSION);
}

async function checkDocumentWithBackend(file, prompt = "") {
  try {
    const formData = new FormData();

    formData.append("file", file, file.name);
    formData.append("prompt", prompt);
    formData.append("session_id", SESSION_ID);
    formData.append("device_label", `${PLATFORM} - Chrome`);

    const response = await fetch(
      `${API_BASE}/incidents/analyze-document`,
      {
        method: "POST",
        body: formData
      }
    );

    if (!response.ok) {
      console.error("[SafeScholar] Document analysis failed:", response.status);
      return null;
    }

    const data = await response.json();

    console.log("[SafeScholar] Document detection result:", data);

    return {
      incident_id: data.incident_id,
      severity: data.severity === "none" ? "safe" : data.severity,
      score: data.score,
      message: data.reason || "Document analyzed by SafeScholar.",
      blocked: data.action === "block"
    };

  } catch (error) {
    console.error("[SafeScholar] Document analysis error:", error);
    return null;
  }
}

async function checkImageWithBackend(file, prompt = "") {
  try {
    const formData = new FormData();

    formData.append("file", file, file.name);
    formData.append("prompt", prompt);
    formData.append("session_id", SESSION_ID);
    formData.append("device_label", `${PLATFORM} - Chrome`);

    const response = await fetch(
      `${API_BASE}/incidents/analyze-image`,
      {
        method: "POST",
        body: formData
      }
    );

    if (!response.ok) {
      console.error(
        "[SafeScholar] Image analysis failed:",
        response.status
      );
      return null;
    }

    const data = await response.json();

    console.log("[SafeScholar] Image detection result:", data);

    return {
      incident_id: data.incident_id,
      severity: data.severity === "none" ? "safe" : data.severity,
      score: data.score,
      message: data.reason || "Image analyzed by SafeScholar.",
      blocked: data.action === "block"
    };

  } catch (error) {
    console.error("[SafeScholar] Image analysis error:", error);
    return null;
  }
}

// ============================================================
// POPUP: SIMPLE WARNING (Low / Medium / High)
// ============================================================

function showSimpleWarning(severity, score, message) {
    removeExistingPopup();

    const colors = {
        low:    { bg: "#f59e0b", name: "LOW" },
        medium: { bg: "#f97316", name: "MEDIUM" },
        high:   { bg: "#ef4444", name: "HIGH" }
    };

    const config = colors[severity] || colors.medium;

    // Overlay
    const overlay = document.createElement("div");
    overlay.id = "safescholar-overlay";
    overlay.style.cssText = `
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.6);
        z-index: 2147483647;
        display: flex;
        align-items: center;
        justify-content: center;
        font-family: -apple-system, sans-serif;
        animation: safescholar-fade-in 0.25s ease-out;
    `;

    // Popup box
    overlay.innerHTML = `
        <div style="
            background: white;
            border-radius: 16px;
            padding: 32px 28px;
            max-width: 420px;
            width: 90%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0,0,0,0.4);
            animation: safescholar-pop-in 0.3s cubic-bezier(0.16,1,0.3,1);
        ">
            <div style="
                width: 64px;
                height: 64px;
                border-radius: 50%;
                background: ${config.bg};
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto 16px;
                font-size: 32px;
            ">⚠️</div>

            <h2 style="
                margin: 0 0 8px 0;
                font-size: 20px;
                color: #111;
                font-weight: 700;
            ">SafeScholar Alert</h2>

            <div style="
                display: inline-block;
                background: ${config.bg};
                color: white;
                font-size: 11px;
                font-weight: 700;
                padding: 4px 12px;
                border-radius: 12px;
                margin-bottom: 16px;
                letter-spacing: 0.5px;
            ">${config.name} RISK</div>

            <p style="
                color: #555;
                font-size: 14px;
                line-height: 1.6;
                margin: 0 0 20px 0;
            ">${message}</p>

            <div style="
                background: #f9fafb;
                border-radius: 10px;
                padding: 12px 16px;
                margin-bottom: 20px;
                font-size: 13px;
                color: #666;
            ">
                <strong style="color: #111;">Risk Score:</strong> ${score}/10
            </div>

            <button id="safescholar-ok-btn" style="
                background: #111;
                color: white;
                border: none;
                padding: 12px 32px;
                border-radius: 10px;
                font-size: 15px;
                font-weight: 600;
                cursor: pointer;
                width: 100%;
                font-family: inherit;
                transition: background 0.2s;
            ">OK</button>
        </div>
    `;

    injectStyles();
    document.body.appendChild(overlay);

    // OK button
    document.getElementById("safescholar-ok-btn").addEventListener("click", () => {
        overlay.remove();
        console.log("[SafeScholar] Warning dismissed by student");
    });
}

// ============================================================
// POPUP: CRITICAL ALERT (with Appeal button)
// ============================================================

function showCriticalPopup(severity, score, message) {
    removeExistingPopup();

    // Overlay
    const overlay = document.createElement("div");
    overlay.id = "safescholar-overlay";
    overlay.style.cssText = `
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.75);
        z-index: 2147483647;
        display: flex;
        align-items: center;
        justify-content: center;
        font-family: -apple-system, sans-serif;
        animation: safescholar-fade-in 0.25s ease-out;
    `;

    overlay.innerHTML = `
        <div style="
            background: white;
            border-radius: 16px;
            padding: 32px 28px;
            max-width: 460px;
            width: 90%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            border-top: 6px solid #7f1d1d;
            animation: safescholar-pop-in 0.3s cubic-bezier(0.16,1,0.3,1);
        ">
            <div style="
                width: 72px;
                height: 72px;
                border-radius: 50%;
                background: #7f1d1d;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto 16px;
                font-size: 36px;
                animation: safescholar-pulse 1.2s ease-in-out infinite;
            ">🚨</div>

            <h2 style="
                margin: 0 0 8px 0;
                font-size: 22px;
                color: #7f1d1d;
                font-weight: 800;
                letter-spacing: 0.5px;
            ">CRITICAL ALERT</h2>

            <div style="
                display: inline-block;
                background: #7f1d1d;
                color: white;
                font-size: 11px;
                font-weight: 700;
                padding: 4px 12px;
                border-radius: 12px;
                margin-bottom: 16px;
                letter-spacing: 0.5px;
            ">SEVERE VIOLATION</div>

            <p style="
                color: #555;
                font-size: 14px;
                line-height: 1.6;
                margin: 0 0 20px 0;
            ">${message}</p>

            <div style="
                background: #fef2f2;
                border: 1px solid #fecaca;
                border-radius: 10px;
                padding: 12px 16px;
                margin-bottom: 20px;
                font-size: 13px;
                color: #7f1d1d;
            ">
                <strong>Risk Score:</strong> ${score}/10
            </div>

            <p style="
                color: #888;
                font-size: 12px;
                margin: 0 0 20px 0;
                line-height: 1.5;
            ">
                If you believe this was flagged incorrectly,<br>
                you may submit an appeal to your Lecturer / TTO.
            </p>

            <div style="display: flex; gap: 10px;">
                <button id="safescholar-appeal-btn" style="
                    flex: 2;
                    background: #7f1d1d;
                    color: white;
                    border: none;
                    padding: 12px 20px;
                    border-radius: 10px;
                    font-size: 14px;
                    font-weight: 600;
                    cursor: pointer;
                    font-family: inherit;
                ">Submit Appeal</button>
                
            </div>
        </div>
    `;

    injectStyles();
    document.body.appendChild(overlay);

    // Appeal button
    document.getElementById("safescholar-appeal-btn").addEventListener("click", async () => {
        if (!latestCriticalIncidentId) { alert("Incident ID unavailable. Please retry."); return; }
        const reason = window.prompt("Explain why you are appealing this Critical violation (at least 5 characters):");
        if (reason === null) return;
        if (reason.trim().length < 5) { alert("Please provide a longer reason."); return; }
        const button = document.getElementById("safescholar-appeal-btn");
        button.disabled = true;
        try {
            const response = await fetch(`${API_BASE}/appeals`, {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({incident_id: latestCriticalIncidentId, session_id: SESSION_ID, reason: reason.trim()})
            });
            if (!response.ok) throw new Error(await response.text());
            showAppealLoading();
            watchAppealStatus(latestCriticalIncidentId);
        } catch (error) {
            alert("Unable to submit appeal: " + error.message);
            button.disabled = false;
        }
    });

}

async function activateRestriction(incidentId, sessionId) {
    accessRestricted = true;
    activeRestriction = {incidentId, sessionId};
    await chrome.storage.local.set({[RESTRICTION_KEY]: {
        incidentId, sessionId, rejectedAt: new Date().toISOString()
    }});
    renderRestriction();
}

// SafeScholar logo, shrunk and embedded so it needs no network request and cannot be blocked
// as an outside file. If the page still refuses it, a plain shield icon is shown instead.
const SAFESCHOLAR_LOGO_DATA_URI = "data:image/png;base64,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";
const BRAND_FALLBACK_SHIELD_SVG = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#0F6B63" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6l8-3z"/></svg>';

function renderRestriction() {
    accessRestricted = true;
    removeExistingPopup();
    document.getElementById("safescholar-access-restricted")?.remove();

    const ACCENT = "#0F6B63";
    const digitBox = (n) => `<input id="safescholar-pin-${n}" class="safescholar-pin-box" type="password" inputmode="numeric" maxlength="1" autocomplete="off" aria-label="PIN digit ${n}" style="width:56px;height:64px;box-sizing:border-box;text-align:center;font:800 24px 'Segoe UI',Arial,sans-serif;border:1.5px solid #B9B4A8;border-radius:12px;background:#fff;color:#1B1F2A;padding:0" />`;

    const overlay = document.createElement("div");
    overlay.id = "safescholar-access-restricted";
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "SafeScholar access restricted");
    overlay.style.cssText = "position:fixed!important;inset:0!important;z-index:2147483647!important;background:#F4F2ED!important;color:#1B1F2A!important;display:flex!important;flex-direction:column!important;align-items:center!important;justify-content:center!important;gap:28px!important;overflow:auto!important;padding:24px!important;box-sizing:border-box!important;font-family:'Segoe UI',Arial,sans-serif!important;";
    overlay.innerHTML = `
      <style>
        #safescholar-access-restricted .safescholar-pin-box:focus,
        #safescholar-access-restricted button:focus-visible { outline: 3px solid #9CCFC8; outline-offset: 2px; }
        #safescholar-access-restricted button:disabled { opacity: 0.6; cursor: default; }
      </style>
      <div style="display:flex;align-items:center;gap:12px;font-weight:800;font-size:24px;color:#1B1F2A">
        <img id="safescholar-brand-logo" src="${SAFESCHOLAR_LOGO_DATA_URI}" alt="" style="height:48px;width:auto;display:block" />
        <span>SafeScholar</span>
      </div>
      <div style="width:100%;max-width:900px;box-sizing:border-box;background:#fff;border:1px solid #E3DFD6;border-radius:20px;box-shadow:0 18px 50px rgba(27,31,42,0.08);display:flex;flex-wrap:wrap;overflow:hidden">
        <div style="flex:1.15 1 340px;padding:44px;display:flex;flex-direction:column;gap:18px;box-sizing:border-box">
          <div style="display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:#A32D25">
            <span style="width:9px;height:9px;border-radius:50%;background:#A32D25;display:inline-block"></span>
            Appeal reviewed: access paused
          </div>
          <h1 style="margin:0;font-size:32px;line-height:1.2;font-weight:800;color:#1B1F2A">Your access to this AI platform is paused</h1>
          <p style="margin:0;font-size:16px;line-height:1.6;color:#4B5160">An administrator reviewed your appeal and the decision stands. You can use this chat again once an authorised administrator restores your access.</p>
          <div style="margin-top:6px;border-top:1px solid #ECE9E1;display:flex;flex-direction:column">
            <div style="display:flex;justify-content:space-between;padding:14px 0;border-bottom:1px solid #ECE9E1;font-size:15px"><span style="color:#4B5160">Severity</span><strong>Critical</strong></div>
            <div style="display:flex;justify-content:space-between;padding:14px 0;border-bottom:1px solid #ECE9E1;font-size:15px"><span style="color:#4B5160">Appeal status</span><strong>Rejected</strong></div>
          </div>
        </div>
        <form id="safescholar-unlock-form" style="flex:1 1 300px;padding:44px 36px;background:#F8F7F3;border-left:1px solid #E3DFD6;display:flex;flex-direction:column;justify-content:center;gap:16px;box-sizing:border-box;margin:0">
          <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#1B1F2A" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>
          <label for="safescholar-pin-1" style="font-size:18px;font-weight:800;color:#1B1F2A">Administrator unlock</label>
          <p style="margin:0;font-size:14px;line-height:1.5;color:#4B5160">Ask your administrator to enter the 4-digit PIN. Only they can restore access.</p>
          <div style="display:flex;gap:10px">${digitBox(1)}${digitBox(2)}${digitBox(3)}${digitBox(4)}</div>
          <button type="submit" style="height:52px;border:0;border-radius:12px;background:${ACCENT};color:#fff;font:800 16px 'Segoe UI',Arial,sans-serif;cursor:pointer">Unlock access</button>
          <div id="safescholar-pin-message" role="status" aria-live="polite" style="font-size:13px;color:#4B5160;min-height:18px"></div>
        </form>
      </div>`;
    (document.documentElement || document.body).appendChild(overlay);

    const brandLogo = overlay.querySelector("#safescholar-brand-logo");
    brandLogo.addEventListener("error", () => {
        const holder = document.createElement("span");
        holder.innerHTML = BRAND_FALLBACK_SHIELD_SVG;
        brandLogo.replaceWith(holder.firstElementChild);
    });

    const boxes = Array.from(overlay.querySelectorAll(".safescholar-pin-box"));
    const message = overlay.querySelector("#safescholar-pin-message");
    const button = overlay.querySelector('button[type="submit"]');

    // Four boxes behave like one PIN field: digits only, auto-advance, backspace goes back, paste fills all.
    boxes.forEach((box, i) => {
        box.addEventListener("input", () => {
            box.value = box.value.replace(/\D/g, "").slice(-1);
            if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
        });
        box.addEventListener("keydown", (event) => {
            if (event.key === "Backspace" && !box.value && i > 0) {
                event.preventDefault();
                boxes[i - 1].value = "";
                boxes[i - 1].focus();
            } else if (event.key === "ArrowLeft" && i > 0) {
                event.preventDefault();
                boxes[i - 1].focus();
            } else if (event.key === "ArrowRight" && i < boxes.length - 1) {
                event.preventDefault();
                boxes[i + 1].focus();
            }
        });
        box.addEventListener("paste", (event) => {
            const digits = ((event.clipboardData && event.clipboardData.getData("text")) || "")
                .replace(/\D/g, "").slice(0, boxes.length);
            if (!digits) return;
            event.preventDefault();
            digits.split("").forEach((digit, j) => { boxes[j].value = digit; });
            boxes[Math.min(digits.length, boxes.length - 1)].focus();
        });
    });

    boxes[0].focus();
    setTimeout(() => { if (document.activeElement !== boxes[0] && !boxes.includes(document.activeElement)) boxes[0].focus(); }, 80);

    overlay.querySelector("#safescholar-unlock-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const pin = boxes.map(box => box.value).join("");
        if (!activeRestriction) return;
        if (!/^[0-9]{4}$/.test(pin)) {
            message.style.color = "#A32D25";
            message.textContent = "Enter all 4 digits of the PIN.";
            return;
        }
        button.disabled = true;
        boxes.forEach(box => { box.disabled = true; });
        message.style.color = "#4B5160";
        message.textContent = "Verifying with SafeScholar server...";
        try {
            const response = await fetch(`${API_BASE}/appeals/unlock`, {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({incident_id: activeRestriction.incidentId, session_id: activeRestriction.sessionId, pin})
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || "Unlock failed");
            }
            await chrome.storage.local.remove(RESTRICTION_KEY);
            activeRestriction = null;
            accessRestricted = false;
            overlay.remove();
            window.location.reload();
        } catch (error) {
            message.style.color = "#A32D25";
            message.textContent = error.message || "Cannot reach the SafeScholar server";
            boxes.forEach(box => { box.disabled = false; box.value = ""; });
            button.disabled = false;
            boxes[0].focus();
        }
    });
}

async function restoreSavedRestriction() {
    try {
        const saved = (await chrome.storage.local.get(RESTRICTION_KEY))[RESTRICTION_KEY];
        if (saved?.incidentId && saved?.sessionId) {
            // Keep a rejected restriction in force even if the backend is temporarily offline.
            activeRestriction = {incidentId: saved.incidentId, sessionId: saved.sessionId};
            renderRestriction();
        }
    } catch (error) { console.warn("SafeScholar restriction restore failed", error); }
}

// Restore on every page load, and re-apply if a single-page app replaces the overlay.
restoreSavedRestriction();
setInterval(() => {
    if (accessRestricted && !document.getElementById("safescholar-access-restricted")) renderRestriction();
}, 1000);

async function watchAppealStatus(incidentId) {
    const timer = setInterval(async () => {
        try {
            const res = await fetch(`${API_BASE}/appeals/${incidentId}/status?session_id=${encodeURIComponent(SESSION_ID)}`);
            if (!res.ok) return;
            const data = await res.json();
            if (data.status === "approved" || data.status === "rejected") {
                clearInterval(timer);
                if (data.status === "rejected") {
                    await activateRestriction(incidentId, SESSION_ID);
                } else {
                    removeExistingPopup();
                    alert("SafeScholar appeal approved.");
                }
            }
        } catch (error) { console.warn("Appeal status check failed", error); }
    }, 5000);
}

// ============================================================
// POPUP: APPEAL LOADING (Waiting for approval)
// ============================================================

function showAppealLoading() {
    const overlay = document.getElementById("safescholar-overlay");
    if (!overlay) return;

    overlay.innerHTML = `
        <div style="
            background: white;
            border-radius: 16px;
            padding: 40px 32px;
            max-width: 400px;
            width: 90%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            animation: safescholar-pop-in 0.3s cubic-bezier(0.16,1,0.3,1);
        ">
            <!-- Spinner -->
            <div style="
                width: 64px;
                height: 64px;
                border: 5px solid #f3f4f6;
                border-top: 5px solid #7f1d1d;
                border-radius: 50%;
                margin: 0 auto 24px;
                animation: safescholar-spin 0.9s linear infinite;
            "></div>

            <h2 style="
                margin: 0 0 12px 0;
                font-size: 20px;
                color: #111;
                font-weight: 700;
            ">Appeal Submitted</h2>

            <p style="
                color: #555;
                font-size: 14px;
                line-height: 1.6;
                margin: 0 0 20px 0;
            ">
                Your appeal has been sent to your<br>
                <strong>Lecturer / TO</strong> for review.
            </p>

            <div style="
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: #fef3c7;
                color: #92400e;
                padding: 10px 18px;
                border-radius: 10px;
                font-size: 13px;
                font-weight: 600;
            ">
                <span>⏳</span>
                <span>Waiting for approval<span id="safescholar-dots"></span></span>
            </div>

            <p style="
                color: #999;
                font-size: 12px;
                margin: 20px 0 0 0;
                line-height: 1.5;
            ">
                Please do not close this window.<br>
                You will be notified when a decision is made.
            </p>
        </div>
    `;

    // Animate dots
    let dotCount = 0;
    const dotsEl = document.getElementById("safescholar-dots");
    setInterval(() => {
        if (!dotsEl) return;
        dotCount = (dotCount + 1) % 4;
        dotsEl.textContent = ".".repeat(dotCount);
    }, 500);

    console.log("[SafeScholar] Appeal submitted - waiting for Lecturer/TO");
}

// ============================================================
// HELPERS
// ============================================================

function removeExistingPopup() {
    const existing = document.getElementById("safescholar-overlay");
    if (existing) existing.remove();
}

let stylesInjected = false;
function injectStyles() {
    if (stylesInjected) return;
    stylesInjected = true;

    const style = document.createElement("style");
    style.id = "safescholar-styles";
    style.textContent = `
        @keyframes safescholar-fade-in {
            from { opacity: 0; }
            to   { opacity: 1; }
        }
        @keyframes safescholar-pop-in {
            from { opacity: 0; transform: scale(0.9); }
            to   { opacity: 1; transform: scale(1); }
        }
        @keyframes safescholar-spin {
            0%   { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        @keyframes safescholar-pulse {
            0%, 100% { transform: scale(1); }
            50%      { transform: scale(1.1); }
        }
    `;
    document.head.appendChild(style);
}


// A PDF/DOCX chip in the composer can contain an icon or preview picture.
// That is not a screenshot the student attached, so it must not go to the image check.
function isDocumentThumbnail(img, docs) {
    if (!img || docs.length === 0) return false;
    const input = findInputBox();
    let el = img;
    for (let i = 0; i < 8 && el.parentElement; i++) {
        el = el.parentElement;
        // Stop once we reach the whole composer (it holds the input or the send button)
        if ((input && el.contains(input)) || el.querySelector(SEND_BUTTON_SELECTOR)) break;
        const text = (el.innerText || el.textContent || "").toLowerCase();
        if (docs.some(d => fileNameShown(text, d.name))) {
            console.log("[SafeScholar] Skipped document thumbnail:", img.alt || "(no alt)");
            return true;
        }
    }
    return false;
}

// ============================================================
// PRE-SUBMISSION GATE: cancel the original browser event BEFORE
// waiting for the asynchronous classification response.
// A safe prompt requires the student to press Send once more.
// This avoids unreliable synthetic clicks and duplicate submissions.
// ============================================================
let pendingReview = false;
let approvedPrompt = null;

const SEVERITY_RANK = { safe: 0, none: 0, low: 1, medium: 2, high: 3, critical: 4 };
const MAX_IMAGES_PER_SUBMISSION = 4;

function rankOf(result) {
    return SEVERITY_RANK[String(result.severity || "").toLowerCase()] ?? 2;
}

// Identifies "this exact prompt + these exact attachments" so an approved
// submission can be sent on the second Send press.
function submissionKey(prompt, previews, docs) {
    return prompt + "\u0000" + previews.map(p => p.dataUrl).join("|") +
        "\u0000" + docs.map(d => d.name + ":" + d.size).join("|");
}

// Checks the typed prompt (if any) and every attached screenshot, and returns
// the most severe result. Returns null if any check could not be completed.
async function reviewSubmission(prompt, previews, docs) {
    const results = [];

    // With an attachment, the typed prompt is judged together with the file or image
    // (Qwen receives it as context), so we do not log a second, separate incident for it.
    // Text-only submissions are still checked on their own.
    const hasAttachments = previews.length > 0 || docs.length > 0;

    if (prompt && !hasAttachments) {
        results.push(await checkPromptWithBackend(prompt));
    }

    for (const preview of previews.slice(0, MAX_IMAGES_PER_SUBMISSION)) {
        const file = await composerPreviewToFile(preview);
        results.push(await checkImageWithBackend(file, prompt));
    }

    for (const doc of docs) {
        results.push(await checkDocumentWithBackend(doc, prompt));
    }

    if (results.length === 0 || results.some(r => !r)) return null;

    return results.reduce((worst, r) => (rankOf(r) > rankOf(worst) ? r : worst));
}

// ============================================================
// "CHECKING" BANNER
// ============================================================
// Qwen can take a minute on a CPU. Without a message the student (or an examiner)
// thinks nothing is happening and keeps pressing Send.
const CHECKING_BANNER_ID = "safescholar-checking-banner";

function showCheckingBanner(hasAttachments) {
    hideCheckingBanner();

    if (!document.getElementById("safescholar-checking-style")) {
        const style = document.createElement("style");
        style.id = "safescholar-checking-style";
        style.textContent =
            "@keyframes safescholar-spin { to { transform: rotate(360deg); } }" +
            "@keyframes safescholar-nudge { 0%,100% { transform: translateX(-50%) scale(1); } 50% { transform: translateX(-50%) scale(1.06); } }";
        document.head.appendChild(style);
    }

    const banner = document.createElement("div");
    banner.id = CHECKING_BANNER_ID;
    banner.setAttribute("role", "status");
    banner.style.cssText =
        "position:fixed;left:50%;bottom:32px;transform:translateX(-50%);" +
        "z-index:2147483646;background:#111827;color:#fff;padding:12px 18px;" +
        "border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,0.35);" +
        "font:14px -apple-system,'Segoe UI',sans-serif;display:flex;" +
        "align-items:center;gap:12px;max-width:90vw;";

    const spinner = document.createElement("span");
    spinner.style.cssText =
        "width:18px;height:18px;border:3px solid rgba(255,255,255,0.3);" +
        "border-top-color:#fff;border-radius:50%;flex:none;" +
        "animation:safescholar-spin 0.9s linear infinite;";

    const text = document.createElement("span");
    text.textContent = hasAttachments
        ? "SafeScholar is checking your message and attachments. This can take up to a minute, so please don't press Send again."
        : "SafeScholar is checking your message. Please wait.";

    banner.append(spinner, text);
    document.body.appendChild(banner);
}

// Called when Send is pressed again while a check is still running.
function nudgeCheckingBanner() {
    const banner = document.getElementById(CHECKING_BANNER_ID);
    if (!banner) return;
    banner.style.animation = "none";
    void banner.offsetWidth; // restart the animation
    banner.style.animation = "safescholar-nudge 0.4s ease";
}

function hideCheckingBanner() {
    const banner = document.getElementById(CHECKING_BANNER_ID);
    if (banner) banner.remove();
}

function renderReviewResult(result, key) {
    hideCheckingBanner();
    if (!result) {
        approvedPrompt = null;
        alert("SafeScholar could not contact its detection server. The prompt was not sent. Please try again.");
        return;
    }
    const severity = String(result.severity || "").toLowerCase();
    if (severity === "safe" || severity === "none") {
        approvedPrompt = key;
        // A second user-initiated submission is required; never automatically
        // dispatch an untrusted synthetic event into a third-party AI website.
        alert("SafeScholar approved this prompt. Press Send again to submit it to the AI.");
    } else {
        approvedPrompt = null;
        if (severity === "critical") {
            latestCriticalIncidentId = result.incident_id;
            showCriticalPopup(severity, result.score || 0, result.message || "");
        } else {
            showSimpleWarning(severity, result.score || 0, result.message || "");
        }
    }
}

function interceptSubmission(event) {
    if (accessRestricted) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
    }
    const input = findInputBox();
    const prompt = getPromptText(input);
    const docs = findAttachedDocuments();
    const previews = findComposerImagePreviews().filter(p => !isDocumentThumbnail(p.el, docs));

    // Nothing typed and nothing attached: nothing to review.
      if (!prompt && previews.length === 0 && docs.length === 0) {
        console.log("[SafeScholar] Nothing to review. Input element:",
            input && input.tagName, input && String(input.className).slice(0, 40));
        return;
    }   

    const key = submissionKey(prompt, previews, docs);
    console.log("[SafeScholar] Send pressed. Prompt chars:", prompt.length, "| Images attached:", previews.length, "| Documents attached:", docs.length);

    // Permit only an exact, previously classified safe submission.
    if (!pendingReview && approvedPrompt === key) {
        approvedPrompt = null;
        capturedDocs.clear(); // these files are being sent now
        return;
    }

    // Cancellation MUST happen synchronously, not inside .then().
    event.preventDefault();
    event.stopImmediatePropagation();
    if (pendingReview) {
        nudgeCheckingBanner();
        return;
    }
    approvedPrompt = null;
    pendingReview = true;
    showCheckingBanner(previews.length > 0 || docs.length > 0);
    reviewSubmission(prompt, previews, docs).then(result => {
        renderReviewResult(result, key);
    }).catch(error => {
        console.error("[SafeScholar] Review failed", error);
        hideCheckingBanner();
        alert("SafeScholar could not verify this submission. It was not sent.");
    }).finally(() => {
        pendingReview = false;
        hideCheckingBanner();
    });
}

document.addEventListener("keydown", function(event) {
    if (accessRestricted) {
        if (event.target.closest?.("#safescholar-access-restricted")) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
    }
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    const input = findInputBox();
        if (!input || !(input === event.target || input.contains(event.target))) {
        console.log("[SafeScholar] Enter ignored. Input found:", !!input,
            "| typing in:", event.target.tagName, String(event.target.className).slice(0, 40));
        return;
    }
    interceptSubmission(event);
}, true);

document.addEventListener("click", function(event) {
    if (accessRestricted) {
        if (event.target.closest?.("#safescholar-access-restricted")) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
    }
    const button = event.target.closest?.("button");
    if (!button || !button.matches(
        'button[data-testid="send-button"], ' +
        'button[aria-label="Send prompt"], ' +
        'button[aria-label="Send message"], ' +
        'button[aria-label="Send"], ' +
        'button[aria-label="Submit"], ' +
        'button[aria-label="Send message"], ' +
        'button.send-button, ' +
        'button[mattooltip="Send message"]'
    )) return;
    interceptSubmission(event);
}, true);

// Also guard form submissions where a site uses native form submission.
document.addEventListener("submit", function(event) {
    if (accessRestricted) {
        if (event.target.closest?.("#safescholar-access-restricted")) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
    }
    const input = findInputBox();
    if (!input || !event.target.contains(input)) return;
    interceptSubmission(event);
}, true);

console.log("[SafeScholar] Pre-submission gate ready.");
