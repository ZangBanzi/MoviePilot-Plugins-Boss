// Browser checks for output explanations. Uses the local QA host and deterministic API fixtures.
// Run after build:preview and tests/preview_server.py; no NAS connections or mutations.
import { createRequire } from 'node:module'
import { readFile, writeFile } from 'node:fs/promises'
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
const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const browser = await chromium.launch({ headless: true, ...(existsSync(edge) ? { executablePath: edge } : {}) })
const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, reducedMotion: 'reduce' })
const page = await context.newPage()
const errors = []
const steps = []
page.on('pageerror', error => errors.push(error.message))
function done(step) { steps.push(step); console.log('PASS:', step) }
const info = (reason, count, message, actual = 'png') => ({ requested_format: 'gif', actual_format: actual, artwork_count: count, reason, message })
const empty = info('empty_library', 0, '当前媒体库为 0 部，没有可轮播海报；请先完成匹配与同步，再生成封面。')
const single = info('single_artwork', 1, '仅取得 1 幅不同海报，已输出静态 PNG；至少需要两幅不同的本库海报才能轮播。')
const animated = info('animated', 3, '已生成 GIF，轮播 3 幅不同的本库海报。', 'gif')
const failedMessage = '备份原生库旧封面失败：图片超过允许大小，已保留原生库图片。'
let fixtureHistory = []
let image = ''
const job = { running: false, done: 4, total: 4, failed: 1, animated: 1, static: 2, fallback: 2,
  message: '完成 4 个媒体库，1 个未完成', errors: [`国产剧 · ${failedMessage}`],
  results: [
    { key: 'attribute:4k', name: '伦理专区', status: 'generated', mime: 'image/png', render_info: empty },
    { key: 'attribute:remux', name: '单张海报库', status: 'generated', mime: 'image/png', render_info: single },
    { key: 'attribute:tvb', name: '正常轮播库', status: 'generated', mime: 'image/gif', render_info: animated },
    { key: 'native:qa:libA', name: '国产剧', native: true, status: 'failed', notice: failedMessage },
  ] }
await page.route('**/api/v1/plugin/MediaArchiver/studio**', async route => {
  const request = route.request()
  if (request.method() === 'GET') {
    const response = await route.fetch()
    const payload = await response.json()
    const data = payload.data ?? payload
    data.job = job
    if (data.views) {
      data.views[0].name = '伦理专区'; data.views[0].count = 0; data.views[0].options.animated = true
      image = data.presets[0].thumbnail
      fixtureHistory = [
        { id: 'empty', batch: 'qa', created: '2026-09-13', key: 'attribute:4k', name: '伦理专区', mime: 'image/png', thumbnail: image, render_info: empty, options: { animated: true } },
        { id: 'single', batch: 'qa', created: '2026-09-13', key: 'attribute:remux', name: '单张海报库', mime: 'image/png', thumbnail: image, render_info: single, options: { animated: true } },
        { id: 'jpeg', batch: 'qa', created: '2026-09-13', key: 'attribute:hdr', name: '旧 JPEG 封面', mime: 'image/jpeg', thumbnail: image, options: { animated: true } },
        { id: 'webp', batch: 'qa', created: '2026-09-13', key: 'attribute:hdr', name: '原生 WebP 备份', native: true, purpose: 'before_native_publish', mime: 'image/webp', thumbnail: image, options: { animated: true } },
      ]
      data.history = fixtureHistory; data.history_count = fixtureHistory.length
    }
    return route.fulfill({ response, json: payload })
  }
  const body = request.postDataJSON()
  if (body.action === 'history_image') {
    const row = fixtureHistory.find(item => item.id === body.id)
    return route.fulfill({ json: { success: true, data: { ...row, image } } })
  }
  const response = await route.fetch()
  if (body.action !== 'preview') return route.fulfill({ response })
  const payload = await response.json()
  const data = payload.data ?? payload
  data.render_info = body.key === 'attribute:4k' ? empty : single
  data.mime = 'image/png'; data.artwork_count = data.render_info.artwork_count
  data.notices = [data.render_info.message]
  return route.fulfill({ response, json: payload })
})
try {
  await page.goto(base)
  await page.locator('.ma-canvas-panel [data-output-reason="empty_library"]').waitFor()
  assert.match(await page.locator('.ma-canvas-panel .ma-output-info').innerText(), /PNG 预览[\s\S]*0 部/)
  assert.equal(await page.locator('.ma-canvas-panel').getByText(empty.message, { exact: true }).count(), 1)
  assert.equal(await page.locator('.ma-library-card[data-library-key="attribute:4k"]').getByText(empty.message, { exact: true }).count(), 1)
  done('Empty-library preview and server card explain static output without duplicated notices')
  await page.getByRole('button', { name: '历史封面' }).click()
  const emptyCard = page.locator('.ma-history-card').filter({ hasText: '伦理专区' })
  assert.match(await emptyCard.innerText(), /PNG[\s\S]*0 部/)
  const oldJpeg = page.locator('.ma-history-card').filter({ hasText: '旧 JPEG 封面' })
  assert.equal(await oldJpeg.locator('.ma-history-image>span').innerText(), 'JPEG')
  assert.match(await oldJpeg.innerText(), /旧记录未保存静态原因/)
  const oldWebp = page.locator('.ma-history-card').filter({ hasText: '原生 WebP 备份' })
  assert.equal(await oldWebp.locator('.ma-history-image>span').innerText(), 'WebP')
  assert.match(await oldWebp.innerText(), /原图备份，保留原格式/)
  assert.doesNotMatch(await oldWebp.innerText(), /旧记录未保存静态原因/)
  await emptyCard.getByRole('button', { name: '查看与下载', exact: true }).click()
  const modal = page.getByRole('dialog', { name: '历史封面预览' })
  assert.match(await modal.locator('.ma-output-info').innerText(), /PNG[\s\S]*0 部/)
  await page.getByRole('button', { name: '关闭预览', exact: true }).click()
  done('History and modal explain PNG; old JPEG/WebP labels and unknown legacy reasons remain truthful')
  await page.getByRole('button', { name: '运行状态', exact: true }).click()
  const results = page.getByRole('region', { name: '最近封面生成结果' })
  assert.equal(await results.locator('.ma-output-summary').innerText(), 'GIF 1 · 静态 2 · 失败 1')
  assert.equal(await results.locator('.ma-result-row').count(), 4)
  assert.match(await results.locator('[data-output-reason="single_artwork"]').innerText(), /1 幅不同海报[\s\S]*两幅不同/)
  assert.match(await results.locator('.ma-result-row.failed').innerText(), /国产剧[\s\S]*备份原生库旧封面失败[\s\S]*已保留/)
  assert.equal(await results.locator('[data-output-reason="empty_library"].failed').count(), 0)
  done('Per-library results separate expected static covers from failed native backup and count actual output')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.ma-toast').waitFor({ state: 'hidden' })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await page.screenshot({ path: path.join(root, 'docs/previews/studio-output-diagnostics-mobile.png'), fullPage: true })
  await page.getByRole('button', { name: /历史封面/ }).click()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await page.screenshot({ path: path.join(root, 'docs/previews/studio-output-history-mobile.png'), fullPage: true })
  done('390px results and history explain long messages without horizontal overflow')
  assert.deepEqual(errors, [])
  await writeFile(path.join(work, 'ui-output-diagnostics.json'), JSON.stringify({ steps, errors }, null, 2))
  console.log(`${steps.length} output diagnostic browser scenarios passed`)
} finally {
  await browser.close()
}
