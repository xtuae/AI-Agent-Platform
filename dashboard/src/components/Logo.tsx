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

/** The Heyozo product mark and wordmark, as on heyozo.com: what clients see in their dashboard. */
export function HeyozoMark({ className, word = true }: { className?: string; word?: boolean }) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <svg viewBox="0 0 32 32" className="size-[1.6em] shrink-0" role="img" aria-label="Heyozo">
        <title>Heyozo</title>
        <path
          fill="rgb(var(--accent))"
          d="M16 3C8.8 3 3 8.4 3 15c0 3.4 1.5 6.4 4 8.6V29l5.3-2.9c1.2.3 2.4.5 3.7.5 7.2 0 13-5.4 13-12S23.2 3 16 3Z"
        />
        <circle cx="11" cy="15" r="2.1" fill="rgb(var(--page))" />
        <circle cx="21" cy="15" r="2.1" fill="rgb(var(--page))" />
      </svg>
      {word ? <span className="font-semibold tracking-tight">heyozo</span> : null}
    </span>
  );
}
