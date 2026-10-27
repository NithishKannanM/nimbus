import type { ReactNode } from "react";

import { InfoTip } from "./InfoTip";

/**
 * A white dashboard card with a title row. `id` lands on the section so the
 * sidebar can jump to it; `titleId` labels it for assistive tech.
 */
export function Card({
  id,
  title,
  subtitle,
  info,
  action,
  children,
  className = "",
}: {
  id?: string;
  title: ReactNode;
  subtitle?: ReactNode;
  /** Longer explanation, shown from an "i" beside the title. */
  info?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const titleId = id ? `${id}-title` : undefined;
  return (
    <section id={id} aria-labelledby={titleId} className={`card scroll-mt-24 p-6 sm:p-[30px] ${className}`}>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 id={titleId} className="text-[18px] font-semibold leading-tight text-ink">
              {title}
            </h2>
            {info && <InfoTip label={`About ${typeof title === "string" ? title : "this section"}`}>{info}</InfoTip>}
          </div>
          {subtitle && <p className="mt-1 text-[14px] text-declared">{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

/** Shown in a card header when its last poll failed but older data remains. */
export function StaleChip() {
  return (
    <span role="status" className="chip-observed">
      API unreachable · last known
    </span>
  );
}
