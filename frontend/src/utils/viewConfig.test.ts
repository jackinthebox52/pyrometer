import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  defaultPins,
  hidePattern,
  isHidden,
  isPinned,
  loadView,
  matchesPattern,
  movePin,
  resolvePins,
  sanitizeView,
  saveView,
  setTabHidden,
  showPattern,
  togglePin,
  viewKey,
} from './viewConfig.ts'

function stubStorage(): Map<string, string> {
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  })
  return store
}

describe('defaultPins', () => {
  it('takes the first existing name of each group, max 4', () => {
    const names = ['loss', 'train/loss', 'val/loss', 'lr', 'auto/steps_per_sec', 'episode/return_mean', 'auto/grad_norm']
    expect(defaultPins(names)).toEqual(['loss', 'val/loss', 'lr', 'auto/steps_per_sec'])
  })
  it('prefers auto names and skips missing groups', () => {
    expect(defaultPins(['auto/loss', 'loss', 'auto/grad_norm'])).toEqual(['auto/loss', 'auto/grad_norm'])
    expect(defaultPins(['accuracy'])).toEqual([])
  })
})

describe('patterns', () => {
  it('matches exact names and globs', () => {
    expect(matchesPattern('auto/loss', 'auto/*')).toBe(true)
    expect(matchesPattern('autoloss', 'auto/*')).toBe(false)
    expect(matchesPattern('a.b', 'a.b')).toBe(true)
    expect(matchesPattern('axb', 'a.b')).toBe(false)
    expect(matchesPattern('train/grad_norm', '*grad*')).toBe(true)
  })
  it('isHidden and isPinned honour patterns and defaults', () => {
    const cfg = { hiddenTabs: [], pinned: null, hidden: ['system/*'] }
    expect(isHidden('system/cpu', cfg)).toBe(true)
    expect(isHidden('loss', cfg)).toBe(false)
    expect(isPinned('loss', cfg, ['loss', 'lr'])).toBe(true)
    expect(isPinned('acc', cfg, ['loss', 'acc'])).toBe(false)
    expect(isPinned('acc', { ...cfg, pinned: ['acc'] })).toBe(true)
  })
})

describe('pin editing', () => {
  const names = ['loss', 'lr', 'auto/a', 'auto/b']
  it('materialises defaults before toggling', () => {
    const cfg = { hiddenTabs: [], pinned: null, hidden: [] }
    expect(togglePin(cfg, 'auto/a', names).pinned).toEqual(['loss', 'lr', 'auto/a'])
    expect(togglePin(cfg, 'loss', names).pinned).toEqual(['lr'])
  })
  it('expands a glob when unpinning one of its names', () => {
    const cfg = { hiddenTabs: [], pinned: ['auto/*'], hidden: [] }
    expect(togglePin(cfg, 'auto/a', names).pinned).toEqual(['auto/b'])
  })
  it('resolvePins expands globs in order and drops hidden', () => {
    const cfg = { hiddenTabs: [], pinned: ['lr', 'auto/*', 'ghost'], hidden: ['auto/b'] }
    expect(resolvePins(cfg, names)).toEqual(['lr', 'auto/a'])
  })
  it('movePin swaps neighbours and ignores out-of-range moves', () => {
    expect(movePin(['a', 'b', 'c'], 1, -1)).toEqual(['b', 'a', 'c'])
    expect(movePin(['a', 'b', 'c'], 2, 1)).toEqual(['a', 'b', 'c'])
  })
  it('hide / show / tabs', () => {
    const cfg = { hiddenTabs: [], pinned: null, hidden: [] }
    const h = hidePattern(hidePattern(cfg, 'x/*'), 'x/*')
    expect(h.hidden).toEqual(['x/*'])
    expect(showPattern(h, 'x/*').hidden).toEqual([])
    expect(setTabHidden(cfg, 'logs', true).hiddenTabs).toEqual(['logs'])
    expect(setTabHidden(setTabHidden(cfg, 'logs', true), 'logs', false).hiddenTabs).toEqual([])
    expect(setTabHidden(cfg, 'overview', true).hiddenTabs).toEqual([])
  })
})

describe('storage', () => {
  beforeEach(() => void stubStorage())
  it('returns defaults when nothing is stored', () => {
    expect(loadView('p')).toEqual({ hiddenTabs: [], pinned: null, hidden: [] })
  })
  it('round-trips per project', () => {
    saveView('p', { hiddenTabs: ['logs'], pinned: ['loss'], hidden: ['a/*'] })
    expect(loadView('p')).toEqual({ hiddenTabs: ['logs'], pinned: ['loss'], hidden: ['a/*'] })
    expect(loadView('q').hiddenTabs).toEqual([])
  })
  it('falls back to defaults on corrupt or hostile values', () => {
    const store = stubStorage()
    store.set(viewKey('p'), '{not json')
    expect(loadView('p').pinned).toBeNull()
    expect(sanitizeView({ hiddenTabs: ['overview', 3, 'logs'], pinned: 'x', hidden: [1] })).toEqual({
      hiddenTabs: ['logs'],
      pinned: null,
      hidden: [],
    })
  })
  it('survives storage that throws', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    })
    expect(loadView('p').hidden).toEqual([])
    expect(() => saveView('p', { hiddenTabs: [], pinned: null, hidden: [] })).not.toThrow()
  })
})
