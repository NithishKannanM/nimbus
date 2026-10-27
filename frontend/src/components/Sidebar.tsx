import { useEffect, useState } from "react";

import type { SchedulerState } from "../lib/api";
import {
  IconActivity,
  IconArchive,
  IconChart,
  IconCloud,
  IconCpu,
  IconLayout,
  IconList,
  IconRoute,
  IconServer,
  IconStack,
  IconX,
} from "./Icons";

const GROUPS = [
  {
    label: "Home",
    items: [{ id: "overview", label: "Dashboard", Icon: IconLayout }],
  },
  {
    label: "Monitor",
    items: [
      { id: "algorithm", label: "Algorithm", Icon: IconCpu },
      { id: "divergence", label: "Divergence", Icon: IconActivity },
      { id: "comparison", label: "Comparison", Icon: IconChart },
      { id: "fleet", label: "Fleet", Icon: IconServer },
      { id: "decisions", label: "Decisions", Icon: IconRoute },
      { id: "queue", label: "Queue", Icon: IconStack },
      { id: "jobs", label: "Jobs", Icon: IconList },
    ],
  },
  {
    label: "Evidence",
    items: [{ id: "runs", label: "Archived runs", Icon: IconArchive }],
  },
];

const IDS = GROUPS.flatMap((g) => g.items.map((i) => i.id));

/** Highlights the section nearest the top of the viewport. */
function useActiveSection(): string {
  const [active, setActive] = useState(IDS[0]!);
  useEffect(() => {
    const onScroll = () => {
      let current = IDS[0]!;
      for (const id of IDS) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top <= 140) current = id;
      }
      // At the very bottom the last short section can never reach the top.
      if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) {
        current = IDS[IDS.length - 1]!;
      }
      setActive(current);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  return active;
}

/** Policy names are long snake_case words with no spaces; let them wrap after
 *  each underscore instead of running out of the card. */
function Breakable({ text }: { text: string }) {
  const parts = text.split("_");
  return (
    <>
      {parts.map((part, i) => (
        <span key={i}>
          {part}
          {i < parts.length - 1 && (
            <>
              _<wbr />
            </>
          )}
        </span>
      ))}
    </>
  );
}

export function Sidebar({
  scheduler,
  open,
  onClose,
}: {
  scheduler: SchedulerState;
  open: boolean;
  onClose: () => void;
}) {
  const active = useActiveSection();

  return (
    <>
      {open && (
        <div aria-hidden className="fixed inset-0 z-30 bg-black/50 lg:hidden" onClick={onClose} />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[270px] flex-col border-r border-hairline bg-paper transition-transform duration-200 lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-[70px] items-center justify-between px-6">
          <a href="#overview" className="flex items-center gap-2.5 text-ink" onClick={onClose}>
            <span className="grid h-9 w-9 place-items-center rounded-lg bg-reserved text-white">
              <IconCloud className="h-5 w-5" />
            </span>
            <span className="text-[20px] font-bold tracking-tight">Nimbus</span>
          </a>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close menu"
            className="rounded-full p-2 text-declared hover:bg-muted lg:hidden"
          >
            <IconX />
          </button>
        </div>

        <nav aria-label="Sections" className="flex-1 overflow-y-auto px-6 pb-6">
          {GROUPS.map((group) => (
            <div key={group.label} className="mt-6 first:mt-2">
              <p className="eyebrow mb-2 text-declared">{group.label}</p>
              <ul className="space-y-0.5">
                {group.items.map(({ id, label, Icon }) => {
                  const isActive = active === id;
                  return (
                    <li key={id}>
                      <a
                        href={`#${id}`}
                        onClick={onClose}
                        aria-current={isActive ? "location" : undefined}
                        className={`flex h-11 items-center gap-3 rounded-card px-3.5 text-[14px] transition-colors ${
                          isActive
                            ? "bg-reserved font-medium text-white"
                            : "text-ink hover:bg-reservedLight hover:text-ink"
                        }`}
                      >
                        <Icon />
                        {label}
                      </a>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        {/* Where the template sells its Pro version, this states what is
            actually running: the strategy and the policy behind it. */}
        <div className="m-6 mt-0 rounded-card bg-reservedLight p-5">
          <p className="text-[12px] font-semibold uppercase tracking-[0.04em] text-declared">Running</p>
          <p className="mt-1 break-words text-[15px] font-semibold leading-snug text-ink">
            <Breakable text={scheduler.strategy} />
          </p>
          <dl className="mt-3 space-y-1.5 text-[12px]">
            <div>
              <dt className="text-declared">Ordering</dt>
              <dd className="break-words font-mono text-ink">{scheduler.ordering_policy}</dd>
            </div>
            <div>
              <dt className="text-declared">Placement</dt>
              <dd className="break-words font-mono text-ink">
                <Breakable text={scheduler.placement_policy} />
              </dd>
            </div>
            <div>
              <dt className="text-declared">Version</dt>
              <dd className="font-mono text-ink">{scheduler.algorithm_version}</dd>
            </div>
          </dl>
        </div>
      </aside>
    </>
  );
}
