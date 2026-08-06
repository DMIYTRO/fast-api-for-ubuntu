<script setup>
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth.js";
import { api } from "../services/api.js";
import AppHeader from "../components/AppHeader.vue";

const auth = useAuthStore();
const router = useRouter();
const filters = reactive({ action: "all", status: "prepared", date_from: "", date_to: "", search: "" });
const items = ref([]); const loading = ref(false); const error = ref(""); const pagination = ref({ page: 1, total_pages: 1, total: 0 });
const label = { print: "Передано в печать", reject: "Возвращено на доработку" };
const formatDate = (value) => new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
async function load(page = 1) {
  loading.value = true; error.value = "";
  try {
    const result = await api.history({ ...filters, page });
    items.value = result.items || [];
    pagination.value = result;
  }
  catch (value) { error.value = value.message; }
  finally { loading.value = false; }
}
async function logout() { await auth.logout(); router.replace("/login"); }
function newCheck() { router.push("/"); }
onMounted(load);
</script>

<template>
  <AppHeader path="История действий" status="completed" @new-check="newCheck" @logout="logout" />
  <main class="history-page">
    <header class="history-title"><div><p class="eyebrow">Архив</p><h1>История заказов</h1><p>Только миниатюры: PDF в этой странице не хранится и не открывается.</p></div></header>
    <form class="history-filters surface" @submit.prevent="load(1)">
      <label>Действие<select v-model="filters.action"><option value="all">Все</option><option value="print">Передано в печать</option><option value="reject">Возвращено на доработку</option></select></label>
      <label>Статус<select v-model="filters.status"><option value="prepared">Выполненные</option><option value="failed">Не выполненные</option><option value="all">Все</option></select></label>
      <label>С даты<input v-model="filters.date_from" type="date"></label>
      <label>По дату<input v-model="filters.date_to" type="date"></label>
      <label class="history-search">Поиск<input v-model.trim="filters.search" placeholder="Номер заказа или клиент"></label>
      <button class="button primary" :disabled="loading">{{ loading ? "Загрузка…" : "Показать" }}</button>
    </form>
    <p v-if="error" class="page-error">{{ error }}</p>
    <section v-if="items.length" class="history-list">
      <article v-for="item in items" :key="item.id" class="history-item surface">
        <div v-if="item.previews.length" class="history-previews"><figure v-for="(preview, index) in item.previews" :key="preview.url" class="history-preview"><a :href="preview.url" target="_blank" :title="`Открыть превью заказа ${item.order_id}`"><img :src="preview.url" loading="lazy" decoding="async" :alt="`Превью ${preview.side || index + 1} заказа ${item.order_id}`"></a><figcaption>{{ preview.side === "face" ? "Лицо" : preview.side === "back" ? "Оборот" : `Макет ${index + 1}` }}</figcaption></figure></div>
        <div v-else class="history-preview"><span>Нет превью</span></div>
        <div class="history-data"><strong>№ {{ item.order_id }}</strong><span>Клиент: {{ item.customer_id || "—" }}</span><span :class="`history-action ${item.action}`">{{ label[item.action] }}</span><span v-if="item.status === 'failed'" class="history-status failed">Операция не выполнена</span><small>{{ formatDate(item.created_at) }}</small><p v-if="item.comment">{{ item.comment }}</p></div>
      </article>
    </section>
    <nav v-if="pagination.total_pages > 1" class="history-pagination" aria-label="Страницы истории">
      <button class="button secondary" :disabled="loading || pagination.page <= 1" @click="load(pagination.page - 1)">← Назад</button>
      <span>Страница {{ pagination.page }} из {{ pagination.total_pages }} · всего {{ pagination.total }}</span>
      <button class="button secondary" :disabled="loading || pagination.page >= pagination.total_pages" @click="load(pagination.page + 1)">Вперёд →</button>
    </nav>
    <section v-if="!items.length && !loading" class="empty-state surface"><div>⌁</div><h2>История пуста</h2><p>Измените фильтры или проведите заказ в печать либо верните его на доработку.</p></section>
  </main>
</template>

<style scoped>
.history-page{max-width:1200px;margin:0 auto;padding:28px 20px 70px}.history-title{margin-bottom:18px}.history-title h1{margin:4px 0}.history-title p{color:var(--muted);margin:0}.history-filters{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr)) minmax(180px,2fr) auto;gap:10px;align-items:end;padding:14px;margin-bottom:16px}.history-filters label{display:grid;gap:5px}.history-list{display:grid;gap:9px}.history-item{display:flex;align-items:center;gap:14px;padding:10px}.history-previews{display:flex;gap:7px;flex:0 0 auto}.history-preview{width:110px;margin:0}.history-preview img{width:110px;height:74px;border-radius:7px;background:#fff;object-fit:contain;display:block}.history-preview a{display:block;border-radius:7px;overflow:hidden}.history-preview a:hover{box-shadow:0 0 0 2px var(--brand)}.history-preview span{width:110px;height:74px;border-radius:7px;background:#eef1f5;display:grid;place-items:center;color:var(--muted);font-size:10px}.history-preview figcaption{font-size:10px;color:var(--muted);text-align:center;margin-top:3px}.history-data{display:grid;gap:3px;min-width:0}.history-data span,.history-data small{color:var(--muted);font-size:12px}.history-data p{margin:4px 0 0;font-size:12px}.history-action{font-weight:800}.history-action.print{color:var(--green)}.history-action.reject{color:var(--red)}.history-status{font-weight:800}.history-status.failed{color:var(--red)}.history-pagination{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:18px;color:var(--muted);font-size:12px}@media(max-width:760px){.history-filters{grid-template-columns:1fr 1fr}.history-search{grid-column:span 2}.history-item{align-items:flex-start}.history-preview,.history-preview img,.history-preview span{width:88px}.history-preview img,.history-preview span{height:60px}.history-pagination{gap:8px}}
</style>
