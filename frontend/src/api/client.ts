/**
 * 同源 HTTP 客户端：会话 JWT 只由 HttpOnly Cookie 携带，前端不读取也不持久化令牌。
 * CSRF Cookie 使用双提交模式：前端读取普通 Cookie 并回传同名 Header。
 */

export class ApiError extends Error {
  status: number
  code: string
  detail?: unknown

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

let csrfCookieName = 'pic_csrf'
let csrfHeaderName = 'X-CSRF-Token'

/** 由登录响应注入后端实际会话配置，支持部署时自定义 Cookie/Header 名称。 */
export function configureCsrf(cookieName: string, headerName: string): void {
  if (cookieName.trim()) csrfCookieName = cookieName.trim()
  if (headerName.trim()) csrfHeaderName = headerName.trim()
}

function getCookie(name: string): string {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((entry) => entry.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : ''
}

function extractError(status: number, payload: unknown): ApiError {
  const body = (payload ?? {}) as Record<string, unknown>
  const detail = body.detail as Record<string, unknown> | string | undefined
  let code = (body.code as string) ?? 'http_error'
  let message = (body.message as string) ?? `请求失败（HTTP ${status}）`

  if (detail && typeof detail === 'object') {
    code = (detail.code as string) ?? code
    message = (detail.message as string) ?? message
  } else if (typeof detail === 'string') {
    message = detail
  } else if (Array.isArray(body.detail)) {
    // 422：FastAPI 校验错误
    const first = (body.detail as Array<Record<string, unknown>>)[0]
    if (first) message = `参数校验未通过：${String(first.msg ?? '')}`
  }
  return new ApiError(status, code, message, body.detail)
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  const type = response.headers.get('content-type') ?? ''
  if (type.includes('application/json')) {
    try {
      return JSON.parse(text)
    } catch {
      return text
    }
  }
  return text
}

async function request<T>(
  method: string,
  path: string,
  options: { body?: unknown; query?: Record<string, unknown>; raw?: boolean } = {},
): Promise<T> {
  const url = new URL(path, window.location.origin)
  if (options.query) {
    for (const [key, value] of Object.entries(options.query)) {
      if (value === undefined || value === null || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }

  const headers: Record<string, string> = {}
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
    const csrf = getCookie(csrfCookieName)
    if (csrf) headers[csrfHeaderName] = csrf
  }
  let body: BodyInit | undefined
  if (options.body instanceof FormData) {
    body = options.body
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json; charset=utf-8'
    body = JSON.stringify(options.body)
  }

  const response = await fetch(url.toString(), {
    method,
    headers,
    body,
    credentials: 'same-origin',
  })
  if (!response.ok) {
    const payload = await parseBody(response)
    const error = extractError(response.status, payload)
    if (response.status === 401) {
      if (!window.location.hash.includes('/login')) window.location.hash = '#/login'
    }
    throw error
  }
  const payload = await parseBody(response)
  return (options.raw ? payload : payload) as T
}

export const http = {
  get: <T>(path: string, query?: Record<string, unknown>) => request<T>('GET', path, { query }),
  post: <T>(path: string, body?: unknown, query?: Record<string, unknown>) =>
    request<T>('POST', path, { body, query }),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, { body }),
  del: <T>(path: string) => request<T>('DELETE', path),
  /** 下载/导出：直接取文本（Markdown、JSON 字符串） */
  text: (path: string, query?: Record<string, unknown>) =>
    request<string>('GET', path, { query, raw: true }),
}
