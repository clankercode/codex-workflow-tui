const canvas = document.querySelector("#signal-canvas");
const header = document.querySelector(".site-header");
const ticker = document.querySelector(".ticker-track");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

if (ticker) {
  ticker.innerHTML += ticker.innerHTML;
}

function setHeaderState() {
  if (!header) return;
  header.dataset.elevated = window.scrollY > 12 ? "true" : "false";
}

window.addEventListener("scroll", setHeaderState, { passive: true });
setHeaderState();

document.querySelectorAll("[data-copy]").forEach((element) => {
  element.addEventListener("click", async () => {
    const value = element.getAttribute("data-copy") || "";
    try {
      await navigator.clipboard.writeText(value);
      element.classList.add("copied");
      const previous = element.getAttribute("aria-label");
      element.setAttribute("aria-label", "Copied");
      window.setTimeout(() => {
        element.classList.remove("copied");
        if (previous) {
          element.setAttribute("aria-label", previous);
        } else {
          element.removeAttribute("aria-label");
        }
      }, 900);
    } catch {
      element.classList.add("copied");
      window.setTimeout(() => element.classList.remove("copied"), 900);
    }
  });
});

function setupCanvas() {
  if (!canvas || reducedMotion) return;
  const context = canvas.getContext("2d");
  if (!context) return;

  let width = 0;
  let height = 0;
  let deviceRatio = 1;
  const lanes = Array.from({ length: 42 }, (_, index) => ({
    x: (index * 137) % 1600,
    y: (index * 91) % 900,
    speed: 0.32 + (index % 7) * 0.065,
    length: 90 + (index % 5) * 44,
    color: index % 3 === 0 ? "rgba(92,255,126,0.58)" : index % 3 === 1 ? "rgba(65,227,239,0.48)" : "rgba(255,193,77,0.48)",
  }));

  function resize() {
    const rect = canvas.getBoundingClientRect();
    deviceRatio = Math.min(window.devicePixelRatio || 1, 2);
    width = Math.max(1, Math.floor(rect.width));
    height = Math.max(1, Math.floor(rect.height));
    canvas.width = Math.floor(width * deviceRatio);
    canvas.height = Math.floor(height * deviceRatio);
    context.setTransform(deviceRatio, 0, 0, deviceRatio, 0, 0);
  }

  function drawGrid(time) {
    context.clearRect(0, 0, width, height);
    context.globalCompositeOperation = "lighter";
    lanes.forEach((lane, index) => {
      const x = (lane.x + time * lane.speed) % (width + 260);
      const y = (lane.y + Math.sin(time * 0.001 + index) * 24) % (height + 160);
      context.strokeStyle = lane.color;
      context.lineWidth = index % 4 === 0 ? 2 : 1;
      context.beginPath();
      context.moveTo(x - 160, y + lane.length);
      context.lineTo(x + lane.length, y - 120);
      context.stroke();
      context.fillStyle = lane.color;
      context.fillRect(x + lane.length - 6, y - 126, 12, 12);
    });
    context.globalCompositeOperation = "source-over";
    window.requestAnimationFrame(drawGrid);
  }

  resize();
  window.addEventListener("resize", resize, { passive: true });
  window.requestAnimationFrame(drawGrid);
}

setupCanvas();
