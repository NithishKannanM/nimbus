/** The dashboard's icon set: official Tabler icons (@tabler/icons-react), the
 *  family the reference template uses. Wrapped so every call site gets the same
 *  stroke, a default size and aria-hidden, and so swapping a glyph is one line
 *  here. Do not hand-draw icon paths; pick another Tabler glyph instead. */
import {
  IconActivity as TActivity,
  IconAlertTriangle,
  IconArchive as TArchive,
  IconChartBar,
  IconCheck as TCheck,
  IconChevronDown,
  IconCloud as TCloud,
  IconCpu as TCpu,
  IconLayoutDashboard,
  IconList as TList,
  IconMenu2,
  IconMoon as TMoon,
  IconRoute as TRoute,
  IconServer as TServer,
  IconStack2,
  IconSun as TSun,
  IconX as TX,
  type Icon,
} from "@tabler/icons-react";

type P = { className?: string };

const STROKE = 1.75;

function wrap(Glyph: Icon, size = "h-5 w-5") {
  return ({ className = size }: P) => <Glyph aria-hidden stroke={STROKE} className={className} />;
}

export const IconCloud = wrap(TCloud, "h-6 w-6");
export const IconLayout = wrap(IconLayoutDashboard);
export const IconActivity = wrap(TActivity);
export const IconServer = wrap(TServer);
export const IconRoute = wrap(TRoute);
export const IconStack = wrap(IconStack2);
export const IconList = wrap(TList);
export const IconArchive = wrap(TArchive);
export const IconMenu = wrap(IconMenu2);
export const IconX = wrap(TX);
export const IconCheck = wrap(TCheck);
export const IconAlert = wrap(IconAlertTriangle);
export const IconChevron = wrap(IconChevronDown, "h-4 w-4");
export const IconSun = wrap(TSun);
export const IconMoon = wrap(TMoon);
export const IconCpu = wrap(TCpu);
export const IconChart = wrap(IconChartBar);
