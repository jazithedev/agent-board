import { expect, test } from 'claude-code/testing'

import { homeOf, snapshot } from './register'

test('snapshot keeps each window with its percentage and reset time', () => {
  const windows = [
    { kind: 'five_hour', percentUsed: 23.5, resetsAt: '2026-10-09T22:00:00Z' },
    { kind: 'seven_day', percentUsed: 7 },
  ]

  const parsed = JSON.parse(snapshot(windows, 1791570000000))

  expect(parsed.updatedAt).toBe(1791570000000)
  expect(parsed.windows).toEqual(windows)
})

test('home is whatever precedes the .claude folder the mod lives in', () => {
  expect(homeOf('/opt/demo-user/.claude/dev-mods/abc/agent-board-limits')).toBe('/opt/demo-user')
  expect(homeOf('/opt/somewhere/else')).toBe('')
})
