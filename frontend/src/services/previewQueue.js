// Warm compact previews after the visible page has started loading.
const MAX_CONCURRENT = 2;
let generation = 0;
let active = 0;
const pending = [];
const queued = new Set();
const completed = new Set();
const loading = new Set();
const idleWaiters = [];

function settleIdle() {
  if (active || pending.length) return;
  for (const resolve of idleWaiters.splice(0)) resolve();
}

function urlsFromOrders(orders) {
  const urls = [];
  for (const order of orders || []) {
    for (const file of order.files || []) {
      if (file.preview_url) urls.push(file.preview_url);
    }
  }
  return urls;
}

function pump() {
  if (typeof Image === "undefined") return;
  while (active < MAX_CONCURRENT && pending.length) {
    const url = pending.shift();
    queued.delete(url);
    if (completed.has(url)) continue;
    const image = new Image();
    const current = generation;
    active += 1;
    loading.add(image);
    const finish = (loaded) => {
      loading.delete(image);
      if (current !== generation) return;
      active -= 1;
      if (loaded) completed.add(url);
      pump();
      settleIdle();
    };
    image.onload = () => finish(true);
    image.onerror = () => finish(false);
    image.src = url;
  }
}

function add(orders, first) {
  const urls = urlsFromOrders(orders);
  if (first) {
    for (const url of urls) {
      const index = pending.indexOf(url);
      if (index >= 0) {
        pending.splice(index, 1);
        queued.delete(url);
      }
    }
  }
  const fresh = urls.filter((url) => !completed.has(url) && !queued.has(url));
  for (const url of fresh) queued.add(url);
  if (first) pending.unshift(...fresh);
  else pending.push(...fresh);
  pump();
  settleIdle();
}

export const previewQueue = {
  whenIdle() {
    if (!active && !pending.length) return Promise.resolve();
    return new Promise((resolve) => idleWaiters.push(resolve));
  },
  prioritize(orders) { add(orders, true); },
  enqueue(orders) { add(orders, false); },
  clear() {
    generation += 1;
    pending.length = 0;
    queued.clear();
    completed.clear();
    for (const image of loading) {
      image.onload = null;
      image.onerror = null;
      image.src = "";
    }
    loading.clear();
    active = 0;
    settleIdle();
  },
};
