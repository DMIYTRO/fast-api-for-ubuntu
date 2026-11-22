import { ref } from "vue";

// Тема интерфейса: «light» | «dark». Выбор оператора хранится в localStorage;
// пока он ничего не выбирал, берётся системная тема (prefers-color-scheme).
const STORAGE_KEY = "image-magic-theme";
const META_COLORS = { light: "#111a2c", dark: "#0b101c" };

function readStored() {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

function systemTheme() {
  try {
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

export const theme = ref(readStored() || systemTheme());

export function applyTheme(value) {
  theme.value = value;
  document.documentElement.dataset.theme = value;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", META_COLORS[value]);
}

export function setTheme(value) {
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // Хранилище недоступно (приватный режим): тема просто не запомнится.
  }
  applyTheme(value);
}

export function toggleTheme() {
  setTheme(theme.value === "dark" ? "light" : "dark");
}

export function initTheme() {
  applyTheme(theme.value);
  // Пока выбор не сделан вручную, следуем за системной темой.
  try {
    window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
      if (!readStored()) applyTheme(systemTheme());
    });
  } catch {
    // нет matchMedia (тесты) — системную тему не отслеживаем
  }
}
