<script setup>
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth.js";

const password = ref("");
const auth = useAuthStore();
const router = useRouter();
async function submit() {
  try {
    await auth.login(password.value);
    await router.replace("/");
  } catch { password.value = ""; }
}
</script>

<template>
  <main class="login-page">
    <section class="login-card">
      <div class="logo-mark" aria-hidden="true">✦</div>
      <p class="eyebrow">Система допечатной проверки</p>
      <h1>Image Magic</h1>
      <p class="muted">Введите пароль, чтобы открыть рабочий пульт.</p>
      <form @submit.prevent="submit">
        <label for="password">Пароль</label>
        <input id="password" v-model="password" type="password" autocomplete="current-password" autofocus required>
        <p v-if="auth.error" class="form-error" role="alert">{{ auth.error }}</p>
        <button class="button primary wide" :disabled="auth.loading">
          <span v-if="auth.loading" class="spinner" aria-hidden="true"></span>
          {{ auth.loading ? "Проверяем…" : "Войти" }}
        </button>
      </form>
    </section>
  </main>
</template>
