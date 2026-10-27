import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

const WIDTH = 288;
const GAP = 8;

/**
 * An "i" that reveals its explanation on hover or keyboard focus.
 *
 * The bubble is portalled to <body> with fixed positioning, so it is never
 * clipped by a scrolling table or a card's overflow. It opens below the icon,
 * flips above when there is no room, and is clamped to the viewport.
 */
export function InfoTip({ children, label = "More information" }: { children: ReactNode; label?: string }) {
  const id = useId();
  const anchor = useRef<HTMLButtonElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    if (!open || !anchor.current || !bubble.current) return;
    const a = anchor.current.getBoundingClientRect();
    const h = bubble.current.offsetHeight;
    const width = Math.min(WIDTH, window.innerWidth - 32);
    const left = Math.min(Math.max(16, a.left + a.width / 2 - width / 2), window.innerWidth - width - 16);
    const below = a.bottom + GAP;
    const top = below + h > window.innerHeight - 8 ? Math.max(8, a.top - GAP - h) : below;
    setPos({ left, top });
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("scroll", close, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const show = () => {
    setPos(null);
    setOpen(true);
  };

  return (
    <>
      <button
        ref={anchor}
        type="button"
        aria-label={label}
        aria-describedby={open ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={() => setOpen(false)}
        onFocus={show}
        onBlur={() => setOpen(false)}
        onClick={() => (open ? setOpen(false) : show())}
        className="inline-grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full border border-declared/50 align-middle font-serif text-[11px] font-semibold italic leading-none text-declared transition-colors hover:border-reserved hover:text-reserved focus-visible:border-reserved focus-visible:text-reserved focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-reserved/40"
      >
        i
      </button>
      {open &&
        createPortal(
          <div
            ref={bubble}
            id={id}
            role="tooltip"
            style={{
              width: Math.min(WIDTH, window.innerWidth - 32),
              left: pos?.left ?? 0,
              top: pos?.top ?? 0,
              visibility: pos ? "visible" : "hidden",
            }}
            className="pointer-events-none fixed z-50 whitespace-normal rounded-card bg-ink px-3.5 py-2.5 text-left text-[12px] font-normal normal-case leading-relaxed tracking-normal text-paper shadow-card"
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
