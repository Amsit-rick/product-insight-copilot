/** Frontend-only regression: shadcn shell, navigation, QA history and responsive layout.
 * Uses an isolated Chrome browser context and the existing demo backend.
 * CMD: set APP_URL=http://127.0.0.1:8010/&& set CDP_URL=http://127.0.0.1:9235&& node frontend\e2e\ui-redesign.mjs
 */
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8010/'
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9235'
const OUT = process.env.UI_ARTIFACT_DIR || fileURLToPath(new URL('../../.tmp/ui-shadcn-2026-11-25/', import.meta.url))
const checks = []
const errors = []
const pending = new Map()
let seq = 0
let sessionId
let contextId
let socket

function record(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) })
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail === undefined ? '' : ` ${JSON.stringify(detail)}`}`)
}
function send(method, params = {}, browser = false) {
  const id = ++seq
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out: ${method}`)) }, 20000)
    pending.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method, params, ...(!browser && sessionId ? { sessionId } : {}) }))
  })
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
  return result.result.value
}
async function until(expression, timeout = 15000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const value = await evaluate(expression)
    if (value) return value
    await new Promise((resolve) => setTimeout(resolve, 120))
  }
  throw new Error(`Condition not met: ${expression.slice(0, 140)}`)
}
async function click(selector) {
  return evaluate(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el || el.disabled || !el.getBoundingClientRect().width) return false; el.click(); return true })()`)
}
async function input(selector, value) {
  return evaluate(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return false; const setter = Object.getOwnPropertyDescriptor(el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set; setter.call(el, ${JSON.stringify(value)}); el.dispatchEvent(new Event('input', { bubbles: true })); return true })()`)
}
async function navigate(hash, selector) {
  await send('Page.navigate', { url: `${APP_URL}${hash}` })
  await until(`Boolean(document.querySelector(${JSON.stringify(selector)}))`)
  await until('!document.querySelector(".el-loading-mask")')
  await new Promise((resolve) => setTimeout(resolve, 250))
}
async function screenshot(name) {
  await until('!document.querySelector(".el-message")', 6000)
  await evaluate('Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => undefined)))')
  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await writeFile(join(OUT, `${name}.png`), Buffer.from(shot.data, 'base64'))
}
async function viewport(width, height) {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false })
  await new Promise((resolve) => setTimeout(resolve, 250))
}
async function layout(name) {
  const data = await evaluate(`(() => { const main=document.querySelector('.pic-main'); return { width: innerWidth, root: document.documentElement.scrollWidth, main: main?.clientWidth, content: main?.scrollWidth } })()`)
  record(name, data.root <= data.width + 1 && (!data.main || data.content <= data.main + 1), data)
}
async function login(username, password, selector) {
  await navigate('#/login', '#login-username')
  await input('#login-username', username)
  await input('#login-password', password)
  await click('.login-submit')
  await until(`Boolean(document.querySelector(${JSON.stringify(selector)}))`)
  await until('!document.querySelector(".el-loading-mask")')
}
const axe = await readFile(fileURLToPath(new URL('../node_modules/axe-core/axe.min.js', import.meta.url)), 'utf8')
async function a11y(name) {
  await evaluate(axe)
  const violations = await evaluate(`(async () => { const r=await axe.run(document,{runOnly:{type:'tag', values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}}); return r.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)})) })()`)
  record(name, violations.length === 0, violations)
}

try {
  await mkdir(OUT, { recursive: true })
  const version = await (await fetch(`${CDP_URL}/json/version`)).json()
  socket = new WebSocket(version.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject })
  socket.onmessage = ({ data }) => {
    const item = JSON.parse(data)
    if (item.id && pending.has(item.id)) {
      const request = pending.get(item.id)
      clearTimeout(request.timer)
      pending.delete(item.id)
      if (item.error) request.reject(new Error(JSON.stringify(item.error)))
      else request.resolve(item.result)
    } else if (item.method === 'Runtime.exceptionThrown') errors.push(item.params.exceptionDetails.text)
  }
  contextId = (await send('Target.createBrowserContext', {}, true)).browserContextId
  const target = await send('Target.createTarget', { url: 'about:blank', browserContextId: contextId }, true)
  sessionId = (await send('Target.attachToTarget', { targetId: target.targetId, flatten: true }, true)).sessionId
  await send('Page.enable')
  await send('Runtime.enable')
  await viewport(1440, 1000)
  await navigate('#/login', '#login-username')
  record('登录页使用 shadcn 原生输入与按钮', await evaluate('Boolean(document.querySelector("input[data-slot=input]")) && Boolean(document.querySelector("button[data-slot=button]"))'))
  await screenshot('login-desktop')
  await a11y('登录页 WCAG A/AA')
  await login('ops', 'ops123', '.tk__stats')
  record('客服导航按权限分组', await evaluate(`document.querySelectorAll('.pic-aside .pic-nav-group').length === 2 && ![...document.querySelectorAll('.pic-aside a')].some(a=>a.getAttribute('href') === '#/system')`))
  await layout('工单桌面无整页横向溢出')
  await screenshot('tickets-desktop')
  await a11y('工单页 WCAG A/AA')
  await input('input[aria-label="搜索工单"]', '没有对应工单的界面回归关键词')
  await click('button[aria-label="查询工单"]')
  record('搜索按钮可筛选工单', await until('document.body.innerText.includes("没有符合条件的工单")'))
  await click('button[aria-label="清除工单搜索"]')
  record('清除搜索恢复工单', await until('document.querySelectorAll(".el-table__row").length > 0'))
  await click('[aria-label="收起侧边导航"]')
  record('侧栏可以收起', await evaluate('document.querySelector(".pic-workspace").classList.contains("is-sidebar-collapsed")'))
  record('收起后的导航链接仍有业务名称', await evaluate('[...document.querySelectorAll(".pic-aside .pic-nav-item")].every(el => Boolean(el.getAttribute("aria-label")))'))
  await click('[aria-label="展开侧边导航"]')
  await click('.pic-skip-link')
  record('跳转到内容不改变业务路由', await evaluate('document.activeElement?.id === "workspace-content" && location.hash === "#/tickets"'))
  await click('[data-testid="account-menu-trigger"]')
  record('账号菜单显示退出入口', await until('Boolean(document.querySelector("[data-slot=dropdown-menu-content] [data-slot=dropdown-menu-item]"))'))
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await navigate('#/qa', '.qa__welcome')
  record('问答初次进入首页', await evaluate('document.querySelectorAll(".qa__turn").length === 0 && !document.querySelector(".qa__conversation-item.is-active")'))
  record('首页按实际可见知识库展示范围，撤掉固定承诺式示例', await evaluate('document.querySelectorAll(".qa__examples button").length > 0 && !document.querySelector(".qa__examples").innerText.includes("抱怨最多")'))
  record('未启用生成模型时明确显示检索模式', await until('document.querySelector("[data-testid=qa-model-mode]")?.innerText.includes("资料检索")'))
  record('检索设置默认收起', await evaluate('document.querySelector("#qa-retrieval-settings").getBoundingClientRect().height === 0'))
  await click('[data-testid="qa-settings-toggle"]')
  record('检索设置可以展开', await evaluate('document.querySelector("#qa-retrieval-settings").getBoundingClientRect().height > 0'))
  await click('[data-testid="qa-settings-toggle"]')
  await layout('问答桌面无整页横向溢出')
  await screenshot('qa-home-desktop')
  await a11y('问答首页 WCAG A/AA')
  await input('.qa__composer textarea', '客服工单里用户抱怨最多的是哪几类问题？')
  await click('.qa__composer-bar button')
  await until('Boolean(document.querySelector(".qa__answer"))', 30000)
  record('回答正文直接显示，引用默认折叠', await evaluate(`Boolean(document.querySelector('.qa__answer')?.innerText.trim()) && !document.querySelector('.qa__supporting-details .el-collapse-item.is-active')`))
  record('原问题仍如实标记依据不足', await evaluate('document.querySelector(".qa__answer-state")?.dataset.answerState === "insufficient"'))
  record('拒答主视图不重复堆警告、不暴露碎词阈值', await evaluate(`(() => { const turn=document.querySelector('.qa__turn'); const body=turn.querySelector('.el-card__body'); return !body.querySelector('.el-alert') && !/coverage|feedback_kb|关键词覆盖|阈值|户抱|怨最/.test(body.innerText) && !body.querySelector('.qa__retrieval-details.is-active') })()`))
  await click('.qa__retrieval-details .el-collapse-item__header')
  record('折叠说明保留原始拒答判定，可供核对', await until('document.querySelector(".qa__retrieval-details").innerText.includes("阈值")'))
  await click('.qa__retrieval-details .el-collapse-item__header')
  await screenshot('qa-answer-desktop')
  await navigate('#/tickets', '.tk__stats')
  await navigate('#/qa', '.qa__welcome')
  record('有历史时再次进入仍是首页', await evaluate('document.querySelectorAll(".qa__turn").length === 0 && document.querySelectorAll(".qa__conversation-item").length > 0'))
  await input('.qa__history-search input', '不匹配的会话过滤词')
  record('会话搜索显示真实空结果', await until('document.querySelectorAll(".qa__conversation-item").length === 0 && document.body.innerText.includes("没有匹配的会话")'))
  await input('.qa__history-search input', '')
  await until('document.querySelectorAll(".qa__conversation-item").length > 0')
  await click('.qa__conversation-item')
  record('历史会话可以恢复', await until('Boolean(document.querySelector(".qa__answer"))'))
  await a11y('历史问答 WCAG A/AA')
  await evaluate(`[...document.querySelectorAll('.qa__conversations-head button')].find(b=>b.innerText.includes('新建会话')).click()`)
  await until('Boolean(document.querySelector(".qa__welcome"))')
  await input('.qa__composer textarea', '物流停滞超过48小时的补偿标准是什么？')
  await click('.qa__composer-bar button')
  await until('Boolean(document.querySelector(".qa__answer-state[data-answer-state=retrieval-only]"))')
  record('命中资料但未生成归纳时不伪装成答案', await evaluate('document.querySelector(".qa__answer").innerText.includes("尚未生成归纳答案") && !document.querySelector(".qa__supporting-details .is-active")'))
  await viewport(390, 844)
  await layout('问答手机无整页横向溢出')
  const composer = await evaluate('(() => { const r=document.querySelector(".qa__composer").getBoundingClientRect(); return {bottom:r.bottom,top:r.top,height:innerHeight} })()')
  record('手机提问框保持可见', composer.bottom <= composer.height + 1 && composer.top > 0, composer)
  await screenshot('qa-mobile')
  await click('[aria-label="打开主导航"]')
  await until('Boolean(document.querySelector("[data-slot=sheet-content]"))')
  record('手机导航为带焦点约束的对话框', await evaluate(`document.querySelector('[data-slot=sheet-content]').getAttribute('role') === 'dialog' && Boolean(document.querySelector('[data-slot=sheet-title]'))`))
  await screenshot('navigation-mobile')
  await a11y('手机导航 WCAG A/AA')
  await click('[data-slot="sheet-content"] a[href="#/tickets"]')
  await until('Boolean(document.querySelector(".tk__stats")) && !document.querySelector("[data-slot=sheet-content]")')
  record('选择手机导航后自动关闭', true)
  await layout('工单手机无整页横向溢出')
  await screenshot('tickets-mobile')
  await a11y('手机工单 WCAG A/AA')
  await viewport(1440, 1000)
  await click('.pic-header-logout')
  await until('Boolean(document.querySelector("#login-username"))')
  await login('reviewer', 'reviewer123', '.tk__stats')
  await navigate('#/approvals', '.pic-page-header')
  await layout('审核桌面无整页横向溢出')
  await screenshot('approvals-desktop')
  await navigate('#/quality-review', '.quality-page')
  await screenshot('quality-desktop')
  await viewport(1024, 768)
  await layout('质检平板无整页横向溢出')
  await viewport(390, 844)
  await layout('质检手机无整页横向溢出')
  await viewport(1440, 1000)
  await navigate('#/ticket-policies', '.pl')
  await layout('政策库桌面无整页横向溢出')
  await screenshot('policies-desktop')
  await viewport(390, 844)
  await layout('政策库手机无整页横向溢出')
  await screenshot('policies-mobile')
  await viewport(1440, 1000)
  await click('.pic-header-logout')
  await until('Boolean(document.querySelector("#login-username"))')
  await login('admin', 'admin123', '.pic-page-header')
  record('管理员拥有完整分组导航', await evaluate('document.querySelectorAll(".pic-aside .pic-nav-group").length === 4'))
  for (const [path, name] of [['/', 'workbench'], ['/knowledge', 'knowledge'], ['/metrics', 'metrics'], ['/system', 'system'], ['/evaluations', 'evaluations'], ['/policies', 'access-control'], ['/audit', 'audit'], ['/tasks/new', 'new-task']]) {
    await navigate(`#${path}`, '.pic-page-header')
    const heading = await evaluate('document.querySelector(".pic-page-header h1")?.innerText')
    record(`${name} 页面有清晰标题`, Boolean(heading), heading)
    await layout(`${name} 桌面布局`)
    if (['workbench', 'knowledge', 'system'].includes(name)) await screenshot(`${name}-desktop`)
    await viewport(1024, 768)
    await layout(`${name} 平板布局`)
    await viewport(390, 844)
    await layout(`${name} 手机布局`)
    await viewport(1440, 1000)
  }
  record('全程无未捕获前端异常', errors.length === 0, errors)
} catch (error) {
  record('浏览器验证完整执行', false, error.message)
  if (sessionId) await screenshot('failure').catch(() => undefined)
} finally {
  await writeFile(join(OUT, 'ui-redesign.json'), JSON.stringify({ app: APP_URL, environment: 'isolated SQLite + stub model, seeded fictional records', checks, passed: checks.filter(x => x.ok).length, failed: checks.filter(x => !x.ok).length }, null, 2))
  if (contextId) await send('Target.disposeBrowserContext', { browserContextId: contextId }, true).catch(() => undefined)
  if (socket) socket.close()
}
console.log(`\n${checks.filter(x => x.ok).length}/${checks.length} passed. Artifacts: ${OUT}`)
process.exitCode = checks.some(x => !x.ok) ? 1 : 0
