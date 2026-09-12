// Local test harness only. Not included in the MoviePilot federation build.
import { createApp, h, ref } from 'vue'
import Studio from './Studio.vue'
const api = {
  async get(path) { return request(path) },
  async post(path, data) { return request(path, data) },
}
async function request(path, data) {
  const response = await fetch('/api/v1/' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer local-test' },
    body: data === undefined ? undefined : JSON.stringify(data),
  })
  const result = await response.json()
  if (!response.ok) throw new Error(result.message || response.statusText)
  return result
}
createApp({ setup() {
  const settings = ref(false)
  return () => h(Studio, { api, settings: settings.value, key: String(settings.value),
    onSwitch: () => { settings.value = !settings.value },
    onSave: async config => { await request('plugin/MediaArchiver', config); settings.value = false },
    onClose: () => { settings.value = false },
  })
} }).mount('#app')
