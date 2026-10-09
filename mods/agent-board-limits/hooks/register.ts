import type { Register } from 'claude-code'

type Window = { kind: string; percentUsed: number; resetsAt?: string }

export const snapshot = (windows: Window[], now: number): string =>
  JSON.stringify({ updatedAt: now, windows }, null, 2)

export const homeOf = (pluginRoot: string): string =>
  pluginRoot.includes('/.claude/') ? pluginRoot.split('/.claude/')[0] : ''

export const register: Register = on => {
  on('turn.complete', async ($, e, next) => {
    const answer = await next(e)

    try {
      const { rateLimits } = await $.session.usage()
      const home = homeOf($.plugin.root)

      if (rateLimits.length > 0 && home !== '') {
        await $.fs.write(`${home}/.cache/agent-board/limits.json`, snapshot(rateLimits, await $.clock.now()))
      }
    } catch {
      // the reading is a convenience; a failed write must never disturb the turn
    }

    return answer
  })
}
