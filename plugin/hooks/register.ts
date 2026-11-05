import type { EngineInterface, ModelCompleteRequest, ModelUsage, Register, Timer } from 'claude-code'

type Session = { session_id: string; transcript_path: string; cwd: string }
type ModelStep = ModelCompleteRequest & {
  action: 'model'
  step: string
  allow_extra_usage: boolean
}
type Reply = ModelStep | { action: 'done' }
type Start = { queued: unknown[]; allow_extra_usage: boolean }

let env: Record<string, string> = {}
const starts = new Map<string, Promise<Start | undefined>>()
let session: Session | undefined
type Job = { sessionId: string; abort: AbortController; timer?: Timer }
let running: Job | undefined
let scheduled: Job | undefined

class ReviewError extends Error {}

// A slow background setup must not hold up session start.
const START_WAIT_MS = 1000

async function command<T>($: EngineInterface, cwd: string, args: string[], input?: unknown, timeoutMs?: number): Promise<T> {
  // bin/nowitknows (nowitknows.cmd on Windows) leads to bin/nowitknows.sh, which runs the environment's own Python once the background setup has marked it ready, else exits 1 at
  // once; it never runs uv
  const result = await $.process.run([`${$.plugin.root}/bin/nowitknows`, 'mod', ...args], {
    cwd, env, stdin: input === undefined ? undefined : JSON.stringify(input),
    ...(timeoutMs === undefined ? {} : { timeoutMs }),
  })
  if (result.exitCode !== 0 || result.isStdoutTruncated) throw new ReviewError('nowitknows command failed')
  let value
  try { value = JSON.parse(result.stdout) } catch { throw new ReviewError('nowitknows command returned invalid JSON') }
  if (value?.action === 'error') throw new ReviewError('nowitknows command failed')
  return value as T
}

async function failure($: EngineInterface, cwd: string, error: unknown, step = '', usage?: ModelUsage, cancelled = false) {
  try {
    // Engine rejections and parser messages may include credentials or prompt text.
    const message = error instanceof ReviewError ? String(error) : 'in-session review failed'
    await command($, cwd, ['fail', '--step', step], { error: message, usage, ...(cancelled ? { cancelled } : {}) })
  } catch {}
}

function within<T>($: EngineInterface, work: Promise<T>, ms: number): Promise<T | undefined> {
  return new Promise(resolve => {
    let timer: Timer | undefined
    try { timer = $.clock.after(ms, () => resolve(undefined)) } catch {}
    work.then(value => { timer?.cancel(); resolve(value) }, () => { timer?.cancel(); resolve(undefined) })
  })
}

async function start($: EngineInterface, id: string, cwd: string) {
  if (!starts.has(id)) {
    // The process timeout matches the wait, so a start that comes too late cannot mark the session after its command
    // hooks chose the CLI path; a later turn tries again.
    const call = command<Start>($, cwd, ['start', '--session', id, '--cwd', cwd], undefined, START_WAIT_MS).then(result => {
      if (!Array.isArray(result?.queued) || typeof result.allow_extra_usage !== 'boolean') {
        throw new ReviewError('nowitknows start failed')
      }
      return result
    })
    const bounded = within($, call, START_WAIT_MS)
    starts.set(id, bounded)
    // An unusable or late answer is forgotten once the call ends, so a later turn starts again, and a failure is
    // reported off the session start path.
    call.then(async () => {
      if (await bounded === undefined && starts.get(id) === bounded) starts.delete(id)
    }, error => {
      if (starts.get(id) === bounded) starts.delete(id)
      void failure($, cwd, error)
    })
  }
  return await starts.get(id)
}

async function allowed($: EngineInterface, extra: boolean) {
  const { rateLimits } = await $.session.usage()
  return rateLimits.some(window => window.kind === 'five_hour' || window.kind === 'seven_day')
    && (extra || rateLimits.every(window => window.percentUsed < 100))
}

async function review($: EngineInterface, current: Session, abort: AbortController) {
  let step: string | undefined
  let failedUsage: ModelUsage | undefined
  let cancelled = false
  try {
    if (abort.signal.aborted) return
    const settings = await start($, current.session_id, current.cwd)
    if (!settings || abort.signal.aborted || !await allowed($, settings.allow_extra_usage)) return
    let result = await command<Reply>($, current.cwd, [
      'plan', '--session', current.session_id, '--transcript', current.transcript_path, '--cwd', current.cwd,
    ])
    while (result.action === 'model') {
      step = result.step
      if (abort.signal.aborted || !await allowed($, result.allow_extra_usage === true)) {
        cancelled = true
        throw new ReviewError('in-session review paused')
      }
      const { model, system, prompt, maxTokens, timeoutMs } = result
      const answer = await $.model.complete({ model, system, prompt, maxTokens, timeoutMs }, { signal: abort.signal })
      failedUsage = answer.usage
      if (abort.signal.aborted) throw new ReviewError('in-session review paused')
      if (!answer.isAnswered) throw new ReviewError(`model completion ${answer.reason}`)
      result = await command<Reply>($, current.cwd, ['answer', '--step', step], {
        text: answer.text, usage: answer.usage,
      })
      step = undefined
      failedUsage = undefined
    }
  } catch (error) {
    await failure($, current.cwd, error, step, failedUsage, cancelled || abort.signal.aborted)
  } finally {
    if (running?.abort === abort) running = undefined
  }
}

function schedule($: EngineInterface) {
  if (!session?.transcript_path || running) return
  const current = session
  const job: Job = { sessionId: current.session_id, abort: new AbortController() }
  try {
    scheduled?.timer?.cancel()
    scheduled = job
    job.timer = $.clock.after(0, () => {
      if (scheduled !== job) return
      scheduled = undefined
      running = job
      return review($, current, job.abort)
    })
  } catch {
    if (scheduled === job) scheduled = undefined
  }
}

export const register: Register = (on, options) => {
  env = Object.fromEntries(Object.entries(options).map(([key, value]) => [
    `CLAUDE_PLUGIN_OPTION_${key.replace(/[^A-Za-z0-9_]/g, '_').toUpperCase()}`,
    String(value),
  ]))
  on('session.start', async ($, e, next) => {
    try { await start($, await $.session.id(), e.cwd) } catch (error) { void failure($, e.cwd, error) }
    return next(e)
  })

  // Only classic inputs expose the transcript, including after /clear and resume.
  on('classic.*', async ($, e, next) => {
    let marked = false
    try {
      if ('session_id' in e && typeof e.session_id === 'string' && typeof e.transcript_path === 'string'
        && typeof e.cwd === 'string' && e.agent_id === undefined && e.hook_event_name !== 'SessionEnd') {
        session = { session_id: e.session_id, transcript_path: e.transcript_path, cwd: e.cwd }
        if (e.hook_event_name === 'SessionStart') {
          const result = await start($, e.session_id, e.cwd)
          // The command hook clears stale persisted ownership when this module is absent on resume.
          await $.env.set('NOWITKNOWS_IN_SESSION', result ? e.session_id : undefined)
          marked = true
        }
      }
    } catch (error) {
      if ('cwd' in e && typeof e.cwd === 'string') await failure($, e.cwd, error)
    }
    let result
    try { result = await next(e) } finally {
      if (marked) { try { await $.env.set('NOWITKNOWS_IN_SESSION', undefined) } catch {} }
    }
    if ('hook_event_name' in e && e.hook_event_name === 'PreCompact' && e.agent_id === undefined) schedule($)
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) schedule($)
    return result
  })

  on('session.end', async (_$, e, next) => {
    if (scheduled?.sessionId === e.sessionId) {
      scheduled.abort.abort()
      try { scheduled.timer?.cancel() } catch {}
      scheduled = undefined
    }
    if (running?.sessionId === e.sessionId) {
      running.abort.abort()
      running = undefined
    }
    starts.delete(e.sessionId)
    if (session?.session_id === e.sessionId) session = undefined
    return next(e)
  })
}
