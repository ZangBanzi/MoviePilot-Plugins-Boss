// Selection, draft and history controls against the isolated local Emby QA server.
// Run after build:preview and preview_server.py, separately from other UI scripts.
import { createRequire } from 'node:module'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import assert from 'node:assert/strict'
const root = fileURLToPath(new URL('..', import.meta.url))
const work = path.resolve(root, '../.work')
const require = createRequire(path.join(root, 'frontend/package.json'))
const { chromium } = require('playwright')
const port = (await readFile(path.join(work, 'preview-port.txt'), 'utf8')).trim()
const base = `http://127.0.0.1:${port}`
const output = path.join(root, 'docs/previews')
await mkdir(output, { recursive: true })
const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const browser = await chromium.launch({ headless: true, ...(existsSync(edge) ? { executablePath: edge } : {}) })
const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, reducedMotion: 'reduce' })
const page = await context.newPage()
const errors = [], apiErrors = [], requests = [], confirmations = [], steps = []
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => { if (request.url().endsWith('/studio/action')) requests.push(request.postDataJSON()) })
page.on('response', response => { if (response.url().endsWith('/studio/action') && response.status() >= 400) apiErrors.push(response.status()) })
page.on('dialog', dialog => { confirmations.push(dialog.message()); dialog.accept() })
const card = key => page.locator(`.ma-library-card[data-library-key="${key}"]`)
const checkbox = key => card(key).getByRole('checkbox')
const edit = key => card(key).locator('.ma-library-editor').click()
const plan = name => page.locator('.ma-presets').getByRole('button', { name: new RegExp(name) })
function done(step) { steps.push(step); console.log('PASS:', step) }
async function readState() {
  const response = await page.request.get(base + '/api/v1/plugin/MediaArchiver/studio', { headers: { Authorization: 'Bearer local-test' } })
  const value = await response.json()
  assert.notEqual(value.success, false)
  return value.data ?? value
}
async function ready() {
  await page.waitForFunction(() => document.querySelector('.ma-canvas>img')?.naturalWidth > 0 && !document.querySelector('.ma-render-pill'), { timeout: 30000 })
}
async function flushPreview() {
  await ready()
  await page.getByRole('button', { name: '刷新预览', exact: true }).click()
  await ready()
}
async function generateSelected() {
  const response = page.waitForResponse(response => response.url().endsWith('/studio/action') && response.request().postDataJSON()?.action === 'generate')
  await page.getByRole('button', { name: '生成并应用已选库', exact: true }).click()
  const payload = (await response).request().postDataJSON()
  await page.waitForFunction(async () => {
    const response = await fetch('/api/v1/plugin/MediaArchiver/studio?light=true', { headers: { Authorization: 'Bearer local-test' } })
    const value = await response.json()
    return !(value.data ?? value).job.running
  }, { timeout: 40000 })
  await page.getByRole('button', { name: '生成并应用已选库', exact: true }).waitFor()
  await page.waitForFunction(() => !document.querySelector('.ma-job'), { timeout: 15000 })
  assert.equal((await readState()).job.failed, 0)
  return payload
}
try {
  await page.goto(base)
  await page.waitForFunction(() => document.querySelectorAll('.ma-library-card input:checked').length === 6)
  await ready()
  const initial = await readState()
  await page.getByRole('button', { name: '反选', exact: true }).click()
  assert.equal(await page.locator('.ma-library-check input:checked').count(), 0)
  assert.equal(await page.getByRole('button', { name: '预览已选库', exact: true }).isDisabled(), true)
  assert.equal(await page.getByRole('button', { name: '生成并应用已选库', exact: true }).isDisabled(), true)
  await page.getByRole('button', { name: '全选', exact: true }).click()
  assert.equal(await page.locator('.ma-library-check input:checked').count(), 6)
  await checkbox('attribute:remux').uncheck()
  assert.equal(await card('attribute:4k').locator('.ma-library-editor').getAttribute('aria-pressed'), 'true')
  assert.equal(await page.locator('.ma-library-check input:checked').count(), 5)
  done('All, invert and individual checkboxes control the batch without changing the editor')

  await plan('光幕').click()
  await page.getByLabel('副标题', { exact: true }).fill('我的光幕草稿')
  await edit('attribute:remux')
  await plan('留白').click()
  await edit('attribute:4k')
  assert.equal(await plan('光幕').getAttribute('aria-pressed'), 'true')
  assert.equal(await page.getByLabel('副标题', { exact: true }).inputValue(), '我的光幕草稿')
  await page.getByRole('button', { name: '刷新媒体库', exact: true }).click()
  await page.getByRole('status').filter({ hasText: '已读取 2 个原生媒体库' }).waitFor()
  assert.equal(await checkbox('attribute:remux').isChecked(), false)
  assert.equal(await page.locator('.ma-library-check input:checked').count(), 5)
  assert.equal(await plan('光幕').getAttribute('aria-pressed'), 'true')
  done('Switching editors and refreshing libraries preserve drafts and explicit selection')

  await page.getByRole('button', { name: '全选', exact: true }).click()
  await page.getByRole('button', { name: '反选', exact: true }).click()
  await checkbox('attribute:4k').check()
  await checkbox('attribute:remux').check()
  await page.getByLabel('主标题', { exact: true }).fill('')
  await page.getByLabel('动态封面', { exact: true }).uncheck()
  await flushPreview()
  const previewsBefore = requests.length
  const beforePreviewConfig = (await readState()).studio_config
  await page.getByRole('button', { name: '预览已选库', exact: true }).click()
  await page.getByRole('button', { name: '预览已选库', exact: true }).waitFor({ timeout: 30000 })
  const previews = requests.slice(previewsBefore).filter(request => request.action === 'preview')
  assert.deepEqual(previews.map(request => request.key), ['attribute:4k', 'attribute:remux'])
  assert.ok(previews.every(request => request.options.style === 'diagonal'))
  assert.deepEqual((await readState()).studio_config, beforePreviewConfig)
  assert.equal(await card('attribute:remux').locator('.ma-library-plan').innerText(), '光幕 · 批量统一方案')
  done('Selected-library previews use the same current plan and never persist settings')

  const unified = await generateSelected()
  assert.deepEqual(unified.keys, ['attribute:4k', 'attribute:remux'])
  assert.equal(unified.options.style, 'diagonal')
  let generated = await readState()
  assert.equal(generated.job.results.length, 2)
  for (const key of unified.keys) assert.equal(generated.views.find(view => view.key === key).options.style, 'diagonal')
  for (const key of ['attribute:tvb', 'attribute:hdr']) assert.deepEqual(generated.views.find(view => view.key === key).options, initial.views.find(view => view.key === key).options)
  assert.equal(await page.locator('.ma-library-check input:checked').count(), 2)
  done('Light curtain applies to every checked library while unchecked libraries remain unchanged')

  await edit('attribute:remux')
  await plan('留白').click()
  await checkbox('attribute:remux').uncheck()
  await page.getByLabel('批量封面方案', { exact: true }).selectOption('saved')
  await edit('attribute:4k')
  await checkbox('attribute:remux').check()
  assert.match(await card('attribute:remux').locator('.ma-library-plan').innerText(), /^光幕/)
  assert.equal(await card('attribute:remux').locator('.ma-draft-note').innerText(), '有未保存修改')
  await checkbox('attribute:remux').uncheck()
  await edit('attribute:remux')
  await flushPreview()
  const beforeSaved = requests.length
  const saved = await generateSelected()
  assert.equal(Object.hasOwn(saved, 'options'), false)
  assert.deepEqual(saved.keys, ['attribute:4k'])
  assert.equal(requests.slice(beforeSaved).filter(request => request.action === 'save_options').length, 0)
  generated = await readState()
  assert.equal(generated.views.find(view => view.key === 'attribute:remux').options.style, 'diagonal')
  assert.equal(await plan('留白').getAttribute('aria-pressed'), 'true')
  await checkbox('attribute:remux').check()
  await checkbox('attribute:4k').uncheck()
  const beforeDraftSave = requests.length
  await generateSelected()
  assert.equal(requests.slice(beforeDraftSave).filter(request => request.action === 'save_options').length, 0)
  assert.equal((await readState()).views.find(view => view.key === 'attribute:remux').options.style, 'diagonal')
  assert.match(await card('attribute:remux').locator('.ma-library-plan').innerText(), /^光幕/)
  await page.getByLabel('应用范围', { exact: true }).selectOption('view')
  const saveResponse = page.waitForResponse(response => response.url().endsWith('/studio/action') && response.request().postDataJSON()?.action === 'save_options')
  await page.getByRole('button', { name: '应用封面方案', exact: true }).click()
  const draftSave = (await saveResponse).request().postDataJSON()
  assert.equal(draftSave.key, 'attribute:remux')
  assert.equal(draftSave.scope, 'view')
  await page.getByRole('status').filter({ hasText: '当前虚拟库封面已更新' }).waitFor()
  await generateSelected()
  assert.equal((await readState()).views.find(view => view.key === 'attribute:remux').options.style, 'minimal')
  done('Saved-plan mode never auto-saves drafts and uses changes only after explicit single-library save')

  await page.getByLabel('应用范围', { exact: true }).selectOption('global')
  await plan('映墙').click()
  const defaultsBefore = (await readState()).options
  const singleResponse = page.waitForResponse(response => response.url().endsWith('/studio/action') && response.request().postDataJSON()?.action === 'generate')
  await page.getByRole('button', { name: '生成当前封面', exact: true }).click()
  const single = (await singleResponse).request().postDataJSON()
  assert.equal(single.key, 'attribute:remux')
  assert.equal(single.options.style, 'wall')
  await page.waitForFunction(() => !document.querySelector('.ma-job'), { timeout: 30000 })
  assert.deepEqual((await readState()).options, defaultsBefore)
  done('Single-library generation carries its draft directly even with global apply scope selected')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '全选', exact: true }).click()
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  assert.equal(await checkbox('attribute:4k').isVisible(), true)
  await page.screenshot({ path: path.join(output, 'studio-selection-mobile.png'), fullPage: true })
  await page.setViewportSize({ width: 1440, height: 1080 })
  await page.screenshot({ path: path.join(output, 'studio-selection.png'), fullPage: true })
  done('Batch controls, selection state and editor remain usable at 390 pixels')

  await page.getByRole('button', { name: /历史封面/ }).first().click()
  await page.locator('.ma-history-card').first().waitFor()
  const historyBefore = await readState()
  const firstHistory = page.locator('.ma-history-card').first()
  const deletedId = await firstHistory.getAttribute('data-history-id')
  await firstHistory.getByRole('button', { name: '删除历史封面', exact: true }).click()
  await page.locator(`.ma-history-card[data-history-id="${deletedId}"]`).waitFor({ state: 'detached' })
  const historyAfter = await readState()
  assert.equal(historyAfter.history_count, historyBefore.history_count - 1)
  assert.deepEqual(historyAfter.studio_config, historyBefore.studio_config)
  assert.match(confirmations.at(-1), /已应用的 Emby 封面与方案设置保持不变/)
  done('A history card deletes only its own record and leaves saved plans intact')

  await page.screenshot({ path: path.join(output, 'studio-history-cleanup.png'), fullPage: true })
  await page.getByRole('button', { name: /清空全部历史/ }).click()
  await page.getByRole('heading', { name: '你的第一张封面，还在等你', exact: true }).waitFor()
  assert.match(confirmations.at(-1), new RegExp(`全部 ${historyAfter.history_count} 张`))
  assert.match(confirmations.at(-1), /所有服务器[\s\S]*原图备份/)
  assert.equal((await readState()).history_count, 0)
  assert.equal(await page.getByRole('button', { name: '清理历史残留', exact: true }).isEnabled(), true)
  assert.equal(requests.findLast(request => request.action === 'clear_history').confirm, true)
  done('Clear-all confirms the full count including backups and removes all local history')

  // An explicit UI fixture exercises counts beyond the 60 displayed rows and disk-failure reporting.
  const fixtureRow = historyBefore.history[0]
  let fixtureCount = 65
  let fixtureClears = 0
  await page.route('**/api/v1/plugin/MediaArchiver/studio**', async route => {
    if (route.request().method() === 'GET') {
      const response = await route.fetch()
      const payload = await response.json()
      const data = payload.data ?? payload
      if (data.history) { data.history = fixtureCount ? [fixtureRow] : []; data.history_count = fixtureCount }
      return route.fulfill({ response, json: payload })
    }
    const body = route.request().postDataJSON()
    if (body.action === 'delete_history') return route.fulfill({ json: { success: true, data: {
      deleted: 0, failed: 1, remaining: fixtureCount, message: '1 张历史封面清理失败，记录已保留，请稍后重试。',
    } } })
    if (body.action === 'clear_history') {
      fixtureCount = 0
      fixtureClears += 1
      return route.fulfill({ json: { success: true, data: fixtureClears === 1
        ? { deleted: 65, failed: 1, remaining: 0, message: '历史索引已清空，但有 1 个残留文件未清理，请重试。' }
        : { deleted: 0, failed: 0, remaining: 0, message: '历史残留文件已清理。' } } })
    }
    return route.continue()
  })
  await page.locator('.ma-history-panel').getByRole('button', { name: '刷新', exact: true }).click()
  await page.getByRole('button', { name: '清空全部历史（65）', exact: true }).waitFor()
  assert.equal(await page.locator('.ma-history-card').count(), 1)
  await page.getByRole('button', { name: '删除历史封面', exact: true }).click()
  await page.locator('.ma-toast.error').filter({ hasText: '清理失败' }).waitFor()
  assert.equal(await page.locator('.ma-history-card').count(), 1)
  await page.getByRole('button', { name: '清空全部历史（65）', exact: true }).click()
  await page.getByRole('heading', { name: '你的第一张封面，还在等你', exact: true }).waitFor()
  assert.match(confirmations.at(-1), /全部 65 张/)
  await page.locator('.ma-toast.error').filter({ hasText: '残留文件未清理' }).waitFor()
  await page.getByRole('button', { name: '清理历史残留', exact: true }).click()
  await page.getByRole('status').filter({ hasText: '历史残留文件已清理' }).waitFor()
  assert.match(confirmations.at(-1), /历史残留文件/)
  assert.equal(fixtureClears, 2)
  done('UI fixtures show full counts, report partial deletion and permit residual cleanup with zero indexed rows')
  assert.deepEqual(errors, [])
  assert.deepEqual(apiErrors, [])
  await writeFile(path.join(work, 'ui-selection-results.json'), JSON.stringify({ steps, errors, apiErrors, browser: await browser.version() }, null, 2))
  console.log(`ALL ${steps.length} SELECTION BROWSER SCENARIOS PASSED`)
} catch (error) {
  await page.screenshot({ path: path.join(work, 'ui-selection-failure.png'), fullPage: true })
  await writeFile(path.join(work, 'ui-selection-failure.txt'), await page.locator('body').innerText())
  throw error
} finally { await browser.close() }
