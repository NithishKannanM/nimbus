import type { ReactNode } from "react";

/**
 * A numbered section title: the index numeral and the name, set in the same
 * heavy face, above a full-width 2px rule. The numeral is an index, not data.
 */
export function SectionHead({
  n,
  id,
  children,
  aside,
}: {
  n: string;
  id?: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
      <h2 id={id} className="display section-title flex items-baseline gap-3">
        <span aria-hidden className="text-declared">{n}</span>
        <span>{children}</span>
      </h2>
      {aside}
    </div>
  );
}

/** Registration crosshair, a structural mark rather than data. */
export function Cross({ className = "" }: { className?: string }) {
  return (
    <span aria-hidden className={`label select-none text-ink ${className}`}>
      +
    </span>
  );
}
