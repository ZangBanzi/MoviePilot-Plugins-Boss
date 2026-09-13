// Template selection, editor capabilities and real rendering through local QA APIs.
// Start a fresh tests/preview_server.py after build:preview, separately from other UI suites.
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
const errors = [], apiErrors = [], requests = [], steps = []
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => { if (request.url().endsWith('/studio/action')) requests.push(request.postDataJSON()) })
page.on('response', response => { if (response.url().endsWith('/studio/action') && response.status() >= 400) apiErrors.push(response.status()) })
page.on('dialog', dialog => dialog.accept())
const preset = id => page.locator(`.ma-preset[data-preset-id="${id}"]`)
const library = key => page.locator(`.ma-library-card[data-library-key="${key}"]`)
const responseFor = (action, predicate = () => true) => page.waitForResponse(response => {
  if (!response.url().endsWith('/studio/action')) return false
  const payload = response.request().postDataJSON()
  return payload?.action === action && predicate(payload)
}, { timeout: 30000 })
function done(step) { steps.push(step); console.log('PASS:', step) }
async function ready() {
  await page.waitForFunction(() => document.querySelector('.ma-canvas>img')?.naturalWidth > 0 && !document.querySelector('.ma-render-pill'), { timeout: 30000 })
}
async function readState() {
  const response = await page.request.get(base + '/api/v1/plugin/MediaArchiver/studio', { headers: { Authorization: 'Bearer local-test' } })
  const value = await response.json()
  assert.notEqual(value.success, false)
  return value.data ?? value
}
async function choose(id) {
  const wait = responseFor('preview', payload => payload.options.style === id)
  await preset(id).click()
  const response = await wait
  const value = await response.json()
  const data = value.data ?? value
  assert.notEqual(value.success, false)
  assert.equal(data.mime, 'image/png')
  assert.ok(data.image.startsWith('data:image/png;base64,'))
  await ready()
  assert.equal(await preset(id).getAttribute('aria-pressed'), 'true')
  return response.request().postDataJSON().options
}
async function edit(key) {
  const wait = responseFor('preview', payload => payload.key === key)
  await library(key).locator('.ma-library-editor').click()
  await wait
  await ready()
}

try {
  await page.goto(base)
  await page.waitForFunction(() => document.querySelectorAll('.ma-library-card input:checked').length === 6)
  await ready()
  const initial = await readState()
  assert.equal(initial.presets.length, 10)
  assert.equal(await page.locator('.ma-preset').count(), 10)
  assert.ok(await page.locator('.ma-preset img').evaluateAll(images => images.every(image => image.naturalWidth > 0)))
  await page.getByLabel('主标题', { exact: true }).fill('模板验收影院')
  await page.getByLabel('副标题', { exact: true }).fill('MY COLLECTION')
  await page.getByRole('combobox', { name: /^海报来源/ }).selectOption('Primary')
  await page.getByRole('combobox', { name: /^素材排序/ }).selectOption('name')
  await page.getByRole('combobox', { name: /^输出分辨率/ }).selectOption('960')
  await page.getByLabel('动态封面', { exact: true }).uncheck()
  for (const item of initial.presets) {
    const options = await choose(item.id)
    assert.equal(options.title, '模板验收影院')
    assert.equal(options.subtitle, 'MY COLLECTION')
    assert.equal(options.source, 'Primary')
    assert.equal(options.sort, 'name')
    assert.equal(options.resolution, 960)
    assert.equal(options.animated, false)
  }
  done('All ten template thumbnails and real previews render while preserving text, source and output settings')

  await choose('editorial')
  for (const id of ['stack', 'diagonal', 'wall', 'minimal']) {
    const options = await choose(id)
    assert.equal(options.foreground.toUpperCase(), '#F5F7FF')
    assert.equal(options.background, '')
    assert.equal(options.accent, '')
    await choose('editorial')
  }
  done('Switching from the light editorial theme resets all original templates to their correct palette')

  await page.getByRole('button', { name: '编辑画布布局', exact: true }).click()
  await page.getByRole('button', { name: '布局与配色', exact: true }).click()
  const names = { text_x: '标题横向位置', text_y: '标题纵向位置', image_x: '海报横向位置', image_y: '海报纵向位置', image_scale: '海报缩放', blur: '背景模糊', overlay: '背景压暗' }
  for (const item of initial.presets.filter(item => item.fixed_layout)) {
    await choose(item.id)
    assert.equal(await page.locator('.ma-drag-box').count(), 0)
    assert.match(await page.locator('.ma-composition-note').innerText(), new RegExp(item.name))
    for (const field of item.hidden_fields) assert.equal(await page.getByRole('slider', { name: names[field], exact: true }).count(), 0)
  }
  await choose('cinema')
  assert.equal(await page.locator('.ma-drag-image').count(), 0)
  await page.getByRole('button', { name: '移动标题，支持方向键', exact: true }).focus()
  await page.keyboard.press('ArrowRight')
  assert.equal(await page.getByRole('slider', { name: '标题横向位置', exact: true }).inputValue(), '8')
  done('Fixed compositions hide unsupported controls and drag handles; cinema keeps movable typography')

  await choose('editorial')
  await page.getByLabel('标题颜色', { exact: true }).fill('#345678')
  await page.getByRole('button', { name: '标题与文案', exact: true }).click()
  await page.getByLabel('主标题', { exact: true }).fill('我的纸感片库')
  await page.getByRole('button', { name: '添加方案', exact: true }).click()
  await page.getByLabel('方案名称', { exact: true }).fill('刊物自定义验证')
  await page.getByRole('button', { name: '保存为我的方案', exact: true }).click()
  await page.getByRole('button', { name: '刊物自定义验证', exact: true }).waitFor()
  await choose('stack')
  const customWait = responseFor('preview', payload => payload.options.style === 'editorial')
  await page.getByRole('button', { name: '刊物自定义验证', exact: true }).click()
  const custom = (await customWait).request().postDataJSON().options
  assert.equal(custom.foreground, '#345678')
  assert.equal(custom.title, '我的纸感片库')
  const exportWait = page.waitForEvent('download')
  await page.getByRole('button', { name: '分享方案', exact: true }).click()
  const presetPath = path.join(work, 'template-preset.json')
  await (await exportWait).saveAs(presetPath)
  await choose('stack')
  const importWait = responseFor('preview', payload => payload.options.style === 'editorial')
  await page.locator('input[type=file]').nth(0).setInputFiles(presetPath)
  const imported = (await importWait).request().postDataJSON().options
  assert.deepEqual(imported, custom)
  await page.getByRole('status').filter({ hasText: '方案已导入' }).waitFor()
  done('Custom templates preserve full overrides through selection, export and JSON reimport')

  const nativeKey = initial.native_views[0].key
  await choose('mosaic')
  await edit(nativeKey)
  await choose('triptych')
  await edit('attribute:4k')
  assert.equal(await preset('mosaic').getAttribute('aria-pressed'), 'true')
  await edit(nativeKey)
  assert.equal(await preset('triptych').getAttribute('aria-pressed'), 'true')
  done('Native and virtual libraries retain their own new-template drafts when switching editors')

  await page.getByRole('button', { name: '全选', exact: true }).click()
  await page.getByRole('button', { name: '反选', exact: true }).click()
  await library(nativeKey).getByRole('checkbox').check()
  await library('attribute:4k').getByRole('checkbox').check()
  await choose('cinema')
  await page.getByLabel('主标题', { exact: true }).fill('')
  await page.getByLabel('动态封面', { exact: true }).uncheck()
  const batchWait = responseFor('generate')
  await page.getByRole('button', { name: '生成并应用已选库', exact: true }).click()
  const batch = (await batchWait).request().postDataJSON()
  assert.deepEqual(new Set(batch.keys), new Set(['attribute:4k', nativeKey]))
  assert.equal(batch.options.style, 'cinema')
  await page.waitForFunction(async () => {
    const response = await fetch('/api/v1/plugin/MediaArchiver/studio?light=true', { headers: { Authorization: 'Bearer local-test' } })
    const value = await response.json()
    return !(value.data ?? value).job.running
  }, { timeout: 40000 })
  await page.waitForFunction(() => !document.querySelector('.ma-job'), { timeout: 15000 })
  const generated = await readState()
  assert.equal(generated.job.failed, 0)
  assert.equal(generated.job.results.length, 2)
  assert.ok(generated.job.results.every(item => item.style === 'cinema'))
  assert.ok(generated.history.filter(item => item.purpose !== 'before_native_publish').every(item => item.options.style === 'cinema'))
  assert.deepEqual(generated.views.find(item => item.key === 'attribute:remux').options, initial.views.find(item => item.key === 'attribute:remux').options)
  done('Batch generation applies cinema to checked native and virtual libraries while leaving unchecked plans unchanged')

  await edit('attribute:4k')
  await page.getByLabel('动态封面', { exact: true }).check()
  await ready()
  const animatedWait = responseFor('preview', payload => payload.animated === true)
  await page.getByRole('button', { name: '播放动图预览', exact: true }).click()
  const animatedValue = await (await animatedWait).json()
  assert.equal((animatedValue.data ?? animatedValue).mime, 'image/gif')
  await page.getByRole('button', { name: '暂停动图预览', exact: true }).waitFor()
  done('The new cinema layout plays a real GIF from the selected virtual library')

  await choose('editorial')
  await page.getByRole('button', { name: '编辑画布布局', exact: true }).click()
  await page.locator('.ma-toast').waitFor({ state: 'detached' })
  await page.locator('.ma-workspace').screenshot({ path: path.join(output, 'studio-templates.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  await choose('mosaic')
  const grid = await page.locator('.ma-presets').evaluate(element => ({
    columns: getComputedStyle(element).gridTemplateColumns.split(' ').length,
    height: element.clientHeight, scroll: element.scrollHeight,
  }))
  assert.equal(grid.columns, 2)
  assert.ok(grid.height <= 342 && grid.scroll > grid.height)
  const cardBounds = await preset('mosaic').boundingBox()
  const copyBounds = await preset('mosaic').locator('.ma-preset-copy').boundingBox()
  assert.ok(cardBounds.height >= 120)
  assert.ok(copyBounds.y + copyBounds.height <= cardBounds.y + cardBounds.height)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  assert.equal(await preset('mosaic').getAttribute('aria-pressed'), 'true')
  await page.screenshot({ path: path.join(output, 'studio-templates-mobile.png'), fullPage: true })
  done('The ten-template gallery stays readable, scrollable and selectable at 390 pixels without overflow')

  assert.deepEqual(errors, [])
  assert.deepEqual(apiErrors, [])
} finally {
  await writeFile(path.join(work, 'ui-template-results.json'), JSON.stringify({ steps, errors, apiErrors }, null, 2))
  await browser.close()
}
