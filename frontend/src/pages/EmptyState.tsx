export default function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 p-6">
      <div className="flex w-full max-w-[640px] flex-col gap-3">
        <h1 className="text-[22px] font-semibold text-py-text">Pick a run, or start one</h1>
        <p className="text-[13px] text-py-muted">Choose a run from the sidebar, or log a new one from your training script.</p>
        <pre className="overflow-x-auto rounded border border-py-border bg-py-n0 p-3 font-mono text-xs leading-relaxed text-py-soft">
{`import pyrometer

with pyrometer.init(name="my-run", project="default") as run:
    train()`}
        </pre>
        <p className="text-[13px] text-py-muted">
          Or launch an existing script unchanged with <code className="font-mono text-py-soft">pyrometer run train.py</code>.
        </p>
      </div>
    </div>
  )
}
