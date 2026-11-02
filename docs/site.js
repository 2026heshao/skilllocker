(() => {
  const buttons = document.querySelectorAll("[data-copy-target]");

  const fallbackCopy = (text) => {
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.top = "-1000px";
    document.body.appendChild(field);
    field.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    field.remove();
    return copied;
  };

  buttons.forEach((button) => {
    const target = document.querySelector(button.dataset.copyTarget);
    const status = button.closest(".install__actions")?.querySelector("[data-copy-status]");

    button.addEventListener("click", async () => {
      const text = target?.textContent?.trim() ?? "";
      if (!text) return;

      let copied = false;
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch {
        copied = fallbackCopy(text);
      }

      if (status) {
        status.textContent = copied ? "已复制" : "复制失败，请手动选中";
        if (copied) {
          window.setTimeout(() => {
            if (status.textContent === "已复制") status.textContent = "";
          }, 2400);
        }
      }
    });
  });
})();
