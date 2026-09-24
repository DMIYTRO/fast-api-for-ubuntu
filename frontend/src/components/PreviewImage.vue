<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps({ src: { type: String, required: true }, alt: { type: String, default: "" }, eager: { type: Boolean, default: false } });
const emit = defineEmits(["load", "error"]);
const image = ref(null);
const status = ref("loading");
let observer;
let nearObserver;
let queuedJob;
let cancelQueued;
let activeRequest;
let displayedObjectUrl;

// All cards share a small pool so a large page cannot fan out dozens of requests.
const MAX_ACTIVE = 4;
const MAX_ATTEMPTS = 3;
const queue = globalThis.__previewImageQueue || (globalThis.__previewImageQueue = { active: 0, waiting: [], sequence: 0 });

function startWaiting() {
  while (queue.active < MAX_ACTIVE && queue.waiting.length) {
    queue.waiting.sort((a, b) => b.priority - a.priority || a.sequence - b.sequence);
    const next = queue.waiting[0];
    // Keep two slots available for cards currently inside the viewport.
    if (next.priority < 2 && queue.active >= MAX_ACTIVE - 2) return;
    queue.waiting.shift().start();
  }
}

function enqueue(priority = 1) {
  if (!props.src || !image.value || status.value === "loaded" || activeRequest) return;
  if (queuedJob) {
    queuedJob.priority = Math.max(queuedJob.priority, priority);
    startWaiting();
    return;
  }
  let cancelled = false;
  const job = () => {
    if (cancelled || !image.value) return;
    queue.active += 1;
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      queue.active -= 1;
      startWaiting();
    };
    const request = { src: props.src, controller: new AbortController(), release, timer: undefined, objectUrl: undefined };
    activeRequest = request;
    let attempts = 0;
    const run = async () => {
      attempts += 1;
      try {
        const response = await fetch(request.src, { credentials: "same-origin", signal: request.controller.signal });
        if (!response.ok) throw new Error(`Preview request failed: ${response.status}`);
        const blob = await response.blob();
        if (activeRequest !== request) return;
        request.objectUrl = URL.createObjectURL(blob);
        displayedObjectUrl = request.objectUrl;
        image.value.src = request.objectUrl;
        status.value = "loaded";
        activeRequest = undefined;
        emit("load", { src: request.src });
        release();
      } catch (error) {
        if (activeRequest !== request || request.controller.signal.aborted) return;
        if (attempts < MAX_ATTEMPTS) {
          request.timer = setTimeout(run, 350 * attempts);
          return;
        }
        activeRequest = undefined;
        status.value = "error";
        emit("error", error);
        release();
      }
    };
    run();
  };
  const wrapped = {
    priority,
    sequence: queue.sequence++,
    start: () => {
      if (cancelled) return;
      queuedJob = undefined;
      cancelQueued = undefined;
      job();
    },
  };
  const mayStart = priority >= 2 || queue.active < MAX_ACTIVE - 2;
  if (queue.active < MAX_ACTIVE && mayStart) wrapped.start();
  else {
    queuedJob = wrapped;
    queue.waiting.push(wrapped);
    cancelQueued = () => {
      cancelled = true;
      const index = queue.waiting.indexOf(wrapped);
      if (index >= 0) queue.waiting.splice(index, 1);
      queuedJob = undefined;
      cancelQueued = undefined;
    };
  }
}

function revokeDisplayedImage() {
  if (image.value) image.value.removeAttribute("src");
  if (displayedObjectUrl) URL.revokeObjectURL(displayedObjectUrl);
  displayedObjectUrl = undefined;
}
function cancelActiveRequest() {
  const request = activeRequest;
  if (!request) return;
  activeRequest = undefined;
  clearTimeout(request.timer);
  request.controller.abort();
  if (request.objectUrl) URL.revokeObjectURL(request.objectUrl);
  request.release();
}
function retry() {
  status.value = "loading";
  enqueue(2);
}
function activate(priority = 1) {
  if (priority === 2) {
    observer?.disconnect();
    nearObserver?.disconnect();
    observer = undefined;
    nearObserver = undefined;
  }
  enqueue(priority);
}

function observePreview() {
  if (props.eager || typeof IntersectionObserver === "undefined") return activate(2);
  const target = image.value;
  nearObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) activate(1);
  }, { rootMargin: "500px 0px" });
  observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) activate(2);
  }, { rootMargin: "0px" });
  if (target) {
    nearObserver.observe(target);
    observer.observe(target);
  }
}

onMounted(observePreview);

watch(() => props.src, () => {
  observer?.disconnect();
  nearObserver?.disconnect();
  observer = undefined;
  nearObserver = undefined;
  cancelQueued?.();
  cancelActiveRequest();
  revokeDisplayedImage();
  status.value = "loading";
  observePreview();
});

onBeforeUnmount(() => {
  observer?.disconnect();
  nearObserver?.disconnect();
  cancelQueued?.();
  cancelActiveRequest();
  revokeDisplayedImage();
});
</script>

<template>
  <span class="preview-loader" :class="{ 'preview-loader-error': status === 'error', 'preview-loader-loading': status === 'loading' }">
    <img ref="image" :class="{ 'preview-loader-placeholder': status !== 'loaded' }" :alt="alt" decoding="async" :aria-hidden="status !== 'loaded'">
    <button v-if="status === 'error'" class="preview-load-retry" type="button" @click.stop="retry">Повторить загрузку</button>
  </span>
</template>
