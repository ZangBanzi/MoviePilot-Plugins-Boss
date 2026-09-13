<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import Icon from './Icon.vue'
import './studio.css'

const props = defineProps({ api: Object, initialConfig: { type: Object, default: () => ({}) }, settings: Boolean })
const emit = defineEmits(['save', 'switch', 'close', 'action'])
const state = ref(null)
const loaded = ref(false)
const error = ref('')
const tab = ref('generate')
const configTab = ref('settings')
const selected = ref('')
const selectedServer = ref('')
const libraryPreviews = ref({})
const libraryOutputs = ref({})
const galleryBusy = ref(false)
const playingPreview = ref(false)
const options = reactive({})
const config = ref({})
const preview = ref('')
const previewBusy = ref(false)
const previewNotices = ref([])
const previewOutput = ref(null)
const artworkCount = ref(0)
const busy = ref(false)
const editing = ref(false)
const editor = ref('type')
const toast = ref('')
const toastError = ref(false)
const canvas = ref(null)
const scope = ref('view')
const presetName = ref('')
const presetDialog = ref(false)
const fontUrl = ref('')
const importPresetInput = ref(null)
const importBackupInput = ref(null)
const fontInput = ref(null)
const selectedHistory = ref(null)
const layoutFields = computed(() => [
  ['text_x','标题横向位置',2,75],['text_y','标题纵向位置',10,70],
  ['image_x','海报横向位置',5,70],['image_y','海报纵向位置',2,50],
  ['image_scale','海报缩放',50,125],['blur','背景模糊',0,60],
  ['overlay','背景压暗',15,90],['subtitle_size','副标题字号',10,50],['text_size','说明字号',10,36],
].filter(field => !(options.style === 'minimal' && ['text_x','image_x','image_y','image_scale'].includes(field[0]))
  && !(options.style === 'diagonal' && ['image_y','image_scale'].includes(field[0]))))
let previewTimer, pollTimer, toastTimer, previewId = 0, disposed = false, suppressWatch = false
const clone = value => JSON.parse(JSON.stringify(value))
const server = computed(() => state.value?.cover_servers?.find(s => s.id === selectedServer.value))
const libraries = computed(() => [...(server.value?.gateway ? (state.value?.views || []) : []), ...(state.value?.native_views || []).filter(v => v.server === selectedServer.value)])
const view = computed(() => libraries.value.find(v => v.key === selected.value))
const job = computed(() => state.value?.job || {})
const runtime = computed(() => state.value?.runtime || {})
const jobSummary = computed(() => {
  const results = job.value.results || []
  const animated = job.value.animated ?? results.filter(row => row.mime === 'image/gif').length
  const still = job.value.static ?? results.filter(row => row.mime && row.mime !== 'image/gif').length
  return `GIF ${animated} · 静态 ${still} · 失败 ${job.value.failed || 0}`
})
function formatLabel(row) {
  const type = row?.mime?.split(';')[0]?.split('/')[1] || row?.render_info?.actual_format
  return ({ gif: 'GIF', png: 'PNG', jpeg: 'JPEG', jpg: 'JPEG', webp: 'WebP' })[type] || (row?.status === 'failed' ? '未生成' : '图片')
}
function outputMessage(row) {
  if (!row) return ''
  if (row.purpose === 'before_native_publish') return '原生库更新前的原图备份，保留原格式。'
  if (row.render_info?.message) return row.render_info.message
  if (row.options?.animated && row.mime && row.mime !== 'image/gif') return '旧记录未保存静态原因，可重新生成查看诊断。'
  return row.notice || ''
}
function libraryOutput(key) {
  return libraryOutputs.value[key] || state.value?.history?.find(row => row.key === key && row.purpose !== 'before_native_publish')
}
function resultLabel(row) {
  return ({ published: '已更新原生库', generated: '已生成', failed: '未完成' })[row.status] || row.status || '已处理'
}
const activePreset = computed(() => state.value?.presets?.find(p => p.id === options.style))
const isDirty = computed(() => JSON.stringify(options) !== JSON.stringify(view.value?.options || state.value?.options || {}))
const historyGroups = computed(() => {
  const groups = []
  for (const row of state.value?.history || []) {
    let group = groups.find(g => g.id === row.batch)
    if (!group) { group = { id: row.batch, created: row.created, rows: [] }; groups.push(group) }
    group.rows.push(row)
  }
  return groups
})
const titleStyle = computed(() => options.style === 'minimal'
  ? { left: '12%', top: `${options.text_y}%`, width: '76%' }
  : { left: `${options.text_x}%`, top: `${options.text_y}%`, width: '46%' })
const imageStyle = computed(() => ({ left: `${options.image_x}%`, top: `${options.image_y}%`, width: `${31.5 * options.image_scale / 100}%` }))

function message(text, failed = false) {
  toast.value = text; toastError.value = failed
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 4500)
}
async function resultOf(promise) {
  const response = await promise
  // MoviePilot's injected API already unwraps Axios' response.data.
  if (!response || response.success === false) throw new Error(response?.message || '操作未完成')
  return response.data ?? response
}
function action(action, data = {}) {
  return resultOf(props.api.post('plugin/MediaArchiver/studio/action', { action, server: selectedServer.value, ...data }))
}
async function attempt(fn, success = '') {
  if (busy.value) return
  busy.value = true
  try { const value = await fn(); if (success) message(success); return value }
  catch (err) { message(err.message || '操作失败，请重试', true) }
  finally { busy.value = false }
}
async function setOptions(value) {
  suppressWatch = true
  Object.keys(options).forEach(key => { delete options[key] })
  Object.assign(options, clone(value))
  await nextTick()
  suppressWatch = false
}
async function load(preserve = false) {
  error.value = ''
  try {
    const data = await resultOf(props.api.get('plugin/MediaArchiver/studio'))
    if (disposed) return
    state.value = data
    if (!data.cover_servers?.some(s => s.id === selectedServer.value)) selectedServer.value = data.cover_servers?.[0]?.id || ''
    if (!preserve) {
      config.value = { ...clone(data.defaults), ...clone(data.config), ...clone(props.initialConfig) }
      config.value.cover_studio = { defaults: clone(data.options), history_enabled: true, history_limit: 30, overrides: {}, presets: [], ...clone(data.studio_config) }
      if (!libraries.value.some(v => v.key === selected.value)) selected.value = libraries.value[0]?.key || ''
      await setOptions(view.value?.options || data.options)
      scope.value = selected.value ? 'view' : 'global'
    }
    loaded.value = true
    if (!props.settings && !preserve) refreshPreview()
  } catch (err) { error.value = err.message || '加载失败，请检查插件依赖与连接' }
}
async function chooseLibrary(key = selected.value) {
  ++previewId
  selected.value = key
  preview.value = libraryPreviews.value[key] || ''
  previewOutput.value = libraryOutputs.value[key] || null
  previewNotices.value = []
  playingPreview.value = false
  await setOptions(view.value?.options || state.value.options)
  refreshPreview()
}
function loadNative() {
  return attempt(async () => {
    const result = await action('load_native'); await load(true)
    if (!selected.value && state.value.native_views.length) { selected.value = state.value.native_views[0].key; await chooseLibrary() }
    message(`已读取 ${result.count} 个原生媒体库，每个库分别从自身影片取材`)
  })
}
async function chooseServer() {
  ++previewId
  selected.value = ''; preview.value = ''; previewOutput.value = null
  await loadNative()
  selected.value = libraries.value[0]?.key || ''
  await chooseLibrary()
}
async function previewServer() {
  if (galleryBusy.value) return
  galleryBusy.value = true
  const owner = selectedServer.value
  try {
    for (const library of libraries.value) {
      if (disposed || owner !== selectedServer.value) break
      const data = await action('preview', { key: library.key, options: library.options, animated: false })
      libraryPreviews.value[library.key] = data.image
      libraryOutputs.value[library.key] = { mime: data.mime, render_info: data.render_info }
    }
  } catch (err) { message(err.message || '部分预览未完成', true) }
  finally { galleryBusy.value = false }
}
function generateServer() {
  return attempt(async () => {
    if (isDirty.value && selected.value) await saveOptions()
    state.value.job = await action('generate', { all_server: true, publish: true })
  }, '已开始逐库生成，原生库更新前自动备份')
}
async function refreshPreview(animated = false) {
  clearTimeout(previewTimer)
  const id = ++previewId
  previewBusy.value = true
  try {
    const data = await action('preview', { key: selected.value, options: clone(options), animated })
    if (disposed || id !== previewId) return
    preview.value = data.image
    playingPreview.value = data.mime === 'image/gif'
    previewOutput.value = { mime: data.mime, render_info: data.render_info }
    if (selected.value) {
      libraryPreviews.value[selected.value] = data.image
      libraryOutputs.value[selected.value] = previewOutput.value
    }
    previewNotices.value = (data.notices || []).filter(notice => notice !== data.render_info?.message)
    artworkCount.value = data.render_info?.artwork_count ?? data.artwork_count
    if (view.value?.native) view.value.count = data.total_count
  } catch (err) { if (id === previewId) message(err.message || '预览失败', true) }
  finally { if (id === previewId) previewBusy.value = false }
}
watch(options, () => {
  if (suppressWatch || !loaded.value || props.settings) return
  ++previewId // Discard any in-flight render immediately when the draft changes.
  clearTimeout(previewTimer)
  previewTimer = setTimeout(() => refreshPreview(), 450)
}, { deep: true })
async function poll() {
  if (disposed) return
  try {
    const data = await resultOf(props.api.get('plugin/MediaArchiver/studio?light=true'))
    if (state.value) {
      const finished = state.value.job?.running && !data.job.running
      state.value.job = data.job
      state.value.runtime = data.runtime
      if (finished) {
        for (const row of data.job.results || []) {
          delete libraryPreviews.value[row.key]
          delete libraryOutputs.value[row.key]
        }
        await load(true); message(`${data.job.message} · ${jobSummary.value}`, data.job.failed > 0)
      }
    }
  } catch { /* The foreground action/retry owns connection errors. */ }
  if (!disposed) pollTimer = setTimeout(poll, 2500)
}
async function saveOptions() {
  await action('save_options', { key: selected.value, scope: scope.value, options: clone(options) })
  await load(true)
}
function applyOptions() { return attempt(saveOptions, scope.value === 'global' ? '已保存默认方案，独立配置的媒体库保留自己的方案' : view.value?.native ? '原生库方案已保存，点击更新按钮后写入 Emby' : '当前虚拟库封面已更新') }
function generate(all = false, publish = false) {
  return attempt(async () => {
    if (isDirty.value) await saveOptions()
    const job = await action('generate', { key: all ? '' : selected.value, publish })
    state.value.job = job
  }, '已开始生成，可在历史封面中查看结果')
}
function choosePreset(preset) {
  if (preset.options) setOptions(preset.options).then(() => refreshPreview())
  else Object.assign(options, { style: preset.id, text_x: 7, text_y: 38, image_x: 58, image_y: 16, image_scale: 100 })
}
function downloadJson(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function downloadImage(image, name, mime) {
  const link = document.createElement('a')
  link.href = image; link.download = `${name}.${mime?.split('/')[1] || 'png'}`; link.click()
}
function exportPreset() { downloadJson({ format: 'mediaarchiver-preset-v1', name: activePreset.value?.name || '我的方案', options: clone(options) }, '媒体虚拟库-封面方案.json') }
async function readJson(event) {
  const file = event.target.files?.[0]; event.target.value = ''
  if (!file) return null
  if (file.size > 2 * 1024 * 1024) throw new Error('JSON 文件不能超过 2 MiB')
  return JSON.parse(await file.text())
}
function importPreset(event) {
  attempt(async () => {
    const value = await readJson(event); if (!value) return
    if (value.format !== 'mediaarchiver-preset-v1') throw new Error('请选择媒体虚拟库导出的封面方案')
    const saved = await action('save_preset', value)
    await load(true); await setOptions(saved.options); refreshPreview()
  }, '方案已导入，点击应用后生效')
}
function addPreset() {
  attempt(async () => {
    await action('save_preset', { name: presetName.value.trim() || '我的方案', options: clone(options) })
    presetDialog.value = false; presetName.value = ''; await load(true)
  }, '已保存自定义方案')
}
function deletePreset(preset) {
  if (!window.confirm(`删除方案“${preset.name}”？已应用的封面设置会保留。`)) return
  attempt(async () => { await action('delete_preset', { id: preset.id }); await load(true) }, '方案已删除')
}
function uploadFont(event) {
  const file = event.target.files?.[0]; event.target.value = ''; if (!file) return
  attempt(async () => {
    if (file.size > 24 * 1024 * 1024) throw new Error('字体文件不能超过 24 MiB')
    const encoded = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file)
    })
    await action('upload_font', { name: file.name, data: encoded }); await load(true)
  }, '字体已加入字体库')
}
function importFontUrl() {
  attempt(async () => { await action('import_font_url', { url: fontUrl.value }); fontUrl.value = ''; await load(true) }, '网络字体已导入')
}
function openHistory(row) {
  attempt(async () => { selectedHistory.value = { ...row, ...(await action('history_image', { id: row.id })) } })
}
function restoreHistory(row) {
  attempt(async () => {
    if (row.native) await action('load_native')
    await action('restore_history', { id: row.id }); await load(true)
    selected.value = row.key; await chooseLibrary(); tab.value = 'generate'; selectedHistory.value = null
  }, '已恢复历史方案；素材继续按当前用户权限获取')
}
function restoreNativeImage(row) {
  attempt(async () => { await action('restore_native_image', { id: row.id }); selectedHistory.value = null; await load(true) }, '原生库图片已恢复，更新前的图片也已保存到历史')
}
function deleteHistory(row) {
  if (!window.confirm(`删除“${row.name}”的这张历史封面？`)) return
  attempt(async () => { await action('delete_history', { id: row.id }); selectedHistory.value = null; await load(true) }, '历史封面已删除')
}
function backup() {
  attempt(async () => { const data = await action('backup'); await load(true); downloadJson(data.backup, '媒体虚拟库-配置备份.json') }, '备份已保存到插件数据目录并下载')
}
async function prepareRestore(snapshot) {
  if (snapshot.format !== 'mediaarchiver-config-v1') throw new Error('请选择媒体虚拟库的配置备份')
  const result = await action('validate_config', { config: snapshot.config })
  config.value = result.config
}
function importBackup(event) { attempt(async () => { const value = await readJson(event); if (value) await prepareRestore(value) }, '备份已载入表单，检查后点击保存配置') }
function restoreBackup(row) { attempt(async () => { const data = await action('read_backup', { id: row.id }); await prepareRestore(data.backup) }, '备份已载入表单，检查后点击保存配置') }
function resetConfig() {
  if (!window.confirm('将表单恢复为默认设置？保存后生效，历史封面和字体保留。')) return
  config.value = clone(state.value.defaults)
  message('已恢复默认表单，保存后生效')
}
function saveConfig() {
  attempt(async () => {
    // The host performs persistence, plugin reinitialization and Cron re-registration.
    const data = await action('validate_config', { config: clone(config.value) })
    emit('save', data.config)
  })
}
function runRebuild() { attempt(async () => { await resultOf(props.api.post('plugin/MediaArchiver/rebuild')); emit('action') }, '已开始重建虚拟库') }
function testConnection() { attempt(async () => { const data = await resultOf(props.api.post('plugin/MediaArchiver/test_connection')); message(data.message || 'Emby 连接成功') }) }
let drag = null
function startDrag(event, kind) {
  if (event.button !== 0) return
  const box = canvas.value.getBoundingClientRect()
  drag = { kind, x: event.clientX, y: event.clientY, box,
    startX: options[kind === 'text' ? 'text_x' : 'image_x'], startY: options[kind === 'text' ? 'text_y' : 'image_y'] }
  event.currentTarget.setPointerCapture(event.pointerId)
  event.preventDefault()
}
function moveDrag(event) {
  if (!drag) return
  const xKey = drag.kind === 'text' ? 'text_x' : 'image_x', yKey = drag.kind === 'text' ? 'text_y' : 'image_y'
  if (!(drag.kind === 'text' && options.style === 'minimal')) options[xKey] = Math.round(Math.max(2, Math.min(drag.kind === 'text' ? 75 : 70, drag.startX + (event.clientX-drag.x)/drag.box.width*100)))
  if (!(drag.kind === 'image' && options.style === 'diagonal')) options[yKey] = Math.round(Math.max(drag.kind === 'text' ? 10 : 2, Math.min(drag.kind === 'text' ? 70 : 50, drag.startY+(event.clientY-drag.y)/drag.box.height*100)))
}
function keyboardMove(event, kind) {
  const movement = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key]
  if (!movement) return
  event.preventDefault()
  const step = event.shiftKey ? 5 : 1
  const xKey = kind === 'text' ? 'text_x' : 'image_x', yKey = kind === 'text' ? 'text_y' : 'image_y'
  if (!(kind === 'text' && options.style === 'minimal')) options[xKey] = Math.max(2, Math.min(kind === 'text' ? 75 : 70, options[xKey]+movement[0]*step))
  if (!(kind === 'image' && options.style === 'diagonal')) options[yKey] = Math.max(kind === 'text' ? 10 : 2, Math.min(kind === 'text' ? 70 : 50, options[yKey]+movement[1]*step))
}
let previousFocus
watch(() => Boolean(presetDialog.value || selectedHistory.value), async open => {
  if (open) { previousFocus = document.activeElement; await nextTick(); document.querySelector('.ma-modal input, .ma-modal button')?.focus() }
  else previousFocus?.focus?.()
})
function modalKey(event) {
  if (!presetDialog.value && !selectedHistory.value) return
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); presetDialog.value = false; selectedHistory.value = null; return }
  if (event.key !== 'Tab') return
  const nodes = [...document.querySelectorAll('.ma-modal button:not(:disabled), .ma-modal input:not(:disabled)')]
  const first = nodes[0], last = nodes[nodes.length-1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(async () => { document.addEventListener('keydown', modalKey, true); await load(); if (!props.settings && selectedServer.value && !disposed) await loadNative(); if (!disposed) pollTimer = setTimeout(poll, 2500) })
onUnmounted(() => { document.removeEventListener('keydown', modalKey, true); disposed = true; ++previewId; clearTimeout(previewTimer); clearTimeout(pollTimer); clearTimeout(toastTimer) })
</script>

<template>
  <div class="ma-studio" :class="{ 'ma-settings': settings }">
    <header class="ma-hero">
      <div class="ma-hero-top">
        <div class="ma-wordmark">
          <div class="ma-logo"><Icon :name="settings ? 'settings' : 'layers'" :size="36" /></div>
          <div class="ma-heading"><span class="ma-ghost" aria-hidden="true">{{ settings ? 'Configuration' : 'BOSS Cover Studio' }}</span><h1>{{ settings ? '配置' : '媒体虚拟库 · 封面工坊' }}</h1><p>为每一个片库，留一个好看的入口。</p></div>
        </div>
        <div class="ma-toolbar">
          <button v-if="!settings" class="ma-icon-btn ma-play" title="生成服务器封面" aria-label="生成服务器封面" :disabled="busy || job.running || !selectedServer" @click="generateServer"><Icon name="play" /></button>
          <button v-if="settings" class="ma-icon-btn ma-play" title="保存配置" aria-label="保存配置" :disabled="busy || !loaded" @click="saveConfig"><Icon name="save" /></button>
          <button class="ma-icon-btn" :title="settings ? '返回封面工坊' : '打开配置'" :aria-label="settings ? '返回封面工坊' : '打开配置'" @click="emit('switch')"><Icon :name="settings ? 'image' : 'settings'" /></button>
          <button class="ma-icon-btn" title="关闭" aria-label="关闭" @click="emit('close')"><Icon name="close" /></button>
        </div>
      </div>
      <div class="ma-hero-bottom">
        <div class="ma-badges"><span><i :class="{ on: runtime.proxy?.running }"></i>{{ runtime.proxy?.running ? '虚拟库已启用' : '等待启用' }}</span><span>{{ activePreset?.name || '封面工坊' }}</span><span>{{ options.animated ? '动态 GIF' : '静态封面' }}</span><span>v{{ state?.version || '4.5.1' }}</span></div>
        <nav v-if="!settings" class="ma-tabs" aria-label="工坊页面"><button v-for="t in [['generate','封面生成'],['history','历史封面'],['status','运行状态']]" :key="t[0]" :class="{ active: tab === t[0] }" :aria-current="tab === t[0] ? 'page' : undefined" @click="tab=t[0]">{{ t[1] }}<span v-if="t[0] === 'history' && state?.history_count">{{ state.history_count }}</span></button></nav>
        <nav v-else class="ma-tabs" aria-label="配置页面"><button :class="{ active: configTab === 'settings' }" @click="configTab='settings'"><Icon name="layout" :size="17" />配置</button><button :class="{ active: configTab === 'titles' }" @click="configTab='titles'">T&nbsp; 默认标题与字体</button></nav>
      </div>
    </header>

    <div v-if="error" class="ma-error" role="alert">{{ error }}<button class="ma-btn" @click="load()">重新加载</button></div>
    <div v-if="!loaded && !error" class="ma-loading" role="status"><span class="ma-spinner"></span>正在打开封面工坊…</div>

    <template v-if="loaded && !settings">
      <div v-if="job.running" class="ma-job" role="status"><span class="ma-spinner"></span><div><strong>{{ job.message }}</strong><progress :value="job.done" :max="job.total || 1"></progress></div><span>{{ job.done }} / {{ job.total }}</span><button class="ma-btn ma-small" @click="attempt(() => action('cancel'))"><Icon name="stop" :size="15" />停止</button></div>
      <div v-if="!job.running && job.errors?.length && tab !== 'status'" class="ma-error" role="alert"><span>{{ job.failed || job.errors.length }} 个媒体库未完成，请查看逐库原因。</span><button class="ma-btn ma-small" @click="tab='status'">查看生成结果</button></div>
      <section v-if="tab === 'generate'" class="ma-panel ma-server-panel">
        <div class="ma-server-head"><div><span class="ma-eyebrow">SERVER COLLECTION</span><h2>整台服务器，一次生成</h2><p>每个媒体库读取自己的影片海报，分别生成封面。原生图片更新前自动备份。</p></div>
          <div class="ma-server-select"><label for="ma-server">Emby 服务器</label><select id="ma-server" v-model="selectedServer" :disabled="busy || job.running || galleryBusy" @change="chooseServer"><option v-if="!state.cover_servers?.length" value="">请先在 MoviePilot 配置 Emby</option><option v-for="item in state.cover_servers" :key="item.id" :value="item.id">{{ item.name }} · {{ item.gateway ? '原生库 + 虚拟库' : '原生库' }}</option></select></div></div>
        <div class="ma-server-toolbar"><span>{{ libraries.length }} 个媒体库 · 点选卡片调整独立方案</span><div class="ma-inline ma-wrap"><button class="ma-btn ma-small" :disabled="busy || job.running || !selectedServer" @click="loadNative"><Icon name="refresh" :size="16" />刷新媒体库</button><button class="ma-btn ma-small" :disabled="galleryBusy || busy || job.running || !libraries.length" @click="previewServer">{{ galleryBusy ? '逐库预览中…' : '预览整台服务器' }}</button><button class="ma-btn ma-primary ma-small" :disabled="busy || job.running || !selectedServer" @click="generateServer"><Icon name="play" :size="16" />生成并应用整台服务器</button></div></div>
        <div class="ma-library-grid"><button v-for="library in libraries" :key="library.key" class="ma-library-card" :class="{active: selected === library.key}" :aria-pressed="selected === library.key" :data-library-key="library.key" :disabled="busy || job.running" @click="chooseLibrary(library.key)"><div class="ma-library-art"><img v-if="libraryPreviews[library.key] || state.history.find(h => h.key === library.key && h.purpose !== 'before_native_publish')?.thumbnail" :src="libraryPreviews[library.key] || state.history.find(h => h.key === library.key && h.purpose !== 'before_native_publish')?.thumbnail" alt="" /><Icon v-else name="image" :size="28" /></div><div><strong>{{ library.name }}</strong><span>{{ library.native ? '原生库' : '虚拟库' }}{{ libraries.filter(v => v.name === library.name).length > 1 ? ' · ' + library.id : '' }}{{ library.customized ? ' · 独立方案' : ' · 默认方案' }}</span><span v-if="libraryOutput(library.key)" class="ma-library-output">{{ libraryOutputs[library.key] ? '预览' : '最近生成' }} {{ formatLabel(libraryOutput(library.key)) }}</span><small v-if="outputMessage(libraryOutput(library.key))" class="ma-output-note">{{ outputMessage(libraryOutput(library.key)) }}</small></div></button></div>
        <p v-if="!libraries.length" class="ma-note">选择 Emby 服务器后读取媒体库。未开启虚拟库时，也能生成原生库封面。</p>
        <p v-if="job.done && !job.running" class="ma-note" role="status">{{ job.message }} · {{ jobSummary }} <button class="ma-text-btn" @click="tab='status'">查看逐库结果</button></p>
      </section>
      <main v-if="tab === 'generate'" class="ma-workspace">
        <section class="ma-panel ma-canvas-panel">
          <div class="ma-section-head"><div><span class="ma-eyebrow">CANVAS</span><h2>可编辑画布预览</h2></div><div class="ma-inline"><button class="ma-icon-btn" :class="{ selected: editing }" title="编辑画布布局" aria-label="编辑画布布局" :aria-pressed="editing" @click="editing=!editing"><Icon name="edit" /></button><button class="ma-icon-btn" title="刷新预览" aria-label="刷新预览" :disabled="previewBusy" @click="options.seed=(options.seed+1)%1000000; refreshPreview()"><Icon name="refresh" :class="{ spinning: previewBusy }" /></button></div></div>
          <div class="ma-preview-caption"><strong>{{ view?.name || '请选择服务器中的媒体库' }}</strong><span>{{ view?.native ? '原生媒体库' : '虚拟媒体库' }} · 每库独立取材</span></div>
          <div class="ma-canvas-shell">
            <div ref="canvas" class="ma-canvas" :class="{ editing }" :aria-busy="previewBusy">
              <img v-if="preview" :src="preview" :alt="`${view?.name || '示例'}封面预览`" draggable="false" />
              <div v-else class="ma-preview-empty"><Icon name="image" :size="42" /><span>正在绘制你的片库封面</span></div>
              <span v-if="previewBusy" class="ma-render-pill"><span class="ma-spinner"></span>绘制中</span>
              <template v-if="editing && preview">
                <button class="ma-drag-box ma-drag-title" :style="titleStyle" aria-label="移动标题，支持方向键" @pointerdown="startDrag($event,'text')" @pointermove="moveDrag" @pointerup="drag=null" @pointercancel="drag=null" @keydown="keyboardMove($event,'text')"><span>标题 · 拖动调整</span></button>
                <button v-if="options.style !== 'minimal'" class="ma-drag-box ma-drag-image" :style="imageStyle" aria-label="移动海报，支持方向键" @pointerdown="startDrag($event,'image')" @pointermove="moveDrag" @pointerup="drag=null" @pointercancel="drag=null" @keydown="keyboardMove($event,'image')"><span>海报</span></button>
              </template>
            </div>
            <div class="ma-canvas-controls"><label>海报来源<select v-model="options.source"><option value="Backdrop">横版 Backdrop</option><option value="Primary">竖版海报 Primary</option><option value="brand">纯品牌画面</option></select></label><label>素材排序<select v-model="options.sort"><option value="random">{{ view?.native ? '随机素材' : '随机 · 固定种子' }}</option><option value="latest">最新入库</option><option value="name">名称排序</option></select></label><label>输出分辨率<select v-model.number="options.resolution"><option :value="640">360p · 轻量</option><option :value="960">540p · 标准</option><option :value="1280">720p · 高清</option><option :value="1920">1080p · 超清</option></select></label></div>
          </div>
          <div class="ma-preview-caption"><span><i class="ma-status-dot"></i>{{ artworkCount ? `已读取 ${artworkCount} 幅 Emby 海报` : '品牌画面' }}<span v-if="!selected"> · 示例，尚未应用</span></span><button v-if="options.animated" class="ma-text-btn" :disabled="previewBusy" @click="refreshPreview(!playingPreview)"><Icon name="play" :size="14" />{{ playingPreview ? '暂停动图预览' : '播放动图预览' }}</button></div>
          <div v-if="previewOutput" class="ma-output-info" :data-output-reason="previewOutput.render_info?.reason"><span class="ma-format-badge">{{ formatLabel(previewOutput) }} 预览</span><p>{{ outputMessage(previewOutput) }}</p></div>
          <p v-for="notice in previewNotices" :key="notice" class="ma-note">{{ notice }}</p>
          <div class="ma-editor-tabs"><button :class="{ active: editor === 'type' }" @click="editor='type'">标题与文案</button><button :class="{ active: editor === 'layout' }" @click="editor='layout'">布局与配色</button></div>
          <div v-if="editor === 'type'" class="ma-fields ma-edit-fields">
            <label>主标题<input v-model="options.title" maxlength="160" :placeholder="view?.name || '跟随虚拟库名称'" /></label><label>副标题<input v-model="options.subtitle" maxlength="160" placeholder="VIRTUAL COLLECTION" /></label>
            <label class="ma-span-2">自定义文本<input v-model="options.text" maxlength="160" placeholder="{count} 部 · 持续更新" /><small>支持 {count} 成员数量和 {name} 虚拟库名称。</small></label>
            <label>主标题字体<select v-model="options.title_font"><option v-for="font in state.fonts" :key="font.id" :value="font.id">{{ font.name }}</option></select></label><label>主标题字号 <output>{{ options.title_size }}</output><input v-model.number="options.title_size" type="range" aria-label="主标题字号" min="24" max="110" /></label>
            <label>副标题字体<select v-model="options.subtitle_font"><option v-for="font in state.fonts" :key="font.id" :value="font.id">{{ font.name }}</option></select></label><label>自定义文本字体<select v-model="options.text_font"><option v-for="font in state.fonts" :key="font.id" :value="font.id">{{ font.name }}</option></select></label>
          </div>
          <div v-else class="ma-fields ma-edit-fields"><label v-for="field in layoutFields" :key="field[0]">{{ field[1] }}<output>{{ options[field[0]] }}</output><input v-model.number="options[field[0]]" type="range" :aria-label="field[1]" :min="field[2]" :max="field[3]" /></label><label>强调色<input v-model="options.accent" type="text" placeholder="自动使用专区品牌色" pattern="#[0-9a-fA-F]{6}" maxlength="7" /></label><label>背景色<input v-model="options.background" type="text" placeholder="自动使用专区背景色" maxlength="7" /></label><label>标题颜色<input v-model="options.foreground" type="color" /></label></div>
          <footer class="ma-apply-bar"><label class="ma-check"><input v-model="options.show_count" type="checkbox" />显示说明文字</label><div class="ma-inline"><select v-model="scope" aria-label="应用范围"><option v-if="selected" value="view">仅当前媒体库</option><option value="global">设为默认方案</option></select><button class="ma-btn ma-primary" :disabled="busy" @click="applyOptions"><Icon name="check" :size="17" />{{ isDirty ? '应用封面方案' : '保存方案设置' }}</button></div></footer>
          <p v-if="view?.customized" class="ma-note">当前库使用独立方案。<button class="ma-text-btn" @click="attempt(async () => { await action('reset_override',{key:selected}); await load(true); await chooseLibrary() },'已恢复使用默认方案')">恢复跟随默认方案</button></p>
        </section>

        <aside class="ma-panel ma-presets-panel"><div class="ma-section-head"><div><span class="ma-eyebrow">PRESETS</span><h2>封面方案</h2></div><label class="ma-mode"><span>静态</span><input v-model="options.animated" type="checkbox" aria-label="动态封面" /><span class="ma-toggle"></span><span>动图</span></label></div>
          <div class="ma-presets"><button v-for="preset in state.presets" :key="preset.id" class="ma-preset" :class="{ active: options.style === preset.id }" :aria-pressed="options.style === preset.id" @click="choosePreset(preset)"><img v-if="preset.thumbnail" :src="preset.thumbnail" :alt="preset.name" /><div v-else class="ma-preset-art" :class="preset.id"><i></i><i></i><i></i></div><div class="ma-preset-copy"><div><strong>{{ preset.name }}</strong><small>{{ preset.description }}</small></div><Icon v-if="options.style === preset.id" name="check" :size="18" /></div></button></div>
          <p class="ma-note">方案同时支持静态和动态输出。动图轮播本库最多 6 幅不同海报，每幅展示约 2 秒；素材不足两幅时输出静态图。最高 540p，支持暂停预览与静态请求。</p>
          <template v-if="state.custom_presets.length"><div class="ma-divider"></div><span class="ma-eyebrow">MY PRESETS</span><div v-for="preset in state.custom_presets" :key="preset.id" class="ma-custom-preset"><button @click="choosePreset(preset)"><Icon name="layers" :size="17" />{{ preset.name }}</button><button class="ma-icon-btn ma-small" :aria-label="`删除方案 ${preset.name}`" @click="deletePreset(preset)"><Icon name="trash" :size="16" /></button></div></template>
          <div class="ma-preset-actions"><button class="ma-btn" @click="presetDialog=true"><Icon name="plus" :size="17" />添加方案</button><button class="ma-btn" @click="exportPreset"><Icon name="share" :size="17" />分享方案</button><button class="ma-text-btn ma-span-2" @click="importPresetInput.click()"><Icon name="upload" :size="15" />导入方案文件</button></div>
          <button class="ma-btn ma-full" :class="{'ma-primary': !view?.native}" :disabled="busy || job.running || !selected" @click="generate(false)"><Icon name="play" :size="18" />生成当前封面</button><button v-if="view?.native" class="ma-btn ma-primary ma-full" :disabled="busy || job.running" @click="generate(false,true)"><Icon name="upload" :size="17" />生成并更新原生库封面</button>
          <p class="ma-note ma-center">{{ view?.native ? '更新仅覆盖所选库的 Primary 图片，旧封面自动备份到历史。' : '保存到历史，随时下载与恢复方案。' }}</p>
        </aside>
      </main>

      <section v-else-if="tab === 'history'" class="ma-panel ma-history-panel"><div class="ma-section-head"><div><span class="ma-eyebrow">TIME MACHINE</span><h2>历史封面</h2><p>按生成批次保存，展示最近 60 张封面。</p></div><button class="ma-btn" @click="attempt(() => load(true))"><Icon name="refresh" :size="17" />刷新</button></div><div v-if="!historyGroups.length" class="ma-empty"><Icon name="history" :size="44" /><h3>你的第一张封面，还在等你</h3><p>选择方案并生成封面，历史记录会保存在这里。</p><button class="ma-btn ma-primary" @click="tab='generate'">去生成封面</button></div><section v-for="group in historyGroups" :key="group.id" class="ma-history-batch"><div class="ma-batch-title"><h3>{{ group.created }}</h3><span>{{ group.rows.length }} 张封面</span></div><div class="ma-history-grid"><article v-for="row in group.rows" :key="row.id" class="ma-history-card" :data-output-reason="row.render_info?.reason"><button class="ma-history-image" @click="openHistory(row)"><img :src="row.thumbnail" :alt="row.name" loading="lazy" /><span>{{ formatLabel(row) }}</span></button><div><strong>{{ row.name }}{{ row.purpose === 'before_native_publish' ? ' · 更新前备份' : '' }}</strong><p v-if="outputMessage(row)" class="ma-output-note">{{ outputMessage(row) }}</p><div class="ma-inline"><button class="ma-icon-btn ma-small" title="恢复此方案" aria-label="恢复此方案" :disabled="busy || job.running" @click="restoreHistory(row)"><Icon name="history" :size="17" /></button><button class="ma-icon-btn ma-small" title="查看与下载" aria-label="查看与下载" @click="openHistory(row)"><Icon name="download" :size="17" /></button></div></div></article></div></section></section>

      <section v-else class="ma-panel ma-status-panel">
        <section v-if="job.done || job.running || job.errors?.length" class="ma-output-results" aria-label="最近封面生成结果">
          <div class="ma-section-head"><div><span class="ma-eyebrow">COVER RESULTS</span><h2>最近封面生成结果</h2><p>{{ job.message }}</p></div><strong class="ma-output-summary">{{ jobSummary }}</strong></div>
          <p v-if="job.fallback" class="ma-note">{{ job.fallback }} 个库按下方原因输出静态封面。至少需要两幅不同的本库海报才能轮播。</p>
          <div class="ma-result-list"><article v-for="row in job.results" :key="row.key" class="ma-result-row" :class="{ failed: row.status === 'failed' }" :data-output-reason="row.render_info?.reason"><div><strong>{{ row.name }}</strong><span>{{ resultLabel(row) }}<template v-if="row.render_info"> · {{ row.render_info.artwork_count }} 幅不同海报</template></span></div><span class="ma-format-badge">{{ formatLabel(row) }}</span><p v-if="outputMessage(row)">{{ outputMessage(row) }}</p><p v-if="row.notice && row.notice !== outputMessage(row)">{{ row.notice }}</p></article></div>
          <details v-if="job.errors?.length" class="ma-details" :open="!job.results?.some(row => row.status === 'failed')"><summary>未完成原因 · {{ job.errors.length }} 项</summary><p v-for="(detail,index) in job.errors" :key="index" class="ma-result-error">{{ detail }}</p></details>
        </section>
        <div class="ma-section-head"><div><span class="ma-eyebrow">LIBRARY STATUS</span><h2>虚拟库运行状态</h2><p>{{ runtime.message || '保存配置后重建虚拟库' }}</p></div><div class="ma-inline"><button class="ma-btn" :disabled="busy" @click="testConnection"><Icon name="link" :size="17" />测试连接</button><button class="ma-btn ma-primary" :disabled="busy || runtime.state === 'running'" @click="runRebuild"><Icon name="refresh" :size="17" />一键重建</button></div></div><div class="ma-metrics"><div><strong>{{ state.views.length }}</strong><span>一级虚拟库</span></div><div><strong>{{ runtime.selected_rankings || 0 }}</strong><span>已选榜单</span></div><div><strong>8098</strong><span>客户端入口</span></div><div><strong>{{ runtime.daily_sync_enabled ? runtime.sync_cron : '未启用' }}</strong><span>定时更新</span></div></div><p class="ma-note">最后同步：{{ runtime.last_sync || '尚未同步' }} · {{ runtime.proxy?.message }}</p><div class="ma-table-wrap"><table><thead><tr><th>虚拟库</th><th>成员数</th><th>封面方案</th><th>来源状态</th></tr></thead><tbody><tr v-for="library in state.views" :key="library.key"><td>{{ library.name }}</td><td>{{ library.count }}</td><td>{{ state.presets.find(p=>p.id===library.options.style)?.name }}{{ library.customized ? ' · 独立配置' : '' }}</td><td v-if="library.key.startsWith('ranking:')">{{ runtime.source_status?.[library.key.slice(8)]?.ok ? (runtime.source_status[library.key.slice(8)].complete === false ? '部分更新，保留旧成员' : '正常') : '来源失败或等待同步' }}</td><td v-else>本库属性识别</td></tr></tbody></table></div><details class="ma-details"><summary>榜单来源诊断</summary><div v-for="(item,key) in runtime.source_status" :key="key" class="ma-diagnostic"><strong>{{ state.rankings[key]?.collection || key }}</strong><p>{{ item.error || item.source || '尚未取得结果' }}</p><span v-if="item.complete === false">部分结果，暂缓清理可信旧成员</span></div></details></section>
    </template>

    <main v-if="loaded && settings" class="ma-config-shell">
      <template v-if="configTab === 'settings'">
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>运行与定时</h2><p>控制虚拟库功能开关和自动更新周期。</p></div><Icon name="settings" /></div><div class="ma-fields"><div class="ma-switch-stack"><label class="ma-switch"><input v-model="config.virtual_enabled" type="checkbox" /><span class="ma-toggle"></span>启用媒体属性专区</label><label class="ma-switch"><input v-model="config.ranking_enabled" type="checkbox" /><span class="ma-toggle"></span>启用榜单虚拟库</label></div><div><label class="ma-switch"><input v-model="config.daily_sync_enabled" type="checkbox" /><span class="ma-toggle"></span>定时更新</label><label class="ma-block">更新 Cron<input v-model="config.sync_cron" placeholder="0 4 * * *" :disabled="!config.daily_sync_enabled" /><small>五段表达式。每天 04:00：0 4 * * *；星期建议使用 mon–sun。</small></label></div></div></section>
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>入库监控</h2><p>跟踪新增、更新与删除，持续维护虚拟库和封面。</p></div></div><div class="ma-fields ma-three"><label class="ma-switch"><input v-model="config.auto_sync" type="checkbox" /><span class="ma-toggle"></span>实时增量维护</label><label>周期校准（分钟）<input v-model.number="config.sync_interval" type="number" min="10" max="1440" /></label><label>请求超时（秒）<input v-model.number="config.timeout" type="number" min="5" max="120" /></label></div><div class="ma-info">复用既有 MoviePilot 入库事件与周期校准。成员变化后封面标签自动更新，客户端仍使用原来的入口。</div></section>
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>媒体库范围</h2><p>读取 MoviePilot 已配置的 Emby，选择需要的一级虚拟专区。</p></div><button class="ma-btn" :disabled="busy" @click="testConnection"><Icon name="link" :size="17" />测试已保存连接</button></div><label class="ma-block">媒体服务器<select v-model="config.emby_server"><option value="">自动使用第一台 Emby</option><option v-for="server in state.servers" :key="server.value" :value="server.value">{{ server.title }}</option></select></label><div class="ma-rule-grid"><label v-for="rule in state.rules" :key="rule.key" class="ma-rule" :class="{ active: config['zone_'+rule.key] }"><input v-model="config['zone_'+rule.key]" type="checkbox" /><div><strong>{{ rule.name }}</strong><small>{{ rule.hint }}</small></div></label></div></section>
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>平台与榜单</h2><p>保持可靠身份匹配，来源失败时明确报告。</p></div></div><details v-for="group in state.ranking_groups" :key="group.key" class="ma-details"><summary>{{ group.name }}<span>{{ group.items.filter(item => config['rank_'+item[0]]).length }} 已选</span></summary><div class="ma-ranking-grid"><label v-for="item in group.items" :key="item[0]" class="ma-check"><input v-model="config['rank_'+item[0]]" type="checkbox" />{{ item[1] }}</label></div></details><details class="ma-details"><summary>榜单高级设置</summary><div class="ma-fields"><label>TMDB API Key<input v-model="config.tmdb_api_key" type="password" autocomplete="off" placeholder="留空复用 MoviePilot 配置" /></label><label>TMDB 域名<input v-model="config.tmdb_domain" placeholder="留空使用默认值" /></label><label>地区列表<input v-model="config.ranking_regions" placeholder="US,GB,JP,KR,HK,TW" /></label><label>每榜候选上限<input v-model.number="config.ranking_limit" type="number" min="20" max="300" /></label><label>语言<input v-model="config.ranking_language" placeholder="zh-CN" /></label><label>自定义 Feed URL<input v-model="config.ranking_feed_url" type="url" /></label><label>Feed Token<input v-model="config.ranking_feed_token" type="password" autocomplete="off" /></label></div></details></section>
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>历史封面</h2><p>按生成批次保留封面，用于下载与恢复方案。</p></div></div><div class="ma-fields"><label class="ma-switch"><input v-model="config.cover_studio.history_enabled" type="checkbox" /><span class="ma-toggle"></span>保存历史封面</label><label>所有批次的上限<input v-model.number="config.cover_studio.history_limit" type="number" min="1" max="100" /><small>默认保留最近 30 个批次，总空间最多 256 MiB。恢复方案时重新按用户权限取图。</small></label></div></section>
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>字体库</h2><p>主标题、副标题和自定义文本可使用不同字体。</p></div><button class="ma-btn" :disabled="busy" @click="fontInput.click()"><Icon name="upload" :size="18" />上传字体</button></div><div class="ma-font-library"><span class="ma-eyebrow">FONT LIBRARY</span><h3>自定义字体库</h3><label class="ma-block">网络字体链接<div class="ma-input-action"><input v-model="fontUrl" placeholder="https://example.com/font.woff2" type="url" /><button class="ma-btn" :disabled="busy || !fontUrl" @click="importFontUrl">导入</button></div><small>支持 TTF / OTF / TTC / WOFF / WOFF2，单个文件最多 24 MiB，保存到插件数据目录。</small></label><div class="ma-font-list"><div v-for="font in state.fonts" :key="font.id"><Icon name="edit" :size="17" /><strong>{{ font.name }}</strong><span>{{ font.id === 'default' ? '内置中文字体' : `${Math.round(font.size/1024)} KiB` }}</span></div></div></div></section>
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>备份还原</h2><p>备份当前已保存配置，上传备份载入表单，确认后保存生效。</p></div></div><div class="ma-info">备份位置：插件数据目录 / cover_studio / backups。配置 JSON 包含插件参数；自定义字体文件和历史图片保留在各自目录。</div><div class="ma-inline ma-wrap"><button class="ma-btn" :disabled="busy" @click="backup"><Icon name="save" :size="18" />立即备份</button><button class="ma-btn" @click="importBackupInput.click()"><Icon name="upload" :size="18" />上传备份</button><button class="ma-btn" @click="resetConfig"><Icon name="history" :size="18" />初始化表单</button><span class="ma-count">已备份 {{ state.backups.length }} 个</span></div><div v-if="!state.backups.length" class="ma-empty ma-compact">暂无备份记录</div><div v-for="row in state.backups" :key="row.id" class="ma-backup-row"><span>{{ row.created }}</span><button class="ma-text-btn" :disabled="busy" @click="restoreBackup(row)">载入这份备份<Icon name="arrow" :size="15" /></button></div></section>
        <section class="ma-config-card"><div class="ma-section-head"><div><h2>清理缓存</h2><p>清理图片或字体缓存，保留历史文件与配置。</p></div></div><div class="ma-fields"><div class="ma-clean-card"><div><h3>图片缓存</h3><p>释放海报与生成图片的内存缓存。</p></div><button class="ma-btn ma-danger" :disabled="busy" @click="attempt(()=>action('clean_images'),'图片缓存已清理')"><Icon name="image" :size="17" />清理图片缓存</button></div><div class="ma-clean-card"><div><h3>字体缓存</h3><p>重新加载字体，已上传文件保留。</p></div><button class="ma-btn ma-danger" :disabled="busy" @click="attempt(()=>action('clean_fonts'),'字体缓存已清理')"><Icon name="edit" :size="17" />清理字体缓存</button></div></div></section>
      </template>
      <template v-else><section class="ma-config-card"><span class="ma-eyebrow">TYPOGRAPHY</span><h2>默认标题与字体</h2><p>新建封面使用以下默认值，独立配置的虚拟库保留各自设置。</p><div class="ma-fields ma-edit-fields"><label>默认主标题<input v-model="config.cover_studio.defaults.title" placeholder="留空跟随每个虚拟库名称" maxlength="160" /></label><label>默认副标题<input v-model="config.cover_studio.defaults.subtitle" maxlength="160" /></label><label class="ma-span-2">默认自定义文本<input v-model="config.cover_studio.defaults.text" maxlength="160" /><small>支持 {count} 和 {name}。</small></label><label v-for="field in [['title_font','主标题字体'],['subtitle_font','副标题字体'],['text_font','自定义文本字体']]" :key="field[0]">{{ field[1] }}<select v-model="config.cover_studio.defaults[field[0]]"><option v-for="font in state.fonts" :key="font.id" :value="font.id">{{ font.name }}</option></select></label></div></section></template>
      <footer class="ma-config-save"><p><Icon name="check" :size="16" />客户端入口 8098 · 原 ItemId 与 302 链路保留</p><button class="ma-btn ma-primary" :disabled="busy" @click="saveConfig"><Icon name="save" :size="19" />保存配置</button></footer>
    </main>
    <footer class="ma-footnote">BOSS COVER STUDIO <span>媒体虚拟库 {{ state?.version || '4.5.1' }}</span><span>Designed for your collection.</span></footer>
    <input ref="importPresetInput" type="file" accept=".json,application/json" hidden @change="importPreset" /><input ref="importBackupInput" type="file" accept=".json,application/json" hidden @change="importBackup" /><input ref="fontInput" type="file" accept=".ttf,.otf,.ttc,.woff,.woff2" hidden @change="uploadFont" />
    <div v-if="toast" class="ma-toast" :class="{ error: toastError }" :role="toastError ? 'alert' : 'status'"><Icon :name="toastError ? 'close' : 'check'" :size="19" />{{ toast }}</div>
    <div v-if="presetDialog" class="ma-modal-backdrop" @click.self="presetDialog=false"><section class="ma-modal" role="dialog" aria-modal="true" aria-label="保存自定义方案" @keydown.esc="presetDialog=false"><div class="ma-section-head"><h2>留住这个设计</h2><button class="ma-icon-btn" aria-label="关闭弹窗" @click="presetDialog=false"><Icon name="close" /></button></div><label class="ma-block">方案名称<input v-model="presetName" maxlength="40" placeholder="例如：我的影院 · 深蓝" @keydown.enter="addPreset" /></label><p>保存当前布局、标题、字体、配色与输出设置。</p><button class="ma-btn ma-primary ma-full" :disabled="busy" @click="addPreset">保存为我的方案</button></section></div>
    <div v-if="selectedHistory" class="ma-modal-backdrop" @click.self="selectedHistory=null"><section class="ma-modal ma-image-modal" role="dialog" aria-modal="true" aria-label="历史封面预览" @keydown.esc="selectedHistory=null"><div class="ma-section-head"><h2>{{ selectedHistory.name }}</h2><button class="ma-icon-btn" aria-label="关闭预览" @click="selectedHistory=null"><Icon name="close" /></button></div><img :src="selectedHistory.image" :alt="selectedHistory.name" /><p>{{ selectedHistory.created }}{{ selectedHistory.purpose === 'before_native_publish' ? ' · 原生库更新前的原图备份' : '' }}</p><div class="ma-output-info" :data-output-reason="selectedHistory.render_info?.reason"><span class="ma-format-badge">{{ formatLabel(selectedHistory) }}</span><p>{{ outputMessage(selectedHistory) }}</p></div><div class="ma-inline ma-wrap"><button class="ma-btn ma-primary" @click="downloadImage(selectedHistory.image,selectedHistory.name,selectedHistory.mime)"><Icon name="download" :size="18" />下载原图</button><button class="ma-btn" :disabled="busy || job.running" @click="restoreHistory(selectedHistory)"><Icon name="history" :size="18" />恢复此方案</button><button v-if="selectedHistory.native" class="ma-btn" :disabled="busy || job.running" @click="restoreNativeImage(selectedHistory)"><Icon name="upload" :size="18" />恢复原生库图片</button><button class="ma-btn ma-danger" :disabled="busy" @click="deleteHistory(selectedHistory)"><Icon name="trash" :size="18" />删除</button></div></section></div>
  </div>
</template>
