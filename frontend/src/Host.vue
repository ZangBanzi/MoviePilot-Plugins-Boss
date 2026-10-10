<script setup>
import { onMounted, ref } from 'vue'
import NativeRender from './NativeRender.vue'
const screen = ref('page'), items = ref([]), config = ref({}), error = ref(''), busy = ref(false), notice = ref('')
async function api(path, method='GET', body) {
  const response = await fetch('/api/v1/' + path, {method, headers:{'Authorization':'Bearer local-test', 'Content-Type':'application/json'}, body:body ? JSON.stringify(body) : undefined})
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
async function load() {
  error.value = ''; busy.value = true
  try {
    const data = await api(`plugin/${screen.value}/${'MediaArchiver'}`)
    if (data.render_mode !== 'vuetify') throw new Error('渲染模式异常')
    items.value = data.page || data.conf || []
    if (data.model) config.value = data.model
  } catch (err) { error.value = '插件数据加载失败：' + err.message }
  finally {busy.value = false}
}
async function switchScreen() {screen.value = screen.value === 'page' ? 'form' : 'page'; await load()}
async function action(value) {
  if (busy.value) return
  busy.value = true
  try { const data = await api(value.api, value.method.toUpperCase(), value.method === 'get' ? undefined : value.params || {}); notice.value=data.message || ''; }
  catch(err) { notice.value = err.message }
  finally {busy.value=false; await load()}
}
async function save() { await api('plugin/MediaArchiver', 'PUT', config.value); notice.value='配置已保存'; await load() }
onMounted(load)
</script>
<template><VApp><VMain><div class="qa-shell"><p class="text-caption">本地 MoviePilot 原生渲染替身 · 未连接 NAS</p><VBtn @click="switchScreen">{{screen === 'page' ? '配置' : '功能'}}</VBtn><VAlert v-if="error" type="error" class="my-3">{{error}}<VBtn @click="load">重试</VBtn></VAlert><div v-if="notice" role="status">{{notice}}</div><VProgressLinear v-if="busy" indeterminate/><NativeRender v-for="(item,index) in items" :key="screen + index" :config="item" :model="config" @action="action"/><VBtn v-if="screen === 'form'" color="primary" @click="save">保存配置</VBtn></div></VMain></VApp></template>
<style>.qa-shell{max-width:1100px;margin:24px auto;padding:16px}.v-table{overflow-x:auto}p{margin:10px 0}.v-btn{margin:4px}.v-alert__content{white-space:pre-line}td,th{overflow-wrap:anywhere}</style>
