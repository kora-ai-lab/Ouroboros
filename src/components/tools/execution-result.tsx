import type { FC } from "react";

interface Props {
  stdout: string;
  stderr: string;
  exitCode: number;
  timedOut: boolean;
}

export const ExecutionResult: FC<Props> = ({ stdout, stderr, exitCode, timedOut }) => {
  const ok = exitCode === 0 && !timedOut;
  const label = timedOut ? "Timed out" : ok ? "Done" : "Error " + exitCode;

  return (
    <div className={"mt-2 rounded-md border p-3 text-body-sm " +
      (ok ? "bg-neutral-800/80 border-white/5" : "bg-error-500/10 border-error-500/30")}>
      <div className={"flex items-center gap-2 mb-1 font-medium " +
        (ok ? "text-success-500" : "text-error-500")}>
        {label}
      </div>
      {stdout && <pre className="font-mono text-xs text-neutral-300 whitespace-pre-wrap max-h-48 overflow-y-auto mt-2">{stdout}</pre>}
      {stderr && <pre className="font-mono text-xs text-error-500/80 whitespace-pre-wrap max-h-32 overflow-y-auto mt-1">{stderr}</pre>}
    </div>
  );
};