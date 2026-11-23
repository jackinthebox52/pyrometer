import { useEffect, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '../api/client.ts'
import type { MediaItem } from '../api/types.ts'
import { CHART, C } from '../theme.ts'
import ExpandModal from './ExpandModal.tsx'

interface Props {
  runId: string
}

/** File-backed images load by URL (no base64 in the list response); rows without one fall back to inline data. */
function imageSrc(item: MediaItem): string {
  return item.url ?? `data:image/png;base64,${item.data ?? ''}`
}

function HistogramChart({ data }: { data: string }) {
  const parsed = useMemo(() => {
    try {
      const { edges, counts } = JSON.parse(data) as { edges: number[]; counts: number[] }
      return counts.map((count, i) => ({
        mid: ((edges[i] + edges[i + 1]) / 2).toPrecision(3),
        count,
      }))
    } catch {
      return []
    }
  }, [data])

  if (parsed.length === 0) return null

  return (
    <ResponsiveContainer width="100%" height={160}>
      <BarChart data={parsed} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
        <XAxis dataKey="mid" tick={CHART.tick} tickLine={false} axisLine={false} interval="preserveStartEnd" />
        <YAxis tick={CHART.tick} tickLine={false} axisLine={false} width={32} />
        <Tooltip
          {...CHART.tooltip}
          itemStyle={{ color: C.series }}
          cursor={{ fill: C.line, fillOpacity: 0.4 }}
        />
        <Bar dataKey="count" fill={C.series} radius={[2, 2, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  )
}

/** Tiny images (class-probability maps, 8x8 weights...) are scaled up with hard pixel edges instead of left as a dot. */
function PreviewImage({ item, target = 240, maxHeight = 280 }: { item: MediaItem; target?: number; maxHeight?: number | string }) {
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  const small = natural != null && Math.max(natural.w, natural.h) < 128
  const scale = small ? Math.max(1, Math.floor(target / Math.max(natural.w, natural.h))) : 1
  return (
    <img
      src={imageSrc(item)}
      loading="lazy"
      decoding="async"
      alt={item.name}
      onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
      style={{
        maxWidth: '100%',
        maxHeight,
        objectFit: 'contain',
        borderRadius: 2,
        imageRendering: small ? 'pixelated' : 'auto',
        ...(small && natural ? { width: natural.w * scale, height: natural.h * scale } : {}),
      }}
    />
  )
}

export default function MediaGallery({ runId }: Props) {
  const [names, setNames] = useState<string[]>([])
  const [selectedName, setSelectedName] = useState<string | null>(null)
  const [items, setItems] = useState<MediaItem[]>([])
  const [stepIdx, setStepIdx] = useState(0)
  const [expandedItem, setExpandedItem] = useState<MediaItem | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api.getMediaNames(runId).then((n) => {
      setNames(n)
      if (n.length > 0) setSelectedName(n[0])
    }).catch(() => {})
  }, [runId])

  useEffect(() => {
    if (!selectedName) return
    setLoading(true)
    api.getMedia(runId, selectedName).then((data) => {
      setItems(data)
      setStepIdx(data.length > 0 ? data.length - 1 : 0)
    }).catch(() => {}).finally(() => setLoading(false))
  }, [runId, selectedName])

  if (names.length === 0) return null

  const mediaType = items[0]?.media_type ?? 'image'
  const currentItem = items[stepIdx] ?? null

  return (
    <div className="mb-6">
      <h2 className="mb-3 text-[15px] font-semibold text-py-text">Media</h2>
      <div className="rounded border border-py-border bg-py-surface p-4">
        {/* Name selector */}
        <div className="mb-3 flex items-center justify-between">
          <div className="flex flex-wrap" role="group" aria-label="Media name">
            {names.map((n) => (
              <button
                key={n}
                onClick={() => setSelectedName(n)}
                aria-pressed={selectedName === n}
                className={`-ml-px h-7 border px-3 font-mono text-xs transition-colors first:ml-0 first:rounded-l last:rounded-r ${
                  selectedName === n
                    ? 'border-py-border-hi bg-py-elevated text-py-text'
                    : 'border-py-border-hi bg-py-surface text-py-soft hover:bg-py-elevated hover:text-py-text'
                }`}
              >
                {n}
              </button>
            ))}
          </div>
          {currentItem && <span className="num flex-shrink-0 text-xs text-py-muted">step {currentItem.step}</span>}
        </div>

        {/* Content */}
        {loading ? (
          <div className="flex h-40 items-center justify-center text-[13px] text-py-muted">
            Loading…
          </div>
        ) : !currentItem ? (
          <div className="flex h-40 items-center justify-center text-[13px] text-py-muted">
            No data
          </div>
        ) : mediaType === 'histogram' ? (
          <HistogramChart data={currentItem.data ?? ''} />
        ) : (
          <div
            className="flex items-center justify-center"
            style={{ minHeight: 120, cursor: 'zoom-in' }}
            onClick={() => setExpandedItem(currentItem)}
          >
            <PreviewImage key={`${currentItem.name}:${currentItem.step}`} item={currentItem} />
          </div>
        )}

        {/* Step slider */}
        {items.length > 1 && (
          <div className="mt-3 flex items-center gap-2">
            <span className="flex-shrink-0 text-xs text-py-muted">Step</span>
            <input
              type="range"
              min={0}
              max={items.length - 1}
              value={stepIdx}
              onChange={(e) => setStepIdx(Number(e.target.value))}
              className="flex-1"
            />
            <span className="num min-w-[40px] flex-shrink-0 text-right text-xs text-py-muted">
              {stepIdx + 1}/{items.length}
            </span>
          </div>
        )}
      </div>

      {expandedItem && (
        <ExpandModal onClose={() => setExpandedItem(null)}>
          <div className="flex items-center justify-center p-4">
            <PreviewImage item={expandedItem} target={640} maxHeight="80vh" />
          </div>
          <div className="num mt-2 text-center text-xs text-py-muted">
            {expandedItem.name} — step {expandedItem.step}
          </div>
        </ExpandModal>
      )}
    </div>
  )
}
