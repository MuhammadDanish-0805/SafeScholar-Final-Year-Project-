// SafeScholar page hook (runs inside the page itself, "MAIN" world).
//
// Why this file exists: ChatGPT and Gemini often create a hidden <input type="file">
// in memory, click it, and never add it to the page. Events from such inputs never
// reach content.js, so the extension would not know a PDF / Word file was chosen.
// This hook watches for those picks and passes the File to content.js.
(function () {
  "use strict";

  function isDocument(file) {
    return (
      /\.(pdf|docx)$/i.test(file.name || "") ||
      file.type === "application/pdf" ||
      file.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    );
  }

  function forward(files, via) {
    const docs = Array.from(files || []).filter(isDocument);
    if (docs.length) {
      window.postMessage({ __safescholar: true, via: via, files: docs }, window.location.origin);
    }
  }

  // 1) File inputs that are created in memory and opened with .click()
  const originalClick = HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click = function () {
    if (this.type === "file" && !this.__safescholarHooked) {
      this.__safescholarHooked = true;
      this.addEventListener("change", () => forward(this.files, "input"));
    }
    return originalClick.apply(this, arguments);
  };

  // 2) The File System Access picker (used by some sites instead of an input)
  if (typeof window.showOpenFilePicker === "function") {
    const originalPicker = window.showOpenFilePicker.bind(window);
    window.showOpenFilePicker = async function () {
      const handles = await originalPicker.apply(null, arguments);
      try {
        const files = await Promise.all(handles.map(handle => handle.getFile()));
        forward(files, "picker");
      } catch (error) { /* the site still gets its handles */ }
      return handles;
    };
  }
})();
