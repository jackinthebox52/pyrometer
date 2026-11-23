/**
 * Per-run colours for the sidebar check marks, compare charts and tables. Nine runs is the documented comparison size,
 * so the first nine are pairwise distinguishable (`runColors.test.ts`); more than that wraps around.
 * A categorical set tuned for the warm dark ground; it avoids the blackbody ramp's red/orange steps and pure red so a
 * run colour is never mistaken for heat or for a failure.
 */
export const RUN_COLORS = [
  '#f0a04b', // warm amber
  '#6f93d1', // steel blue
  '#46b59a', // teal
  '#bf8ae6', // violet
  '#e07a9a', // rose
  '#93a437', // olive
  '#ecd48f', // sand
  '#6fd3ee', // sky
  '#e4ded6', // bone
]
