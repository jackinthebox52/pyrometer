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
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2128" vertical={false} />
        <XAxis
          dataKey="mid"
          tick={{ fontSize: 9, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
          tickLine={false}
          axisLine={false}
          interval="preserveStartEnd"
        />
        <YAxis
          tick={{ fontSize: 9, fill: '#4a4e60', fontFamily: 'JetBrains Mono' }}
          tickLine={false}
          axisLine={false}
          width={32}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: '#1a1d24',
            border: '1px solid #353840',
            borderRadius: 6,
            fontSize: 11,
            fontFamily: 'JetBrains Mono',
            padding: '4px 8px',
          }}
          labelStyle={{ color: '#8b8fa8' }}
          itemStyle={{ color: '#f59e0b' }}
          cursor={{ fill: 'rgba(255,255,255,0.04)' }}
        />
        <Bar dataKey="count" fill="#f59e0b" radius={[2, 2, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
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
    <>
      <div className="rounded-lg p-4" style={{ backgroundColor: '#111318', border: '1px solid #252830' }}>
        {/* Name selector */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex gap-1 flex-wrap">
            {names.map((n) => (
              <button
                key={n}
                onClick={() => setSelectedName(n)}
                className="text-xs font-mono px-2 py-0.5 rounded transition-colors"
                style={{
                  backgroundColor: selectedName === n ? '#1e2128' : 'transparent',
                  color: selectedName === n ? '#c8cad6' : '#4a4e60',
                  border: `1px solid ${selectedName === n ? '#353840' : 'transparent'}`,
                  cursor: 'pointer',
                }}
              >
                {n}
              </button>
            ))}
          </div>
          {currentItem && (
            <span className="text-xs font-mono flex-shrink-0" style={{ color: '#4a4e60' }}>
              step {currentItem.step}
            </span>
          )}
        </div>

        {/* Content */}
        {loading ? (
          <div
            className="flex items-center justify-center text-xs font-mono"
            style={{ height: 160, color: '#4a4e60' }}
          >
            loading…
          </div>
        ) : !currentItem ? (
          <div
            className="flex items-center justify-center text-xs font-mono"
            style={{ height: 160, color: '#4a4e60' }}
          >
            no data
          </div>
        ) : mediaType === 'histogram' ? (
          <HistogramChart data={currentItem.data ?? ''} />
        ) : (
          <div
            className="flex items-center justify-center"
            style={{ minHeight: 120, cursor: 'zoom-in' }}
            onClick={() => setExpandedItem(currentItem)}
          >
            <img
              src={imageSrc(currentItem)}
              loading="lazy"
              decoding="async"
              alt={currentItem.name}
              style={{ maxWidth: '100%', maxHeight: 280, objectFit: 'contain', borderRadius: 4 }}
            />
          </div>
        )}

        {/* Step slider */}
        {items.length > 1 && (
          <div className="mt-3 flex items-center gap-2">
            <span className="text-xs font-mono flex-shrink-0" style={{ color: '#4a4e60' }}>
              step
            </span>
            <input
              type="range"
              min={0}
              max={items.length - 1}
              value={stepIdx}
              onChange={(e) => setStepIdx(Number(e.target.value))}
              style={{ flex: 1, accentColor: '#f59e0b' }}
            />
            <span className="text-xs font-mono flex-shrink-0" style={{ color: '#4a4e60', minWidth: 40, textAlign: 'right' }}>
              {stepIdx + 1}/{items.length}
            </span>
          </div>
        )}
      </div>

      {expandedItem && (
        <ExpandModal onClose={() => setExpandedItem(null)}>
          <div className="flex items-center justify-center p-4">
            <img
              src={imageSrc(expandedItem)}
              alt={expandedItem.name}
              style={{ maxWidth: '90vw', maxHeight: '80vh', objectFit: 'contain' }}
            />
          </div>
          <div className="text-center mt-2 text-xs font-mono" style={{ color: '#4a4e60' }}>
            {expandedItem.name} — step {expandedItem.step}
          </div>
        </ExpandModal>
      )}
    </>
  )
}
