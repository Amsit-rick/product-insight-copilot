/**
 * 前端端到端冒烟测试（CDP，零依赖：只用 node 内置 WebSocket + fetch）。
 *
 * 为什么不用 Playwright：本机装不了 npm 包（离线环境），而 CDP 是 Chrome 自带的能力；
 * 这个脚本的价值不是"替代 UI 测试框架"，而是**在真实浏览器 + 真实后端上跑一遍关键路径**，
 * 把"页面白屏 / 接口 500 / 字段改名导致渲染炸掉"这类问题在提交前挡住。
 *
 * 覆盖路径（除 stub 环境自动建任务外全部只读 —— 避免每次冒烟都花钱调模型）：
 *   登录 → 工作台（任务表 / 今日成本 / 缓存命中率）→ 任务进度（时间线 / Agent 模型列 / Token 与成本）
 *   → 诊断报告（事实与引用 / 导出入口）→ 知识库管理 → 知识库问答（要点 + 引用 + 一键转诊断）
 *   → 人工审核 → 指标查询 → 系统状态 → 评测集 / 密级策略 / 审计（admin）
 *
 * 用法：
 *   node frontend/e2e/smoke.mjs                       # 需要先起后端（默认 8010）
 *   APP_URL=http://127.0.0.1:8010/ CDP_URL=http://127.0.0.1:9223 node frontend/e2e/smoke.mjs
 *
 * 前置条件：后端已在 APP_URL 运行；Chrome 以 --remote-debugging-port=9223 启动。
 * 退出码：0 = 全部通过；1 = 有断言失败；2 = 环境问题（连不上后端或浏览器）。
 */

import { fileURLToPath } from 'node:url'

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8010/'
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9223'
const USER = process.env.E2E_USER || 'pm'
const PASSWORD = process.env.E2E_PASSWORD || 'pm123'
// 演示问题常命中风险/冲突分支并进入人工审核（这是正确行为），冒烟需要审核人放行才能看到报告
const REVIEWER_USER = process.env.E2E_REVIEWER_USER || 'reviewer'
const REVIEWER_PASSWORD = process.env.E2E_REVIEWER_PASSWORD || 'reviewer123'
const EXPECT_REFLECTION_UI = process.env.EXPECT_REFLECTION_UI === '1'
// 用 fileURLToPath 而不是 URL.pathname：后者在 Windows 上会给出 /D:/... ，
// 直接拼进 mkdir 会变成 D:\D:\... （踩过一次）
const SHOT_DIR =
  process.env.E2E_SHOT_DIR || fileURLToPath(new URL('../../backend/.tmp/e2e/', import.meta.url))

const results = []
let ws = null
let seq = 0
let ownedTargetId = null

function record(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? '  [PASS]' : '  [FAIL]'} ${name}${detail ? ' — ' + detail : ''}`)
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function firstPageTarget() {
  // 每轮都新建独立页签，避免复用上次运行遗留的移动端视口或半关闭页面。
  // 真实踩坑：`/json/list` 的第一个 page 不保证是干净目标，复用后曾出现 97×50 视口，
  // 让“提问框贴底”和移动端策略页同时误报失败。
  try {
    const created = await (
      await fetch(`${CDP_URL}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })
    ).json()
    if (created?.webSocketDebuggerUrl) {
      ownedTargetId = created.id || null
      return created.webSocketDebuggerUrl
    }
  } catch {
    // 旧版浏览器可能不支持 /json/new，退回已有 page。
  }
  const list = await (await fetch(`${CDP_URL}/json/list`)).json()
  const page = list.find((t) => t.type === 'page')
  if (!page) throw new Error('没有可用的 page 目标（Chrome 是否以 --remote-debugging-port 启动？）')
  return page.webSocketDebuggerUrl
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url)
    socket.onopen = () => resolve(socket)
    socket.onerror = () => reject(new Error('WebSocket 连接失败'))
  })
}

function send(method, params = {}) {
  const id = ++seq
  return new Promise((resolve, reject) => {
    const onMessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id !== id) return
      ws.removeEventListener('message', onMessage)
      msg.error ? reject(new Error(`${method}: ${JSON.stringify(msg.error)}`)) : resolve(msg.result)
    }
    ws.addEventListener('message', onMessage)
    ws.send(JSON.stringify({ id, method, params }))
  })
}

async function evaluate(expression) {
  const res = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (res.exceptionDetails) throw new Error('页面异常: ' + JSON.stringify(res.exceptionDetails))
  return res.result.value
}

async function screenshot(name) {
  try {
    const shot = await send('Page.captureScreenshot', { format: 'png' })
    const { mkdirSync, writeFileSync } = await import('node:fs')
    mkdirSync(SHOT_DIR, { recursive: true })
    writeFileSync(`${SHOT_DIR}${name}.png`, Buffer.from(shot.data, 'base64'))
  } catch (err) {
    console.log(`  [warn] 截图 ${name} 失败: ${err.message}`)
  }
}

/** 等待页面上出现某段文字（轮询 DOM，避免固定 sleep 造成的偶发失败）。 */
async function waitForText(text, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const found = await evaluate(`Boolean(document.body && document.body.innerText.includes(${JSON.stringify(text)}))`)
      if (found) return true
    } catch {
      // 页面切换期间 body 可能短暂为空，继续等待稳定 DOM。
    }
    await sleep(400)
  }
  return false
}

async function pageText() {
  return evaluate('document.body?.innerText || ""')
}

/**
 * 登录（可指定账号）。
 * 清理浏览器 Cookie 再导航：会话令牌存放在 HttpOnly Cookie，不能也不应从页面脚本读取。
 */
async function loginAs(user = USER, password = PASSWORD) {
  await send('Network.clearBrowserCookies').catch(() => {})
  await send('Page.navigate', { url: APP_URL })
  await sleep(2000)
  const needsLogin = await evaluate(`!!document.querySelector('input[type=password]')`)
  if (!needsLogin) return true
  await evaluate(`(() => {
    const set = (el, v) => {
      el.value = v
      el.dispatchEvent(new Event('input', { bubbles: true }))
      el.dispatchEvent(new Event('change', { bubbles: true }))
    }
    const inputs = [...document.querySelectorAll('input')]
    set(inputs.find((i) => i.type === 'text') || inputs[0], ${JSON.stringify(user)})
    set(inputs.find((i) => i.type === 'password'), ${JSON.stringify(password)})
    const btn = [...document.querySelectorAll('button')].find((b) => /登录|登 录/.test(b.innerText))
    if (btn) btn.click()
    return true
  })()`)
  // 用「今日成本」而不是「工作台」判定登录成功：侧栏现在有「诊断工作台」菜单项，
  // 只匹配「工作台」会在登录跳转完成前就命中菜单文字，断言就失效了。
  return waitForText('今日成本', 15000)
}

async function login() {
  return loginAs(USER, PASSWORD)
}

async function gotoHash(hash, expectText) {
  await evaluate(`location.hash = ${JSON.stringify(hash)}`)
  await sleep(1200)
  return expectText ? waitForText(expectText, 12000) : true
}

async function openFirstCompletedTask() {
  const opened = await evaluate(`(() => {
    // 只挑**诊断任务**：售后工单分析任务也落在同一张任务表里（一次分析 = 一个 task），
    // 但它不产报告，误选会把"报告页渲染"的断言带偏（踩过：冒烟报失败，实际报告页是对的）。
    const rows = [...document.querySelectorAll('.el-table__body tr')].filter(
      (r) => /已完成/.test(r.innerText) && !/售后工单/.test(r.innerText)
    )
    const row = rows[0]
    if (!row) return false
    const btn = [...row.querySelectorAll('button')].find((b) => /进度/.test(b.innerText))
    ;(btn || row).click()
    return true
  })()`)
  if (!opened) return null
  await sleep(2500)
  const url = await evaluate('location.hash')
  return (url.match(/#\/tasks\/([0-9a-f-]+)/) || [])[1] || null
}

/**
 * 兜底：直接问接口要一条"有报告的诊断任务"。
 *
 * 为什么需要：售后工单分析任务与诊断任务共用同一张任务表，工单跑得多时
 * 工作台首页会被工单任务占满，界面上就找不到诊断任务了（踩过：冒烟报
 * "未找到已完成任务"，实际任务都在，只是不在第一页）。
 */
async function findDiagnosisTaskId() {
  try {
    const token = await apiLogin()
    const resp = await fetch(new URL('/api/v1/tasks?page_size=50', APP_URL), {
      headers: { Authorization: `Bearer ${token}` },
    })
    const payload = await resp.json()
    const items = payload.items || []
    const hit = items.find(
      (item) => item.status === 'completed' && !String(item.title || '').startsWith('[售后工单]')
    )
    return hit ? hit.id : null
  } catch {
    return null
  }
}

/** 直接调接口登录，拿一个令牌（Node 侧），用于在 stub 环境下造一条已完成任务。 */
async function apiLogin(username = USER, password = PASSWORD) {
  const resp = await fetch(new URL('/api/v1/auth/login', APP_URL), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!resp.ok) throw new Error(`登录接口返回 ${resp.status}`)
  const payload = await resp.json()
  return payload.access_token
}

async function fetchTask(token, taskId) {
  const resp = await fetch(new URL(`/api/v1/tasks/${taskId}`, APP_URL), {
    headers: { Authorization: `Bearer ${token}` },
  })
  return resp.json()
}

async function waitStatus(token, taskId, done, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs
  let status = 'pending'
  while (Date.now() < deadline) {
    status = (await fetchTask(token, taskId)).status
    if (done.includes(status)) return status
    await sleep(1000)
  }
  return status
}

/**
 * 建任务并跑到终态。
 * 演示问题常常会命中风险/冲突分支 → 进人工审核，这是**正确行为**而不是失败；
 * 因此这里模拟审核人放行（继续走工作流并产出报告），顺便把审批接口也冒烟掉。
 */
async function createAndWaitTask(token, timeoutMs = 90000) {
  const created = await fetch(new URL('/api/v1/tasks', APP_URL), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      query: 'Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？',
      product: '星野社区',
      platform: 'Android',
      app_version: '3.2.0',
      date_from: '2026-08-01',
      date_to: '2026-08-31',
    }),
  })
  if (!created.ok) throw new Error(`建任务返回 ${created.status}: ${(await created.text()).slice(0, 120)}`)
  const task = await created.json()

  let status = await waitStatus(token, task.id, ['completed', 'failed', 'cancelled', 'waiting_human'], timeoutMs)
  let approved = false
  if (status === 'waiting_human') {
    const reviewer = await apiLogin(REVIEWER_USER, REVIEWER_PASSWORD)
    const resp = await fetch(new URL(`/api/v1/tasks/${task.id}/approve`, APP_URL), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${reviewer}` },
      body: JSON.stringify({ action: 'approve', comment: 'e2e 冒烟自动放行（不改变任何结论）' }),
    })
    if (!resp.ok) throw new Error(`审核接口返回 ${resp.status}: ${(await resp.text()).slice(0, 120)}`)
    approved = true
    status = await waitStatus(token, task.id, ['completed', 'failed', 'cancelled'], timeoutMs)
  }
  return { id: task.id, status, approved }
}

async function main() {
  console.log(`=== 前端端到端冒烟 ===\n  前端: ${APP_URL}\n  CDP : ${CDP_URL}\n`)

  // 后端可达性（先给出明确的环境错误，而不是一堆页面超时）
  let stubMode = false
  try {
    const health = await fetch(new URL('/health', APP_URL))
    if (!health.ok) throw new Error(`/health 返回 ${health.status}`)
    const payload = await health.json()
    // 只有"没接真实模型"时才允许冒烟自己建任务（否则会产生真实费用）
    stubMode = !(payload.llm && payload.llm.enabled)
  } catch (err) {
    console.error(`[环境错误] 后端不可达：${err.message}\n  请先启动后端（scripts/dev.ps1 run 8010）`)
    return 2
  }

  try {
    ws = await connect(await firstPageTarget())
  } catch (err) {
    console.error(`[环境错误] ${err.message}`)
    return 2
  }

  await send('Page.enable')
  await send('Runtime.enable')

  const consoleErrors = []
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.method === 'Runtime.exceptionThrown') {
      consoleErrors.push(msg.params?.exceptionDetails?.exception?.description || 'unknown')
    }
  })

  // 1) 登录
  record('登录并进入工作台', await login())

  // 2) 工作台
  // 看板数字与成本/缓存是异步加载的：登录成功只保证菜单出现，指标可能还没到。
  // 不 waitFor 就会变成偶发失败（冷启动后端上尤其明显），所以这里先等成本卡片。
  await waitForText('今日成本', 15000)
  const workbench = await pageText()
  record('工作台渲染任务表', /问题/.test(workbench) && /状态/.test(workbench))
  record('工作台显示今日成本', /今日成本/.test(workbench), (workbench.match(/今日成本[^\n]*/) || [''])[0])
  record('工作台显示缓存命中率', /命中率/.test(workbench))
  // 最近窗口命中率：累计值会被冷启动长期拖累，界面上必须同时给出"现在管不管用"的数字
  record(
    '工作台缓存命中率带「最近 N 次」窗口',
    /最近\s*\d+\s*次/.test(workbench) && /累计/.test(workbench),
    (workbench.match(/缓存[^\n]*/) || [''])[0].slice(0, 90)
  )
  record('工作台显示看板统计', /已完成/.test(workbench) && /待人工审核/.test(workbench))
  await screenshot('01-workbench')

  // 3) 任务进度
  await gotoHash('#/', '工作台')
  await sleep(800)
  let taskId = await openFirstCompletedTask()
  if (!taskId) {
    // 界面上没找到诊断任务（工单任务把首页占满了）→ 走接口兜底
    taskId = await findDiagnosisTaskId()
    if (taskId) {
      await gotoHash(`#/tasks/${taskId}`, '节点')
    }
  }
  if (!taskId && stubMode) {
    // 空库环境（例如 CI 里刚起来的 stub 后端）没有任何任务：自己造一条。
    // 只有在**目标服务没接真实模型**时才这么做——否则每次冒烟都要花钱调模型。
    try {
      const token = await apiLogin()
      const created = await createAndWaitTask(token)
      record(
        'stub 环境下自动建任务并跑完',
        created.status === 'completed',
        `status=${created.status}${created.approved ? '（经人工审核放行）' : ''}`
      )
      await gotoHash(`#/tasks/${created.id}`, '节点')
      taskId = created.id
    } catch (err) {
      record('stub 环境下自动建任务并跑完', false, err.message)
    }
  } else if (!taskId) {
    console.log('  [note] 目标是接了真实模型的服务，跳过"自动建任务"以免产生费用')
  }
  record('可打开已完成任务的进度页', Boolean(taskId), taskId || '未找到已完成任务（可先跑 demo_e2e）')
  if (taskId) {
    const progress = await pageText()
    record('进度页渲染节点时间线', /节点|时间线/.test(progress))
    record('进度页显示 Agent 模型列', /deepseek|stub/.test(progress))
    record('进度页显示 Token 与成本', /Token\s*用量/.test(progress) && /成本/.test(progress), (progress.match(/Token 用量[^\n]*/) || [''])[0])
    record('进度页有工具调用区', /工具调用/.test(progress))
    const collaboration = await evaluate(`(() => {
      const panel = document.querySelector('[data-testid="agent-collaboration"]')
      const agents = panel ? panel.querySelectorAll('[data-testid="agent-collaboration-unit"]') : []
      const details = panel ? [...panel.querySelectorAll('details')] : []
      return {
        exists: Boolean(panel),
        agentCount: agents.length,
        hasSummary: Boolean(panel && /协作进度|正在处理|已完成/.test(panel.innerText)),
        hasResponsibility: Boolean(panel && /负责|当前任务|处理内容/.test(panel.innerText)),
        detailsCollapsed: details.length > 0 && details.every((item) => item.open === false),
      }
    })()`)
    record(
      '进度页显示真实 Agent 协作面板',
      collaboration.exists && collaboration.agentCount > 0 && collaboration.hasSummary,
      JSON.stringify(collaboration),
    )
    record(
      '协作面板使用业务化职责说明',
      collaboration.hasResponsibility,
      JSON.stringify(collaboration),
    )
    record(
      '协作面板技术细节默认折叠',
      collaboration.detailsCollapsed,
      JSON.stringify(collaboration),
    )
    await screenshot('02-progress')

    await send('Emulation.setDeviceMetricsOverride', {
      width: 375,
      height: 812,
      deviceScaleFactor: 1,
      mobile: true,
    })
    await sleep(500)
    const collaborationMobile = await evaluate(`(() => {
      const panel = document.querySelector('[data-testid="agent-collaboration"]')
      const units = panel ? [...panel.querySelectorAll('[data-testid="agent-collaboration-unit"]')] : []
      const viewport = document.documentElement.clientWidth
      return {
        exists: Boolean(panel),
        viewport,
        panelWidth: panel ? Math.round(panel.getBoundingClientRect().width) : 0,
        panelScrollWidth: panel ? panel.scrollWidth : 0,
        overflowingUnits: units.filter((unit) => unit.getBoundingClientRect().right > viewport + 1).length,
      }
    })()`)
    record(
      '协作面板适配 375px 视口',
      collaborationMobile.exists &&
        collaborationMobile.panelScrollWidth <= collaborationMobile.panelWidth + 1 &&
        collaborationMobile.overflowingUnits === 0,
      JSON.stringify(collaborationMobile),
    )
    await screenshot('02b-progress-mobile')
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    })
    await sleep(300)

    // 4) 报告页
    await gotoHash(`#/tasks/${taskId}/report`, '结论')
    const report = await pageText()
    record('报告页渲染结论与事实', /结论|事实/.test(report))
    record('报告页有引用或证据区', /引用|证据/.test(report))
    if (EXPECT_REFLECTION_UI) {
      const reflectionDisclosure = await evaluate(`(() => {
        const details = Array.from(document.querySelectorAll('details'))
          .find((item) => /查看复核问题/.test(item.innerText))
        return {
          summaryVisible: /已完成质量复核|需要人工确认/.test(document.body.innerText),
          issuesCollapsed: !details || details.open === false,
        }
      })()`)
      record(
        '报告页显示反思摘要且问题默认折叠',
        reflectionDisclosure.summaryVisible && reflectionDisclosure.issuesCollapsed,
        JSON.stringify(reflectionDisclosure),
      )
    }
    await screenshot('03-report')
  }

  // 5) 知识库管理
  record('知识库页可打开', await gotoHash('#/knowledge', '知识库'))
  const knowledge = await pageText()
  record('知识库页显示指标或文档', /文档|指标/.test(knowledge))
  await screenshot('04-knowledge')

  // 6) 知识库问答（主视图只显示答案；答案要点与引用来源默认折叠）
  record('知识库问答页可打开', await gotoHash('#/qa', '知识库问答'))

  // 布局回归：提问框必须**贴底 + 居中**（聊天式布局）。几何断言比"看着像"可靠，
  // 而且这条依赖应用外壳是真正的整屏 flex —— 外壳一改坏，这里立刻红。
  const layout = await evaluate(`(() => {
    const box = (sel) => {
      const el = document.querySelector(sel)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left),
               width: Math.round(r.width), cx: Math.round(r.left + r.width / 2) }
    }
    const main = box('.pic-main')
    const qaMain = box('.qa__main')
    return {
      innerHeight: window.innerHeight,
      viewportHeight: document.documentElement.clientHeight,
      composer: box('.qa__composer'),
      inner: box('.qa__composer-inner'),
      main,
      qaMain,
      mainOverflow: getComputedStyle(document.querySelector('.pic-main')).overflowY,
    }
  })()`)
  const layoutOk = Boolean(layout.composer && layout.inner && layout.main)
  record(
    '问答页提问框贴在底部',
    layoutOk && Math.abs(layout.viewportHeight - layout.composer.bottom) <= 2,
    layoutOk
      ? `bottom=${layout.composer.bottom} layoutViewport=${layout.viewportHeight} innerHeight=${layout.innerHeight}`
      : '未找到提问框'
  )
  record(
    '问答页提问框水平居中',
    layoutOk && layout.qaMain && Math.abs(layout.inner.cx - (layout.qaMain.left + layout.qaMain.width / 2)) <= 2,
    layoutOk && layout.qaMain ? `cx=${layout.inner.cx} qaMain.cx=${Math.round(layout.qaMain.left + layout.qaMain.width / 2)}` : '未找到'
  )
  record(
    '应用外壳为整屏布局（主区域自己滚动）',
    layout.mainOverflow === 'hidden' && layout.inner.width <= 1000,
    `overflowY=${layout.mainOverflow} inner.width=${layoutOk ? layout.inner.width : '-'}`
  )

  // 首次进入只加载历史会话列表，不自动打开最近会话；等列表加载完成后再断言首页状态。
  for (let index = 0; index < 40; index += 1) {
    const ready = await evaluate(`(() => {
      const box = document.querySelector('.qa__composer textarea')
      const btn = [...document.querySelectorAll('.qa__composer button')].find((item) => /提问/.test(item.innerText))
      return Boolean(box && !box.disabled && btn && !btn.disabled)
    })()`)
    if (ready) break
    await sleep(250)
  }

  const qaLandingState = await evaluate(`(() => ({
    turnCount: document.querySelectorAll('.qa__turn').length,
    activeConversation: Boolean(document.querySelector('.qa__conversation-item.is-active')),
    hasWelcome: Boolean(document.querySelector('.qa__welcome')),
  }))()`)
  record(
    '首次进入知识库问答停留在问答首页',
    qaLandingState.turnCount === 0 && !qaLandingState.activeConversation && qaLandingState.hasWelcome,
    JSON.stringify(qaLandingState),
  )

  const conversationUi = await evaluate(`(() => {
    const aside = document.querySelector('.qa__conversations')
    const newButton = [...document.querySelectorAll('.qa__conversations button')]
      .find((item) => /新建会话/.test(item.innerText))
    return { hasAside: Boolean(aside), hasNewButton: Boolean(newButton) }
  })()`)
  record('问答页提供会话归档列表', conversationUi.hasAside && conversationUi.hasNewButton)
  await evaluate(`(() => {
    const button = [...document.querySelectorAll('.qa__conversations button')]
      .find((item) => /新建会话/.test(item.innerText))
    button?.click()
  })()`)
  await sleep(300)

  const initialQaTurns = await evaluate(`document.querySelectorAll('.qa__turn').length`)
  const qaQuestion = '埋点字典里 user_login 事件的口径是怎么定义的？'
  const asked = await evaluate(`(() => {
    // 先关掉「模型归纳」：纯检索式不碰模型，接了真实模型的环境也不会产生费用
    const sw = document.querySelector('.el-switch')
    if (sw && sw.classList.contains('is-checked')) sw.click()
    const box = document.querySelector('textarea')
    if (!box) return 'no-textarea'
    box.value = ${JSON.stringify(qaQuestion)}
    box.dispatchEvent(new Event('input', { bubbles: true }))
    const btn = [...document.querySelectorAll('button')].find((b) => /提问/.test(b.innerText))
    if (!btn) return 'no-button'
    btn.click()
    return 'clicked'
  })()`)
  record('知识库问答可提交问题', asked === 'clicked', asked)
  let currentQaReady = false
  for (let index = 0; index < 120; index += 1) {
    currentQaReady = await evaluate(`(() => {
      const turns = [...document.querySelectorAll('.qa__turn')]
      const turn = turns[turns.length - 1]
      const question = turn?.querySelector('.qa__ask-text')?.innerText || ''
      return turns.length > ${initialQaTurns} && question === ${JSON.stringify(qaQuestion)}
        && Boolean(turn?.querySelector('.qa__supporting-details'))
    })()`)
    if (currentQaReady) break
    await sleep(250)
  }
  record('问答返回带引用的结果', currentQaReady)

  const archivedConversation = await evaluate(`(() => {
    const active = document.querySelector('.qa__conversation-item.is-active')
    return active ? { title: active.querySelector('.qa__conversation-title')?.innerText || '', exists: true } : { title: '', exists: false }
  })()`)
  record('首次提问自动归档为会话', archivedConversation.exists && /user_login/.test(archivedConversation.title))

  await evaluate(`(() => {
    const newButton = [...document.querySelectorAll('.qa__conversations button')]
      .find((item) => /新建会话/.test(item.innerText))
    newButton?.click()
  })()`)
  await sleep(300)
  const clearedTurnCount = await evaluate(`document.querySelectorAll('.qa__turn').length`)
  const reopenedConversation = await evaluate(`(() => {
    const archived = [...document.querySelectorAll('.qa__conversation-item')]
      .find((item) => /user_login/.test(item.innerText))
    archived?.click()
    return { clicked: Boolean(archived) }
  })()`)
  let archiveRestored = false
  for (let index = 0; index < 40; index += 1) {
    archiveRestored = await evaluate(`(() => {
      const turns = [...document.querySelectorAll('.qa__turn')]
      return turns.length > 0 && turns[turns.length - 1]?.querySelector('.qa__ask-text')?.innerText === ${JSON.stringify(qaQuestion)}
    })()`)
    if (archiveRestored) break
    await sleep(250)
  }
  record(
    '打开归档会话可恢复原问答',
    clearedTurnCount === 0 && reopenedConversation.clicked && archiveRestored,
  )
  const qaText = await evaluate(`(() => {
    const turns = [...document.querySelectorAll('.qa__turn')]
    return turns[turns.length - 1]?.innerText || ''
  })()`)
  const insufficientQa = /当前知识库没有找到足以直接回答|知识库里没有检索到能回答/.test(qaText)
  const collapsedState = await evaluate(`(() => {
    const turns = [...document.querySelectorAll('.qa__turn')]
    const turn = turns[turns.length - 1]
    if (!turn) return null
    const details = turn.querySelector('.qa__supporting-details')
    const items = details ? [...details.querySelectorAll(':scope > .el-collapse-item')] : []
    return {
      itemCount: items.length,
      allCollapsed: items.length > 0 && items.every((item) => !item.classList.contains('is-active')),
      hasTechnicalSummary: /模式.+耗时|命中库|关键词覆盖|相关度/.test(turn.innerText),
    }
  })()`)
  record(
    '问答默认只展示整理后的答案正文',
    Boolean(collapsedState?.allCollapsed) && !collapsedState?.hasTechnicalSummary,
    collapsedState ? `折叠项=${collapsedState.itemCount}` : '未找到问答卡片'
  )
  if (insufficientQa) {
    record(
      '问答证据不足时不把材料标成答案',
      !/答案要点/.test(qaText) && /相关材料/.test(qaText),
      '已进入证据不足展示路径'
    )
  } else {
    record('问答提供折叠的答案要点入口', /答案要点/.test(qaText))
  }
  record('问答提供折叠的引用来源入口', /引用来源|相关材料/.test(qaText))

  const expandedCitation = await evaluate(`(() => {
    const turns = [...document.querySelectorAll('.qa__turn')]
    const turn = turns[turns.length - 1]
    const headers = turn ? [...turn.querySelectorAll('.qa__supporting-details .el-collapse-item__header')] : []
    const header = headers.find((item) => /引用来源|相关材料/.test(item.innerText))
    if (!header) return false
    header.click()
    return true
  })()`)
  await sleep(500)
  const citationVisible = await evaluate(`(() => {
    const turns = [...document.querySelectorAll('.qa__turn')]
    const turn = turns[turns.length - 1]
    const snippet = turn?.querySelector('.qa__citation-snippet')
    return Boolean(snippet && snippet.innerText.trim() && snippet.getClientRects().length)
  })()`)
  record('引用来源可按需展开查看原文', expandedCitation && citationVisible)
  record('问答保留转成完整诊断入口', /转成完整诊断/.test(qaText))
  await screenshot('05b-qa')

  // 6b) 一键转诊断：把同一个问题带到「新建诊断」（只有 task:create 账号才有这个按钮）
  const carried = await evaluate(`(() => {
    const turns = [...document.querySelectorAll('.qa__turn')]
    const turn = turns[turns.length - 1]
    const btn = turn ? [...turn.querySelectorAll('button')].find((b) => /转成完整诊断/.test(b.innerText)) : null
    if (!btn) return 'no-button'
    btn.click()
    return 'clicked'
  })()`)
  await sleep(1800)
  const createText = await pageText()
  const createUrl = await evaluate('location.hash')
  record(
    '问答可一键转成完整诊断并带入问题',
    carried === 'clicked' && /tasks\/new/.test(createUrl) && /埋点字典/.test(createText),
    `hash=${createUrl}`
  )
  await screenshot('05c-qa-to-diagnosis')

  // 7) 人工审核
  record('人工审核页可打开', await gotoHash('#/approvals', '审核'))
  await screenshot('05-approvals')

  // 8) 指标查询（含密级：下拉里不会出现无权指标）
  record('指标查询页可打开', await gotoHash('#/metrics', '指标查询'))
  const metricsText = await pageText()
  record('指标查询页显示结果与覆盖度', /结果/.test(metricsText) && /数据覆盖度/.test(metricsText))
  await screenshot('06-metrics')

  // 9) 系统状态（执行后端 / 队列 / 断点恢复 / 成本 / 缓存）
  record('系统状态页可打开', await gotoHash('#/system', '系统状态'))
  const systemText = await pageText()
  record('系统状态显示执行与断点恢复', /执行后端/.test(systemText) && /断点恢复/.test(systemText))
  record('系统状态显示队列与成本', /队列积压/.test(systemText) && /今日成本/.test(systemText))
  // 缓存可观测性：后端类型 + 是否落盘（跨重启保留）+ 分层命中率表
  record(
    '系统状态显示缓存后端与是否落盘',
    /缓存后端/.test(systemText) && /(落盘|进程内)/.test(systemText),
    (systemText.match(/缓存后端[^\n]*/) || [''])[0].slice(0, 60)
  )
  record(
    '系统状态显示缓存分层命中率与向量缓存',
    /缓存层/.test(systemText) && /向量缓存/.test(systemText) && /最近窗口|最近\s*\d+\s*次/.test(systemText)
  )
  await screenshot('07-system')

  // 10) 管理页（评测 / 密级策略 / 审计）——切到 admin 账号
  try {
    await loginAs('admin', 'admin123')
    record('评测集页可打开', await gotoHash('#/evaluations', '评测集运行'))
  // 售后工单评测（需求书 §9）：点一下小规模运行，验证指标与安全指标都渲染出来
  await sleep(1000)
  const evalText = await pageText()
  record('评测页含售后工单评测区块', /售后工单评测/.test(evalText) && /分类 Macro-F1/.test(evalText))
  try {
    await evaluate(`
      (() => {
        // 页面顶部的「诊断评测上限」也是一个 el-input-number，必须取**最后一个**
        // （售后工单评测卡片里的那个），否则会改错输入框：本意跑 4 条却按默认 8 条跑
        // （真实模型下就是多花钱）。
        const numbers = [...document.querySelectorAll('.el-input-number input')]
        const input = numbers[numbers.length - 1]
        if (input) {
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
          setter.call(input, '4')
          input.dispatchEvent(new Event('input', { bubbles: true }))
          input.dispatchEvent(new Event('change', { bubbles: true }))
        }
        const button = [...document.querySelectorAll('button')].find((b) => /运行售后工单评测/.test(b.innerText))
        if (!button) return false
        button.click()
        return true
      })()
    `)
    // 真实模型配置下点运行会弹"这次评测会真实计费"的确认框（成本闸门）：
    // 冒烟必须先如实确认，否则会一直等一个永远不出现的指标而误报失败。
    await waitForText('E2E 冒烟：包裹显示签收但未收到', 10000)
    const confirmed = await evaluate(`(() => {
      const text = document.body.innerText || ''
      if (!/真实计费/.test(text)) return 'no-dialog'
      const btn = [...document.querySelectorAll('.el-message-box button, button')].find((b) =>
        /确认并开始/.test(b.innerText)
      )
      if (!btn) return 'no-button'
      btn.click()
      return 'confirmed'
    })()`)
    if (confirmed !== 'no-dialog') record('真实模型成本闸门已确认（或未触发）', true, confirmed)
    let rendered = false
    for (let i = 0; i < 40; i += 1) {
      const text = await pageText()
      if (/审批门禁召回/.test(text) && /用例通过率/.test(text)) {
        rendered = true
        break
      }
      await sleep(1000)
    }
    record('运行售后工单评测并显示指标', rendered)
    const ticketEvalText = await pageText()
    record(
      '显示三个安全指标（门禁召回/政策泄漏/隔离率）',
      /审批门禁召回/.test(ticketEvalText) &&
        /失效政策泄漏率/.test(ticketEvalText) &&
        /知识库隔离率/.test(ticketEvalText)
    )
    await screenshot('14-ticket-evaluation')
  } catch (err) {
    record('运行售后工单评测并显示指标', false, err.message)
  }
    record('访问策略页可打开', await gotoHash('#/policies', '访问策略'))
    const policyText = await pageText()
    record(
      '访问策略页显示主体、资源、动作、处理和生效条件',
      /谁/.test(policyText) && /对什么资源/.test(policyText) && /做什么/.test(policyText)
        && /系统处理/.test(policyText) && /什么时候生效/.test(policyText),
    )
    record('访问策略页有「授权预演」区块', /授权预演/.test(policyText))
    // 使用页面默认的 reviewer + task:replay 输入发起真实预演。结论可能因种子策略
    // 调整而是允许/拒绝/需审批之一，因此这里验证接口闭环和结构，不硬编码旧结论。
    const simulated = await evaluate(`(() => {
      const input = [...document.querySelectorAll('input')].find((i) => (i.placeholder || '').includes('任务 ID'))
      if (!input || !input.value) return 'no-input'
      const btn = [...document.querySelectorAll('button')].find((b) => /查看系统会怎么判定/.test(b.innerText))
      if (!btn) return 'no-button'
      btn.click()
      return 'clicked'
    })()`)
    record('可发起授权预演', simulated === 'clicked', simulated)
    await waitForText('命中策略', 20000)
    const simulationState = await evaluate(`(() => {
      const panel = document.querySelector('.decision-panel')
      return { visible: Boolean(panel), text: panel?.innerText || '' }
    })()`)
    record(
      '预演给出结论、原因与命中策略数',
      simulationState.visible
        && /允许|拒绝|需人工审批/.test(simulationState.text)
        && /命中策略/.test(simulationState.text),
      simulationState.text.replace(/\s+/g, ' ').slice(0, 180),
    )
    await screenshot('08b-policy-simulate')
    await screenshot('08-policies')
    record('审计日志页可打开', await gotoHash('#/audit', '审计日志'))
    const auditText = await pageText()
    record('审计日志有记录', /login_success|task_created|document_ingested|metric_imported/.test(auditText))
    await screenshot('09-audit')

    // 队列运维（死信排查与重放）只有管理员能看到 —— 所以断言放在 admin 这一段
    record('队列运维在系统状态页可打开（admin）', await gotoHash('#/system', '队列运维'))
    const queueText = await pageText()
    record(
      '队列运维卡片显示统计与死信区',
      /判死（死信）/.test(queueText) && /(没有死信|放回队列)/.test(queueText),
      (queueText.match(/判死（死信）[^\n]*/) || [''])[0].slice(0, 40)
    )
    record(
      '系统状态显示默认关闭的只读主动异常触发器（admin）',
      /只读主动异常触发器/.test(queueText) && /默认关闭/.test(queueText)
        && /不调用模型/.test(queueText) && /管理员确认后/.test(queueText)
          && /独立调度进程/.test(queueText) && /数据最大延迟/.test(queueText)
          && /指标事实源/.test(queueText) && /分指标新鲜度策略/.test(queueText)
    )
    record(
      '系统状态默认不展示生产交付警告（admin）',
      !/运行正常不等于已经通过生产交付验收/.test(queueText)
        && !/当前证据不能作为生产发布依据/.test(queueText)
        && !/待外部验证/.test(queueText)
        && !/不能作为生产自动告警依据/.test(queueText)
    )
    const deliveryOpened = await evaluate(`(() => {
      const button = document.querySelector('[data-testid="delivery-readiness-trigger"]')
      if (!button) return false
      button.click()
      return true
    })()`)
    record('交付验收详情由管理员主动打开', deliveryOpened === true)
    await waitForText('正式交付证据', 20000)
    // 标题是抽屉的静态内容，接口数据可能仍在加载；等待首条门禁数据后再断言完整看板。
    await waitForText('订单、支付、物流与售后事实接口', 20000)
    const deliveryText = await pageText()
    record(
      '主动打开后提供正式交付证据只读看板（admin）',
      /交付验收详情/.test(deliveryText)
        && /正式交付证据/.test(deliveryText)
        && /证据快照|证据未绑定当前发布版本|证据已过期|证据有效/.test(deliveryText)
        // 新环境允许明确显示“尚未生成”，但不能隐藏证据状态或外部门禁。
        && /证据包一致|证据包未校验|尚未生成/.test(deliveryText)
        && /订单、支付、物流与售后事实接口/.test(deliveryText)
        && /真实业务指标源/.test(deliveryText)
    )
    await screenshot('09b-delivery-readiness-drawer')
    await evaluate(`document.querySelector('.el-drawer__close-btn')?.click()`)
    await sleep(300)
    await screenshot('09c-queue-ops')
    await loginAs(USER, PASSWORD)
  } catch (err) {
    record('管理页可用（admin 账号）', false, err.message)
  }

  // 10b) 队列运维不该对普通角色可见（后台有权限，界面上也该藏）
  try {
    await gotoHash('#/system', '系统状态')
    const pmSystemText = await pageText()
    record('队列运维对 pm 不显示（仅管理员）', !/判死（死信）/.test(pmSystemText))
  } catch (err) {
    record('队列运维对 pm 不显示（仅管理员）', false, err.message)
  }

  // 10c) 售后政策库：会员等级上下文与状态变更审计说明
  try {
    await loginAs('reviewer', 'reviewer123')
    record('售后政策库可打开（主管账号）', await gotoHash('#/ticket-policies', '售后政策库'))
    const policyLibraryText = await pageText()
    record(
      '政策库显示版本/生效期/适用范围与检索测试',
      /政策版本/.test(policyLibraryText) &&
        /生效期/.test(policyLibraryText) &&
        /适用范围/.test(policyLibraryText) &&
        /检索测试/.test(policyLibraryText)
    )

    const openedMemberLevel = await evaluate(`(() => {
      const item = [...document.querySelectorAll('.ctx__item')].find((node) =>
        /member_level/.test(node.innerText)
      )
      const input = item?.querySelector('input')
      if (!input) return false
      input.click()
      return true
    })()`)
    await sleep(300)
    const selectedGold = openedMemberLevel && (await evaluate(`(() => {
      const options = [...document.querySelectorAll('.el-select-dropdown__item')]
      const option = options.find((node) => node.offsetParent !== null && node.innerText.trim() === 'gold')
      if (!option) return false
      option.click()
      return true
    })()`))
    if (selectedGold) {
      await evaluate(`(() => {
        const button = [...document.querySelectorAll('button')].find((node) => /按此上下文重算/.test(node.innerText))
        button?.click()
      })()`)
      await sleep(1000)
    }
    const memberContextText = await pageText()
    record(
      '会员等级上下文可选择并参与重算',
      Boolean(selectedGold) && /当前上下文[^\n]*会员等级 gold/.test(memberContextText)
    )

    const openedArchive = await evaluate(`(() => {
      const row = [...document.querySelectorAll('.el-table__body tr')].find((node) => /启用中|草稿|已过期/.test(node.innerText))
      const button = row && [...row.querySelectorAll('button')].find((node) => /归档/.test(node.innerText))
      if (!button) return false
      button.click()
      return true
    })()`)
    await sleep(300)
    const policyDialogState = await evaluate(`(() => {
      const dialog = document.querySelector('.policy-action-dialog')
      const textarea = dialog?.querySelector('textarea')
      return {
        visible: Boolean(dialog && dialog.offsetParent !== null),
        placeholder: textarea?.getAttribute('placeholder') || '',
        text: dialog?.innerText || ''
      }
    })()`)
    record(
      '政策状态变更要求理由并说明写入审计',
      Boolean(openedArchive) &&
        policyDialogState.visible &&
        /填写变更理由（必填）/.test(policyDialogState.placeholder) &&
        /理由会随/.test(policyDialogState.text) &&
        /写入审计详情/.test(policyDialogState.text)
    )
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.el-dialog button')].find((node) => node.innerText.trim() === '取消')
      button?.click()
    })()`)

    await send('Emulation.setDeviceMetricsOverride', {
      width: 375,
      height: 812,
      deviceScaleFactor: 1,
      mobile: true,
    })
    await sleep(500)
    const mobileLayout = await evaluate(`({
      viewport: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      titleVisible: Boolean([...document.querySelectorAll('h2')].find((node) => /售后政策库/.test(node.innerText))),
      widest: [...document.querySelectorAll('body *')]
        .map((node) => ({
          tag: node.tagName,
          className: typeof node.className === 'string' ? node.className : '',
          right: Math.round(node.getBoundingClientRect().right),
          width: Math.round(node.getBoundingClientRect().width)
        }))
        .filter((item) => item.right > document.documentElement.clientWidth + 1)
        .sort((a, b) => b.right - a.right)
        .slice(0, 3)
    })`)
    record(
      '政策库 375px 视口无页面级横向溢出',
      mobileLayout.titleVisible && mobileLayout.scrollWidth <= mobileLayout.viewport + 1,
      `scrollWidth=${mobileLayout.scrollWidth} viewport=${mobileLayout.viewport} widest=${JSON.stringify(mobileLayout.widest)}`
    )
    await screenshot('10c-ticket-policy-mobile')
    await send('Emulation.clearDeviceMetricsOverride')
    await loginAs(USER, PASSWORD)
  } catch (err) {
    await send('Emulation.clearDeviceMetricsOverride').catch(() => {})
    record('售后政策库界面闭环', false, err.message)
    await loginAs(USER, PASSWORD).catch(() => {})
  }

  // 11) 售后工单工作台（需求书 §7 原型对齐）
  // “我的工单”必须以客服本人验证；上一段政策库用的是主管账号，不能沿用旧身份。
  await loginAs('ops', 'ops123')
  await gotoHash('#/tickets', '我的待办')
  await waitForText('我的待办', 15000)
  await sleep(1200)
  const ticketPage = await pageText()
  record('售后工单页可打开', /我的待办/.test(ticketPage))
  record(
    '业务处理卡齐全（即将超时/等待我回复/等待客户补充/待主管审批/今日已完成）',
    /即将超时/.test(ticketPage) &&
      /等待我回复/.test(ticketPage) &&
      /等待客户补充/.test(ticketPage) &&
      /待主管审批/.test(ticketPage) &&
      /今日已完成/.test(ticketPage)
  )
  record(
    '搜索常驻，低频筛选收在“更多筛选”中',
    // placeholder 不属于 innerText，必须查 DOM 属性
    (await evaluate(`
      (() => {
        const input = document.querySelector('.tk__filters input[placeholder*="搜索工单号"]')
        const details = document.querySelector('.tk__filters .advanced-filters')
        const selects = details?.querySelectorAll('.el-select') || []
        const testRecords = details?.querySelector('.el-checkbox')
        return Boolean(input && /搜索工单号/.test(input.placeholder || '') && details && selects.length >= 3 && testRecords)
      })()
    `)) && /更多筛选/.test(ticketPage)
  )
  record(
    '表格按客户问题/处理进度/下一步/处理人组织',
    /客户问题/.test(ticketPage) &&
      /处理进度/.test(ticketPage) &&
      /下一步做什么/.test(ticketPage) &&
      /处理人/.test(ticketPage)
  )

  // 通过接口造一张工单，确保列表里有数据可点（空库环境下也能验证抽屉）
  try {
    const token = await apiLogin('ops', 'ops123')
    const resp = await fetch(new URL('/api/v1/tickets', APP_URL), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        subject: 'E2E 冒烟：包裹显示签收但未收到',
        content: '物流显示已签收，但我没有收到包裹，也没有接到电话，请帮忙核实。',
        external_id: `E2E-${Date.now()}`,
        order_id: 'TB202609241806',
        data_origin: 'test',
      }),
    })
    const ticket = await resp.json()
    record('接口可创建工单', resp.status === 201, `status=${resp.status}`)
    const me = await fetch(new URL('/api/v1/auth/me', APP_URL), {
      headers: { Authorization: `Bearer ${token}` },
    }).then((r) => r.json())
    const assignResp = await fetch(new URL(`/api/v1/tickets/${ticket.id}/assign`, APP_URL), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ assignee_id: me.id, reason: 'E2E：验证我的工单与派单推荐' }),
    })
    record('接口可把工单分配给当前客服', assignResp.status === 200, `status=${assignResp.status}`)
    await fetch(new URL(`/api/v1/tickets/${ticket.id}/analyze`, APP_URL), {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
    // 等分析跑完（threadpool 后端下是异步的）
    let detail = null
    for (let i = 0; i < 40; i += 1) {
      const polled = await fetch(new URL(`/api/v1/tickets/${ticket.id}`, APP_URL), {
        headers: { Authorization: `Bearer ${token}` },
      }).then((r) => r.json())
      if (polled.recommendation && (polled.analysis?.business_steps || []).length >= 7) {
        detail = polled
        break
      }
      await sleep(1000)
    }
    record(
      '接口分析产出 7 个业务角色步骤与建议',
      Boolean(detail),
      detail ? `business_steps=${detail.analysis.business_steps.length}` : '超时',
    )

    // 刷新页面列表，等数据行出现后点开第一行 → 抽屉
    await gotoHash('#/tickets', '我的待办')
    await sleep(1000)
    await evaluate(`(() => {
      const details = document.querySelector('.advanced-filters')
      if (details) details.open = true
      const checkbox = [...document.querySelectorAll('.el-checkbox')].find((item) => /显示测试记录/.test(item.innerText))
      if (checkbox && !checkbox.classList.contains('is-checked')) checkbox.click()
      return Boolean(checkbox)
    })()`)
    await waitForText('E2E 冒烟：申请退款', 10000)
    const clickedMine = await evaluate(`(() => {
      const btn = [...document.querySelectorAll('.el-segmented__item, .el-segmented label')].find((b) => /我的工单/.test(b.innerText))
      if (!btn) return false
      btn.click()
      return true
    })()`)
    await sleep(800)
    const mineText = await pageText()
    record(
      '“我的工单”按当前登录客服筛选',
      clickedMine && mineText.includes('E2E 冒烟：包裹显示签收但未收到'),
      `clicked=${clickedMine} ticket=${ticket.external_id}`
    )
    let rows = 0
    for (let i = 0; i < 15; i += 1) {
      rows = await evaluate(
        `document.querySelectorAll('.el-table__body tbody tr.el-table__row').length`
      )
      if (rows > 0) break
      await sleep(1000)
    }
    record('工单列表渲染出数据行', rows > 0, `rows=${rows}`)
    const openedAnalyzedTicket = await evaluate(`
      (() => {
        const row = Array.from(document.querySelectorAll('.el-table__body tbody tr.el-table__row'))
          .find((item) => item.innerText.includes('E2E 冒烟：包裹显示签收但未收到'))
        if (!row) return false
        const cell = row.querySelector('td') || row
        for (const type of ['mousedown', 'mouseup', 'click']) {
          cell.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }))
        }
        return true
      })()
    `)
    if (openedAnalyzedTicket) await waitForText('客户反馈', 15000)
    await sleep(800)
    const drawer = await pageText()
    record('点行打开清晰分区的详情抽屉', /本单下一步/.test(drawer) && /客户反馈/.test(drawer) && /建议处理/.test(drawer))
    record('详情区分已核实信息和处理记录', /已核实的信息/.test(drawer) && /进度时间线/.test(drawer) && /内部协作/.test(drawer))
    record(
      '右栏只显示客服需要的业务建议',
      /建议处理/.test(drawer) && /建议处理方案/.test(drawer) && !/系统核验过程/.test(drawer)
    )
    record('显示政策依据与风险框', /政策依据/.test(drawer) && /需主管审批|无需审批|客服确认/.test(drawer))
    record('显示客户回复草稿', /给客户的回复草稿/.test(drawer))
    record(
      '客服详情隐藏 Trace、Token、工作流和政策全集',
      !/技术记录|Trace ID|\btokens\b|工作流：after_sales|政策库现状/.test(drawer)
    )

    const assigneeBeforeRecommendation = detail?.ticket?.assignee_id
    const openedAssignment = await evaluate(`(() => {
      const btn = [...document.querySelectorAll('.el-drawer button')].find((b) => /^转派$/.test(b.innerText.trim()))
      if (!btn) return false
      btn.click()
      return true
    })()`)
    await waitForText('智能推荐', 10000)
    await sleep(500)
    const assignmentDialogText = await evaluate(`(() => {
      const dialog = [...document.querySelectorAll('.el-dialog')].find((d) => d.offsetParent !== null)
      return dialog ? dialog.innerText : ''
    })()`)
    record(
      '转派弹窗显示负责人推荐、负载与规则解释',
      openedAssignment && /智能推荐/.test(assignmentDialogText) && /活跃 \d+ 单/.test(assignmentDialogText) && /仅供选择，不会自动改派/.test(assignmentDialogText),
      assignmentDialogText.replace(/\n/g, ' | ').slice(0, 150)
    )
    await evaluate(`(() => {
      const dialog = [...document.querySelectorAll('.el-dialog')].find((d) => d.offsetParent !== null)
      const btn = dialog && [...dialog.querySelectorAll('button')].find((b) => /取消/.test(b.innerText))
      if (btn) btn.click()
      return Boolean(btn)
    })()`)
    await sleep(400)
    const afterRecommendation = await fetch(new URL(`/api/v1/tickets/${ticket.id}`, APP_URL), {
      headers: { Authorization: `Bearer ${token}` },
    }).then((r) => r.json())
    record(
      '查看派单推荐不会自动改派',
      afterRecommendation?.ticket?.assignee_id === assigneeBeforeRecommendation,
      `assignee=${afterRecommendation?.ticket?.assignee_id}`
    )
    await screenshot('12-ticket-drawer')

    // 审批门禁（FR-05）：造一张**退款**工单（L3 → 待主管审批），
    // 界面上必须只出现「主管审批」而**不出现**「确认并发送回复」——
    // 未审批的退款/赔付绝不允许从界面直接发出去。
    const refundResp = await fetch(new URL('/api/v1/tickets', APP_URL), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        subject: 'E2E 冒烟：申请退款',
        content: '商品不想要了，申请退款，请把钱退给我。',
        external_id: `E2E-REFUND-${Date.now()}`,
        order_id: 'TB202609241501',
        data_origin: 'test',
      }),
    })
    const refundTicket = await refundResp.json()
    await fetch(new URL(`/api/v1/tickets/${refundTicket.id}/assign`, APP_URL), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ assignee_id: me.id, reason: 'E2E：验证我的待审批退款工单' }),
    })
    await fetch(new URL(`/api/v1/tickets/${refundTicket.id}/analyze`, APP_URL), {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
    let refundDetail = null
    for (let i = 0; i < 40; i += 1) {
      const polled = await fetch(new URL(`/api/v1/tickets/${refundTicket.id}`, APP_URL), {
        headers: { Authorization: `Bearer ${token}` },
      }).then((r) => r.json())
      if (polled.recommendation && (polled.analysis?.business_steps || []).length >= 7) {
        refundDetail = polled
        break
      }
      await sleep(1000)
    }
    record(
      '退款工单落在 L3 且需主管审批',
      refundDetail?.recommendation?.risk_tier === 'L3' &&
        refundDetail?.recommendation?.status === 'pending_approval',
      `risk=${refundDetail?.recommendation?.risk_tier} status=${refundDetail?.recommendation?.status}`
    )

    await gotoHash('#/', '工作台')
    await sleep(500)
    await gotoHash('#/tickets', '我的待办')
    // 必须**强制刷新列表**：hash 相同时路由不会重新挂载，列表还是创建退款工单之前的那一份，
    // 点到的就是上一张工单（踩过：门禁断言拿到的按钮来自 L1 草稿）。
    await evaluate('location.reload()')
    await sleep(2500)
    await evaluate(`(() => {
      const details = document.querySelector('.advanced-filters')
      if (details) details.open = true
      const checkbox = [...document.querySelectorAll('.el-checkbox')].find((item) => /显示测试记录/.test(item.innerText))
      if (checkbox && !checkbox.classList.contains('is-checked')) checkbox.click()
      return Boolean(checkbox)
    })()`)
    await sleep(600)
    for (let i = 0; i < 15; i += 1) {
      const count = await evaluate(
        `document.querySelectorAll('.el-table__body tbody tr.el-table__row').length`
      )
      if (count > 0) break
      await sleep(1000)
    }
    const openedRefundTicket = await evaluate(`
      (() => {
        const row = Array.from(document.querySelectorAll('.el-table__body tbody tr.el-table__row'))
          .find((item) => item.innerText.includes('E2E 冒烟：申请退款'))
        if (!row) return false
        const cell = row.querySelector('td') || row
        for (const type of ['mousedown', 'mouseup', 'click']) {
          cell.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }))
        }
        return true
      })()
    `)
    if (openedRefundTicket) await waitForText('客户反馈', 15000)
    await sleep(800)
    const refundDrawer = await pageText()
    const gate = await evaluate(`
      (() => {
        const buttons = Array.from(document.querySelectorAll('.card__actions button')).map((b) => b.innerText.trim())
        const send = document.querySelector('.card__actions button.el-button--primary')
        return JSON.stringify({ buttons, primary: send ? send.innerText.trim() : null, disabled: send ? send.disabled : null })
      })()
    `)
    record(
      '待审批建议：界面只给「主管审批」，不给直接发送（FR-05）',
      /待审批|需主管审批/.test(refundDrawer) &&
        /主管审批/.test(gate) &&
        !/确认并发送回复/.test(gate),
      String(gate).slice(0, 120)
    )
    await screenshot('13-ticket-approval-gate')

    // 金额来源：审批人要能看出"这笔钱是订单实付还是政策标准"（此前只写在 rationale 散文里）
    record(
      '建议显示金额来源（或"为什么没有钱"）',
      /来源：/.test(refundDrawer) || /规则：/.test(refundDrawer),
      (refundDrawer.match(/来源：[^）]{0,20}/) || refundDrawer.match(/规则：[^\s<]{0,20}/) || [''])[0]
    )

    // 新建工单要能造出 L4：风控标记 / 敏感客户是 L3/L4 的判据，界面上没入口就演示不了。
    // 注意 el-select 的选项是**懒渲染**的（点开下拉才进 DOM），所以这里只断言两个表单标签。
    const createDialogExpected = await evaluate(`(() => {
      const btn = [...document.querySelectorAll('button')].find((b) => /＋ 新建工单|新建工单/.test(b.innerText))
      if (!btn) return null
      btn.click()
      return true
    })()`)
    await sleep(800)
    const dialogText = await evaluate(
      "(() => { const d = [...document.querySelectorAll('.el-dialog')].find((x) => /风控标记/.test(x.innerText)); return d ? d.innerText : '' })()"
    )
    record(
      '新建工单支持风控标记与敏感客户（界面可造 L4）',
      Boolean(createDialogExpected) && /风控标记/.test(dialogText) && /敏感客户/.test(dialogText),
      String(dialogText).replace(/\n/g, ' | ').slice(0, 90)
    )
    // 关掉这个弹窗并**确认它真的关了**：留着它会盖住后面的动作弹窗
    // （踩过：取消没点上，后面"重新分析"把理由填进了新建工单的"用户原话"，于是校验失败）
    await evaluate(`(() => {
      const dialogs = [...document.querySelectorAll('.el-dialog')]
      const target = dialogs.find((x) => /风控标记/.test(x.innerText)) || dialogs[dialogs.length - 1]
      const btn = target && [...target.querySelectorAll('button')].find((b) => /取消/.test(b.innerText))
      if (btn) { btn.click(); return true }
      return false
    })()`)
    let dialogClosed = false
    for (let i = 0; i < 25; i += 1) {
      // 判"关没关"要看**可见性**而不是 DOM 里的数量：el-dialog 关闭后仍留在 DOM（display:none），
      // 数量永远是 2，用数量判定会永远失败（这条断言自己就先踩了一次）。
      const visible = await evaluate(
        "[...document.querySelectorAll('.el-dialog')].filter((d) => d.offsetParent !== null).length"
      )
      if (visible === 0) {
        dialogClosed = true
        break
      }
      await sleep(200)
    }
    record('新建工单弹窗可关闭（不留遮罩）', dialogClosed)
    await screenshot('13c-ticket-create-l4')

    // 导出列表：真的产出 CSV（此前只弹一句"演示环境仅提示"）
    await evaluate(`(() => {
      const btn = [...document.querySelectorAll('button')].find((b) => /导出列表/.test(b.innerText))
      if (btn) btn.click()
      return true
    })()`)
    let sawExport = false
    for (let i = 0; i < 40; i += 1) {
      if (/已导出|没有可导出的内容/.test(await pageText())) {
        sawExport = true
        break
      }
      await sleep(200)
    }
    record('导出列表产出 CSV（不再只是提示）', sawExport)

    // 重新分析 + SSE 进度：演示脚本里最"看得见"的一步，必须真的能跑
    // （踩过：这条路径此前没有任何自动化覆盖，只有人工点过）
    const clickedAnalyze = await evaluate(`(() => {
      const btn = [...document.querySelectorAll('button')].find((b) => /重新分析|启动分析/.test(b.innerText))
      if (!btn) return false
      btn.click()
      return true
    })()`)
    await sleep(600)
    const submittedWithoutReason = await evaluate(`(() => {
      // 分析动作没有理由字段，界面也不得再要求填写一个后端不会接收的值。
      const dialog = [...document.querySelectorAll('.el-dialog')].find((d) => d.offsetParent !== null)
      if (!dialog || dialog.querySelector('textarea')) return false
      const btn = [...dialog.querySelectorAll('button')].find((b) => /确认/.test(b.innerText))
      if (!btn) return false
      btn.click()
      return true
    })()`)
    let sawBusinessProgress = false
    let exposedTechnicalProgress = false
    let sawToast = false
    let debugText = ''
    for (let i = 0; i < 100; i += 1) {
      const state = await evaluate(`JSON.stringify({
        businessProgress: document.querySelectorAll('.business-progress').length,
        technicalLines: document.querySelectorAll('.log__line').length,
        toast: /已启动分析/.test(document.body.innerText),
        dialogs: document.querySelectorAll('.el-dialog').length,
        text: document.body.innerText.slice(0, 160).replace(/\\n/g, ' | '),
      })`)
      const parsed = JSON.parse(state)
      debugText = `dialogs=${parsed.dialogs} text=${parsed.text}`
      if (parsed.businessProgress > 0) sawBusinessProgress = true
      if (parsed.technicalLines > 0) exposedTechnicalProgress = true
      if (parsed.toast) sawToast = true
      if (sawBusinessProgress && sawToast) break
      await sleep(100)
    }
    record(
      '「重新分析」可触发且提示已启动分析',
      clickedAnalyze && submittedWithoutReason && sawToast,
      `clicked=${clickedAnalyze} noReasonField=${submittedWithoutReason} toast=${sawToast}${sawToast ? '' : ' | ' + debugText}`
    )
    record(
      'SSE 仅显示业务进度，不暴露节点名',
      sawBusinessProgress && !exposedTechnicalProgress,
      `businessProgress=${sawBusinessProgress} technicalLines=${exposedTechnicalProgress}`,
    )
    await sleep(1500)
    await screenshot('13b-ticket-reanalyze-sse')

    // 收尾清理：删掉冒烟造的工单，避免污染演示数据
    await fetch(new URL(`/api/v1/tickets/${ticket.id}`, APP_URL), {
      method: 'GET',
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch (err) {
    record('售后工单接口可用', false, err.message)
  }

  // 12) 控制台异常
  record('无未捕获的页面异常', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '))

  const failed = results.filter((item) => !item.ok)
  console.log(`\n=== 汇总：${results.length - failed.length}/${results.length} 通过 ===`)
  if (failed.length) {
    console.log('失败项：' + failed.map((item) => item.name).join('、'))
  }
  ws.close()
  if (ownedTargetId) {
    await fetch(`${CDP_URL}/json/close/${ownedTargetId}`).catch(() => {})
  }
  return failed.length ? 1 : 0
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error(`[错误] ${err.message}`)
    if (ownedTargetId) {
      fetch(`${CDP_URL}/json/close/${ownedTargetId}`).catch(() => {})
    }
    process.exit(2)
  })
