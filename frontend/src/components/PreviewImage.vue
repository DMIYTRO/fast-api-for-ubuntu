<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps({ src: { type: String, required: true }, alt: { type: String, default: "" }, eager: { type: Boolean, default: false } });
const emit = defineEmits(["load", "error"]);
const image = ref(null);
const status = ref("loading");
let observer;
let cancelQueued;
let activeRequest;
let displayedObjectUrl;

// All cards share a small pool so a large page cannot fan out dozens of requests.
const MAX_ACTIVE = 4;
const MAX_ATTEMPTS = 3;
const queue = globalThis.__previewImageQueue || (globalThis.__previewImageQueue = { active: 0, waiting: [] });

function enqueue() {
  if (!props.src || !image.value || status.value === "loaded" || cancelQueued || activeRequest) return;
  let cancelled = false;
  const job = () => {
    if (cancelled || !image.value) return;
    queue.active += 1;
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      queue.active -= 1;
      while (queue.active < MAX_ACTIVE && queue.waiting.length) queue.waiting.shift()();
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
  const wrapped = () => {
    if (cancelled) return;
    cancelQueued = undefined;
    job();
  };
  if (queue.active < MAX_ACTIVE) wrapped();
  else {
    queue.waiting.push(wrapped);
    cancelQueued = () => {
      cancelled = true;
      const index = queue.waiting.indexOf(wrapped);
      if (index >= 0) queue.waiting.splice(index, 1);
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
  enqueue();
}
function activate() {
  observer?.disconnect();
  observer = undefined;
  enqueue();
}

onMounted(() => {
  if (props.eager || typeof IntersectionObserver === "undefined") return activate();
  observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) activate();
  }, { rootMargin: "250px 0px" });
  if (image.value) observer.observe(image.value);
});

watch(() => props.src, () => {
  cancelQueued?.();
  cancelActiveRequest();
  revokeDisplayedImage();
  status.value = "loading";
  if (!observer) enqueue();
  else if (image.value) observer.observe(image.value);
});

onBeforeUnmount(() => {
  observer?.disconnect();
  cancelQueued?.();
  cancelActiveRequest();
  revokeDisplayedImage();
});
</script>

<template>
  <span class="preview-loader" :class="{ 'preview-loader-error': status === 'error' }">
    <img ref="image" :class="{ 'preview-loader-placeholder': status !== 'loaded' }" :alt="alt" decoding="async" :aria-hidden="status !== 'loaded'">
    <button v-if="status === 'error'" class="preview-load-retry" type="button" @click.stop="retry">Повторить загрузку</button>
  </span>
</template>
