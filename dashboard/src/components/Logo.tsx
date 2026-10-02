import { cn } from "@/lib/utils";

// The HMH Labz mark, traced as a vector. It is drawn in the current text colour, so with
// `text-ink` it is black on the light theme and white on the dark theme.
const HALF = (
  <>
    {/* centre pillar with its top wing */}
    <path d="M519 50V1025L330 915V268L210 200Z" />
    {/* upper ear */}
    <path d="M210 200L208 478L96 405Q40 368 55 310Q66 262 105 245Z" />
    {/* lower block */}
    <path
      d="M67 545L183 610L193 640V785L162 808L74 748L67 725Z"
      stroke="currentColor"
      strokeWidth="24"
      strokeLinejoin="round"
    />
  </>
);

export function Logo({ className, title = "HMH Labz" }: { className?: string; title?: string }) {
  return (
    <svg viewBox="30 30 1030 1015" fill="currentColor" role="img" aria-label={title} className={cn("text-ink", className)}>
      <title>{title}</title>
      {HALF}
      <g transform="translate(1090 0) scale(-1 1)">{HALF}</g>
    </svg>
  );
}
