export type IconName =
  | 'search'
  | 'chevron-right'
  | 'chevron-down'
  | 'x'
  | 'pin'
  | 'compare'
  | 'settings'
  | 'copy'
  | 'more'
  | 'check'
  | 'filter'
  | 'eye'
  | 'eye-off'
  | 'archive'
  | 'trash'
  | 'menu'
  | 'external'
  | 'plus'
  | 'pencil'
  | 'expand'
  | 'sun'

/** 16x16 viewBox line paths, drawn with a 1.5 stroke in `currentColor`. */
const PATHS: Record<IconName, string> = {
  search: 'M7 12.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11ZM11 11l3.5 3.5',
  'chevron-right': 'M6 3l5 5-5 5',
  'chevron-down': 'M3 6l5 5 5-5',
  x: 'M3.5 3.5l9 9M12.5 3.5l-9 9',
  pin: 'M9.5 1.5l5 5-2 .5-2.5 2.5.5 3-1 1-3-3-4.5 4.5M5 6l1-1 3 .5',
  compare: 'M2 3v10M2 8h4M6 5v6M10 5v6M14 3v10M10 8h4',
  sun: 'M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M13 3l-1 1M4 12l-1 1',
  settings: 'M2 4h7M12 4h2M2 12h2M7 12h7M10.5 4a1.5 1.5 0 1 0 0 .01M5.5 12a1.5 1.5 0 1 0 0 .01',
  copy: 'M5.5 5.5h8v8h-8zM10.5 5.5v-3h-8v8h3',
  more: 'M3 8h.01M8 8h.01M13 8h.01',
  check: 'M3 8.5l3.5 3.5L13 4.5',
  filter: 'M2 3h12l-4.5 5.5V13l-3-1.5V8.5z',
  eye: 'M1 8s2.5-4.5 7-4.5S15 8 15 8s-2.5 4.5-7 4.5S1 8 1 8ZM8 9.8a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6Z',
  'eye-off': 'M2 2l12 12M6.3 4c.55-.3 1.14-.5 1.7-.5 4.5 0 7 4.5 7 4.5s-.8 1.4-2.2 2.6M4 5.4C2.3 6.6 1 8 1 8s2.5 4.5 7 4.5c.9 0 1.7-.2 2.4-.5',
  archive: 'M1.5 3h13v3h-13zM2.5 6v7.5h11V6M6.5 9h3',
  trash: 'M2.5 4h11M6 4V2.5h4V4M3.5 4l.7 9.5h7.6l.7-9.5M6.5 7v4M9.5 7v4',
  menu: 'M2 4h12M2 8h12M2 12h12',
  external: 'M9 2.5h4.5V7M13.5 2.5L7 9M12 9.5v4H2.5V4H6.5',
  plus: 'M8 2.5v11M2.5 8h11',
  pencil: 'M10.5 2.5l3 3L5.5 13.5l-3.5.5.5-3.5zM9 4l3 3',
  expand: 'M6 2.5H2.5V6M10 2.5h3.5V6M6 13.5H2.5V10M10 13.5h3.5V10',
}

interface Props {
  name: IconName
  size?: number
  className?: string
}

/** Shared inline-SVG icon: 1.5 stroke, `currentColor`, decorative (`aria-hidden`). Size it with `size`, colour it with text colour. */
export default function Icon({ name, size = 16, className }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
