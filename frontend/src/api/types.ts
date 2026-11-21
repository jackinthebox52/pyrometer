export type Role = 'admin' | 'user' | 'global' | 'project'

export interface Session {
  auth: 'required' | 'off'
  state: 'setup_required' | 'login' | 'ready'
  authenticated: boolean
  user: string | null
  role: Role | null
}

export interface ApiKey {
  id: string
  name: string
  project: string | null
  created_at: number
  last_used_at?: number | null
}

export interface CreatedApiKey {
  id: string
  name: string
  project: string | null
  key: string
}

export interface Project {
  name: string
  visibility: 'public' | 'private'
  created_at: string | null
  created_by: string
  runs: number | null
  members?: string[]
  archived?: boolean
  archived_at?: string | null
  archived_runs?: number
  last_activity?: string | null
  retention?: { older_than_days: Record<string, number> } | null
}

export interface CreatedProject {
  project: Project
  key: string | null
}

export interface UserAccount {
  username: string
  role: 'admin' | 'user'
  created_at: number
}

export type RunStatus = 'running' | 'completed' | 'failed' | 'crashed' | 'killed'

export interface Run {
  run_id: string
  project: string
  name: string
  status: RunStatus
  created_at: string
  finished_at: string | null
  last_heartbeat?: string | null
  hyperparams: Record<string, unknown>
  tags: string[]
  notes: string | null
  git_hash: string | null
  git_dirty: boolean
  gpu_model: string | null
  gpu_memory_total: number | null
  /** Every GPU visible to the run (empty for runs from before multi-GPU hardware capture). */
  gpus?: GpuInfo[]
  gpu_count?: number | null
  driver_version?: string | null
  cuda_version: string | null
  pytorch_version: string | null
  python_version: string | null
  hostname: string | null
  updated_at?: string
  group?: string | null
  archived?: boolean
  project_archived?: boolean
  pinned?: boolean
}

/** `GET /runs/facets`: what the run list can be filtered by. */
export interface RunFacets {
  groups: {
    project: string
    group: string
    runs: number
    running: number
    statuses: Record<string, number>
    last_activity: string | null
  }[]
  tags: { tag: string; runs: number }[]
  statuses: Record<string, number>
}

export interface Metric {
  step: number
  timestamp: number
  name: string
  value: number
}

export interface SystemSnapshot {
  timestamp: number
  gpu_utilization: number | null
  memory_used: number | null
  memory_total: number | null
  temperature: number | null
  power_draw: number | null
  fan_speed: number | null
  sm_clock_mhz: number | null
  memory_clock_mhz: number | null
  // Per-process attributed metrics (null on runs predating this feature)
  process_memory_used: number | null
  process_exclusive: boolean | null
  attributed_utilization: number | null
  attributed_power: number | null
  /** CUDA ordinal within the run's visible GPUs (0 for runs predating multi-GPU monitoring) */
  gpu_index: number
}

export interface ProfileEvent {
  step: number
  timestamp: number
  name: string
  category: string
  duration_us: number
  cuda_time_us: number
  cpu_time_us: number
  input_shapes: number[][]
  memory_bytes: number | null
  device_id: number
}

export interface ParamStat {
  step: number
  timestamp: number
  param_name: string
  grad_norm: number
  grad_min: number
  grad_max: number
  grad_mean: number
  weight_norm: number
  weight_min: number
  weight_max: number
  weight_mean: number
  grad_histogram: number[]
  weight_histogram: number[]
}

export interface RunEvent {
  timestamp: number
  step: number | null
  event_type: string
  severity: string
  message: string
  metadata: Record<string, unknown>
}

export interface RunLog {
  timestamp: number
  stream: 'stdout' | 'stderr'
  line: string
  step: number | null
}

export interface Artifact {
  timestamp: number
  step: number | null
  artifact_type: string
  path: string
  size_bytes: number | null
  metadata: Record<string, unknown>
}

export interface MediaItem {
  step: number
  timestamp: number
  name: string
  media_type: 'image' | 'histogram' | 'figure'
  data: string | null  // base64 PNG for image/figure (omitted with ?inline=false), JSON string for histogram
  url?: string | null  // file-backed images/figures: GET this for the PNG
}

export interface BestMetricInfo {
  value: number
  step: number | null
  direction: 'min' | 'max'
}

export interface MigDevice {
  index: number
  uuid: string | null
  name: string | null
  memory_total: number | null
  /** Always null: NVML does not report utilization per MIG instance. */
  utilization: number | null
}

export interface GpuInfo {
  index: number
  name: string | null
  uuid: string | null
  memory_total: number | null
  driver_version: string | null
  mig_mode: 'enabled' | 'disabled' | null
  is_mig: boolean
  mig_devices: MigDevice[]
  note?: string
}

export interface GpuSummary {
  gpu_index: number
  avg_gpu_utilization: number | null
  avg_device_utilization: number | null
  peak_process_memory_bytes: number | null
  peak_memory_bytes: number | null
  memory_total_bytes: number | null
  samples: number
}

export interface RunSummary {
  run_id: string
  computed_at: number
  total_training_time_s: number | null
  total_steps: number | null
  steps_per_sec: number | null
  avg_gpu_utilization: number | null
  peak_process_memory_bytes: number | null
  anomaly_count: number
  final_metrics: Record<string, number>
  best_metrics: Record<string, BestMetricInfo>
  efficiency_score: 'A' | 'B' | 'C' | 'D' | 'F' | null
  efficiency_details: Record<string, number>
  per_gpu?: GpuSummary[] | null
}

export interface ApiResponse<T> {
  data: T
  error: string | null
  /** Present on paged/downsampled reads: `GET /runs?limit=` gives total/limit/offset/returned. */
  meta?: Record<string, number | boolean | null>
}

export interface SampleStep {
  name: string
  step: number
  count: number
  timestamp: number
}

export interface SamplesPage {
  total: number
  items: { idx: number; sample: Record<string, unknown> }[]
}

export type AlertKind = 'run_crashed' | 'run_failed' | 'event' | 'anomaly' | 'metric_threshold'
export type AlertFormat = 'generic' | 'ntfy' | 'discord' | 'slack'

export interface AlertTrigger {
  kind: AlertKind
  min_severity?: string | null // kind "event"
  metric?: string | null // kind "metric_threshold"
  op?: string | null
  threshold?: number | null
}

export interface AlertRule {
  id: string
  name: string
  url: string // masked by the server: scheme and host only
  format: AlertFormat
  project: string | null
  triggers: AlertTrigger[] // the rule fires when any of them matches
  enabled: boolean
  updated_at?: number | null
}

export type AlertRuleInput = Omit<AlertRule, 'id'>

export interface RunListMeta {
  total: number
  limit: number
  offset: number
  returned: number
}

export interface RetentionPolicy {
  older_than_days: Record<string, number>
}

export interface BulkResponse {
  results: { run_id: string; ok: boolean; error: string | null }[]
  succeeded: number
  failed: number
}
